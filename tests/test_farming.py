from farming import is_farmed


def test_is_farmed_recognises_the_flowers():
    assert is_farmed("Blue Flowers")
    assert is_farmed("red flowers")


def test_is_farmed_excludes_non_farm_items():
    assert not is_farmed("Wood")
    assert not is_farmed("Peach")
