from pkg.calc import discount, is_even, clamp, average


def test_discount():
    assert discount(100, 10) == 90
    assert discount(50, 50) == 25


def test_is_even():
    assert is_even(4) is True
    assert is_even(7) is False


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-3, 0, 10) == 0
    assert clamp(99, 0, 10) == 10


def test_average():
    assert average([2, 4, 6]) == 4
    assert average([10]) == 10
