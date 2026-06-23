from pipeline import normalize


def test_normalize_keeps_and_trims_every_row():
    assert normalize([" a ", "b ", " c"]) == ["a", "b", "c"]
