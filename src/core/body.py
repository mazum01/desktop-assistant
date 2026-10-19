"""Resolve machine-local body identity before any motion hardware is opened."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.motion.head_tracker import TUNABLE_FIELDS

log = logging.getLogger(__name__)


@dataclass
class BodyConfiguration:
    name: str
    config: dict
    runtime: dict
    identity_path: Path
    revision: int = 1


def load_body_config(config: dict, runtime: dict, config_dir: Path) -> BodyConfiguration:
    identity_path = config_dir / "body.yaml"
    if identity_path.exists():
        identity = yaml.safe_load(identity_path.read_text())
    else:
        log.warning(
            "No body identity at %s; using new body for backward compatibility. "
            "Original hardware requires 'body: original' in this file.",
            identity_path,
        )
        identity = {"body": "new"}
    if not isinstance(identity, dict) or identity.get("body") not in ("original", "new"):
        raise ValueError(f"Invalid body identity in {identity_path}; expected original or new")
    name = identity["body"]
    profiles = yaml.safe_load((config_dir / "body_profiles.yaml").read_text())
    profile = profiles[name]
    # Bump a profile's revision when its wiring/geometry is corrected so stale
    # saved servo settings from the previous revision are not reapplied.
    revision = profile.get("revision", 1)
    resolved = deepcopy(config)
    for section in ("servo", "head_tracking"):
        resolved.setdefault(section, {}).update(profile[section])

    tuning = identity.get("head_tracking", {})
    if not isinstance(tuning, dict) or tuning.keys() - TUNABLE_FIELDS.keys():
        raise ValueError(f"Invalid body-local tracking parameters in {identity_path}")
    for key, value in tuning.items():
        lo, hi = TUNABLE_FIELDS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ValueError(f"Invalid body-local tracking parameter {key}={value!r}")
    resolved["head_tracking"].update(tuning)

    state = deepcopy(runtime)
    saved_body = state.get("body")
    saved_revision = state.get("body_revision", 1)
    if saved_body is None and name == "new" and saved_revision == revision:
        log.warning("Adopting untagged new-body servo settings for backward compatibility")
    elif saved_body != name or saved_revision != revision:
        log.warning(
            "Body %s revision %s selected; discarding servo geometry saved for body %r revision %s",
            name, revision, saved_body, saved_revision,
        )
        # Preserve the user's enable switch, but never reuse another body's wiring/limits.
        state["servo"] = {
            key: value for key, value in state.get("servo", {}).items() if key == "enabled"
        }
    state["body"] = name
    state["body_revision"] = revision
    return BodyConfiguration(name, resolved, state, identity_path, revision)
