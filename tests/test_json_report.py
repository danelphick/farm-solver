"""Tests for ``--format json`` / ``--json``: report.py's :class:`JsonReporter`
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
from optimizer import GraphSeries, SourcingPlan
from report import (
    Bullets,
    Graph,
    Heading,
    JsonReporter,
    MarkdownDoc,
    MarkdownReporter,
    Paragraph,
    Table,
    build_reporter,
)

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
    assert [row["#"] for row in table["rows"]] == ["1", "2"]
    assert "fastest" in table["rows"][0]["Notes"]
    assert "cheapest" in table["rows"][1]["Notes"]


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
    # The unreachable-goal mark and item names have to stay readable rather
    # than turning into \\uXXXX.
    solution = farm_solver.Solution(
        needs=[farm_solver.ItemNeed("Briomine", have=0, target=5)]
    )
    reporter = JsonReporter()
    reporter.solution(solution, {"briomine": 5})
    assert "✗" in reporter.render()


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


def test_json_flag_is_shorthand_for_the_format():
    parser = main.build_parser()
    assert parser.parse_args([]).format == "text"
    assert parser.parse_args(["--json"]).format == "json"
    # One setting under two spellings: whichever comes last on the command
    # line wins, rather than --json's absence clobbering an explicit --format.
    assert parser.parse_args(["--json", "--format", "md"]).format == "markdown"
    assert parser.parse_args(["--format", "md", "--json"]).format == "json"
