from inventory import apply_restock, is_in_stock


def test_restock_increases_stock():
    assert apply_restock(10, 5) == 15


def test_in_stock_reports_availability():
    assert is_in_stock(1) is True
    assert is_in_stock(0) is False
