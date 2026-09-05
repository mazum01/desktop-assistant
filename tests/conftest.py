"""Shared test fixtures and safety guards.

Autouse guard: several service-level tests exercise the real EQ code paths
(``av.set_custom_eq`` / ``av.set_eq_preset``). Those tests redirect AVService's
own state files to ``tmp_path`` but not the PipeWire filter-chain config, so
they used to overwrite the developer's live
``~/.config/pipewire/filter-chain.conf.d/da-eq.conf`` and restart
``filter-chain.service`` mid-run — mutating real system audio and churning the
PipeWire sink ID.

Redirect the config path at the module level and neuter the systemctl restart
for every test, so the suite can never touch real audio state.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_pipewire_eq(tmp_path_factory, monkeypatch):
    try:
        from src.audio import pipewire_eq
    except Exception:
        yield
        return

    conf_dir = tmp_path_factory.mktemp("pw_eq")
    monkeypatch.setattr(pipewire_eq, "_CONF_DIR", conf_dir, raising=False)
    monkeypatch.setattr(
        pipewire_eq, "_CONF_FILE", conf_dir / "da-eq.conf", raising=False
    )
    monkeypatch.setattr(
        pipewire_eq, "_restart_filter_chain", lambda *a, **k: True, raising=False
    )
    yield
