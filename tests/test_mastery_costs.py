from datetime import timedelta
from pathlib import Path

import pytest

import main
import optimizer
from farm_solver import GameState
from main import (
    SELF_SOURCED_TOOLS,
    MasteryCostRow,
    MasteryCostsResult,
    _sorted_mastery_rows,
    build_parser,
    run,
    self_sourced_tools,
    sourceable_items,
    unmasterable_chain,
)
from report import MarkdownDoc, markdown_mastery_costs, print_mastery_costs
from sources import Offer

# A small craft graph: Awl is craftable from Steel Wire (obtainable) and Flarite,
# which is craftable from Unpolished Flarite, which has no source at all.
RECIPES = {
    "awl": {"steel wire": 2.0, "flarite": 1.0},
    "flarite": {"unpolished flarite": 3.0},
    "sturdy pickaxe": {"stone shell": 1.0, "board": 4.0},
}
DISPLAY = {
    "awl": "Awl",
    "flarite": "Flarite",
    "unpolished flarite": "Unpolished Flarite",
    "sturdy pickaxe": "Sturdy Pickaxe",
    "stone shell": "Stone Shell",
    "board": "Board",
}


def test_chain_bare_leaf_has_no_recipe():
    # An item with no recipe of its own is a bare "(unobtainable)" leaf.
    assert unmasterable_chain("stone shell", RECIPES, {"board"}, DISPLAY) == (
        "Stone Shell (unobtainable)"
    )


def test_chain_single_blocking_material():
    # Sturdy Pickaxe needs Stone Shell (no source); Board is obtainable, so only
    # the blocker is named.
    available = {"board", "steel wire"}
    assert unmasterable_chain("sturdy pickaxe", RECIPES, available, DISPLAY) == (
        "Sturdy Pickaxe needs Stone Shell (unobtainable)"
    )


def test_chain_nested_craftable_but_stuck():
    # Awl needs Flarite, itself craftable but stuck on Unpolished Flarite: the
    # nested link reads "(which needs ...)" and the leaf "(unobtainable)".
    available = {"steel wire"}
    assert unmasterable_chain("awl", RECIPES, available, DISPLAY) == (
        "Awl needs Flarite (which needs Unpolished Flarite (unobtainable))"
    )


def test_chain_uses_title_case_fallback_for_unknown_display():
    # A material missing from the display map falls back to .title().
    recipes = {"box": {"widget grommet": 1.0}}
    assert unmasterable_chain("box", recipes, set(), {"box": "Box"}) == (
        "Box needs Widget Grommet (unobtainable)"
    )


def test_chain_cycle_guard_stops_recursion():
    # A recipe cycle (a needs b, b needs a) must not recurse forever; the second
    # visit of a node bottoms out as an "(unobtainable)" leaf.
    recipes = {"a": {"b": 1.0}, "b": {"a": 1.0}}
    display = {"a": "A", "b": "B"}
    result = unmasterable_chain("a", recipes, set(), display)
    assert result == "A needs B (which needs A (unobtainable))"


def test_sourceable_ignores_held_material_that_no_channel_uses():
    # Compass regression: an item craftable only from a material you merely HOLD
    # (nothing produces it, no offer consumes it) is NOT sourceable -- a finite
    # hoard is not a renewable source, matching what `-m Compass:GM` reports.
    recipes = {"compass": {"seeing stone": 2.0}}
    offers = [Offer("grow it", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1))]
    available = sourceable_items(
        offers, recipes, [], prices={}, ap_tradeable=frozenset(),
        inventory={"seeing stone": 5000},  # held, but no channel uses it
    )
    assert "compass" not in available
    assert "seeing stone" not in available


def test_sourceable_counts_held_material_a_channel_consumes():
    # A held item an offer BURNS grounds that channel, so its yields are sourceable
    # (this is the legitimate held-tool case, e.g. an Acorn Pie the explorer uses).
    offers = [Offer("explore", "exploring", {"hide": 100}, consumes={"acorn pie": 1.0})]
    available = sourceable_items(
        offers, {}, [], prices={}, ap_tradeable=frozenset(),
        inventory={"acorn pie": 5},
    )
    assert "hide" in available  # channel grounded by the held pie it consumes


def _row(item, tier, days, gold):
    return MasteryCostRow(item=item, tier=tier, time=timedelta(days=days), gold=gold)


def test_sort_by_time_breaks_ties_on_gold():
    rows = [
        _row("Slow", "M", 5, 10.0),
        _row("FastDear", "M", 1, 900.0),
        _row("FastCheap", "M", 1, 100.0),
    ]
    ordered = [(r.item) for r in _sorted_mastery_rows(rows, "time")]
    # Both 1-day rows come before the 5-day one; the cheaper 1-day row leads.
    assert ordered == ["FastCheap", "FastDear", "Slow"]


def test_sort_by_gold_breaks_ties_on_time():
    rows = [
        _row("DearFast", "M", 1, 500.0),
        _row("CheapSlow", "M", 9, 20.0),
        _row("CheapFast", "M", 2, 20.0),
    ]
    ordered = [r.item for r in _sorted_mastery_rows(rows, "gold")]
    # Both 20 g rows lead (cheapest gold); the faster of them first.
    assert ordered == ["CheapFast", "CheapSlow", "DearFast"]


