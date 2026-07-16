from fs_diloco.protocol.fragment_scheduler import expected_fragment_versions_after_events, select_fragment


def test_round_robin_fragment_schedule():
    assert [select_fragment(index, 4) for index in range(6)] == [0, 1, 2, 3, 0, 1]


def test_expected_fragment_versions_for_50_events():
    assert expected_fragment_versions_after_events(4, 50) == {0: 13, 1: 13, 2: 12, 3: 12}
