"""Tests for hardware-specific HEF selection and atomic installation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from src.perception import hailo_models as models


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    data = {
        "device": "auto",
        "profiles": {
            family: {
                kind: {"file": f"{kind}.hef", "url": "https://example.com/model.hef",
                       "candidates": []}
                for kind in ("face_detection", "face_embedding", "mono_depth")
            }
            for family in ("Hailo-8", "Hailo-10H")
        },
    }
    config = tmp_path / "models.yaml"
    config.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(models, "_CONFIG", config)
    monkeypatch.setattr(models, "_ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize("family", ["Hailo-8", "Hailo-10H"])
@pytest.mark.parametrize("kind", ["face_detection", "face_embedding", "mono_depth"])
def test_device_matched_selection(profiles, monkeypatch, family, kind):
    monkeypatch.setattr(models, "probe_hailo", lambda: SimpleNamespace(family=family))
    for name in ("hailo8", "hailo10h"):
        path = profiles / "config" / "hailo" / name / f"{kind}.hef"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert family.lower().replace("-", "") in str(models.resolve_model(kind))


def test_missing_model_never_selects_other_family(profiles, monkeypatch):
    monkeypatch.setattr(models, "probe_hailo", lambda: SimpleNamespace(family="Hailo-10H"))
    path = profiles / "config/hailo/hailo8/face_detection.hef"
    path.parent.mkdir(parents=True)
    path.touch()
    assert models.resolve_model("face_detection") is None


def test_explicit_missing_path_does_not_fall_back(profiles):
    assert models.resolve_model("mono_depth", profiles / "missing.hef") is None


def test_explicit_path(profiles):
    path = profiles / "custom.hef"
    path.touch()
    assert models.resolve_model("face_detection", path) == path


def test_device_override(profiles):
    config = models._CONFIG
    data = yaml.safe_load(config.read_text())
    data["device"] = "Hailo-8"
    config.write_text(yaml.safe_dump(data))
    assert models._profile()[0] == "Hailo-8"


def test_invalid_device_is_explicit_error(profiles):
    data = yaml.safe_load(models._CONFIG.read_text())
    data["device"] = "typo"
    models._CONFIG.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Unknown Hailo"):
        models.resolve_model("mono_depth")


def test_install_validation_failure_leaves_no_model(profiles, monkeypatch):
    import io
    import subprocess

    monkeypatch.setattr(models, "probe_hailo", lambda: SimpleNamespace(family="Hailo-10H"))
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"bad"))
    validate = MagicMock(side_effect=subprocess.CalledProcessError(1, "parse-hef"))
    monkeypatch.setattr(models.subprocess, "run", validate)
    with pytest.raises(RuntimeError, match="Invalid face_detection"):
        models.install_models()
    assert not list(profiles.rglob("*.hef"))
