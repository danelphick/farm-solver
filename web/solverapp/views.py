"""HTTP views: the two pages and their JSON solve APIs.

All solving logic lives in :mod:`webapi` (framework-free, plain-pytest
tested); this module is purely the HTTP <-> webapi bridge -- decoding the
request, mapping :mod:`webapi`'s exceptions to status codes, rendering each
markdown section to HTML, and shaping the JSON response.
"""

from __future__ import annotations

import argparse
import json
import time
from functools import lru_cache

import markdown
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

import webapi

from .cache import CACHE, SOLVE_SEMAPHORE, key_lock

# Solve-semaphore acquire timeout: long enough to queue behind one in-flight
# solve (budget ~1-3s per ARCHITECTURE.md's server-hardware note) without
# hanging a request indefinitely if something is stuck.
_SEMAPHORE_TIMEOUT_SECONDS = 20

# The mastery-costs sweep gets its own, longer budget: it is one LP solve per
# remaining (item, tier) pair -- ~1s on a well-progressed save but ~5s locally
# on an empty one (every catalogue item x 3 tiers), and slower again on the
# deploy box. Queueing behind one of those would blow the 20s budget above and
# 503 a request that was only ever going to be a little slow.
_MASTERY_SEMAPHORE_TIMEOUT_SECONDS = 60


def index(request: HttpRequest) -> HttpResponse:
    # Bundled config.toml values, so the preference panels can show each as a
    # placeholder ("blank = use default"). Rendered into a <script> tag via
    # Django's json_script (DjangoJSONEncoder serialises any date to ISO).
    # `debug` gates the CLI-command debug button (see api_solve's debug_command).
    return render(
        request,
        "solverapp/index.html",
        {"config_defaults": webapi.config_defaults(), "debug": settings.DEBUG},
    )


def masteries(request: HttpRequest) -> HttpResponse:
    """The mastery-costs page (`--mastery-costs` in the browser).

    Same ``config_defaults`` as :func:`index`: this page includes the very same
    settings popover (``_settings.html``), whose preference panels and
    material-trading list are generated from those defaults. Its client reads
    (and, on a popover save, writes) the pasted state and every parameter in the
    same ``localStorage`` the solver page uses.
    """
    return render(
        request,
        "solverapp/mastery_costs.html",
        {"config_defaults": webapi.config_defaults()},
    )


def timeline(request: HttpRequest) -> HttpResponse:
    """The mastery-history timeline page (`/timeline?item=X`).

    Has no API of its own and never solves: every byte it charts comes from the
    dated mastery snapshots the browser holds in IndexedDB (see
    ``static/solverapp/history.js``), which never reach the server. The view
    exists only to serve the page -- and, like :func:`masteries`, the same
    ``config_defaults`` the shared settings popover's generated panels need.
    """
    return render(
        request,
        "solverapp/timeline.html",
        {"config_defaults": webapi.config_defaults()},
    )


@lru_cache(maxsize=1)
def _catalogue() -> dict:
    """The crafting/zone reference data, built once per process.

    Static game content read from files that cannot change under a running
    server, so it is worth exactly one build -- unlike the solve endpoints,
    there is no player state to key on.
    """
    return webapi.crafting_catalogue()


@require_GET
def api_catalogue(request: HttpRequest) -> JsonResponse:
    """Recipe graph + zone drops for the timeline page's "made from" filter.

    A GET, and the only API here that is: it carries no player state in either
    direction (see :func:`webapi.crafting_catalogue`), so there is nothing to
    post and nothing to key a cache on -- the response is the same for
    everyone, and a browser may cache it freely.
    """
    return JsonResponse(_catalogue())


def _section_html(text: str) -> str:
    """Render one SectionedReporter markdown section to HTML."""
    return markdown.markdown(text, extensions=["tables"])


def _debug_command(args, mastery_text: str, inventory_text: str) -> str | None:
    """The debug popup's CLI command, or None outside DEBUG.

    In DEBUG we capture the pasted mastery/inventory to a local temp directory
    (webapi.capture_debug_state) and point the command's --mastery-file/
    --inventory-file at them, so running it reproduces the exact browser solve.
    """
    if not settings.DEBUG:
        return None
    mastery_file, inventory_file = webapi.capture_debug_state(
        mastery_text, inventory_text
    )
    return webapi.args_to_command(
        args, mastery_file=mastery_file, inventory_file=inventory_file
    )


def _decode_request(
    request: HttpRequest,
) -> tuple[str, str, argparse.Namespace] | JsonResponse:
    """``(mastery_text, inventory_text, args)``, or the 400 to return instead.

    Shared by both JSON endpoints: every solve request has the same
    ``{mastery_text, inventory_text, params}`` body, and every way it can be
    malformed is the client's fault (400) rather than an unparseable *paste*,
    which only surfaces later, inside :mod:`webapi` (422).
    """
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "malformed JSON body"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "request body must be a JSON object"}, status=400)

    params = payload.get("params", {})
    if not isinstance(params, dict):
        return JsonResponse({"error": "params must be a JSON object"}, status=400)

    try:
        args = webapi.params_to_args(params)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return payload.get("mastery_text", ""), payload.get("inventory_text", ""), args


