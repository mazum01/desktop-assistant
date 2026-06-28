from src.voice.dialog_manager import DialogManager, DialogManagerConfig
from src.voice.intent_router import IntentDecision


def test_dialog_manager_tracks_turns_and_last_intent():
    dm = DialogManager(DialogManagerConfig(session_timeout_s=30))
    dm.observe(IntentDecision("skill_dispatch", "av_utterance", 0.7, "weather"))
    snap = dm.snapshot()
    assert snap["turns"] == 1
    assert snap["last_intent"] == "skill_dispatch"


def test_dialog_manager_resolves_confirmation_yes_no():
    dm = DialogManager()
    dm.set_pending_confirmation("reboot_request", {"utterance": "reboot"})

    handled_yes, payload_yes = dm.resolve_confirmation(
        IntentDecision("confirm", "dialog", 1.0, "yes")
    )
    assert handled_yes is True
    assert payload_yes is not None
    assert payload_yes["action"] == "reboot_request"

    dm.set_pending_confirmation("shutdown_request", {"utterance": "shutdown"})
    handled_no, payload_no = dm.resolve_confirmation(
        IntentDecision("deny", "dialog", 1.0, "no")
    )
    assert handled_no is True
    assert payload_no is None


def test_dialog_manager_ignores_confirm_without_pending():
    dm = DialogManager()
    handled, payload = dm.resolve_confirmation(
        IntentDecision("confirm", "dialog", 1.0, "yes")
    )
    assert handled is False
    assert payload is None
