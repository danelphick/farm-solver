from datetime import timedelta

from main import print_passive
from passive import PassiveUse


def test_passive_total_omits_concurrent_for_a_single_item(capsys):
    print_passive([PassiveUse(item="Corn Oil", quantity=720, time=timedelta(days=30))])
    out = capsys.readouterr().out
    assert "Total time: 30d" in out
    assert "(concurrent)" not in out


def test_passive_total_notes_concurrent_for_several_items(capsys):
    print_passive(
        [
            PassiveUse(item="Corn Oil", quantity=720, time=timedelta(days=30)),
            PassiveUse(item="Steel", quantity=400, time=timedelta(hours=2)),
        ]
    )
    out = capsys.readouterr().out
    # The total is the longest wait, marked concurrent since the waits overlap.
    assert "Total time: 30d (concurrent)" in out
