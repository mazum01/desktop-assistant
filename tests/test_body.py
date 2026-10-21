"""Machine-local body selection and isolation of saved servo geometry."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src.core.body import load_body_config
from src.motion.head_tracker import HeadTracker, HeadTrackerConfig
from src.motion.servo_controller import ServoConfig, ServoController
from src.services.motion_service import MotionService
from src.services.tracking_service import TrackingService


CONFIG_DIR = Path(__file__).parents[1] / "config"


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "body_profiles.yaml").write_text(
        (CONFIG_DIR / "body_profiles.yaml").read_text()
    )
    return tmp_path


def select_body(config_dir, name, runtime=None):
    (config_dir / "body.yaml").write_text(yaml.safe_dump({"body": name}))
    return load_body_config(
        yaml.safe_load((CONFIG_DIR / "assistant.yaml").read_text()),
        runtime or {}, config_dir,
    )


@pytest.mark.parametrize("body,channel,angle_range,limits,tilt,controller,invert", [
    ("original", 15, 360, (135, 215), False, "legacy", False),
    ("new", 1, 180, (70, 110), True, "visual_servo", True),
])
def test_body_selects_matching_hardware_and_controller(
    config_dir, body, channel, angle_range, limits, tilt, controller, invert,
):
    selected = select_body(config_dir, body)
    servo, tracking = selected.config["servo"], selected.config["head_tracking"]
    assert servo["pan_channel"] == channel
    assert servo["range_deg"] == angle_range
    assert (servo["soft_min_deg"], servo["soft_max_deg"]) == limits
    assert servo["tilt_enabled"] is tilt
    assert tracking["controller"] == controller
    assert tracking["invert_pan"] is invert
    config = ServoConfig(**{
        key: servo[key] for key in (
            "range_deg", "soft_min_deg", "soft_max_deg", "pulse_min_us", "pulse_max_us",
        )
    }, channel=channel)
    assert 500 <= config.pulse_min_us < config.pulse_max_us <= 2500


@pytest.mark.parametrize("saved_body", [None, "new"])
def test_original_discards_new_or_unidentified_runtime_geometry(config_dir, saved_body):
    runtime = {
        "body": saved_body,
        "servo": {
            "enabled": False, "pan_channel": 1, "tilt_channel": 0,
            "soft_min_deg": 70, "soft_max_deg": 110,
        },
        "head_tracking": {"random_motion_enabled": False},
        "camera": {"rotation_deg": 90},
    }
    selected = select_body(config_dir, "original", runtime)
    assert selected.runtime["body"] == "original"
    assert selected.runtime["servo"] == {"enabled": False}
    assert selected.runtime["head_tracking"] == runtime["head_tracking"]
    assert selected.runtime["camera"] == runtime["camera"]
    assert runtime["servo"]["pan_channel"] == 1


def test_same_body_preserves_runtime_limits_and_channels(config_dir):
    state = {
        "body": "original", "body_revision": 2,
        "servo": {"soft_min_deg": 140, "pan_channel": 2},
    }
    selected = select_body(config_dir, "original", state)
    assert selected.runtime == state


def test_original_discards_geometry_saved_before_channel_15_correction(config_dir):
    selected = select_body(config_dir, "original", {
        "body": "original",
        "servo": {"enabled": True, "pan_channel": 0, "tilt_channel": 1, "soft_min_deg": 140},
    })
    assert selected.runtime["servo"] == {"enabled": True}
    assert selected.runtime["body_revision"] == selected.revision == 2
    assert selected.config["servo"]["pan_channel"] == 15


def test_new_body_preserves_untagged_runtime_for_backward_compatibility(config_dir):
    state = {"servo": {"pan_channel": 2, "soft_min_deg": 75, "soft_max_deg": 105}}
    selected = select_body(config_dir, "new", state)
    assert selected.runtime["servo"] == state["servo"]
    assert selected.runtime["body"] == "new"


def test_new_body_preserves_this_devices_calibrated_tilt_and_other_hardware(config_dir):
    runtime = {
        "servo": {
            "pan_channel": 1, "tilt_channel": 0,
            "soft_min_deg": 70, "soft_max_deg": 110,
            "tilt_min_deg": 50, "tilt_max_deg": 90,
        },
        "camera": {"rotation_deg": 90},
    }
    config = {
        "camera": {"width": 640}, "camera2": {"enabled": False},
        "display": {"ble_address": "device-local"},
        "depth": {"mono_enabled": False},
    }
    (config_dir / "body.yaml").write_text("body: new\n")
    selected = load_body_config(config, runtime, config_dir)
    assert selected.runtime["servo"] == runtime["servo"]
    assert selected.runtime["camera"] == runtime["camera"]
    for section in ("camera", "camera2", "display", "depth"):
        assert selected.config[section] == config[section]


def test_switching_to_new_body_discards_original_geometry(config_dir):
    selected = select_body(config_dir, "new", {
        "body": "original", "servo": {"pan_channel": 0, "soft_max_deg": 215},
    })
    assert selected.runtime["servo"] == {}
    assert selected.config["servo"]["pan_channel"] == 1


def test_missing_identity_keeps_new_body_behavior_with_explicit_warning(config_dir, caplog):
    selected = load_body_config({}, {}, config_dir)
    assert selected.name == "new"
    assert selected.config["head_tracking"]["controller"] == "visual_servo"
    assert selected.config["servo"]["pan_channel"] == 1
    assert "using new body for backward compatibility" in caplog.text


def test_new_body_tuning_save_creates_local_identity_when_absent(config_dir):
    selected = load_body_config({}, {}, config_dir)
    service = TrackingService(
        bus=MagicMock(), body=selected.name, params_path=selected.identity_path,
    )
    service._tracker = HeadTracker(config=HeadTrackerConfig(tracking_gain=0.3))
    service._on_save_params(None, {})
    assert yaml.safe_load(selected.identity_path.read_text())["body"] == "new"
    assert load_body_config({}, {}, config_dir).config["head_tracking"]["tracking_gain"] == 0.3


@pytest.mark.parametrize("identity", ["body: unknown", "[]", "body: null"])
def test_invalid_identity_is_explicit_error(config_dir, identity):
    (config_dir / "body.yaml").write_text(identity)
    with pytest.raises(ValueError, match="Invalid body identity"):
        load_body_config({}, {}, config_dir)


@pytest.mark.parametrize("tuning", [
    {"controller": "visual_servo"}, {"tracking_gain": float("nan")},
    {"dead_zone_frac": True}, {"tracking_gain": 5}, [],
])
def test_local_tuning_cannot_override_body_or_accept_invalid_values(config_dir, tuning):
    (config_dir / "body.yaml").write_text(yaml.safe_dump({
        "body": "original", "head_tracking": tuning,
    }))
    with pytest.raises(ValueError, match="Invalid body-local tracking"):
        load_body_config({}, {}, config_dir)


def test_saved_tuning_survives_reload_without_changing_shared_config(config_dir):
    selected = select_body(config_dir, "original")
    bus = MagicMock()
    service = TrackingService(
        bus=bus, controller="legacy", body="original", params_path=selected.identity_path,
    )
    service._tracker = HeadTracker(config=HeadTrackerConfig(dead_zone_frac=0.08))
    service._on_save_params(None, {})
    reloaded = load_body_config({}, {}, config_dir)
    assert reloaded.config["head_tracking"]["dead_zone_frac"] == 0.08
    assert reloaded.config["head_tracking"]["controller"] == "legacy"
    assert yaml.safe_load(selected.identity_path.read_text())["body"] == "original"
    assert not (config_dir / "assistant.yaml").exists()


def test_tuning_save_refuses_to_overwrite_changed_body_identity(config_dir, caplog):
    selected = select_body(config_dir, "original")
    service = TrackingService(
        bus=MagicMock(), controller="legacy", body="original", params_path=selected.identity_path,
    )
    selected.identity_path.write_text("body: new\n")
    service._on_save_params(None, {})
    assert selected.identity_path.read_text() == "body: new\n"
    assert "Body identity changed" in caplog.text


def test_original_motion_writes_only_legacy_pan_channel(config_dir):
    selected = select_body(config_dir, "original", {
        "body": "new", "servo": {"pan_channel": 1, "soft_min_deg": 70, "soft_max_deg": 110},
    })
    config = selected.config["servo"]
    kit = MagicMock()
    controller = ServoController(ServoConfig(
        channel=config["pan_channel"], range_deg=config["range_deg"],
        soft_min_deg=config["soft_min_deg"], soft_max_deg=config["soft_max_deg"],
        pulse_min_us=config["pulse_min_us"], pulse_max_us=config["pulse_max_us"],
    ), kit=kit)
    motion = MotionService(
        controller=controller, bus=MagicMock(),
        soft_min_deg=config["soft_min_deg"], soft_max_deg=config["soft_max_deg"],
        pan_channel=config["pan_channel"], range_deg=config["range_deg"],
        tilt_enabled=config["tilt_enabled"],
    )
    motion.on_start()
    try:
        assert motion.get_status()["pan_channel"] == 15
        assert motion.get_status()["range_deg"] == 360
        assert motion.get_status()["tilt_available"] is False
        assert motion._tilt_controller is None
        assert {args.args[0] for args in kit.servo.__getitem__.call_args_list} == {15}
        motion.set_channels(14, 1)
        assert motion.get_status()["pan_channel"] == 14
        assert kit.servo.__getitem__.call_args_list[-1].args[0] == 14
    finally:
        motion.on_stop()
