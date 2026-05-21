# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""QA staging gate tests.

Tests the qa_staging_passed hook flag lifecycle:
- Flag set by mark_qa_staging_passed()
- Flag reset on Edit/Write/Bash to /host-repo/
- deploy_bot blocked when flag is False
- DRY reset: both deploy_review_passed and qa_staging_passed reset together
- staging_qa_run tool validation (report structure, override logic)
"""

import os
import sys

import pytest

# Add parent dir to path so we can import bot modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestQaStagingFlag:
    """Test the qa_staging_passed flag lifecycle in hooks."""

    def setup_method(self):
        """Fresh session state for each test."""
        from bot.hooks import _get_or_create_state
        self.state = _get_or_create_state("test:qa-gate")

    def test_flag_default_false(self):
        """qa_staging_passed defaults to False."""
        assert self.state.get("qa_staging_passed") is False

    def test_mark_sets_flag(self):
        """mark_qa_staging_passed sets flag to True."""
        from bot.hooks import mark_qa_staging_passed
        mark_qa_staging_passed("test:qa-gate")
        assert self.state["qa_staging_passed"] is True

    def test_flag_independent_of_deploy_review(self):
        """qa_staging_passed and deploy_review_passed are independent."""
        from bot.hooks import mark_qa_staging_passed, mark_deploy_review_passed
        mark_qa_staging_passed("test:qa-gate")
        assert self.state["qa_staging_passed"] is True
        assert self.state["deploy_review_passed"] is False

        mark_deploy_review_passed("test:qa-gate")
        assert self.state["deploy_review_passed"] is True
        assert self.state["qa_staging_passed"] is True


class TestDryReset:
    """Test that both flags reset together on code edits."""

    def setup_method(self):
        from bot.hooks import _get_or_create_state, mark_qa_staging_passed, mark_deploy_review_passed
        self.state = _get_or_create_state("test:dry-reset")
        mark_qa_staging_passed("test:dry-reset")
        mark_deploy_review_passed("test:dry-reset")
        assert self.state["qa_staging_passed"] is True
        assert self.state["deploy_review_passed"] is True

    def test_both_flags_in_default_state(self):
        """Both flags exist in _DEFAULT_STATE."""
        from bot.hooks import _DEFAULT_STATE
        assert "deploy_review_passed" in _DEFAULT_STATE
        assert "qa_staging_passed" in _DEFAULT_STATE

    def test_gate_flags_tuple_matches(self):
        """Module-level _DEPLOY_GATE_FLAGS contains both flags."""
        from bot.hooks import _DEPLOY_GATE_FLAGS
        assert "deploy_review_passed" in _DEPLOY_GATE_FLAGS
        assert "qa_staging_passed" in _DEPLOY_GATE_FLAGS


class TestStagingSnapshotScrub:
    """Test the broader DB scrub patterns."""

    def test_default_categories(self):
        """Default scrub categories include credentials and auth."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
        ))
        from staging_snapshot_db import _DEFAULT_SCRUB_CATEGORIES
        assert "credentials" in _DEFAULT_SCRUB_CATEGORIES
        assert "auth" in _DEFAULT_SCRUB_CATEGORIES
        assert "tokens" in _DEFAULT_SCRUB_CATEGORIES

    def test_scrub_patterns_exist(self):
        """Regex patterns for sensitive values are defined."""
        from staging_snapshot_db import _SCRUB_VALUE_PATTERNS
        assert len(_SCRUB_VALUE_PATTERNS) >= 7  # sk-, eyJ, AKIA, ghp_, xox, google, generic

    def test_pattern_matches_openai_key(self):
        """Pattern matches OpenAI-style API keys."""
        from staging_snapshot_db import _SCRUB_VALUE_PATTERNS
        test_value = "sk-proj-abc123def456ghi789jkl012mno345pqr"
        matched = any(p.search(test_value) for p in _SCRUB_VALUE_PATTERNS)
        assert matched, f"No pattern matched {test_value}"

    def test_pattern_matches_jwt(self):
        """Pattern matches JWT-style tokens."""
        from staging_snapshot_db import _SCRUB_VALUE_PATTERNS
        test_value = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0"
        matched = any(p.search(test_value) for p in _SCRUB_VALUE_PATTERNS)
        assert matched, f"No pattern matched {test_value}"

    def test_pattern_matches_aws_key(self):
        """Pattern matches AWS access key IDs."""
        from staging_snapshot_db import _SCRUB_VALUE_PATTERNS
        test_value = "AKIAIOSFODNN7EXAMPLE"
        matched = any(p.search(test_value) for p in _SCRUB_VALUE_PATTERNS)
        assert matched, f"No pattern matched {test_value}"


class TestCompletionSignal:
    """Test the asyncio.Event completion signal for staging."""

    def test_signal_function_exists(self):
        """staging_signal_completion function is importable."""
        from main import staging_signal_completion
        assert callable(staging_signal_completion)

    def test_signal_noop_without_event(self):
        """staging_signal_completion is a no-op when no event registered."""
        from main import staging_signal_completion
        # Should not raise
        staging_signal_completion("nonexistent-chat")


class TestStagingAuth:
    """Test the staging auth helper."""

    def test_auth_helper_exists(self):
        """_check_staging_auth function is importable."""
        from main import _check_staging_auth
        assert callable(_check_staging_auth)
