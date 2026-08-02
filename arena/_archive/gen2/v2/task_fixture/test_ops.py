from ops import add_prices, apply_discount


def test_add_prices():
    assert add_prices(10, 5) == 15


def test_apply_discount():
    assert apply_discount(100, 20) == 80
