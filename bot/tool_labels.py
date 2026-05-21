# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tool label constants and security constants.

Extracted from hooks.py (Phase 0 of codebase reorganization).
All pure data definitions — no functions, no mutable runtime state.
"""

import re

# ═══════════════════════════════════════════════════════════════════════
# TOOL CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════

# Tools that constitute a "reply sent" (bare names — matched after stripping MCP namespace)
SEND_TOOLS = frozenset({
    "telegram_send_message", "telegram_send_photo", "telegram_send_document",
    "teams_send_message",  # MS Teams bot
})

# Friendly labels for tool status (bare names — matched after stripping MCP namespace)
TOOL_LABELS = {
    # SDK built-in tools
    "WebSearch": "Searching web",
    "WebFetch": "Fetching page",
    "Read": "Reading file",
    "Write": "Writing file",
    "Edit": "Editing file",
    "Bash": "Running command",
    "Grep": "Searching code",
    "Glob": "Finding files",
    # Google Workspace MCP tools
    "search_gmail_messages": "Checking email",
    "get_gmail_message_content": "Reading email",
    "get_gmail_messages_content_batch": "Reading emails",
    "get_gmail_thread_content": "Reading thread",
    "get_gmail_threads_content_batch": "Reading threads",
    "send_gmail_message": "Sending email",
    "draft_gmail_message": "Drafting email",
    "get_gmail_attachment_content": "Downloading attachment",
    "download_gmail_attachment": "Downloading email attachment",
    "list_gmail_labels": "Listing labels",
    "manage_gmail_label": "Managing label",
    "list_gmail_filters": "Listing filters",
    "manage_gmail_filter": "Managing filter",
    "modify_gmail_message_labels": "Labeling email",
    "batch_modify_gmail_message_labels": "Labeling emails",
    "get_events": "Checking calendar",
    "manage_event": "Managing event",
    "list_calendars": "Listing calendars",
    "query_freebusy": "Checking availability",
    # Memory & facts tools
    "memory_search": "Searching memory",
    "search_facts": "Looking up facts",
    "get_document": "Finding document",
    "search_media": "Searching media",
    "get_message_context": "Loading context",
    "get_messages_by_range": "Loading messages",
    "get_recent_conversation": "Loading history",
    "rag_backfill": "Rebuilding index",
    "rag_stats": "Checking index",
    # Services
    "phone_call": "Making call",
    "phone_get_transcript": "Getting transcript",
    "generate_image": "Generating image",
    "edit_image": "Editing image",
    "deploy_bot": "Deploying",
    "deploy_status": "Checking deploy",
    "deploy_staging": "Deploying staging",
    "deploy_staging_status": "Checking staging",
    "staging_command": "Running staging command",
    "manage_chat": "Managing chat registry",
    "list_scheduled_tasks": "Listing tasks",
    "manage_scheduled_task": "Managing task",
    # Playwright MCP tools
    "browser_navigate": "Opening browser",
    "browser_snapshot": "Reading page",
    "browser_take_screenshot": "Taking screenshot",
    "browser_click": "Clicking",
    "browser_type": "Typing",
    "browser_fill_form": "Filling form",
    "browser_select_option": "Selecting option",
    "send_message": "Sending message",
    # Teams tools
    "teams_send_message": "Sending Teams message",
}


# NOTE: Legacy constants removed (Sprint CB2-2, RBAC-only migration):
# _EXPLICIT_HARNESS_TOOLS, _EXTERNAL_CHAT_BLOCKED_PREFIXES,
# _EXTERNAL_CHAT_BLOCKED_EXACT, _EMAIL_GATED_PREFIXES,
# _SANDBOX_ALLOWED_SERVICE_PREFIXES — all replaced by RBAC policies.

# ═══════════════════════════════════════════════════════════════════════
# SECURITY: DISCOVERED CHAT — CHAT ISOLATION ENFORCEMENT
# ═══════════════════════════════════════════════════════════════════════

# Tools where the target chat/recipient MUST match the session's origin_chat_id
# in external chats. Prevents cross-chat messaging, editing, and data access.
# Key: bare tool name → list of parameter names that contain the target chat ID.
# The hook checks each parameter; if ANY doesn't match origin_chat_id, the call is denied.
_CHAT_ISOLATED_TOOLS: dict[str, list[str]] = {
    # Telegram send tools — chat_id must match origin
    "telegram_send_message": ["chat_id"],
    "telegram_send_photo": ["chat_id"],
    "telegram_send_document": ["chat_id"],
    "telegram_send_video": ["chat_id"],
    "telegram_send_audio": ["chat_id"],
    "telegram_send_voice": ["chat_id"],
    "telegram_send_location": ["chat_id"],
    # Telegram message manipulation — chat_id must match origin
    "telegram_edit_message": ["chat_id"],
    "telegram_delete_message": ["chat_id"],
    "telegram_pin_message": ["chat_id"],
    "telegram_unpin_message": ["chat_id"],
    # Telegram chat info — can only read own chat
    "telegram_get_chat_history": ["chat_id"],
    "telegram_get_chat_members": ["chat_id"],
    "telegram_set_chat_photo": ["chat_id"],
    "telegram_set_chat_title": ["chat_id"],
    # Telegram forward — both from_chat_id AND to_chat_id must match origin
    # (blocks forwarding FROM primary chats and TO other chats)
    "telegram_forward_message": ["from_chat_id", "to_chat_id"],
    # Teams tools — chat_id must match origin
    "teams_send_message": ["chat_id"],
    "teams_edit_message": ["chat_id"],
    "teams_delete_message": ["chat_id"],
    "teams_send_document": ["chat_id"],
    # NOTE: telegram_create_group, telegram_leave_chat, telegram_set_profile_photo,
    # telegram_update_profile are blocked via RBAC policies.
    # No need for chat isolation here — they never reach this check.
}

# ═══════════════════════════════════════════════════════════════════════
# SECURITY: FILE PATH PROTECTION
# ═══════════════════════════════════════════════════════════════════════

# --- NON-ADMIN path blocklist (credential + infra protection) ---
# Patterns that MUST NOT appear anywhere in a file path (case-insensitive)
_BLOCKED_PATH_PATTERNS = {
    ".env", "credentials", "token", "secret",
    "gmail-api/", "google-workspace-creds/",
    "/run/secrets",  # Docker secrets mount (defense-in-depth for Phase 2)
    "/proc/",  # Kernel exposes env snapshot at /proc/self/environ — must block Read
    ".ssh/", "id_rsa", "id_ed25519",
    "/etc/shadow", "/etc/passwd",
    ".docker/config",
    # Pyrogram session files (contain MTProto auth keys — full account access)
    ".session", "tg_session/",
    # Host watcher isolation — bot must not modify deploy/infra scripts
    "host-watcher.sh", "staging-watcher.sh", "corporate-bot-watcher.service",
    "staging-watcher.service",
    # Backup script isolation — bot must not modify cron-executed backup script
    "corporate-bot-backup",
    # Backup encryption password — must not be readable or writable by bot
    ".backup-password",
    # Deploy state — watcher-owned, bot must not forge baseline
    "last_healthy_commit",
    # Deploy trigger — must use deploy_bot tool (has confirmation gate)
    "deploy_trigger.json",
    # Approval files — bot must not self-approve deploys
    "deploy_approved", "staging_deploy_approved",
    "approval_request.json",
    # Config state files — bot writes via Python API, SDK must not overwrite
    # (incident: relay_sessions.json wiped by accidental SDK write, Apr 29 2026)
    "relay_sessions.json", "external_chats.json", "wa_external_chats.json",
    "harnesses.json", "chat_harnesses.json", "scheduled_tasks.json",
    "model_overrides.json", "oauth_overrides.json", "external_tool_overrides.json",
    "config_versions/",
}
_BLOCKED_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".jks"}

# --- ADMIN path blocklist ---
# Minimal: host-level files that cannot be defended by the watcher gate
# (because they ARE the watcher or are needed before the watcher runs).
# Docker/infra files (Dockerfile, compose, entrypoint) are now gated by
# the watcher's sensitive-file check — bot can edit them in /host-repo.
_ADMIN_BLOCKED_PATH_PATTERNS = {
    # Docker secrets + kernel env — structural isolation for encryption keys
    "/run/secrets",
    "/proc/",  # /proc/self/environ exposes original env snapshot even after os.environ.pop()
    # Host watcher isolation — defense-in-depth alongside watcher's sensitive-file gate.
    # Path blocklist protects reads/writes; watcher gate protects deploy-time execution.
    "host-watcher.sh", "staging-watcher.sh",
    "corporate-bot-watcher.service", "staging-watcher.service",
    # Approval files — bot must not self-approve deploys
    "deploy_approved", "staging_deploy_approved",
    # Backup script isolation — cron-executed, must not be modified by bot
    "corporate-bot-backup",
    # Backup encryption password — host-only, never in container
    ".backup-password",
    # Deploy state — watcher-owned, bot must not forge baseline
    "last_healthy_commit",
    # Deploy trigger — must use deploy_bot tool (has confirmation gate)
    "deploy_trigger.json",
    # Config state files — bot writes via Python API only (admin + non-admin)
    "relay_sessions.json", "external_chats.json", "wa_external_chats.json",
    "scheduled_tasks.json", "model_overrides.json", "external_tool_overrides.json",
    "config_versions/",
}

# Write-only blocklist: these files can be READ by admin but NEVER written by any tool.
# Escalation threat model: an agent modifying these files could expand its own permissions.
# Reading is safe — the config values are not secrets, just permission definitions.
_WRITE_ONLY_BLOCKED_PATTERNS = {
    "harnesses.json", "chat_harnesses.json",
    "oauth_default_label.json", "oauth_overrides.json",
}
# .env write protection — uses regex with trailing boundary (/ or end-of-string)
# to avoid false positives on .envrc, .env.example, etc.
_WRITE_ONLY_ENV_PATTERN = re.compile(r"[/\\]\.env$")

# ═══════════════════════════════════════════════════════════════════════
# SECURITY: FILE-SEND PATH VALIDATION
# ═══════════════════════════════════════════════════════════════════════

# Directories considered safe for file-send operations in sandbox/external chats.
# Files here are transient outputs (browser screenshots, processed media).
# SECURITY INVARIANT: Any MCP tool that writes model-controlled content to these
# directories creates an indirect file-send path. Audit new tmp-writing tools
# for data exfiltration risk before adding them.
_SAFE_MEDIA_DIRS = ("/app/data/tmp",)

# Mapping of tool bare names → file path parameter for Layer 2b path validation.
# Covers BOTH file-send tools (send_photo, send_document) AND file-input tools
# (edit_image reads a local file). Same allowlist/blocklist logic applies.
# SECURITY: Any new MCP tool that accepts a local file path MUST be added here.
# Missing tools bypass path validation = file exfiltration vector.
_FILE_SEND_PATH_PARAMS: dict[str, str] = {
    # TG file-send tools
    "telegram_send_photo": "photo_path",
    "telegram_send_document": "document_path",
    "telegram_send_video": "video_path",
    "telegram_send_audio": "audio_path",
    "telegram_send_voice": "voice_path",
    "telegram_set_chat_photo": "photo_path",
    # Gemini image editing — reads a local file and sends to Google API
    "edit_image": "image_path",
    # NOTE: telegram_set_profile_photo excluded — admin-only via RBAC, sets bot avatar.
    # NOTE: telegram_send_location — no file path parameter.
    # NOTE: generate_image — text-only input, no local file access.
    # NOTE: WhatsApp removed (Sprint B2).
}


# ═══════════════════════════════════════════════════════════════════════
# SECURITY: BASH COMMAND RESTRICTIONS
# ═══════════════════════════════════════════════════════════════════════

_SECRET_VAR_WORDS = frozenset({"token", "key", "secret", "password", "credential", "api_hash"})
_SECRET_VAR_RE = re.compile(r"\$\{?(\w+)\}?")

# ═══════════════════════════════════════════════════════════════════════
# ADMIN bash blocklist — Docker escape + host-level escalation only.
# Admin can read env vars, credentials, use curl/ssh — but cannot escape
# the Docker sandbox or modify host-level infrastructure.
# ═══════════════════════════════════════════════════════════════════════
_ADMIN_BASH_BLOCKLIST = [
    # --- Docker CLI (blanket block) ---
    # Blocks any bash command containing "docker" — including filenames like docker-compose.yml.
    # For file operations on docker-compose.yml, use Edit/Write tools or helper scripts.
    # Blanket block is necessary because Docker CLI surface is too large to allowlist safely.
    re.compile(r"\bdocker\b"),
    # --- Host-level escalation (HARD BLOCK) ---
    re.compile(r"\bsystemctl\b"),         # no systemd control
    re.compile(r"\.backup-password"),     # backup encryption key — host-only
    re.compile(r"deploy_approved"),       # approval files — bot must not self-approve
    re.compile(r"staging_deploy_approved"),
    re.compile(r"approval_request\.json"),
    re.compile(r"last_healthy_commit"),      # bot must not forge baseline
    # --- Destructive git (HARD BLOCK) ---
    re.compile(r"\bgit\b.*\b(reset\s+--hard|push\b.*--force|push\s+-f)\b"),
    # --- Deploy trigger bypass ---
    re.compile(r"deploy_trigger\.json"),
    # --- Privilege escalation ---
    re.compile(r"\bchmod\b.*\+s"),        # no setuid
    re.compile(r"\bsudo\b"),              # no sudo
    re.compile(r"\bsu\b\s"),              # no su
    re.compile(r"\brm\s+-rf\s+/"),        # no rm -rf /
    # --- Blocked path write prevention (shell redirects/copies to .env) ---
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*\.env\b"),
    # --- Harness config write prevention ---
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*harnesses\.json"),
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*chat_harnesses\.json"),
    re.compile(r"""open\s*\(.*harnesses\.json.*['\"]w"""),
    # --- Config state file write prevention (relay_sessions wipe incident Apr 29 2026) ---
    # Bot must use Python API (JCS) to modify these, never direct Bash writes.
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*relay_sessions\.json"),
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*external_chats.json"),
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*scheduled_tasks\.json"),
    re.compile(r"""open\s*\(.*relay_sessions\.json.*['\"]w"""),
]

# ═══════════════════════════════════════════════════════════════════════
# CODING SANDBOX bash blocklist — for project-scoped coding chats.
# Applied to ALL users (admin + non-admin) in coding harnesses with
# project_dir set. Blocks credential access, env vars, Docker escape.
# Network is ALLOWED (coding needs internet for packages, APIs, etc).
# ═══════════════════════════════════════════════════════════════════════
_CODING_SANDBOX_BASH_BLOCKLIST = [
    # --- Secret exfiltration (env vars) ---
    re.compile(r"\benv\b"),               # dump all env vars
    re.compile(r"\bprintenv\b"),          # dump env vars
    re.compile(r"\bset\b(?!\s+-e)"),      # dump shell vars (allow set -e)
    re.compile(r"\bexport\s+\w+="),       # export VAR=value (narrower for coding)
    re.compile(r"\.env\b"),               # any reference to .env files
    re.compile(r"os\.environ"),           # python os.environ access
    re.compile(r"process\.env"),          # node process.env access
    # --- Docker escape ---
    re.compile(r"\bdocker\b"),
    re.compile(r"\bsystemctl\b"),
    re.compile(r"\bhost-watcher\b"),
    re.compile(r"\bstaging-watcher\b"),
    re.compile(r"\bcorporate-bot-backup\b"), # no backup script access
    re.compile(r"\.backup-password"),     # no backup encryption key access
    # --- Privilege escalation ---
    re.compile(r"\bchmod\b.*\+s"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bsu\b\s"),
    re.compile(r"\bpasswd\b"),
    # --- System damage ---
    re.compile(r"\brm\s+-rf\s+/"),
    # --- Destructive git ---
    re.compile(r"\bgit\b.*\b(reset\s+--hard|push\b.*--force|push\s+-f)\b"),
    re.compile(r"deploy_trigger\.json"),
    # --- Harness config ---
    re.compile(r"harnesses\.json"),
    re.compile(r"chat_harnesses\.json"),
    # --- /proc and /sys access (leaks env vars, process info, secrets) ---
    re.compile(r"/proc/|/sys/"),
    # --- Package manager (npm blocked — can install arbitrary native code) ---
    re.compile(r"\bnpm\b.*\binstall\b"),
    # NOTE: pip install is ALLOWED in sandbox — bwrap provides filesystem isolation
    # (--tmpfs /app hides all bot data, --clearenv removes secrets). Network is
    # allowed by default for pip to reach PyPI. Packages install into project .venv/.
    # --- Symlink escape (creating symlinks to files outside sandbox) ---
    re.compile(r"\bln\b.*\s-s\b"),        # block all symlink creation in sandbox
    # --- Inline code escape prevention ---
    re.compile(r"\bpython[23]?\s+-c\b.*\b(subprocess|socket)\b"),
    re.compile(r"\bnode\s+-e\b.*\bchild_process\b"),
]

# NON-ADMIN bash blocklist — full protection (credential + infra + exfil).
# Bash uses BLOCKLIST-ONLY approach: everything is allowed EXCEPT dangerous patterns.
# Rationale: container is already sandboxed (Docker, non-root, no secrets in image).
# The blocklist prevents secret exfiltration and destructive host-level operations.
_BASH_BLOCKLIST = [
    # --- Secret exfiltration (env vars) ---
    re.compile(r"\benv\b"),           # dump all env vars (contains tokens)
    re.compile(r"\bprintenv\b"),      # dump env vars
    re.compile(r"\bset\b(?!\s+-e)"),  # dump shell vars (but allow set -e)
    re.compile(r"\bexport\b"),        # export vars
    re.compile(r"\.env\b"),           # any reference to .env files
    re.compile(r"os\.environ"),       # python os.environ access
    re.compile(r"process\.env"),      # node process.env access
    # --- Secret exfiltration (files) ---
    re.compile(r"\b(cat|head|tail|less|more|strings|xxd|base64|od)\b.*\.(env|pem|key|secret)"),
    re.compile(r"\b(cat|head|tail|less|more|strings|xxd|base64|od)\b.*\.credentials"),
    re.compile(r"\bpython.*open\(.*\.(env|pem|key)"),  # python file reading of secrets
    # --- Network exfiltration ---
    re.compile(r"\bwget\b"),          # no wget
    re.compile(r"\bnc\b|\bnetcat\b"), # no netcat
    re.compile(r"\bssh\b(?!-keyscan)"),  # no ssh (except keyscan)
    re.compile(r"\bcurl\b(?!.*localhost).*-[dX]"),  # curl POST/data to non-localhost
    re.compile(r"\bcurl\b.*@"),       # curl with file upload (@file)
    # --- Docker secrets ---
    re.compile(r"\bdocker\b.*\binspect\b"),  # reveals env vars in container config
    re.compile(r"\bdocker\b.*\bexec\b.*\b(env|printenv|set)\b"),  # env dump via docker exec
    # --- Privilege escalation ---
    re.compile(r"\bchmod\b.*\+s"),    # no setuid
    re.compile(r"\bsudo\b"),          # no sudo
    re.compile(r"\bsu\b\s"),          # no su
    re.compile(r"\bpasswd\b"),        # no passwd
    re.compile(r"\buseradd\b|\busermod\b"),  # no user management
    # --- System damage ---
    re.compile(r"/proc/|/sys/"),      # no proc/sys access
    re.compile(r"\brm\s+-rf\s+/"),    # no rm -rf /
    # --- Host watcher isolation ---
    # Git push is controlled via deploy_bot tool only; block direct git push/reset
    re.compile(r"\bgit\b.*\b(reset\s+--hard|push\b.*--force|push\s+-f)\b"),  # destructive git
    # Block modifying deploy watcher or systemd services
    re.compile(r"\bsystemctl\b"),     # no systemd control
    re.compile(r"\bhost-watcher\b"),  # no direct watcher access
    re.compile(r"\bstaging-watcher\b"),  # no staging watcher access either
    re.compile(r"\bcorporate-bot-backup\b"),  # no backup script access
    re.compile(r"\.backup-password"),      # no backup encryption key access
    # --- Docker container escape prevention ---
    re.compile(r"\bdocker\b"),            # no docker CLI at all (deploy uses trigger files)
    # --- Write bypass prevention (tee/dd/redirect to sensitive paths) ---
    re.compile(r"\btee\b.*/(host-repo|deploy|\.env)"),
    re.compile(r"\bdd\b.*of=.*/host-repo"),
    # Block writing deploy trigger files via shell (must use deploy_bot tool)
    re.compile(r"deploy_trigger\.json"),
    # Harness config — block WRITE operations (reads are safe for admin).
    # Catches: redirect (>), tee, cp/mv to, rm, and inline python open(...,'w').
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*harnesses\.json"),
    re.compile(r"(>|tee\b|cp\b|mv\b|rm\b|truncate\b|chmod\b).*chat_harnesses\.json"),
    re.compile(r"""open\s*\(.*harnesses\.json.*['\"]w"""),
    # --- Inline code escape prevention ---
    re.compile(r"\bpython[23]?\s+-c\b.*\b(subprocess|socket)\b"),
    re.compile(r"\bnode\s+-e\b.*\bchild_process\b"),
    re.compile(r"\bpip\b.*\binstall\b"),   # no pip install (prevents docker SDK install)
]

# Patterns that indicate Bash being used for media file operations.
# Focused on /app/data/media_cache and /app/data/tmp paths to avoid false positives
# on legitimate commands (e.g. grep for .jpg in code, git show image, etc.)
_MEDIA_BASH_PATTERNS = [
    re.compile(r"\b(ls|dir)\b.*/app/data/(media_cache|tmp)"),   # listing media/tmp dirs
    re.compile(r"\b(cp|mv|rename)\b.*/app/data/(media_cache|tmp)"),  # copying/moving in media dirs
    re.compile(r"\btest\s+-[ef]\b.*/app/data/(media_cache|tmp)"),    # file existence checks
    re.compile(r"\b(file|stat|wc)\b.*/app/data/(media_cache|tmp)"),  # inspecting media files
    re.compile(r"\bfind\b.*/app/data/(media_cache|tmp)"),            # searching media dirs
]

# ═══════════════════════════════════════════════════════════════════════
# DEPLOY / BUDGET APPROVAL PATTERNS
# ═══════════════════════════════════════════════════════════════════════

# Deploy command pattern: ONLY /deploy (with slash prefix) counts as deploy approval.
# This eliminates false positives from casual mentions of the word "deploy" in conversation
# (e.g. "staging deploy lock files" was a false positive that triggered an unauthorized deploy).
# Variants: /deploy (all gates), /deploy no-qa (skip QA), /deploy force (skip ALL hook gates).
_DEPLOY_KEYWORDS = re.compile(
    r"(?:^|\n)\s*/deploy\b",
    re.IGNORECASE | re.MULTILINE,
)

# /deploy force — skip ALL hook-level deploy gates (admin check still required).
_DEPLOY_FORCE_PATTERN = re.compile(
    r"(?:^|\n)\s*/deploy\s+force\b",
    re.IGNORECASE | re.MULTILINE,
)

# /deploy no-qa — skip staging QA gate only (deploy_review still required).
_DEPLOY_NOQA_PATTERN = re.compile(
    r"(?:^|\n)\s*/deploy\s+no-?qa\b",
    re.IGNORECASE | re.MULTILINE,
)

# Keywords that count as explicit budget-extension approval. MUST be the
# exact phrase "extend budget" (or Russian equivalents) — no fuzzy matching,
# no partial keywords. Same stripping pipeline as deploy: reply-quote blocks
# and enrichment markers are removed so a bot echo can't self-authorize.
_EXTEND_BUDGET_KEYWORDS = re.compile(
    r"\b(extend\s+budget|increase\s+budget|продли\s+бюджет|увеличь\s+бюджет)\b",
    re.IGNORECASE,
)

# Outbound action gate (FB-73): /send command pattern
_OUTBOUND_SEND_KEYWORDS = re.compile(
    r"(?:^|\n)\s*/send\b",
    re.IGNORECASE | re.MULTILINE,
)

# Strip reply-quote blocks before checking deploy keyword: matches
# [Replying to <LABEL>: "<possibly multi-line quoted text>"]. The quoted
# text can contain "deploy" / "ship it" etc. from a prior bot response —
# those MUST NOT count as user approval.
_REPLY_QUOTE_BLOCK = re.compile(
    r'\[Replying to [^:\]]+?: *"[\s\S]*?"\]'
)
# Strip single-line enrichment markers that aren't reply-quotes:
# [Forwarded ...], [Attached ...], [SYSTEM: ...], [VOICE attached: ...],
# [PHOTO attached: ...], [Replying to location/...], etc.
_ENRICHMENT_MARKER = re.compile(
    r'^\[(?:Replying to (?:location|live location|shared contact|message with media)'
    r'|Forwarded|Attached|SYSTEM|VOICE|PHOTO|DOCUMENT|VIDEO|AUDIO|STICKER'
    r'|Live location|Shared location|Shared contact)'
    r'[^\]]*\] *$',
    re.MULTILINE,
)
