import textwrap

from farm_solver import parse_tower_requirements

# level, item, mastery quantity. Levels below 201 carry no mastery requirement.
SAMPLE = textwrap.dedent(
    """\
    level,item,quantity
    150,,
    201,Flying Machine,50000
    201,Gainite,10000
    205,Flying Machine,100000
    210,Board,1,000,000
    """
)


def test_header_is_skipped():
    reqs = parse_tower_requirements(SAMPLE)
    assert "item" not in reqs.levels.get(0, {})
    assert set(reqs.levels) == {201, 205, 210}


def test_optional_header_absent():
    text = "201,Gainite,10000\n"
    reqs = parse_tower_requirements(text)
    assert reqs.levels == {201: {"Gainite": 10000}}


def test_multiple_items_per_level():
    reqs = parse_tower_requirements(SAMPLE)
    assert reqs.levels[201] == {"Flying Machine": 50000, "Gainite": 10000}


def test_rows_without_requirement_skipped():
    reqs = parse_tower_requirements(SAMPLE)
    assert 150 not in reqs.levels


def test_quantity_strips_comma_grouping():
    reqs = parse_tower_requirements(SAMPLE)
    assert reqs.levels[210] == {"Board": 1_000_000}


def test_max_level():
    assert parse_tower_requirements(SAMPLE).max_level == 210


def test_requirements_through_lists_each_level_in_order():
    reqs = parse_tower_requirements(SAMPLE)
    # Flying Machine appears at both 201 (50k) and 205 (100k); each is kept as a
    # separate requirement, in tower order (by level, then item).
    assert reqs.requirements_through(205) == [
        (201, "Flying Machine", 50000),
        (201, "Gainite", 10000),
        (205, "Flying Machine", 100000),
    ]


def test_requirements_through_excludes_higher_levels():
    reqs = parse_tower_requirements(SAMPLE)
    assert reqs.requirements_through(201) == [
        (201, "Flying Machine", 50000),
        (201, "Gainite", 10000),
    ]


def test_requirements_below_201_is_empty():
    reqs = parse_tower_requirements(SAMPLE)
    assert reqs.requirements_through(200) == []


def test_empty_input():
    assert parse_tower_requirements("").levels == {}
