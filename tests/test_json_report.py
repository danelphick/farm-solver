"""Tests for ``--format json``: report.py's :class:`JsonReporter`
and the structured :class:`Block`s :class:`MarkdownDoc` now keeps.

The point of the design is that JSON is *the same report*, not a second one:
``JsonReporter`` drives the identical ``markdown_*`` functions and serializes
the blocks they produce. So the tests here check two things -- that each block
kind serializes faithfully, and that the blocks a JSON run collects are exactly
the ones a Markdown run renders (the parity test at the bottom).
"""

import argparse
import json
from datetime import timedelta

import farm_solver
import main
from crafting import Workshop, WorkshopRecipe
from crops import Crop, Farm
from optimizer import GraphSeries, SourcingPlan
from report import (
    Bullets,
    Cell,
    Graph,
    Heading,
    JsonReporter,
    MarkdownDoc,
    MarkdownReporter,
    Paragraph,
    Table,
    build_reporter,
)
from sources import Draw, Offer

PLANS = [
    SourcingPlan(time=timedelta(days=1), gold=100_000),
    SourcingPlan(time=timedelta(days=4), gold=0),
]


# --- MarkdownDoc keeps structured blocks, renders the same Markdown ---------


def test_doc_records_one_block_per_call():
    doc = MarkdownDoc()
    doc.heading("Goal")
    doc.para("some prose")
    doc.bullets(["a", "b"])
    doc.table(["Item", "Qty"], [["Corn", 5]])
    assert [type(b) for b in doc.blocks] == [Heading, Paragraph, Bullets, Table]
    # Cells are stringified on the way in, exactly as the Markdown renderer
    # always did -- so a JSON consumer sees the report's own text, not an int
    # for one table and a string for the next.
    assert doc.blocks[3].rows == [["Corn", "5"]]


def test_doc_render_is_unchanged_markdown():
    doc = MarkdownDoc()
    doc.heading("Goal")
    doc.para("prose")
    doc.bullets(["a", "b"])
    doc.table(["Item", "Qty"], [["Corn", "5"]])
    assert doc.render() == (
        "## Goal\n"
        "\n"
        "prose\n"
        "\n"
        "- a\n"
        "- b\n"
        "\n"
        "| Item | Qty |\n"
        "| --- | ---: |\n"
        "| Corn | 5 |\n"
    )


def test_heading_shift_still_applies_at_append_time():
    doc = MarkdownDoc(heading_shift=1)
    doc.heading("Nested", level=2)
    assert doc.blocks[0].level == 3
    assert doc.render().startswith("### Nested")


def test_empty_table_appends_no_block():
    doc = MarkdownDoc()
    doc.table(["Item", "Qty"], [])
    assert doc.blocks == []


def test_table_cell_pipe_escaped_in_markdown_but_not_in_json():
    doc = MarkdownDoc()
    doc.table(["Item"], [["a|b"]])
    assert r"a\|b" in doc.render()
    # Markdown's escaping is a Markdown concern; JSON carries the real value.
    assert doc.blocks[0].as_json()["rows"] == [{"Item": "a|b"}]


# --- Block.as_json ----------------------------------------------------------


def test_heading_as_json():
    assert Heading("Goal", 2).as_json() == {
        "kind": "heading", "level": 2, "text": "Goal"
    }


def test_paragraph_as_json():
    assert Paragraph("prose").as_json() == {"kind": "paragraph", "text": "prose"}


def test_bullets_as_json():
    assert Bullets(["a", "b"]).as_json() == {"kind": "list", "items": ["a", "b"]}


def test_table_as_json_keys_rows_by_column():
    table = Table(["Item", "Qty"], [["Corn", "5"], ["Wood", "2"]])
    assert table.as_json() == {
        "kind": "table",
        "columns": ["Item", "Qty"],
        "rows": [{"Item": "Corn", "Qty": "5"}, {"Item": "Wood", "Qty": "2"}],
    }


