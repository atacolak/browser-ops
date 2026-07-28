"""Pure terminal-quota classification + strike-ledger semantics (library only).

Adapted from legacy metabolism tests. No network, no CPA, no deletion, no
absolute paths. Callers own persistence and explicit follow-up actions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import metabolism_core as metabolism  # noqa: E402


def upstream(status_code, body):
    return {"status_code": status_code, "body": json.dumps(body)}


def terminal_probe():
    return metabolism.classify_terminal_probe_response(
        upstream(403, {"error": {"message": "You have reached your weekly response limit."}})
    )


def success_probe():
    return metabolism.classify_terminal_probe_response(
        upstream(200, {"choices": [{"message": {"content": "ok"}}]})
    )


def access_denied_probe():
    return metabolism.classify_terminal_probe_response(
        upstream(403, {"error": {"message": "Access denied"}})
    )


def auth_file(**overrides):
    """Minimal live-shaped xAI auth-files row."""
    base = {
        "provider": "xai",
        "email": "user@example.test",
        "account": "user@example.test",
        "id": "xai-user@example.test.json",
        "name": "xai-user@example.test.json",
        "auth_index": "auth-idx-1",
        "status": "active",
        "status_message": "",
        "disabled": False,
        "unavailable": False,
        "failed": 0,
        "success": 12,
        "recent_requests": [
            {"time": "10:00-10:10", "success": 3, "failed": 0},
            {"time": "10:10-10:20", "success": 2, "failed": 1},
        ],
    }
    base.update(overrides)
    return base


class TestTerminalProbeClassifier:
    def test_200_with_nonempty_assistant_content_keeps_account(self):
        result = success_probe()

        assert result["classifier"] == "keep_success_200_assistant_content"
        assert result["delete_allowed"] is False

    def test_weekly_quota_403_is_terminal_class(self):
        result = terminal_probe()

        assert result["classifier"] == metabolism.TERMINAL_WEEKLY_QUOTA_CLASS
        assert result["delete_allowed"] is True
        assert any("weekly response limit" in s for s in result["matched_terminal_strings"])

    def test_rate_limit_is_not_terminal(self):
        result = metabolism.classify_terminal_probe_response(
            upstream(429, {"error": {"message": "Too many requests; temporarily rate limited."}})
        )

        assert result["classifier"] == "keep_rate_limit"
        assert result["delete_allowed"] is False

    def test_access_denied_is_not_terminal(self):
        result = access_denied_probe()

        assert result["classifier"] == "keep_access_denied"
        assert result["delete_allowed"] is False

    def test_access_denied_with_weekly_words_still_not_terminal(self):
        result = metabolism.classify_terminal_probe_response(
            upstream(403, {"error": {"message": "Access denied: weekly response limit"}})
        )

        assert result["classifier"] == "keep_access_denied"
        assert result["delete_allowed"] is False

    def test_parse_error_is_not_terminal(self):
        result = metabolism.classify_terminal_probe_response(
            {
                "status_code": 403,
                "body": "not-json: weekly response limit",
            }
        )

        assert result["classifier"] == "keep_parse_error"
        assert result["delete_allowed"] is False

    def test_auth_expiry_is_not_terminal(self):
        result = metabolism.classify_terminal_probe_response(
            upstream(401, {"error": {"message": "invalid_token / unauthorized"}})
        )
        assert result["classifier"] == "keep_auth_expiry_or_bad_credentials"
        assert result["delete_allowed"] is False

    def test_empty_200_assistant_is_not_terminal(self):
        result = metabolism.classify_terminal_probe_response(
            upstream(200, {"choices": [{"message": {"content": "   "}}]})
        )
        assert result["classifier"] == "keep_200_empty_assistant_content"
        assert result["delete_allowed"] is False

    def test_redact_probe_text_strips_secrets(self):
        text = metabolism.redact_probe_text(
            "Bearer eyJhbGciOiJIUzI1NiJ9.aaa.bbb and bare "
            "eyJhbGciOiJIUzI1NiJ9.aaa.bbb user@secret.test"
        )
        assert "eyJhbGciOiJIUzI1NiJ9" not in text
        assert "user@secret.test" not in text
        assert "Bearer [REDACTED]" in text
        assert "[JWT_REDACTED]" in text
        assert "[EMAIL_REDACTED]" in text


class TestMetadataCandidateSelection:
    """Fail-closed candidacy from management auth-files metadata (not billing %)."""

    def test_explicit_quota_like_status_message_is_candidate(self):
        row = auth_file(status="error", status_message="weekly quota exhausted")
        result = metabolism.select_probe_candidate_from_auth_file(row)

        assert result["discard_candidate"] is True
        assert "quota_like_metadata" in result["candidate_reasons"]
        assert result["matched_quota_strings"]
        assert any("weekly" in m for m in result["matched_quota_strings"])

    def test_explicit_quota_like_error_field_is_candidate(self):
        row = auth_file(
            status="unavailable",
            error={"message": "You have reached your weekly response limit."},
        )
        result = metabolism.select_probe_candidate_from_auth_file(row)

        assert result["discard_candidate"] is True
        assert "weekly response limit" in result["matched_quota_strings"]

    def test_status_string_alone_with_quota_words_is_candidate(self):
        row = auth_file(status="weekly_quota_exhausted", status_message="")
        result = metabolism.select_probe_candidate_from_auth_file(row)

        assert result["discard_candidate"] is True

    def test_healthy_active_account_is_not_candidate(self):
        row = auth_file(status="active", status_message="", failed=0, success=50)
        result = metabolism.select_probe_candidate_from_auth_file(row)

        assert result["discard_candidate"] is False
        assert result["candidate_reasons"] == []
        assert result["matched_quota_strings"] == []

    def test_counters_alone_are_not_sufficient(self):
        """failed/success/recent_requests without quota-like error must not nominate."""
        row = auth_file(
            status="active",
            status_message="",
            failed=999,
            success=0,
            recent_requests=[
                {"time": "10:00-10:10", "success": 0, "failed": 40},
                {"time": "10:10-10:20", "success": 0, "failed": 40},
            ],
        )
        result = metabolism.select_probe_candidate_from_auth_file(row)

        assert result["discard_candidate"] is False
        assert result["matched_quota_strings"] == []

    def test_counters_plus_quota_like_error_is_candidate(self):
        row = auth_file(
            status="error",
            status_message="weekly limit has been exhausted",
            failed=12,
            success=0,
        )
        result = metabolism.select_probe_candidate_from_auth_file(row)

        assert result["discard_candidate"] is True
        assert result["matched_quota_strings"]

    def test_disabled_or_unavailable_without_quota_text_is_not_candidate(self):
        for row in (
            auth_file(disabled=True, status="active"),
            auth_file(unavailable=True, status="unavailable", status_message=""),
            auth_file(status="error", status_message="token refresh failed"),
            auth_file(status="error", error="Access denied"),
        ):
            result = metabolism.select_probe_candidate_from_auth_file(row)
            assert result["discard_candidate"] is False, f"unexpected candidate for {row}"

    def test_billing_percent_is_not_used_by_selector(self):
        """Selector only sees auth-file metadata — inject used_pct must not matter."""
        row = auth_file(status="active")
        row["used_pct"] = 99.0  # would have been old trigger; must be ignored
        result = metabolism.select_probe_candidate_from_auth_file(row)
        assert result["discard_candidate"] is False

    def test_snake_case_status_token_matches(self):
        matches = metabolism.match_quota_exhaustion_candidate_strings("weekly_quota_exhausted")
        assert matches
        assert any("weekly" in m for m in matches)


class TestTerminalStrikeLedger:
    """In-memory strike transitions; caller owns persistence."""

    @pytest.fixture
    def identity(self):
        return {
            "email": "user@example.test",
            "auth_id": "xai-user@example.test.json",
            "auth_index": "auth-idx-1",
        }

    def _apply(self, prior, probe, *, run_id, identity):
        return metabolism.apply_terminal_strike_transition(
            prior,
            probe,
            run_id=run_id,
            email=identity["email"],
            auth_id=identity["auth_id"],
            auth_index=identity["auth_index"],
        )

    def test_three_separate_runs_set_action_required(self, identity):
        probe = terminal_probe()
        state = None

        for i in range(1, 4):
            state = self._apply(state, probe, run_id=f"run-{i}", identity=identity)
            assert state["count"] == i
            if i < 3:
                assert state["action_required"] is False
                assert state["delete_allowed"] is False
            else:
                assert state["action_required"] is True
                assert state["delete_allowed"] is True

        assert len(state["strikes"]) == 3
        for strike in state["strikes"]:
            assert strike["classifier"] == metabolism.TERMINAL_WEEKLY_QUOTA_CLASS
            assert "recorded_at" in strike
            assert "body_sha256" in strike
            assert strike["matched_terminal_strings"]
            assert "run_id" in strike

    def test_same_run_cannot_add_multiple_strikes(self, identity):
        probe = terminal_probe()
        first = self._apply(None, probe, run_id="same-run", identity=identity)
        second = self._apply(first, probe, run_id="same-run", identity=identity)

        assert first["count"] == 1
        assert first["recorded"] is True
        assert second["count"] == 1
        assert second["recorded"] is False
        assert second["reason"] == "already_recorded_this_run"
        assert second["action_required"] is False
        assert len(second["strikes"]) == 1

    def test_success_probe_resets_strikes(self, identity):
        term = terminal_probe()
        state = self._apply(None, term, run_id="r1", identity=identity)
        state = self._apply(state, term, run_id="r2", identity=identity)
        assert state["count"] == 2

        state = self._apply(state, success_probe(), run_id="r3", identity=identity)
        assert state["count"] == 0
        assert state["action_required"] is False
        assert state["strikes"] == []
        assert state["reason"].startswith("reset:")

    def test_nonterminal_classification_resets_strikes(self, identity):
        term = terminal_probe()
        state = self._apply(None, term, run_id="r1", identity=identity)

        state = self._apply(state, access_denied_probe(), run_id="r2", identity=identity)
        assert state["count"] == 0
        assert state["reason"].startswith("reset:")
        assert state["strikes"] == []

    def test_unknown_classification_resets_strikes(self, identity):
        term = terminal_probe()
        state = self._apply(None, term, run_id="r1", identity=identity)
        unknown = metabolism.classify_terminal_probe_response(
            upstream(418, {"error": {"message": "I am a teapot"}})
        )
        state = self._apply(state, unknown, run_id="r2", identity=identity)
        assert state["count"] == 0
        assert state["action_required"] is False
        assert "reset:" in state["reason"]

    def test_auth_index_change_resets_prior_strikes(self, identity):
        term = terminal_probe()
        state = self._apply(None, term, run_id="r1", identity=identity)
        state = self._apply(state, term, run_id="r2", identity=identity)
        assert state["count"] == 2

        new_identity = {**identity, "auth_index": "auth-idx-NEW"}
        state = self._apply(state, term, run_id="r3", identity=new_identity)
        # Prior two strikes dropped; only the new-identity strike remains.
        assert state["count"] == 1
        assert state["action_required"] is False
        assert state["identity_reset"] is True
        assert state["auth_index"] == "auth-idx-NEW"
        assert len(state["strikes"]) == 1

    def test_auth_id_change_resets_prior_strikes(self, identity):
        term = terminal_probe()
        state = self._apply(None, term, run_id="r1", identity=identity)
        new_identity = {**identity, "auth_id": "xai-user-NEW.json"}
        state = self._apply(state, term, run_id="r2", identity=new_identity)
        assert state["count"] == 1
        assert state["identity_reset"] is True
        assert state["auth_id"] == "xai-user-NEW.json"

    def test_strike_record_is_secret_safe(self, identity):
        probe = terminal_probe()
        probe = {
            **probe,
            "body_excerpt": "Bearer eyJhbGciOiJIUzI1NiJ9.aaa.bbb and user@secret.test",
            "error_excerpt": "token eyJhbGciOiJIUzI1NiJ9.aaa.bbb",
        }
        state = self._apply(None, probe, run_id="r1", identity=identity)
        blob = json.dumps(state)
        assert "eyJhbGciOiJIUzI1NiJ9" not in blob
        assert "user@secret.test" not in blob
        assert "Bearer eyJ" not in blob
        strike = state["strikes"][0]
        assert "[JWT_REDACTED]" in (strike["body_excerpt"] + strike.get("error_excerpt", ""))

    def test_threshold_action_required_performs_no_deletion(self, identity):
        """Library signals action_required only; no side effects beyond returned state."""
        probe = terminal_probe()
        state = None
        for i in range(1, 4):
            state = self._apply(state, probe, run_id=f"run-{i}", identity=identity)
        assert state["action_required"] is True
        assert state["required"] == 3
        # No mutation of probe; no external hooks. Pure return value.
        assert metabolism.is_terminal_quota_probe(probe) is True

    def test_empty_prior_state_accepted(self, identity):
        state = metabolism.apply_terminal_strike_transition(
            metabolism.empty_strike_state(),
            terminal_probe(),
            run_id="r1",
            **identity,
        )
        assert state["count"] == 1
        assert state["recorded"] is True
