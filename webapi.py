"""Framework-free web API layer over the solver, for the Django app (web/).

Kept separate from :mod:`main` (which owns the CLI: argument parsing, file
loading, printing dispatch) and from Django (which owns HTTP: routing,
request/response, the process-wide cache and solve semaphore -- see
web/solverapp/). This module bridges the two: it turns a web request's JSON
``params`` into the same :class:`argparse.Namespace` the CLI builds, drives
:mod:`main`'s three solve stages (``build_inputs``/``solve_frontier``/
``derive_report_inputs``) from pasted text instead of files, and renders a
report.SectionedReporter's sections into the fields a JSON response needs.
Nothing here imports Django, so it is plain-pytest testable.

Two parameter classes drive the caching/re-solve split (ARCHITECTURE.md has
the full design writeup): a change to a **resolve** field
(:data:`RESOLVE_FIELDS`) changes what gets solved, so it is part of
:func:`fingerprint`'s cache key; a change to a **refilter** field
(:data:`REFILTER_FIELDS`) only changes how the *same* cached frontier is
displayed (which plans pass the limits, which one is selected, whether the
crafts breakdown is shown), so :func:`render` recomputes it against the
cached :class:`SolveSession` without a re-solve -- except the one horizon-guard
edge case documented on :func:`render`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shlex
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

import config
import crafting
import farm_solver
import main
import optimizer
import parsing
import passive
import pets
import report

# Args fields that change what gets solved -- part of the cache key (see
# fingerprint). A change to any of these invalidates the cached frontier.
RESOLVE_FIELDS = (
    "target", "level", "mastery", "items", "use_pumpkin_juice",
    "current_stamina", "cookie_sets", "plans", "spread", "graph", "start_date",
    "model_calendar", "average_calendar", "ignore_calendar", "crafting_bonus",
    "mastery_bonus", "override_inventory", "override", "price_pumpkin_juice",
    "self_sourced",
)

# The args fields a mastery-costs request depends on -- its own cache key (see
# mastery_costs_fingerprint), deliberately NOT RESOLVE_FIELDS. main.
# solve_mastery_costs solves its own per-item goals, so every goal/frontier
# field (target, level, mastery, items, plans, spread, graph, the pumpkin-juice
# knobs) is irrelevant to it: keying on those would throw the cached table away
# every time the solver page's goal changed. What is left is what the channel
# setup and the per-item solves actually read: the calendar trio + start_date
# (which passive sources exist and when), the two bonuses, the resource seeds
# _build_channel_setup folds in, the inventory overrides, every config override
# (prices, preference panels, AP trading -- all funnelled through `override`),
# and self_sourced (withholds the AP/LN/AC buys). `sort` is excluded on purpose:
# the client re-sorts the same rows without a round trip.
MASTERY_COST_FIELDS = (
    "start_date", "model_calendar", "average_calendar", "ignore_calendar",
    "crafting_bonus", "mastery_bonus", "current_stamina", "cookie_sets",
    "override_inventory", "override", "self_sourced",
)

# Args fields that only change how the cached frontier is displayed --
# render() recomputes these against the cached SolveSession, no re-solve.
REFILTER_FIELDS = (
    "max_gold", "max_ap", "max_ln", "max_ac", "passive_limit",
    "plan", "cheapest_plan", "fastest_plan", "breakdown",
)

# JSON keys accepted in a request's "params" object; anything else is a
# request error (params_to_args fails loud rather than silently ignoring a
# typo'd or client/server-version-mismatched key).
_KNOWN_PARAM_KEYS = frozenset({
    "target", "level", "mastery", "items", "override_inventory",
    "use_pumpkin_juice", "max_gold", "cookie_sets", "current_stamina",
    "plans", "spread", "graph", "start_date", "calendar", "plan_select",
    "max_ap", "max_ln", "max_ac", "passive_limit", "overrides",
    "crafting_bonus", "mastery_bonus", "breakdown", "count_pumpkin_juice_gold",
    # --self-sourced: don't buy Arnold Palmers / Large Nets / Apple Cider, make
    # them instead. Sent by the mastery-costs page's own checkbox; accepted on
    # /api/solve too, since it is a plain solve knob.
    "self_sourced",
    # Trading Values / Advanced config knobs -- exposed with their own web
    # controls but funnelled through args.override (see _CONFIG_PARAM_FLAGS).
    "price_orange_juice", "price_apple_cider", "price_arnold_palmer",
    "price_large_net", "price_cookie_set", "price_pumpkin_juice",
    "price_hickory_omelette",
    "instant_grow_time", "hickory_omelette",
    # The Material trading pane: which items may be traded for Arnold Palmers,
    # plus any custom AP prices. Turned into [trading.ap] overrides (see
    # _ap_trading_override_lines) -- an allow list only, never a deny list.
    "ap_trading",
    # The preference panels (exploring effectiveness, farming, pets, buildings)
    # send every control they touch as one {section.flag: value} object under
    # this key; params_to_args turns each entry into an args.override line (see
    # _config_override_line), the structured counterpart to the free-text
    # ``overrides`` box.
    "config",
})

# Web params that map straight onto a single ``section.flag`` config override.
# They get first-class controls (labelled, validated, defaulted) in the UI
# instead of being typed into the free-text overrides box, but there is no need
# for a dedicated ``args`` field or solve wiring: params_to_args appends each as
# an override string, so solve()'s one apply_overrides call feeds both these and
# the free-text overrides into the config. Because they land in ``args.override``
# (a RESOLVE_FIELDS member) a price change re-solves and re-keys the cache. The
# Grape Juice price is deliberately not here -- it is derived from the grapes
# price, not set directly (see config.trading_prices).
_CONFIG_PARAM_FLAGS = {
    "price_orange_juice": "trading.orange_juice",
    "price_apple_cider": "trading.apple_cider",
    "price_arnold_palmer": "trading.arnold_palmer",
    "price_large_net": "trading.large_net",
    "price_cookie_set": "trading.cookie_set",
    "price_pumpkin_juice": "trading.pumpkin_juice",
    "price_hickory_omelette": "trading.hickory_omelette",
}


def _config_override_line(key: str, value: object) -> str:
    """Format one preference-panel entry as a ``section.flag=<toml>`` override.

    The value arrives already typed from the request JSON (bool / number /
    string); render it as the TOML literal :func:`main.apply_overrides` parses
    back, so a panel control funnels through the exact same machinery as a
    hand-typed override line. ``bool`` is checked before ``int`` because Python
    booleans are an ``int`` subclass.
    """
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, (int, float)):
        literal = repr(value)
    elif isinstance(value, str):
        literal = json.dumps(value)  # a TOML basic (double-quoted) string
    else:
        raise ValueError(
            f"config value for {key!r} must be a bool, number or string, "
            f"not {type(value).__name__}"
        )
    return f"{key}={literal}"


def _ap_trading_override_lines(trading: object) -> list[str]:
    """Turn the Material-trading pane's ``ap_trading`` param into config overrides.

    The pane sends ``{"allow": [item, ...], "prices": {item: apk, ...}}``. The web
    UI works purely with an **allow** list (never a deny list, per the design), so:

    * ``allow`` non-empty -> ``trading.ap.enabled=true`` + the allow list + an empty
      ``deny`` (clearing web_default_config's own deny so the two never collide in
      :func:`config.arnold_palmer_prices`, which errors on both being set).
    * ``allow`` empty -> ``trading.ap.enabled=false``: an *empty* allow list would be
      falsey there and wrongly fall through to "trade everything", so an all-off
      pane turns the whole feature off instead.

    ``prices`` (optional) fully **replaces** ``[trading.ap.prices]`` -- the pane sends
    every allowed item's price (default or edited) when any was edited, and omits the
    key entirely when none were, leaving config's defaults in place. Rendered as a
    TOML inline table so :func:`main.apply_overrides` parses it like a hand-typed
    override; item names become double-quoted keys (they carry spaces/apostrophes).
    """
    if not isinstance(trading, dict):
        raise ValueError("ap_trading must be a JSON object")
    allow = trading.get("allow")
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ValueError("ap_trading.allow must be a list of item names")

    lines: list[str] = []
    if allow:
        lines.append("trading.ap.enabled=true")
        lines.append(f"trading.ap.allow={json.dumps(allow)}")
        lines.append("trading.ap.deny=[]")
    else:
        lines.append("trading.ap.enabled=false")

    prices = trading.get("prices")
    if prices is not None:
        if not isinstance(prices, dict):
            raise ValueError("ap_trading.prices must be a JSON object")
        items = []
        for item, value in prices.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"ap_trading price for {item!r} must be a number")
            items.append(f"{json.dumps(item)} = {value}")
        lines.append("trading.ap.prices={" + ", ".join(items) + "}")
    return lines


# The web app's own default config, deliberately separate from the developer's
# personal ``config.toml`` (which the CLI uses): a fresh browser session starts
# from these values -- both the base config every solve builds on and the
# placeholders the preference panels show. Copied from ``config.toml`` and
# edited independently thereafter.
WEB_CONFIG_PATH = Path(__file__).with_name("web_default_config.toml")


def load_web_config() -> dict:
    """Load the web app's default config (:data:`WEB_CONFIG_PATH`).

    The web counterpart to :func:`main.load_config`: falls back to an empty
    config if the file is missing, so the app still starts (every solve just
    self-sources from scratch) rather than erroring.
    """
    try:
        return config.load_config(WEB_CONFIG_PATH)
    except FileNotFoundError:
        return {}


# DEBUG-only capture directory for the browser's pasted mastery/inventory text,
# so the "CLI command" popup can point --mastery-file/--inventory-file at the
# exact state the browser solved. Lives on the local disk under the system temp
# dir; each solve writes a fresh timestamped pair, so past captures are kept and
# the command reflects that specific solve.
DEBUG_CAPTURE_DIR = Path(tempfile.gettempdir()) / "farm-solver-debug"


def capture_debug_state(
    mastery_text: str, inventory_text: str
) -> tuple[Path, Path]:
    """Write the pasted mastery/inventory text to :data:`DEBUG_CAPTURE_DIR`.

    DEBUG-only helper for :func:`args_to_command`: dumps the browser's pasted
    text to a fresh timestamped file pair on the local disk and returns their
    paths, so the reconstructed CLI command can reproduce the *exact* solve via
    ``--mastery-file``/``--inventory-file`` instead of relying on whatever the
    configured data directory happens to hold. Each solve gets its own pair
    (``mastery-<ts>.txt``/``inventory-<ts>.txt``, ``ts`` a microsecond-precision
    timestamp that sorts chronologically), so earlier captures are preserved and
    a command keeps working after a later solve. The user vetted storing their
    config this way for a local-only debug feature.
    """
    DEBUG_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
    mastery_path = DEBUG_CAPTURE_DIR / f"mastery-{stamp}.txt"
    inventory_path = DEBUG_CAPTURE_DIR / f"inventory-{stamp}.txt"
    mastery_path.write_text(mastery_text, encoding="utf-8")
    inventory_path.write_text(inventory_text, encoding="utf-8")
    return mastery_path, inventory_path


def _shell_quote(token: str) -> str:
    """Shell-quote one command token, readably.

    Like :func:`shlex.quote` (single-quote wrapping, safe tokens left bare) but
    with a friendlier escape for an embedded apostrophe: instead of ``shlex``'s
    ``'"'"'`` close/escaped-quote/reopen dance, use bash/zsh ANSI-C quoting
    (``$'...'``), where a single quote is simply ``\\'``. A literal backslash
    must be doubled too, since backslash is the escape character inside
    ``$'...'``. Apostrophe-free tokens keep ``shlex.quote``'s output unchanged.
    """
    if "'" not in token:
        return shlex.quote(token)
    escaped = token.replace("\\", "\\\\").replace("'", "\\'")
    return f"$'{escaped}'"


def args_to_command(
    args: argparse.Namespace,
    *,
    mastery_file: Path | None = None,
    inventory_file: Path | None = None,
) -> str:
    """Reconstruct the equivalent ``python main.py`` command line from ``args``.

    A debug aid (the web app's "CLI command" popup): reproduces the same solve
    from the terminal. The config-panel/price/instant-grow/hickory knobs come
    through verbatim as the same ``--override`` lines the request built
    (``args.override``), so this never re-derives that mapping. Only settings
    whose value differs from the CLI default are emitted, to keep it short.

    The command loads the same ``web_default_config.toml`` the web app uses
    (via ``--config``). When ``mastery_file``/``inventory_file`` are given (the
    web view passes the :func:`capture_debug_state` paths), the command points
    ``--mastery-file``/``--inventory-file`` at them so the CLI solves the exact
    pasted state; without them it falls back to the configured data directory.
    """
    tokens: list[str] = ["python", "main.py", "--config", WEB_CONFIG_PATH.name]

    def add(flag: str, value: object) -> None:
        tokens.append(flag)
        tokens.append(str(value))

    if mastery_file is not None:
        add("--mastery-file", mastery_file)
    if inventory_file is not None:
        add("--inventory-file", inventory_file)
    if args.target is not None:
        add("--target", args.target)
    if args.mastery:
        add("-m", args.mastery)
    if args.items:
        add("-i", args.items)
    if args.override_inventory:
        add("--override-inventory", args.override_inventory)
    if args.max_gold is not None:
        add("--max-gold", args.max_gold)
    if args.max_ap is not None:
        add("--max-ap", args.max_ap)
    if args.max_ln is not None:
        add("--max-ln", args.max_ln)
    if args.max_ac is not None:
        add("--max-ac", args.max_ac)
    if args.passive_limit:
        add("--passive-limit", args.passive_limit)
    if args.plans != 5:  # build_parser's default
        add("--plans", args.plans)
    if args.current_stamina:  # default 0
        add("--current-stamina", args.current_stamina)
    if args.cookie_sets is not None:
        add("--cookie-sets", args.cookie_sets)
    if args.use_pumpkin_juice is not None:
        if args.use_pumpkin_juice < 0:  # bare flag (const=-1): any amount
            tokens.append("--use-pumpkin-juice")
        else:
            tokens.append(f"--use-pumpkin-juice={args.use_pumpkin_juice}")
    if args.start_date is not None:
        add("--start-date", args.start_date.isoformat())
    if args.model_calendar:
        tokens.append("--model-calendar")
    elif args.average_calendar:
        tokens.append("--average-calendar")
    elif args.ignore_calendar:
        tokens.append("--ignore-calendar")
    if args.cheapest_plan:
        tokens.append("--cheapest-plan")
    elif args.fastest_plan:
        tokens.append("--fastest-plan")
    elif args.plan is not None:
        add("--plan", args.plan)
    spread = getattr(args, "spread", None)
    if spread is not None:
        text = spread.axis
        if spread.lo is not None and spread.hi is not None:
            text += f"={spread.lo:g}:{spread.hi:g}"
        add("--spread", text)
    if getattr(args, "graph", None) is not None:
        add("--graph", args.graph)
    if getattr(args, "price_pumpkin_juice", None) is False:
        tokens.append("--no-price-pumpkin-juice")
    if getattr(args, "self_sourced", False):
        tokens.append("--self-sourced")
    for override in args.override or []:
        add("--override", override)

    return " ".join(_shell_quote(token) for token in tokens)


def config_defaults() -> dict:
    """The config sections the web UI's preference panels read for their defaults.

    Each panel control shows the :data:`WEB_CONFIG_PATH` value as its
    placeholder (and a bool checkbox's initial on/off state), so a control left
    at its default sends no override. The ``[solver]`` section is withheld -- it
    holds server-side file paths, nothing a control maps to.
    """
    cfg = load_web_config()
    cfg.pop("solver", None)
    return cfg


# Where a zone's drops come from: the same two CSVs the exploring and fishing
# channels read. Only the item *names* are wanted here (not the drop rates the
# solver models), so the columns are read straight rather than through
# exploring.read_locations / fishing.read_zones, whose models lower-case the
# names and split the flagged alternate tables out -- a "what drops here" list
# wants every row's item, spelled as the data file spells it.
_ZONE_SOURCES = (
    ("exploring", Path(__file__).with_name("exploring.csv"), "Location"),
    ("fishing", Path(__file__).with_name("fishing.csv"), "Zone"),
)


def _zone_drops() -> list[dict]:
    """``[{kind, name, items}]`` -- every exploring location and fishing zone."""
    zones: list[dict] = []
    for kind, path, column in _ZONE_SOURCES:
        if not path.exists():
            continue
        by_name: dict[str, dict] = {}
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get(column) or "").strip()
                item = (row.get("Item") or "").strip()
                if not name or not item:
                    continue
                zone = by_name.setdefault(
                    name, {"kind": kind, "name": name, "items": []}
                )
                if item not in zone["items"]:
                    zone["items"].append(item)
        zones.extend(by_name.values())
    return zones


def crafting_catalogue() -> dict:
    """Reference data for the timeline page's "made from" filter.

    Static game content -- the workshop's recipe graph and what each zone
    drops -- with no player state in it at all, which is why it is a plain
    cacheable GET (see ``views.api_catalogue``) rather than one of the two
    solve POSTs. The page uses it to answer "if I have a pile of Straw, what
    could I be crafting?".

    ``uses`` is the graph **inverted**: material -> the items whose recipe
    consumes it directly, keyed lower-cased like the rest of the model. The
    client walks it transitively (Straw -> Twine -> Rope -> ...), which is
    cheaper to ship and lets it union several materials without a round trip.
    Materials the game supplies free (:data:`crafting.IGNORED_MATERIALS`) are
    left in: they are still real inputs, and a player asking what their Iron
    could become deserves an answer.

    ``names`` maps every lower-cased key here (recipe, material or zone drop)
    to its display spelling, so the client can show and suggest names without a
    second catalogue.
    """
    workshop = crafting.load_workshop()
    uses: dict[str, list[str]] = {}
    names: dict[str, str] = {}

    for recipe in workshop.recipes.values():
        names.setdefault(recipe.name.lower(), recipe.name)
        for material in recipe.materials:
            key = material.lower()
            names.setdefault(key, material)
            consumers = uses.setdefault(key, [])
            if recipe.name.lower() not in consumers:
                consumers.append(recipe.name.lower())

    zones = _zone_drops()
    for zone in zones:
        for item in zone["items"]:
            names.setdefault(item.lower(), item)
        zone["items"] = [item.lower() for item in zone["items"]]

    return {"uses": uses, "names": names, "zones": zones}


def _wrap_argument_error(fn, value):
    """Call an ``argparse``-flavoured parser (raises ``ArgumentTypeError``),
    re-raising as ``ValueError`` so every ``params_to_args`` failure is the
    same exception type regardless of which CLI helper it reuses.
    """
    try:
        return fn(value)
    except argparse.ArgumentTypeError as exc:
        raise ValueError(str(exc)) from exc


def params_to_args(params: dict) -> argparse.Namespace:
    """Build a CLI-equivalent ``argparse.Namespace`` from a request's params.

    Starts from ``build_parser().parse_args([])`` (every default) and applies
    each recognised key on top; an unrecognised key raises ``ValueError``
    rather than being silently ignored. ``mastery``/``items``/
    ``override_inventory`` are validated here (so a bad spec fails before any
    solving starts) but the *raw string* is stored on ``args`` -- exactly what
    the CLI itself carries, so ``main.build_inputs`` re-parses it the same way
    either path.

    Never sets ``args.solver``, ``args.format``, ``args.pager``, any file
    path, or either ``--empty-*`` flag: the LP backend is pinned once at
    Django startup (never per-request), and there is no file path or output
    format in a web request.
    """
    unknown = set(params) - _KNOWN_PARAM_KEYS
    if unknown:
        raise ValueError(f"unknown param(s): {', '.join(sorted(unknown))}")

    args = main.build_parser().parse_args([])

    if "target" in params:
        args.target = _wrap_argument_error(main.parse_target, str(params["target"]))
    if "level" in params and params["level"] is not None:
        args.level = int(params["level"])
    if "mastery" in params and params["mastery"] is not None:
        raw = params["mastery"]
        parsing.parse_item_amounts(raw)
        args.mastery = raw
    if "items" in params and params["items"] is not None:
        raw = params["items"]
        parsing.parse_item_amounts(raw)
        args.items = raw
    if "override_inventory" in params and params["override_inventory"] is not None:
        raw = params["override_inventory"]
        parsing.parse_item_amounts(raw)
        args.override_inventory = raw
    if "use_pumpkin_juice" in params:
        value = params["use_pumpkin_juice"]
        args.use_pumpkin_juice = None if value is None else int(value)
    if "max_gold" in params and params["max_gold"] is not None:
        args.max_gold = _wrap_argument_error(
            main.parse_gold_budget, str(params["max_gold"])
        )
    if "cookie_sets" in params and params["cookie_sets"] is not None:
        args.cookie_sets = int(params["cookie_sets"])
    if "current_stamina" in params and params["current_stamina"] is not None:
        args.current_stamina = int(params["current_stamina"])
    if "plans" in params and params["plans"] is not None:
        args.plans = int(params["plans"])
    if "spread" in params:
        # AXIS[=LO:HI], e.g. "pj=0:10" or bare "time" -- same grammar as
        # --spread, parsed by the CLI's own main._parse_spread so both paths
        # agree on axis aliases/range syntax. An empty/absent value is "off"
        # (the default time-thinned frontier, args.spread stays None).
        raw = params["spread"]
        args.spread = (
            _wrap_argument_error(main._parse_spread, str(raw)) if raw else None
        )
    if "graph" in params:
        # "" / absent -> off (None); otherwise one of --graph's own choices.
        raw = params["graph"]
        if not raw:
            args.graph = None
        elif raw in ("auto", "gold", "time"):
            args.graph = raw
        else:
            raise ValueError(
                f"graph must be 'auto', 'gold', 'time', or empty, not {raw!r}"
            )
    if "start_date" in params:
        value = params["start_date"]
        args.start_date = date.fromisoformat(value) if value else None
    if "max_ap" in params:
        value = params["max_ap"]
        args.max_ap = int(value) if value is not None else None
    if "max_ln" in params:
        value = params["max_ln"]
        args.max_ln = int(value) if value is not None else None
    if "max_ac" in params:
        value = params["max_ac"]
        args.max_ac = int(value) if value is not None else None
    if "passive_limit" in params:
        raw = params["passive_limit"]
        if raw is not None:
            passive.parse_duration(raw)
        args.passive_limit = raw
    if "crafting_bonus" in params and params["crafting_bonus"] is not None:
        args.crafting_bonus = float(params["crafting_bonus"])
    if "mastery_bonus" in params and params["mastery_bonus"] is not None:
        args.mastery_bonus = float(params["mastery_bonus"])
    if "breakdown" in params:
        args.breakdown = bool(params["breakdown"])
    if "self_sourced" in params:
        args.self_sourced = bool(params["self_sourced"])
    if "count_pumpkin_juice_gold" in params:
        value = params["count_pumpkin_juice_gold"]
        # Whether juice's cost counts in net gold (see main.price_pumpkin_juice_
        # enabled); the LP prices it either way. None -> config default.
        args.price_pumpkin_juice = None if value is None else bool(value)

    calendar = params.get("calendar")
    if calendar in ("full", "model"):  # "model" = the pre-enum wire value
        args.model_calendar = True
    elif calendar == "average":
        args.average_calendar = True
    elif calendar == "ignore":
        args.ignore_calendar = True
    elif calendar is not None:
        raise ValueError(
            f"calendar must be 'full', 'average', 'ignore', or null, not {calendar!r}"
        )

    plan_select = params.get("plan_select")
    if plan_select is not None:
        kind = plan_select.get("kind")
        if kind == "fastest":
            args.fastest_plan = True
        elif kind == "cheapest":
            args.cheapest_plan = True
        elif kind == "index":
            index = plan_select.get("index")
            if index is None:
                raise ValueError("plan_select kind 'index' needs an 'index'")
            args.plan = int(index)
        else:
            raise ValueError(
                "plan_select.kind must be 'fastest', 'cheapest' or 'index', "
                f"not {kind!r}"
            )

    # Free-text overrides plus the first-class config knobs (Trading Values,
    # instant grow time) both become args.override entries, applied in order --
    # the dedicated knobs come last so an explicit control wins over a
    # conflicting free-text line for the same flag.
    config_overrides: list[str] = []
    if "overrides" in params and params["overrides"] is not None:
        overrides = list(params["overrides"])
        main.apply_overrides({}, overrides)  # validate syntax only; cfg discarded
        config_overrides.extend(overrides)
    # The preference panels' {section.flag: value} object -- same override
    # channel as the free-text box, so it follows it (explicit control wins).
    if "config" in params and params["config"] is not None:
        panel_config = params["config"]
        if not isinstance(panel_config, dict):
            raise ValueError("config must be a JSON object of section.flag -> value")
        panel_lines = [_config_override_line(k, v) for k, v in panel_config.items()]
        main.apply_overrides({}, panel_lines)  # validate keys/values before solving
        config_overrides.extend(panel_lines)
    for key, flag in _CONFIG_PARAM_FLAGS.items():
        if key in params and params[key] is not None:
            config_overrides.append(f"{flag}={float(params[key])}")
    if "instant_grow_time" in params and params["instant_grow_time"] is not None:
        raw = str(params["instant_grow_time"])
        passive.parse_duration(raw)  # validate the duration before it is solved
        config_overrides.append(f'farming.instant_grow_time="{raw}"')
    if "hickory_omelette" in params and params["hickory_omelette"] is not None:
        raw = str(params["hickory_omelette"])
        if raw not in config.HICKORY_MODES:  # validate before it is solved
            raise ValueError(
                f"hickory_omelette must be one of {config.HICKORY_MODES}, not {raw!r}"
            )
        config_overrides.append(f'sawmill.hickory_omelette="{raw}"')
    if "ap_trading" in params and params["ap_trading"] is not None:
        ap_lines = _ap_trading_override_lines(params["ap_trading"])
        main.apply_overrides({}, ap_lines)  # validate before solving; cfg discarded
        config_overrides.extend(ap_lines)
    if config_overrides:
        args.override = config_overrides

    return args


def _hash_state(
    mastery_text: str,
    inventory_text: str,
    args: argparse.Namespace,
    fields: tuple[str, ...],
) -> str:
    """sha256 of both pasted texts plus the named ``args`` fields.

    ``start_date`` is special-cased to today's date when unset (rather than
    hashing ``None``), so the *default* key doesn't quietly rot at midnight
    between an initial solve and a same-day repeat request.
    """
    parts = [mastery_text, inventory_text]
    for name in fields:
        value = date.today().isoformat() if name == "start_date" and not args.start_date else getattr(args, name)
        parts.append(repr(value))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def fingerprint(mastery_text: str, inventory_text: str, args: argparse.Namespace) -> str:
    """Session identity: a sha256 of both pasted texts plus every resolve field.

    Two requests that agree on this hash would produce the identical frontier,
    so a cache hit skips solving entirely; anything that only changes
    :data:`REFILTER_FIELDS` is deliberately excluded, so those requests keep
    hitting the same cache entry.
    """
    return _hash_state(mastery_text, inventory_text, args, RESOLVE_FIELDS)


def mastery_costs_fingerprint(
    mastery_text: str, inventory_text: str, args: argparse.Namespace
) -> str:
    """Cache key for a mastery-costs request (:data:`MASTERY_COST_FIELDS`).

    Namespaced with an ``"mc:"`` prefix because both kinds of cached artefact
    share one :class:`web.solverapp.cache.SessionCache`: without it a
    mastery-costs payload and a :class:`SolveSession` could never actually
    collide (their field sets differ), but the prefix makes that a property of
    the key rather than of the hash, and keeps the two readable in a dump.
    """
    return "mc:" + _hash_state(
        mastery_text, inventory_text, args, MASTERY_COST_FIELDS
    )


@dataclass
class SolveSession:
    """Everything cached for a fingerprint: the solved frontier and its context.

    ``cfg`` (the loaded, ``--override``-applied config) is not part of the
    plan's original 3-field sketch, but is needed here: :func:`main.solve_frontier`
    takes it as an explicit parameter (see main.py's docstring on that
    function for why it isn't folded into ``SolveInputs``), and the horizon
    guard in :func:`render` re-solves without going through :func:`solve`
    again, so it has to come from somewhere -- caching it alongside the rest
    of the session is cheaper than reloading/reapplying overrides every
    refilter and is safe: it depends only on ``args.override``, itself a
    :data:`RESOLVE_FIELDS` member, so it can never go stale within one
    fingerprint's session.
    """

    inputs: main.SolveInputs
    frontier: main.FrontierResult
    ctx: main.PlanContextData
    cfg: dict


@dataclass
class SolveResult:
    """What a request needs back: rendered markdown sections + the frontier table."""

    sections: dict[str, str]  # section name -> markdown (SectionedReporter.docs)
    notes: list[str]
    warnings: list[str]
    frontier_rows: list[dict]
    selected: int
    resolved: bool  # True when this render triggered a re-solve (horizon guard)
    # The tower's top (highest reachable) level, for the UI's "Tower (highest
    # reachable)" option label. Constant from the tower data -- independent of
    # what this solve actually targeted (a specific --level must not relabel it).
    tower_max_level: int | None = None


def _empty_plan_context() -> main.PlanContextData:
    """A placeholder PlanContextData for the "nothing left to source" case.

    derive_report_inputs would crash on an empty frontier (cheapest_plan's
    min() over no plans); nothing calls it in that case.
    """
    return main.PlanContextData(
        cheapest=0, display={}, max_passive_residuals={}, reasons=[],
        golds=None, sourceable_elsewhere=set(),
    )


def _nothing_to_source_result(inputs: main.SolveInputs, resolved: bool) -> SolveResult:
    reporter = report.SectionedReporter()
    reporter.solution(inputs.solution)  # no plans, so no remainder to report
    reporter.note("Nothing left to source.")
    return SolveResult(
        sections={name: doc.render() for name, doc in reporter.docs.items()},
        notes=list(reporter.notes), warnings=[], frontier_rows=[],
        selected=-1, resolved=resolved, tower_max_level=inputs.requirements.max_level,
    )


def _prepare(
    mastery_text: str, inventory_text: str, args: argparse.Namespace
) -> tuple[dict, farm_solver.GameState]:
    """The config + game state every solve path starts from.

    Mirrors main.run's config-load / calendar-strip / crafting-bonus-resolve
    preamble and its file-loading + override-inventory step (there: load_state
    on the CLI's files; here: a GameState built directly from request text), so
    :func:`solve` and :func:`mastery_costs` can never drift apart on it.
    """
    cfg = load_web_config()
    main.apply_overrides(cfg, args.override)
    # Mirror main.run: the calendar 'ignore' mode drops seasonal pets from the
    # config before anything reads [pets].
    if main.resolve_calendar_mode(args, cfg) == "ignore":
        cfg = main.strip_dated_sources(cfg)
    # Mirror main.run: resolve an unset --crafting-bonus from [workshop]/[tower]
    # before anything downstream reads args.crafting_bonus.
    main.resolve_crafting_bonus(args, cfg)
    state = farm_solver.GameState(
        masteries=parsing.parse_masteries(mastery_text),
        inventory=parsing.parse_inventory(inventory_text),
    )
    if args.override_inventory is not None:
        main.apply_inventory_overrides(
            state.inventory, parsing.parse_item_amounts(args.override_inventory)
        )
    # Mirror main.run: settle [cow_pasture] has_mariyas_wheelbarrow from the
    # inventory before anything builds passive production off cfg.
    main.resolve_mariyas_wheelbarrow(cfg, state.inventory)
    return cfg, state


def solve(mastery_text: str, inventory_text: str, args: argparse.Namespace) -> SolveSession:
    """Run a full (re-)solve from pasted text: the ~0.5-3s path (see ARCHITECTURE.md).

    The :func:`_prepare` preamble followed by the same three solve stages
    build_inputs / solve_frontier / derive_report_inputs main.run itself drives.
    """
    cfg, state = _prepare(mastery_text, inventory_text, args)
    inputs = main.build_inputs(args, cfg, state)
    fr = main.solve_frontier(inputs, args, cfg)
    ctx = main.derive_report_inputs(inputs, fr, args) if fr.plans else _empty_plan_context()
    return SolveSession(inputs=inputs, frontier=fr, ctx=ctx, cfg=cfg)


@dataclass
class MasteryCostsPayload:
    """The mastery-costs table, ready to serialise (see :func:`mastery_costs`)."""

    rows: list[dict]
    unmasterable: list[str]
    limited: list[str]


def mastery_costs(
    mastery_text: str, inventory_text: str, args: argparse.Namespace
) -> MasteryCostsPayload:
    """Run --mastery-costs from pasted text: one fastest plan per (item, tier).

    The :func:`_prepare` preamble, then :func:`main.solve_mastery_costs`, which
    solves its own per-item goals and so ignores ``target``/``mastery``/
    ``items`` entirely -- the CLI's ``--mastery-costs`` conflict check has no
    counterpart here, since nothing on this path would read them.

    Rows are returned **structured**, not as rendered markdown the way
    :func:`render`'s sections are: the client sorts them itself (no round trip)
    and turns each into a link to the solver page seeded with that goal, both of
    which need the item and tier as data rather than as table cells. ``time`` is
    carried twice -- preformatted for display and in seconds for that sort.
    """
    cfg, state = _prepare(mastery_text, inventory_text, args)
    result = main.solve_mastery_costs(args, cfg, state)
    return MasteryCostsPayload(
        rows=[
            {
                "item": row.item,
                "tier": row.tier,
                "time": passive.format_duration(row.time),
                "time_seconds": row.time.total_seconds(),
                "gold": row.gold,
            }
            for row in result.rows
        ],
        unmasterable=list(result.unmasterable),
        limited=list(result.limited),
    )


def render(
    session: SolveSession, args: argparse.Namespace
) -> tuple[SolveResult, SolveSession]:
    """Apply a refilter (instant) against ``session``'s cached frontier.

    HORIZON GUARD -- the one refilter that can force a re-solve: with the
    seasonal calendar modelled, the cached frontier's ``horizon`` is the
    makespan the fixed point converged on for whichever plan was selected at
    solve time. A refilter (limits, or an explicit plan/cheapest selection)
    can land on a *different* plan whose own makespan no longer agrees with
    that horizon -- a date-dependent source (a seasonal pet, the Mechanical
    Heart's grants) would then be modelled at the wrong span, so its passive
    breakpoints and the plan's own numbers could be inconsistent. When that
    happens, re-solve the fixed point seeded from the newly selected plan's
    makespan instead of displaying stale seasonal figures; every other
    refilter is pure post-processing over the cached frontier, no LP solve.
    """
    inputs = session.inputs
    fr = session.frontier

    # A cache-hit request builds its own fresh `args` (params_to_args), never
    # passed through solve()'s resolution -- so an unset --crafting-bonus would
    # otherwise still read None here (crafting_bonus is a RESOLVE_FIELD, so a
    # cache hit means this request agrees with the one that solved, including
    # on this field's *unresolved* value; resolving against the cached cfg
    # reproduces the exact float solve() used).
    main.resolve_crafting_bonus(args, session.cfg)

    if not fr.plans:
        return _nothing_to_source_result(inputs, resolved=False), session

    time_limit = (
        passive.parse_duration(args.passive_limit)
        if args.passive_limit is not None
        else None
    )

    def _refilter(
        frontier: main.FrontierResult,
    ) -> tuple[list[optimizer.SourcingPlan], list[optimizer.SourcingPlan], int]:
        within = [
            p for p in frontier.plans
            if main.within_limits(
                p, time_limit, args.max_ap, args.max_ln, args.max_gold, args.max_ac
            )
        ]
        shown = within or frontier.plans
        selected = main.selected_plan(shown, args.plan, args.cheapest_plan)
        return within, shown, selected

    within, shown, selected = _refilter(fr)
    resolved = False

    if inputs.model_calendar and not main._makespans_close(
        shown[selected].time, fr.horizon
    ):
        fr = main.solve_frontier(
            inputs, args, session.cfg, horizon_seed=shown[selected].time
        )
        resolved = True
        if not fr.plans:
            session = SolveSession(
                inputs=inputs, frontier=fr, ctx=_empty_plan_context(), cfg=session.cfg
            )
            return _nothing_to_source_result(inputs, resolved=True), session
        session = SolveSession(
            inputs=inputs, frontier=fr,
            ctx=main.derive_report_inputs(inputs, fr, args), cfg=session.cfg,
        )
        within, shown, selected = _refilter(fr)

    # ctx.reasons/golds/cheapest were computed over whatever `shown` looked
    # like at solve() (or the re-solve above) time; a refilter can leave
    # `shown` different (fewer plans, different order), so recompute those
    # three here against the current `shown` -- cheap (no LP; the reference
    # plan's self_sourced_endpoint/passive_residuals in max_passive_residuals
    # stay cached, as does display).
    ctx = session.ctx
    reasons = report._frontier_reasons(
        shown, inputs.demand, inputs.recipes, args.crafting_bonus,
        inputs.open_flows, inputs.inventory, ctx.max_passive_residuals,
        fr.production, ctx.display,
    )
    golds = None
    if inputs.prices is not None:
        golds = [optimizer.plan_gold_breakdown(p, inputs.prices) for p in shown]
    cheapest = main.cheapest_plan(shown)
    ctx = replace(ctx, reasons=reasons, golds=golds, cheapest=cheapest)
    session = replace(session, ctx=ctx)

    reporter = report.SectionedReporter()
    reporter.solution(inputs.solution, fr.plans[0].remainder)
    if inputs.model_calendar:
        dated = []
        if any(name.lower() in pets.SEASONAL_MONTHS for name in session.cfg.get("pets", {})):
            dated.append("seasonal pets")
        if inputs.monthly_grants:
            dated.append("the Mechanical Heart's grants")
        reporter.note(
            f"Starting {inputs.start:%d %b %Y}; {' and '.join(dated)} count only "
            "for the part of the plan they fall in."
        )
    warnings = []
    if not within:
        # Surfaced as a warning only (the client renders notes and warnings into
        # the same area, so adding it to both showed it twice).
        message = main.limit_shortfall(
            fr.plans, time_limit, args.max_ap, args.max_ln, args.max_gold, args.max_ac
        )
        warnings.append(message)

    # Resolved the same way main.run does (both are pure functions of args+cfg,
    # see resolve_frontier_spread/resolve_frontier_graph's own docstrings) --
    # not threaded through FrontierResult, since a refilter never changes them.
    resolved_spread = main.resolve_frontier_spread(args, session.cfg)
    graph_metric = main.resolve_frontier_graph(args, session.cfg)
    reporter.frontier(
        shown, selected, ctx.cheapest, ctx.reasons, inputs.prices, ctx.golds,
        spread_axis=resolved_spread.axis if resolved_spread else None,
        graph_metric=graph_metric,
    )
    chosen = shown[selected]
    inputs.solution.arnold_palmers = round(chosen.arnold_palmers)
    inputs.solution.large_nets = round(chosen.large_nets)
    pj_schedule = main.pumpkin_juice_schedule(chosen, inputs.solution.needs, inputs.pj_gain)
    reporter.plan(
        chosen, inputs.demand, inputs.farm, inputs.workshop, args.crafting_bonus,
        fr.production, inputs.open_flows, inputs.smith.names, inputs.inventory,
        ctx.sourceable_elsewhere, ctx.max_passive_residuals, inputs.prices, pj_schedule,
        ap_prices=inputs.ap_prices,
    )
    if args.breakdown:
        reporter.crafts(chosen, inputs.workshop, args.crafting_bonus, args.mastery_bonus)

    # The impact graph's own axis + metric, resolved once for every row below.
    # "axis_value" is each plan's position on the spread axis (falling back to
    # "time" -- the default time-thinned frontier's own axis -- when no spread
    # was requested); "secondary" is the graph's y metric. Both are read off
    # the fields already computed per row rather than re-deriving them (mirrors
    # optimizer.spread_graph_series's own "auto" -> the *other* of gold/time
    # resolution), so app.js's graph tooltip can plot/label a point without
    # importing any solver logic of its own.
    graph_axis = resolved_spread.axis if resolved_spread else "time"
    graph_y_metric = graph_metric
    if graph_y_metric in (None, "auto"):
        graph_y_metric = "time" if graph_axis == "gold" else "gold"

    frontier_rows = []
    for i, plan in enumerate(shown):
        time_seconds = plan.time.total_seconds()
        net_gold = plan.gold - plan.surplus_gold
        pumpkin_juices = round(report._pumpkin_juices(plan))
        axis_value = {
            "time": time_seconds,
            "passive": time_seconds,
            "gold": net_gold,
            "pumpkin-juice": pumpkin_juices,
        }[graph_axis]
        secondary = time_seconds if graph_y_metric == "time" else net_gold
        frontier_rows.append({
            "index": i,
            "time": passive.format_duration(plan.time),
            "time_seconds": time_seconds,
            "gold": plan.gold,
            "net_gold": net_gold,
            "pumpkin_juices": pumpkin_juices,
            "selected": i == selected,
            "cheapest": i == ctx.cheapest,
            # The frontier is fastest-first, so shown[0] is the fastest plan --
            # the same row markdown_frontier tags "[fastest]".
            "fastest": i == 0,
            # This row's point on the impact graph (see graph_axis/graph_y_metric
            # above) -- what app.js's wireGraphHover tooltip reads per point.
            "axis_value": axis_value,
            "secondary": secondary,
        })

    result = SolveResult(
        sections={name: doc.render() for name, doc in reporter.docs.items()},
        notes=list(reporter.notes),
        warnings=warnings,
        frontier_rows=frontier_rows,
        selected=selected,
        resolved=resolved,
        tower_max_level=session.inputs.requirements.max_level,
    )
    return result, session
