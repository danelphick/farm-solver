from farming import is_farmed, split_farmed


def test_is_farmed_is_case_insensitive():
    assert is_farmed("Carrot")
    assert is_farmed("carrot")
    assert is_farmed("Blue Flowers")


def test_peach_is_not_farmed():
    assert not is_farmed("Peach")


def test_non_farm_items_are_not_farmed():
    assert not is_farmed("Wood")
    assert not is_farmed("Iron")


def test_split_farmed_partitions_by_name():
    materials = {"Wood": 100, "Carrot": 5, "Red Flowers": 2, "Board": 7}
    rest, farmed = split_farmed(materials)
    assert rest == {"Wood": 100, "Board": 7}
    assert farmed == {"Carrot": 5, "Red Flowers": 2}


def test_split_farmed_with_no_farm_items():
    rest, farmed = split_farmed({"Wood": 100})
    assert rest == {"Wood": 100}
    assert farmed == {}
