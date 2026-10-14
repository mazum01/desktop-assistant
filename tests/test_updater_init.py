"""Updater package import smoke test."""


def test_import():
    import src.updater
    assert src.updater.__doc__