@csrf_exempt  # a JSON POST API with no session/cookie auth -- nothing to forge
@require_POST
def api_solve(request: HttpRequest) -> JsonResponse:
    decoded = _decode_request(request)
    if isinstance(decoded, JsonResponse):
        return decoded
    mastery_text, inventory_text, args = decoded

    key = webapi.fingerprint(mastery_text, inventory_text, args)
    started = time.monotonic()

    with key_lock(key):
        session = CACHE.get(key)
        # The cache also holds mastery-costs payloads, under their own "mc:"
        # key prefix -- a fingerprint collision across the two is impossible,
        # but narrow the type rather than assume it.
        if not isinstance(session, webapi.SolveSession):
            session = None
        cached = session is not None
        if not SOLVE_SEMAPHORE.acquire(timeout=_SEMAPHORE_TIMEOUT_SECONDS):
            return JsonResponse({"error": "solver busy, try again"}, status=503)
        try:
            if session is None:
                session = webapi.solve(mastery_text, inventory_text, args)
            result, session = webapi.render(session, args)
        except ValueError as exc:
            # Only reachable from webapi.solve's parsing of mastery_text/
            # inventory_text (params were already validated above), so this
            # is always an unparseable paste.
            return JsonResponse({"error": str(exc)}, status=422)
        finally:
            SOLVE_SEMAPHORE.release()
        CACHE.put(key, session)

    solved_ms = round((time.monotonic() - started) * 1000)

    return JsonResponse({
        "session": key,
        "cached": cached and not result.resolved,
        "solved_ms": solved_ms,
        # The tower's top level, for the UI's "Tower (highest reachable)" label
        # (constant -- not the level a specific --level solve targeted).
        "tower_max_level": result.tower_max_level,
        "solution_html": _section_html(result.sections["solution"]),
        "notes": result.notes,
        "warnings": result.warnings,
        "frontier": {
            "html": _section_html(result.sections["frontier"]),
            "plans": result.frontier_rows,
        },
        # The impact graph (--graph's web counterpart): a "graph" section only
        # exists in SectionedReporter.docs when the request asked for one (see
        # report.SectionedReporter.frontier); absent -> no graph was requested,
        # so the client gets an empty html string and leaves #graph-section as-is.
        # It is already a self-contained <svg> fragment -- served raw, NOT through
        # the Markdown converter, which would wrap the opening tag as `<p><svg></p>`
        # and break the graphic (leaving only its stray text nodes visible).
        "graph": {
            "html": (
                result.sections["graph"]
                if result.sections.get("graph", "").strip()
                else ""
            ),
        },
        "plan": {
            "index": result.selected,
            "html": _section_html(result.sections["plan"]),
            "note": result.plan_note,
        },
        # Rendered into its own collapsible pane after the plan; empty when
        # the request didn't ask for the breakdown.
        "crafts": {
            "html": (
                _section_html(result.sections["crafts"])
                if result.sections.get("crafts", "").strip()
                else ""
            ),
        },
        # DEBUG only: the equivalent `python main.py` command, for the debug
        # button's popup. None in production so the button never has content.
        # The pasted mastery/inventory are dumped to a local temp directory so
        # the command reproduces the exact solve (see webapi.capture_debug_state).
        "debug_command": _debug_command(args, mastery_text, inventory_text),
    })


@csrf_exempt  # same reasoning as api_solve: a JSON POST API with no auth
@require_POST
def api_mastery_costs(request: HttpRequest) -> JsonResponse:
    """The mastery-costs table for one pasted state (`--mastery-costs`).

    Same flow as :func:`api_solve` -- decode, key, dogpile lock, cache, solve
    under the shared semaphore -- but with its own narrower cache key
    (``webapi.mastery_costs_fingerprint``: the goal fields don't reach this
    solve, so changing a goal on the solver page must not evict this table) and
    its own longer semaphore budget. There is no refilter step: the rows are
    returned structured and the client sorts them itself. The response carries
    both "cannot" lists the sweep collects -- ``unmasterable`` (no source at
    all) and ``limited`` (sourceable, but not in a tier's quantity).
    """
    decoded = _decode_request(request)
    if isinstance(decoded, JsonResponse):
        return decoded
    mastery_text, inventory_text, args = decoded

    key = webapi.mastery_costs_fingerprint(mastery_text, inventory_text, args)
    started = time.monotonic()

    with key_lock(key):
        payload = CACHE.get(key)
        if not isinstance(payload, webapi.MasteryCostsPayload):
            payload = None
        cached = payload is not None
        if payload is None:
            if not SOLVE_SEMAPHORE.acquire(
                timeout=_MASTERY_SEMAPHORE_TIMEOUT_SECONDS
            ):
                return JsonResponse({"error": "solver busy, try again"}, status=503)
            try:
                payload = webapi.mastery_costs(mastery_text, inventory_text, args)
            except ValueError as exc:
                # As in api_solve: params were validated above, so the only
                # ValueError left is an unparseable mastery/inventory paste.
                return JsonResponse({"error": str(exc)}, status=422)
            finally:
                SOLVE_SEMAPHORE.release()
            CACHE.put(key, payload)

    return JsonResponse({
        "cached": cached,
        "solved_ms": round((time.monotonic() - started) * 1000),
        "rows": payload.rows,
        "unmasterable": payload.unmasterable,
        "limited": payload.limited,
        "items": payload.items,
    })