def test_graph_as_json_carries_points_not_svg():
    series = GraphSeries(xs=[0.0, 1.0], ys=[10.0, 5.0], x_label="Time", y_label="Gold")
    payload = Graph(series, PLANS).as_json()
    assert payload == {
        "kind": "graph",
        "x_label": "Time",
        "y_label": "Gold",
        "points": [{"x": 0.0, "y": 10.0}, {"x": 1.0, "y": 5.0}],
    }
    # ... while the Markdown rendering of the very same block stays the SVG.
    assert "<svg" in Graph(series, PLANS).markdown()


# --- JsonReporter -----------------------------------------------------------


def test_build_reporter_picks_the_json_reporter():
    assert isinstance(build_reporter("json"), JsonReporter)


def _sections(reporter: JsonReporter) -> list[dict]:
    return json.loads(reporter.render())["sections"]


def test_sections_are_named_and_ordered_as_emitted():
    reporter = JsonReporter()
    reporter.note("first")
    reporter.frontier(PLANS, selected=0, cheapest=1)
    reporter.note("second")
    sections = _sections(reporter)
    assert [s["name"] for s in sections] == ["notes", "frontier", "notes"]
    # A list, not an object, precisely so a name can repeat like this.
    assert sections[0]["blocks"] == [{"kind": "paragraph", "text": "first"}]
    assert sections[2]["blocks"] == [{"kind": "paragraph", "text": "second"}]


def test_empty_sections_are_dropped():
    # A goal-less solve prints nothing at all in text and Markdown (no needs,
    # no notes, no remainder), so the JSON carries no section for it either --
    # the document says what the other formats print.
    reporter = JsonReporter()
    reporter.solution(farm_solver.Solution())
    assert _sections(reporter) == []
    reporter.unobtainable(["Briomine"])
    assert [s["name"] for s in _sections(reporter)] == ["unobtainable"]


def test_frontier_section_carries_the_table_rows():
    reporter = JsonReporter()
    reporter.frontier(PLANS, selected=0, cheapest=1)
    (section,) = _sections(reporter)
    heading, table = section["blocks"]
    assert heading == {"kind": "heading", "level": 2, "text": "Sourcing plans"}
    assert table["columns"][:4] == ["#", "Time", "AP", "LN"]
    assert [row["#"] for row in table["rows"]] == [1, 2]
    # Durations come through as whole seconds, gold without its "g".
    assert [row["Time"] for row in table["rows"]] == [86400, 345600]
    assert [row["Notes"] for row in table["rows"]] == [
        "[fastest, selected]", "[cheapest]"
    ]


def test_frontier_graph_lands_as_a_graph_block():
    reporter = JsonReporter()
    reporter.frontier(
        PLANS, selected=0, cheapest=1, spread_axis="time", graph_metric="gold"
    )
    (section,) = _sections(reporter)
    graph = section["blocks"][-1]
    assert graph["kind"] == "graph"
    assert graph["points"]
    # No SVG anywhere: the whole point of the graph block is that each format
    # takes the shape it wants from the same series.
    assert "<svg" not in reporter.render()


def test_finish_prints_the_document(capsys):
    reporter = JsonReporter()
    reporter.note("hello")
    reporter.finish()
    assert json.loads(capsys.readouterr().out) == json.loads(reporter.render())


def test_unicode_is_not_escaped():
    # Item names have to stay readable rather than turning into \\uXXXX.
    solution = farm_solver.Solution(
        needs=[farm_solver.ItemNeed("Crème Brûlée", have=0, target=5)]
    )
    reporter = JsonReporter()
    reporter.solution(solution)
    assert "Crème Brûlée" in reporter.render()


