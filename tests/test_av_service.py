"""Tests for AVService EQ restore behaviour."""
from unittest.mock import patch



# ── Startup EQ reconciliation regressions ───────────────────────────────────
#
# ensure_default() only re-elects the existing sink; it never verified that the
# running da-eq.conf matched the user's saved custom_eq.json. Drift therefore
# persisted silently and the EQ appeared dead until a manual re-save.

def test_restore_pipewire_eq_reapplies_when_config_drifts():
    from src.services.av_service import AVService
    from src.audio import pipewire_eq

    svc = AVService.__new__(AVService)
    svc._audio = None
    bands = [{"hz": 80.0, "gain_db": 10.0, "q": 1.0}]

    with patch.object(pipewire_eq, "ensure_default"), \
         patch.object(pipewire_eq, "config_matches", return_value=False) as cm, \
         patch.object(pipewire_eq, "apply_custom_bands") as apply_, \
         patch.object(pipewire_eq, "is_active", return_value=False), \
         patch.object(AVService, "_persisted_custom_eq_bands", return_value=bands):
        svc._restore_pipewire_eq()

    cm.assert_called_once_with(bands)
    apply_.assert_called_once_with(bands)


def test_restore_pipewire_eq_no_restart_when_config_matches():
    """Matching config must NOT restart filter-chain (it churns the sink ID)."""
    from src.services.av_service import AVService
    from src.audio import pipewire_eq

    svc = AVService.__new__(AVService)
    svc._audio = None
    bands = [{"hz": 80.0, "gain_db": 10.0, "q": 1.0}]

    with patch.object(pipewire_eq, "ensure_default"), \
         patch.object(pipewire_eq, "config_matches", return_value=True), \
         patch.object(pipewire_eq, "apply_custom_bands") as apply_, \
         patch.object(pipewire_eq, "is_active", return_value=False), \
         patch.object(AVService, "_persisted_custom_eq_bands", return_value=bands):
        svc._restore_pipewire_eq()

    apply_.assert_not_called()