def test_print_mastery_costs_renders_table_and_unmasterable(capsys):
    result = MasteryCostsResult(
        rows=[
            _row("Valve", "GM", 2, 100.0),
            _row("Ship in a Bottle", "MM", 50, 105.0),
        ],
        unmasterable=["Awl needs Flarite (which needs Unpolished Flarite (unobtainable))"],
    )
    print_mastery_costs(result, "time")
    out = capsys.readouterr().out
    assert "Item" in out and "Target" in out and "Time" in out and "Cost" in out
    assert "Valve" in out and "2d" in out and "100 g" in out
    assert "cannot be mastered" in out
    assert "Unpolished Flarite (unobtainable)" in out


def test_markdown_mastery_costs_builds_table():
    doc = MarkdownDoc()
    result = MasteryCostsResult(
        rows=[_row("Valve", "GM", 2, 100.0)],
        unmasterable=["Cicada (unobtainable)"],
    )
    markdown_mastery_costs(doc, result, "gold")
    rendered = doc.render()
    assert "## Mastery costs" in rendered
    assert "| Item | Target | Time | Cost |" in rendered
    assert "| Valve | GM | 2d | 100 g |" in rendered
    assert "Cicada (unobtainable)" in rendered


def test_empty_result_prints_nothing_left(capsys):
    print_mastery_costs(MasteryCostsResult(rows=[], unmasterable=[]), "time")
    out = capsys.readouterr().out
    assert "nothing left to master" in out


def test_parser_defaults_sort_to_time():
    args = build_parser().parse_args(["--mastery-costs"])
    assert args.mastery_costs is True
    assert args.sort == "time"
    assert build_parser().parse_args(["--mastery-costs", "--sort", "gold"]).sort == "gold"


def test_self_sourced_flag_resolves_to_the_tool_set():
    off = build_parser().parse_args(["--mastery-costs"])
    assert off.self_sourced is False
    assert self_sourced_tools(off) == frozenset()
    on = build_parser().parse_args(["--mastery-costs", "--self-sourced"])
    assert on.self_sourced is True
    assert self_sourced_tools(on) == SELF_SOURCED_TOOLS
    assert SELF_SOURCED_TOOLS == frozenset({"arnold palmer", "large net", "apple cider"})


@pytest.mark.parametrize(
    "extra",
    [["-m", "Butter:100000"], ["-i", "Gainite:5"], ["--target", "tower"], ["--unobtainable"]],
)
def test_mastery_costs_conflicts_raise_before_solving(extra):
    # The conflict check runs before any file load or solve, so a bad combo
    # fails fast rather than after the (slow) per-item sweep.
    args = build_parser().parse_args(["--mastery-costs", *extra])
    with pytest.raises(ValueError, match="--mastery-costs conflicts with"):
        run(args)


# --- infeasible tiers ("limited") --------------------------------------------


def _solve_with_failing_tier(monkeypatch, fail_on_call: int):
    """Run solve_mastery_costs over a one-item catalogue, failing one tier's LP.

    An item can be perfectly sourceable and still not sourceable *in the
    quantity* a tier needs -- a finitely bounded supply makes the LP infeasible
    rather than unobtainable (see optimizer._assert_optimal). Simulated here by
    making the Nth fastest_endpoint call raise, since it takes the real save's
    particular config to hit naturally.
    """
    args = build_parser().parse_args(["--mastery-costs"])
    cfg = main.load_config(Path("web_default_config.toml"))
    main.resolve_crafting_bonus(args, cfg)

    calls: list[dict] = []
    real = optimizer.fastest_endpoint

    def failing(offers, demand, *rest, **kwargs):
        calls.append(demand)
        if len(calls) == fail_on_call:
            raise ValueError("no plan can satisfy the demand (solver status: Infeasible)")
        return real(offers, demand, *rest, **kwargs)

    monkeypatch.setattr(optimizer, "fastest_endpoint", failing)
    monkeypatch.setattr(main, "load_mastery_catalogue", lambda: {"board": "Board"})
    return main.solve_mastery_costs(args, cfg, GameState()), calls


def test_an_infeasible_tier_is_reported_instead_of_aborting_the_sweep(monkeypatch):
    # Master and Grandmaster solve; Megamaster is infeasible -> the rows already
    # found survive and the failure becomes a `limited` line, rather than the
    # ValueError escaping and killing every other item's row too.
    result, _ = _solve_with_failing_tier(monkeypatch, fail_on_call=3)
    assert [(r.item, r.tier) for r in result.rows] == [("Board", "M"), ("Board", "GM")]
    assert result.limited == [
        "Board cannot reach Megamaster: its sources cannot supply that many"
    ]
    assert result.unmasterable == []  # a different failure: this one HAS sources


