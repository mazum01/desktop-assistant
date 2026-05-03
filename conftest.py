"""Repo-root conftest — ensures src/ is importable when pytest is invoked
from any working directory, and when test files are run directly."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
