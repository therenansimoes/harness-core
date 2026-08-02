from buggy_calc import clamp, is_positive, average


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-5, 0, 10) == 0
    assert clamp(15, 0, 10) == 10


def test_is_positive():
    assert is_positive(1) is True
    assert is_positive(0) is False
    assert is_positive(-1) is False


def test_average():
    assert average([2, 4, 6]) == 4
    assert average([10]) == 10