def test_an_infeasible_tier_stops_the_climb(monkeypatch):
    # Master fails, so Grandmaster and Megamaster -- which need strictly more of
    # the same item -- are not even attempted.
    result, calls = _solve_with_failing_tier(monkeypatch, fail_on_call=1)
    assert result.rows == []
    assert len(calls) == 1
    assert result.limited == [
        "Board cannot reach Master: its sources cannot supply that many"
    ]


def test_print_mastery_costs_renders_the_limited_section(capsys):
    line = (
        "Bouquet of Flowers cannot reach Megamaster: "
        "its sources cannot supply that many"
    )
    print_mastery_costs(
        MasteryCostsResult(
            rows=[_row("Valve", "GM", 2, 100.0)], unmasterable=[], limited=[line]
        ),
        "time",
    )
    out = capsys.readouterr().out
    assert "Tiers that cannot be reached" in out
    assert "Bouquet of Flowers cannot reach Megamaster" in out
    assert "cannot be mastered" not in out  # the other list is empty


def test_markdown_mastery_costs_renders_the_limited_section():
    doc = MarkdownDoc()
    markdown_mastery_costs(
        doc,
        MasteryCostsResult(rows=[], unmasterable=[], limited=["Board cannot reach Master"]),
        "time",
    )
    rendered = doc.render()
    assert "## Tiers that cannot be reached" in rendered
    assert "Board cannot reach Master" in rendered


def test_limited_defaults_to_empty_so_older_callers_still_construct():
    assert MasteryCostsResult(rows=[], unmasterable=[]).limited == []


# --- unmodelled reasons -------------------------------------------------------


def _reasons_file(tmp_path, body):
    path = tmp_path / "unmodelled.csv"
    path.write_text(body)
    return path


def test_load_unmodelled_reasons_renders_a_feature_as_not_modelled(tmp_path):
    path = _reasons_file(tmp_path, "item,source\nNeigh,Cooking\n")
    assert main.load_unmodelled_reasons(path) == {"neigh": "Cooking not modelled"}


def test_load_unmodelled_reasons_maps_the_three_special_sources(tmp_path):
    path = _reasons_file(
        tmp_path,
        "item,source\nSnowman,seasonal\nGold Feed,unknown\nLobster,none\n",
    )
    assert main.load_unmodelled_reasons(path) == {
        "snowman": "seasonal",
        "gold feed": "not modelled",
        "lobster": "unobtainable",
    }


def test_load_unmodelled_reasons_joins_several_rows_for_one_item(tmp_path):
    # A seasonal item you also have to cook carries both reasons, in file order.
    path = _reasons_file(
        tmp_path, "item,source\nFestive Eggnog,Cooking\nFestive Eggnog,seasonal\n"
    )
    assert main.load_unmodelled_reasons(path) == {
        "festive eggnog": "Cooking not modelled; seasonal"
    }


def test_load_unmodelled_reasons_skips_comments_blanks_and_partial_rows(tmp_path):
    path = _reasons_file(
        tmp_path,
        "# a comment\n\nitem,source\nNeigh,Cooking\n\n# another\nBlank,\n,Cooking\n",
    )
    assert main.load_unmodelled_reasons(path) == {"neigh": "Cooking not modelled"}


def test_load_unmodelled_reasons_missing_file_is_empty(tmp_path):
    assert main.load_unmodelled_reasons(tmp_path / "nope.csv") == {}


def test_the_shipped_reasons_file_parses_and_covers_the_named_features():
    reasons = main.load_unmodelled_reasons()
    assert reasons["neigh"] == "Cooking not modelled"
    assert reasons["magna core"] == "Wishing Well not modelled"
    assert reasons["white sugar"] == "Sugar Cane Mill not modelled"
    assert reasons["stone shell"] == "Mining not modelled"
    assert reasons["gold feed"] == "not modelled"
    assert reasons["lobster"] == "unobtainable"
    assert reasons["yule goat"] == "seasonal"


def test_chain_reports_the_reason_instead_of_bare_unobtainable():
    reasons = {"stone shell": "Mining not modelled"}
    assert unmasterable_chain(
        "sturdy pickaxe", RECIPES, {"board"}, DISPLAY, reasons
    ) == "Sturdy Pickaxe needs Stone Shell (Mining not modelled)"


def test_a_reason_ends_the_descent_into_that_items_recipe():
    # Flarite has a recipe, but the reason IS the explanation -- unpicking a
    # recipe it does not really need (Magna Core's Wishing Well case) would only
    # mislead, so the chain stops there.
    reasons = {"flarite": "Mining not modelled"}
    assert unmasterable_chain("awl", RECIPES, {"steel wire"}, DISPLAY, reasons) == (
        "Awl needs Flarite (Mining not modelled)"
    )


def test_an_item_with_no_reason_keeps_the_old_wording():
    assert unmasterable_chain(
        "sturdy pickaxe", RECIPES, {"board"}, DISPLAY, {"unrelated": "Cooking not modelled"}
    ) == "Sturdy Pickaxe needs Stone Shell (unobtainable)"
    # ... and omitting `reasons` entirely behaves exactly as before.
    assert unmasterable_chain("sturdy pickaxe", RECIPES, {"board"}, DISPLAY) == (
        "Sturdy Pickaxe needs Stone Shell (unobtainable)"
    )
