# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Employee onboarding wizard — interactive setup flow for new users.

Detects new users, guides them through auth setup, domain access requests,
and knowledge import. Tracks progress per-user with resumable state.

Modules:
- schema.py    — DDL for onboarding tables
- models.py    — OnboardingState dataclass + step definitions
- store.py     — CRUD operations (get/update/list onboarding state)
- detection.py — New user detection + welcome trigger
- engine.py    — Step-by-step wizard state machine
- steps.py     — Individual step implementations
"""
