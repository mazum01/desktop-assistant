from src.voice.intent_router import IntentRouter


def test_intent_router_classifies_version_query():
    router = IntentRouter()
    decision = router.classify("what version are you")
    assert decision.name == "version_query"
    assert decision.route == "av_utterance"


def test_intent_router_classifies_confirmation_tokens():
    router = IntentRouter()
    yes = router.classify("yes")
    no = router.classify("cancel")
    assert yes.name == "confirm"
    assert yes.route == "dialog"
    assert no.name == "deny"
    assert no.route == "dialog"


def test_intent_router_classifies_power_requests_as_confirmable():
    router = IntentRouter()
    reboot = router.classify("please reboot now")
    shutdown = router.classify("shut down")
    assert reboot.name == "reboot_request"
    assert reboot.route == "dialog_confirm"
    assert shutdown.name == "shutdown_request"
    assert shutdown.route == "dialog_confirm"


def test_intent_router_defaults_to_skill_dispatch():
    router = IntentRouter()
    decision = router.classify("tell me a joke")
    assert decision.name == "skill_dispatch"
    assert decision.route == "av_utterance"
