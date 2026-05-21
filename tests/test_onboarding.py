# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for bot.onboarding module."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from bot.onboarding.models import (
    OnboardingState,
    ONBOARDING_STEPS,
    STEP_IDS,
    get_step_def,
    get_applicable_steps,
)


class TestOnboardingModels(unittest.TestCase):
    """Test OnboardingState dataclass and step definitions."""

    def test_step_ids_match_definitions(self):
        """STEP_IDS list matches ONBOARDING_STEPS id fields."""
        ids = [s["id"] for s in ONBOARDING_STEPS]
        self.assertEqual(STEP_IDS, ids)

    def test_get_step_def_found(self):
        """get_step_def returns definition for known step."""
        step = get_step_def("welcome")
        self.assertIsNotNone(step)
        self.assertEqual(step["id"], "welcome")
        self.assertTrue(step["required"])

    def test_get_step_def_not_found(self):
        """get_step_def returns None for unknown step."""
        self.assertIsNone(get_step_def("nonexistent_step"))

    def test_state_from_row_roundtrip(self):
        """OnboardingState survives from_row -> to_row roundtrip."""
        state = OnboardingState(
            user_id="telegram:12345",
            source="telegram",
            display_name="Alice",
            status="in_progress",
            current_step="claude_auth",
            completed_steps=["welcome"],
            skipped_steps=[],
            step_data={"role": "engineer"},
            created_at="2026-05-12T00:00:00Z",
            updated_at="2026-05-12T00:00:00Z",
        )
        row = state.to_row()
        restored = OnboardingState.from_row(row)
        self.assertEqual(restored.user_id, "telegram:12345")
        self.assertEqual(restored.display_name, "Alice")
        self.assertEqual(restored.completed_steps, ["welcome"])
        self.assertEqual(restored.step_data, {"role": "engineer"})

    def test_from_row_json_parsing(self):
        """from_row correctly parses JSON string fields."""
        row = {
            "user_id": "teams:aad-123",
            "source": "teams",
            "display_name": "Bob",
            "status": "complete",
            "current_step": "",
            "completed_steps": '["welcome", "claude_auth", "github_auth"]',
            "skipped_steps": '["m365_auth"]',
            "step_data": '{"notification_pref": "quiet"}',
            "created_at": "2026-05-12T00:00:00Z",
            "updated_at": "2026-05-12T01:00:00Z",
            "completed_at": "2026-05-12T01:00:00Z",
        }
        state = OnboardingState.from_row(row)
        self.assertEqual(len(state.completed_steps), 3)
        self.assertIn("m365_auth", state.skipped_steps)
        self.assertEqual(state.step_data["notification_pref"], "quiet")

    def test_is_step_done(self):
        state = OnboardingState(
            user_id="test",
            completed_steps=["welcome", "claude_auth"],
        )
        self.assertTrue(state.is_step_done("welcome"))
        self.assertTrue(state.is_step_done("claude_auth"))
        self.assertFalse(state.is_step_done("m365_auth"))

    def test_is_step_skipped(self):
        state = OnboardingState(
            user_id="test",
            skipped_steps=["google_auth"],
        )
        self.assertTrue(state.is_step_skipped("google_auth"))
        self.assertFalse(state.is_step_skipped("welcome"))

    def test_next_incomplete_step_skips_completed_and_skipped(self):
        """next_incomplete_step skips both completed and skipped steps."""
        state = OnboardingState(
            user_id="test",
            completed_steps=["welcome"],
            skipped_steps=["claude_auth"],
        )
        next_step = state.next_incomplete_step()
        # Should skip welcome (completed) and claude_auth (skipped)
        self.assertIsNotNone(next_step)
        self.assertNotEqual(next_step, "welcome")
        self.assertNotEqual(next_step, "claude_auth")

    def test_next_incomplete_step_all_done(self):
        """next_incomplete_step returns None when all applicable steps done."""
        all_ids = [s["id"] for s in get_applicable_steps()]
        state = OnboardingState(
            user_id="test",
            completed_steps=all_ids,
        )
        self.assertIsNone(state.next_incomplete_step())

    def test_progress_summary_format(self):
        """progress_summary returns formatted checklist string."""
        state = OnboardingState(
            user_id="test",
            current_step="claude_auth",
            completed_steps=["welcome"],
        )
        summary = state.progress_summary()
        self.assertIn("✅", summary)  # completed welcome
        self.assertIn("▶️", summary)  # current claude_auth
        self.assertIn("Progress:", summary)

    def test_to_row_sets_updated_at(self):
        """to_row always refreshes updated_at."""
        state = OnboardingState(
            user_id="test",
            updated_at="2026-01-01T00:00:00Z",
        )
        row = state.to_row()
        self.assertNotEqual(row["updated_at"], "2026-01-01T00:00:00Z")


class TestStepsSanitization(unittest.TestCase):
    """Test knowledge import sanitization."""

    def test_blocked_patterns_stripped(self):
        """Blocked patterns are replaced in imported text."""
        from bot.onboarding.steps import _BLOCKED_PATTERNS
        text = "Hello FACTS_UPDATE world"
        for pattern in _BLOCKED_PATTERNS:
            text = text.replace(pattern, f"[blocked:{pattern[:8]}]")
        self.assertNotIn("FACTS_UPDATE", text)
        self.assertIn("[blocked:", text)

    def test_rate_limit_constants(self):
        """Rate limit constants are reasonable."""
        from bot.onboarding.steps import _MAX_IMPORTS_PER_HOUR
        self.assertGreater(_MAX_IMPORTS_PER_HOUR, 0)
        self.assertLessEqual(_MAX_IMPORTS_PER_HOUR, 100)


class TestDetection(unittest.TestCase):
    """Test new user detection logic."""

    @patch("bot.rbac.get_engine", side_effect=ImportError("no RBAC in test"))
    @patch("bot.chat_registry.is_registered", return_value=True)
    @patch("config.ADMIN_USERS", {("telegram", "99999")})
    def test_is_allowed_member_registered_in_chat_registry(self, _mock_rbac, _mock_reg):
        """User registered in chat_registry is allowed."""
        from bot.onboarding.detection import _is_allowed_member
        self.assertTrue(_is_allowed_member("telegram", "12345"))

    @patch("bot.rbac.get_engine", side_effect=ImportError("no RBAC in test"))
    @patch("config.ADMIN_USERS", {("telegram", "99999")})
    def test_admin_allowed_for_onboarding(self, _mock_rbac):
        """Admin users ARE allowed (routed to admin onboarding path)."""
        from bot.onboarding.detection import _is_allowed_member
        # RBAC raises → falls through to ADMIN_USERS check
        self.assertTrue(_is_allowed_member("telegram", "99999"))

    @patch("bot.rbac.get_engine", side_effect=ImportError("no RBAC in test"))
    @patch("bot.chat_registry.is_registered", return_value=False)
    @patch("config.ADMIN_USERS", set())
    @patch("config.ORG_CONFIG", {"members": {}})
    def test_unknown_user_not_allowed(self, _mock_rbac, _mock_reg):
        """User not in any list is not allowed."""
        from bot.onboarding.detection import _is_allowed_member
        self.assertFalse(_is_allowed_member("telegram", "77777"))


if __name__ == "__main__":
    unittest.main()