def test_shortfall_is_the_number_behind_done_and_the_unreachable_mark():
    # The Goal table's "Still needed" reads "done" or "4,616 ✗" for a human;
    # JSON carries the shortfall itself. Nothing is lost -- the unreachable
    # items are named in the "no known source" table right below.
    solution = farm_solver.Solution(
        needs=[
            farm_solver.ItemNeed("Yule Goat", have=17991, target=20000),
            farm_solver.ItemNeed("Wood", have=100, target=100),
        ]
    )
    md = MarkdownReporter()
    js = JsonReporter()
    for reporter in (md, js):
        reporter.solution(solution, {"Yule Goat": 1826})
    assert "| 2,009 ✗ |" in md.doc.render()
    assert "| done |" in md.doc.render()
    goal, remainder = (b for b in _sections(js)[0]["blocks"] if b["kind"] == "table")
    assert [r["Still needed"] for r in goal["rows"]] == [2009, 0]
    assert [r["Have"] for r in goal["rows"]] == [17991, 100]
    assert remainder["rows"] == [{"Material": "Yule Goat", "Qty": 1826}]


# --- Parity: JSON collects exactly what Markdown renders --------------------


def test_json_blocks_match_the_markdown_reporters():
    """Both reporters must see the identical block stream for identical calls.

    This is the invariant the whole design rests on -- ``JsonReporter`` shares
    every *decision* with ``MarkdownReporter`` because it calls the same
    functions; only the serialization differs.
    """
    solution = farm_solver.Solution(
        needs=[farm_solver.ItemNeed("Corn", have=384, target=5000)]
    )
    calls = [
        ("solution", (solution,), {}),
        ("note", ("a note",), {}),
        ("frontier", (PLANS, 0, 1), {"spread_axis": "time", "graph_metric": "gold"}),
    ]
    md = MarkdownReporter()
    js = JsonReporter()
    for name, args, kwargs in calls:
        getattr(md, name)(*args, **kwargs)
        getattr(js, name)(*args, **kwargs)

    flattened = [block for _, doc in js.sections for block in doc.blocks]
    assert flattened == md.doc.blocks


# --- CLI wiring -------------------------------------------------------------


def test_parse_format_accepts_json():
    assert main.parse_format("json") == "json"
    assert main.parse_format(" JSON ") == "json"


def test_parse_format_rejects_an_unknown_format():
    try:
        main.parse_format("yaml")
    except argparse.ArgumentTypeError as exc:
        assert "'json'" in str(exc)
    else:
        raise AssertionError("expected an ArgumentTypeError")


def test_format_json_parses_off_the_command_line():
    parser = main.build_parser()
    assert parser.parse_args([]).format == "text"
    assert parser.parse_args(["--format", "json"]).format == "json"


# --- Plan sections: the printed text and the JSON value must agree ----------
#
# Real solves reach Fishing/Exploring/Opening/Buy/Surplus, and a same-timeframe
# stash-diff of those confirmed the Markdown byte-for-byte. Farming, Passive
# production, Pumpkin Juice, Trade and Required Crafts need a hand-built plan
# to reach at all, so they are pinned here instead: each case asserts the
# human text AND the machine value off the very same call.

_WORKSHOP = Workshop()
_FARM = Farm(crops={"corn": Crop("Corn", timedelta(hours=1), {"corn": 5})}, rows=1)


def _plan_docs(plan, demand, **kwargs) -> tuple[str, list[dict]]:
    """Render one plan through both reporters: Markdown source, JSON sections."""
    md = MarkdownReporter()
    js = JsonReporter()
    for reporter in (md, js):
        reporter.plan(plan, demand, _FARM, _WORKSHOP, 1.0, **kwargs)
    return md.doc.render(), _sections(js)


def _only_table(sections: list[dict], columns: list[str]) -> dict:
    tables = [
        b
        for s in sections
        for b in s["blocks"]
        if b["kind"] == "table" and b["columns"] == columns
    ]
    assert len(tables) == 1, f"expected one {columns} table, got {len(tables)}"
    return tables[0]


