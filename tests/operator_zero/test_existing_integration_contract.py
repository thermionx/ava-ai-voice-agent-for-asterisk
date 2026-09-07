"""Inventory the existing production-facing coverage required by Operator Zero."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_regression_runner_is_obvious_and_runs_full_pytest_suite():
    runner = (ROOT / "test-operator-zero").read_text()
    assert "-m pytest -q" in runner
    assert "tests/operator_zero" not in runner


def test_required_focused_integration_tests_remain_present():
    required = {
        "tests/test_attended_transfer_streaming.py": [
            "test_predial_transfer_finalize_removes_ai_media_and_bridges_destination",
            "test_predial_transfer_bridge_failure_cleans_destination_leg_and_provider",
            "test_predial_transfer_finalize_is_serialized",
            "test_unbridged_predial_leg_cleanup_does_not_cleanup_caller",
        ],
        "tests/tools/telephony/test_unified_transfer_tool.py": [
            "test_operator_zero_accepts_spoken_caller_and_named_recipient",
            "test_operator_zero_rejects_generic_household_recipient",
            "test_operator_zero_accepts_spoken_official_reason",
        ],
        "tests/tools/telephony/test_check_voicemail_tool.py": [
            "test_check_voicemail_requires_explicit_latest_user_intent",
            "test_check_voicemail_accepts_explicit_retrieval_intent",
        ],
        "tests/test_engine_provider_failure_v701.py": [
            "provider_failure",
        ],
        "tests/test_connection_audio.py": ["audio"],
    }
    for relative, symbols in required.items():
        text = (ROOT / relative).read_text()
        for symbol in symbols:
            assert symbol in text, f"missing {symbol} from {relative}"
