"""Repo-root conftest — ensures src/ is importable when pytest is invoked
from any working directory, and when test files are run directly."""
import sys
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_web_configuration(tmp_path, monkeypatch):
    from src.services import web_service

    config_path = tmp_path / "web-config" / "assistant.yaml"
    config_path.parent.mkdir()
    shutil.copyfile(ROOT / "config" / "assistant.yaml", config_path)
    monkeypatch.setattr(web_service, "_ASSISTANT_CONFIG_PATH", config_path)