def test_farming_section_duration_in_seconds_and_bare_quantity():
    corn = Offer("grow Corn", "farming", {"corn": 5.0}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(corn, 4)], time=timedelta(hours=4))
    text, sections = _plan_docs(plan, {"corn": 20})
    assert "| 4 Harvest Alls of Corn | 4h | corn | 20 |" in text
    (row,) = _only_table(sections, ["Action", "Duration", "Item", "Qty", "Detail"])["rows"]
    assert row["Duration"] == 4 * 3600
    assert row["Qty"] == 20


def test_passive_section_wait_in_seconds():
    stone = Offer("passive", "passive", {"stone": 10.0}, time_per_unit=timedelta(minutes=1))
    plan = SourcingPlan(draws=[Draw(stone, 10)], time=timedelta(minutes=10))
    text, sections = _plan_docs(plan, {"stone": 100})
    assert "| stone | 100 | 10m |" in text
    (row,) = _only_table(sections, ["Item", "Qty", "Time", "Source"])["rows"]
    assert row["Qty"] == 100
    assert row["Time"] == 600


def test_pumpkin_juice_section_drops_the_gold_suffix_and_lists_the_levels():
    plan = SourcingPlan(pumpkin_juices={"butter": 3.0}, pumpkin_gold=690.0, gold=690.0)
    text, sections = _plan_docs(
        plan, {}, prices={"butter": 10.0},
        pumpkin_schedule={"butter": [18000, 19000, 20000]},
    )
    assert "| butter | 3 | 690.0 g | 18,000 / 19,000 / 20,000 |" in text
    (row,) = _only_table(sections, ["Goal", "Juices", "Gold", "Drink at mastery"])["rows"]
    assert row["Juices"] == 3
    assert row["Gold"] == 690
    # A schedule of mastery levels, not one pre-joined string.
    assert row["Drink at mastery"] == [18000, 19000, 20000]


def test_trade_section_drops_the_ap_and_gold_suffixes():
    plan = SourcingPlan(bought={"magicite": 5.0})
    text, sections = _plan_docs(
        plan, {}, prices={"magicite": 100.0}, ap_prices={"magicite": 2.0}
    )
    assert "| 5 | 10 AP | 500.0 g |" in text
    (row,) = _only_table(
        sections, ["Item", "Qty", "Arnold Palmers", "Gold value"]
    )["rows"]
    assert row["Arnold Palmers"] == 10
    assert row["Gold value"] == 500


def test_buy_section_drops_the_gold_suffix():
    plan = SourcingPlan(bought={"arnold palmer": 12.0}, gold=1200.0)
    text, sections = _plan_docs(plan, {}, prices={"arnold palmer": 100.0})
    assert "| 12 | 1,200.0 g |" in text
    (row,) = _only_table(sections, ["Item", "Qty", "Gold"])["rows"]
    assert row["Qty"] == 12
    assert row["Gold"] == 1200


def test_required_crafts_quantities_are_bare_numbers():
    workshop = Workshop(
        recipes={"board": WorkshopRecipe("Board", materials={"Wood": 2000})}
    )
    plan = SourcingPlan(crafts={"board": 3.0})
    md, js = MarkdownReporter(), JsonReporter()
    for reporter in (md, js):
        reporter.crafts(plan, workshop, 1.0, 1.1)
    assert "| Wood | 6,000 |" in md.doc.render()
    assert _only_table(_sections(js), ["Material", "Qty"])["rows"] == [
        {"Material": "Wood", "Qty": 6000}
    ]


def test_a_cell_keeps_its_text_for_humans_and_its_value_for_json():
    doc = MarkdownDoc()
    doc.table(["Time", "Gold", "Prose"], [[Cell.duration(timedelta(days=1)),
                                           Cell.num(1234, "g"), "just words"]])
    assert "| 1d | 1,234 g | just words |" in doc.render()
    assert doc.blocks[0].as_json()["rows"] == [
        {"Time": 86400, "Gold": 1234, "Prose": "just words"}
    ]
