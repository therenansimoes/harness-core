from stats import mean, clamp


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5
    assert mean([10, 20]) == 15


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
