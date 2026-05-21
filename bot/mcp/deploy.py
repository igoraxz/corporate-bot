# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Deploy MCP tools — deploy_bot, deploy_review, staging_qa_run, deploy_status, staging tools.

Extracted from bot/mcp_tools.py (Phase 1B refactor). Pure extraction — no behavior changes.
"""

import asyncio
import json
import logging
import os
import time as _time
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _to_bool, _text_response

log = logging.getLogger(__name__)


# ============================================================
# ASYNC SUBPROCESS HELPER
# ============================================================

import subprocess


async def _arun(
    cmd: list[str],
    *,
    cwd: str = "/host-repo",
    timeout: int = 30,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess without blocking the asyncio event loop.

    Drop-in replacement for subprocess.run(cmd, cwd=..., capture_output=True,
    text=True, timeout=...). Uses asyncio.to_thread to offload to a thread pool,
    keeping the event loop free for ticker updates and message handling.
    """
    return await asyncio.to_thread(
        subprocess.run, cmd,
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
        env=env,
    )


# ============================================================
# DOCS STALENESS CHECK
# ============================================================

async def _check_docs_staleness() -> list[str]:
    """Check module manifest for stale docs based on git diff.

    Compares changed source files against module_manifest.json dependencies.
    Returns list of warning strings (empty = all clean).
    Non-blocking — failures in this check don't prevent deploy.
    """
    warnings: list[str] = []
    manifest_path = Path("/host-repo/data/module_manifest.json")
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        return ["Could not parse module_manifest.json"]

    modules = manifest.get("modules", {})
    if not modules:
        return []

    # Get changed files since last deploy (unpushed commits)
    try:
        result = await _arun(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            timeout=10,
        )
        if result.returncode != 0:
            # Fallback: diff against last commit
            result = await _arun(
                ["git", "diff", "--name-only", "HEAD~1"],
                timeout=10,
            )
        changed_files = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    except Exception:
        return ["Could not determine changed files from git"]

    if not changed_files:
        return []

    # Check each module's dependencies against changed files
    for mod_name, mod_info in modules.items():
        deps = mod_info.get("depends_on", [])
        mod_file = mod_info.get("file", "")
        if not deps:
            continue

        # Check if any dependency was changed
        affected_deps = []
        for dep in deps:
            # dep can be a file or directory (ending with /)
            for cf in changed_files:
                if dep.endswith("/"):
                    if cf.startswith(dep):
                        affected_deps.append(cf)
                else:
                    if cf == dep:
                        affected_deps.append(cf)

        if affected_deps and mod_file not in changed_files:
            dep_list = ", ".join(affected_deps[:3])
            if len(affected_deps) > 3:
                dep_list += f" (+{len(affected_deps) - 3} more)"
            warnings.append(
                f"{mod_name}: source changed ({dep_list}) but {mod_file} not updated"
            )

    return warnings


# ============================================================
# DEPLOY TOOLS
# ============================================================

@tool("deploy_review", "MANDATORY pre-deploy review gate. Must be called BEFORE deploy_bot — a hook blocks deploy without it. Runs automated checks: syntax (ast.parse all .py), imports (catches NameError/ImportError). Flag auto-resets when code is edited (Edit/Write on /host-repo/), requiring re-review. The model MUST also run security + quality review agents on the diff before calling this tool.", {})
async def deploy_review(args: dict[str, Any]) -> dict:
    """Run mandatory pre-deploy automated checks and set the review gate flag.

    This is the automated portion of the deploy review pipeline. The model
    is responsible for running security-reviewer + quality-reviewer agents
    on the diff BEFORE calling this tool. This tool then runs syntax and
    import checks as an additional safety net, and sets the deploy_review_passed
    flag in session state so deploy_bot is unblocked.
    """
    import ast
    import glob as glob_mod

    _review_sk = args.pop("_session_key", "")

    results: list[str] = []
    has_errors = False

    # Gate 1: Syntax check all Python files
    py_files = glob_mod.glob("/host-repo/**/*.py", recursive=True)
    syntax_errors = []
    for py_file in py_files:
        try:
            with open(py_file) as f:
                ast.parse(f.read(), filename=py_file)
        except SyntaxError as e:
            syntax_errors.append(f"{py_file}:{e.lineno} \u2014 {e.msg}")
    if syntax_errors:
        has_errors = True
        results.append(f"\u274c Syntax errors ({len(syntax_errors)})")
        for err in syntax_errors:
            results.append(f"  {err}")
    else:
        results.append(f"\u2705 Syntax check passed ({len(py_files)} files)")

    # Gate 2: Import check — catches NameError/ImportError at import time
    bot_modules = [
        "config", "bot.agent", "bot.hooks", "bot.mcp_tools",
        "bot.storage.memory", "bot.storage.rag", "bot.prompts", "bot.scheduler",
        "bot.storage.media", "bot.storage.db", "bot.mcp_config",
        "bot.desktop_relay", "bot.relay.pool",
        "bot.md_to_tg_html", "bot.mcp_size_guard",
        "integrations.telegram",
        "integrations.gemini", "integrations.phone",
    ]
    # Also try importing main — catches top-level import issues
    bot_modules.append("main")
    import_errors = []
    for mod in bot_modules:
        try:
            result = await _arun(
                ["python3", "-c", f"import {mod}"],
                timeout=15,
                env={**os.environ, "PYTHONPATH": "/host-repo"},
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                err_line = err.split("\n")[-1] if err else "unknown error"
                import_errors.append(f"{mod}: {err_line}")
        except subprocess.TimeoutExpired:
            import_errors.append(f"{mod}: timeout")
        except Exception as e:
            import_errors.append(f"{mod}: {e}")
    if import_errors:
        has_errors = True
        results.append(f"\u274c Import errors ({len(import_errors)})")
        for err in import_errors:
            results.append(f"  {err}")
    else:
        results.append(f"\u2705 Import check passed ({len(bot_modules)} modules)")

    # Gate 3: Smoke tests (catch import ordering, typos, missing params)
    smoke_files = [
        "/host-repo/tests/test_smoke_commands.py",
        "/host-repo/tests/test_smoke_mcp.py",
        "/host-repo/tests/test_smoke_query.py",
    ]
    existing_smoke = [f for f in smoke_files if os.path.exists(f)]
    if existing_smoke:
        try:
            smoke_result = await _arun(
                ["python3", "-m", "pytest"] + existing_smoke + ["-x", "-q", "--tb=short"],
                timeout=120,
                env={**os.environ, "PYTHONPATH": "/host-repo"},
            )
            if smoke_result.returncode == 0:
                results.append(f"\u2705 Smoke tests passed ({len(existing_smoke)} files)")
            else:
                has_errors = True
                results.append(f"\u274c Smoke tests failed:\n{smoke_result.stdout[-500:]}")
        except subprocess.TimeoutExpired:
            results.append("\u26a0\ufe0f Smoke tests timed out (120s)")
        except Exception as e:
            results.append(f"\u26a0\ufe0f Smoke tests error: {e}")

    # Gate 4: Docs staleness check via module manifest (HARD BLOCK)
    # /deploy force bypasses deploy_review entirely, so this is skippable via force.
    stale_warnings = await _check_docs_staleness()
    if stale_warnings:
        has_errors = True
        results.append(f"\u274c Docs staleness ({len(stale_warnings)}) \u2014 update docs or use /deploy force:")
        for w in stale_warnings:
            results.append(f"  {w}")
    else:
        results.append("\u2705 Docs staleness check passed")

    # Gate 5: Static analysis via pyflakes — catches NameError, undefined vars,
    # unused imports. This is the gate that would have caught the _harness incident.
    try:
        # Get changed .py files vs last deploy to focus analysis
        _changed_files: list[str] = []
        try:
            _healthy_file = Path("/host-repo/deploy/last_healthy_commit")
            if _healthy_file.exists():
                _base_commit = _healthy_file.read_text().strip()
                _diff_result = await _arun(
                    ["git", "diff", "--name-only", _base_commit, "HEAD", "--", "*.py"],
                    timeout=10,
                )
                if _diff_result.returncode == 0 and _diff_result.stdout.strip():
                    _changed_files = [
                        f"/host-repo/{f.strip()}" for f in _diff_result.stdout.strip().split("\n")
                        if f.strip() and os.path.exists(f"/host-repo/{f.strip()}")
                    ]
        except Exception:
            pass
        # Fallback: if no changed files detected, scan critical modules
        if not _changed_files:
            _changed_files = [
                f"/host-repo/{m.replace('.', '/')}.py"
                for m in bot_modules if os.path.exists(f"/host-repo/{m.replace('.', '/')}.py")
            ] + [
                f for f in glob_mod.glob("/host-repo/bot/**/*.py", recursive=True)
            ]
            # Deduplicate
            _changed_files = list(set(_changed_files))
        pyflakes_errors = []
        for pf_file in _changed_files:
            try:
                pf_result = await _arun(
                    ["python3", "-m", "pyflakes", pf_file],
                    timeout=15,
                    env={**os.environ, "PYTHONPATH": "/host-repo"},
                )
                if pf_result.returncode != 0 and pf_result.stdout.strip():
                    for _line in pf_result.stdout.strip().split("\n"):
                        # Filter: only report undefined names and redefined unused
                        # (skip "imported but unused" — too noisy for existing code)
                        # Exclude "unable to detect undefined names" (import * warning)
                        if "unable to detect" in _line:
                            continue
                        if "undefined name" in _line or "redefined while unused" in _line:
                            pyflakes_errors.append(_line.replace("/host-repo/", ""))
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
        if pyflakes_errors:
            has_errors = True
            results.append(f"\u274c Pyflakes undefined names ({len(pyflakes_errors)}):")
            for pf_err in pyflakes_errors[:20]:
                results.append(f"  {pf_err}")
        else:
            results.append(f"\u2705 Pyflakes static analysis passed ({len(_changed_files)} files)")
    except Exception as pf_exc:
        # Pyflakes not installed or other error — warn but don't block
        results.append(f"\u26a0\ufe0f Pyflakes check skipped: {pf_exc}")

    # Gate 6: Staging boot verification — rebuild staging + health check.
    # This catches permission errors, volume layout issues, and config.py
    # startup crashes that static analysis cannot detect. If staging boots
    # successfully, the code handles all volume/permission scenarios.
    # WARNING-only when staging infra is unreachable (not a code problem).
    from config import STAGING_URL as _staging_url
    _staging_boot_ok = False
    try:
        import httpx as _httpx_check
        _local_head = (await _arun(
            ["git", "rev-parse", "HEAD"],
            timeout=5,
        )).stdout.strip()

        # First check if staging is reachable at all
        _staging_reachable = False
        try:
            async with _httpx_check.AsyncClient(timeout=10) as _hc:
                _ping = await _hc.get(f"{_staging_url}/health")
                _staging_reachable = _ping.status_code == 200
        except Exception:
            pass

        if not _staging_reachable:
            results.append(
                "\u26a0\ufe0f Staging boot check skipped (staging unreachable). "
                "Start staging to enable this gate."
            )
            _staging_boot_ok = True  # don't block on infra issues
        else:
            # Check if staging already at HEAD — skip rebuild if so
            _staging_commit = ""
            try:
                async with _httpx_check.AsyncClient(timeout=10) as _hc:
                    _sh = await _hc.get(f"{_staging_url}/health")
                    if _sh.status_code == 200:
                        _staging_commit = _sh.json().get("commit", "")
            except Exception:
                pass

            _g6_cmp = min(len(_local_head), len(_staging_commit), 12)
            if _staging_commit and _local_head[:_g6_cmp] == _staging_commit[:_g6_cmp]:
                results.append(f"\u2705 Staging already at HEAD ({_staging_commit[:8]}) \u2014 boot verified")
                _staging_boot_ok = True
            else:
                # Rebuild staging and verify it boots
                results.append(
                    f"\u2699\ufe0f Rebuilding staging (local={_local_head[:8]}, "
                    f"staging={_staging_commit[:8] if _staging_commit else 'unknown'})..."
                )
                try:
                    # Trigger staging rebuild directly (can't call deploy_staging —
                    # it's an MCP tool wrapper, not a plain function).
                    _stg_trigger_dir = _staging_trigger_dir()
                    _stg_trigger_dir.mkdir(parents=True, exist_ok=True)
                    _stg_trigger = _stg_trigger_dir / "deploy_trigger.json"
                    _stg_result = _stg_trigger_dir / "deploy_result.json"
                    from datetime import datetime as _dt6
                    _stg_trigger_data = {
                        "action": "rebuild",
                        "reason": "deploy_review Gate 6 — staging boot verification",
                        "timestamp": _dt6.now().isoformat(),
                        "head_commit": _local_head[:8],
                    }
                    _stg_tmp = _stg_trigger.with_suffix(".tmp")
                    _stg_tmp.write_text(json.dumps(_stg_trigger_data, indent=2))
                    _stg_tmp.rename(_stg_trigger)

                    # Poll for watcher result (up to 5 min)
                    _stg_status = "timeout"
                    _stg_poll_start = _time.time()
                    while _time.time() - _stg_poll_start < 300:
                        await asyncio.sleep(5)
                        if not _stg_trigger.exists() and _stg_result.exists():
                            try:
                                _stg_res = json.loads(_stg_result.read_text())
                                _s = _stg_res.get("status", "")
                                if _s == "pending_approval":
                                    _stg_status = "pending_approval"
                                    break
                                if _s not in ("in_progress", "pending", ""):
                                    _stg_status = _s
                                    break
                            except Exception:
                                pass

                    if _stg_status == "success":
                        # Verify health/deep (exercises harness + prompt building)
                        try:
                            async with _httpx_check.AsyncClient(timeout=30) as _hc:
                                _deep = await _hc.get(f"{_staging_url}/health/deep")
                                if _deep.status_code == 200:
                                    results.append(
                                        "\u2705 Staging boot verified (success, "
                                        "/health/deep OK)"
                                    )
                                    _staging_boot_ok = True
                                else:
                                    results.append(
                                        f"\u274c Staging booted but /health/deep returned "
                                        f"{_deep.status_code} \u2014 fix before prod deploy"
                                    )
                        except Exception as _he:
                            results.append(
                                f"\u274c Staging booted but /health/deep unreachable: {_he}"
                            )
                    elif _stg_status == "pending_approval":
                        results.append(
                            "\u26a0\ufe0f Staging watcher needs APPROVAL \u2014 "
                            "sensitive files changed (compose, entrypoint, service files). "
                            "Run: sudo /opt/<project>-staging-watcher/approve-deploy.sh "
                            "then re-run deploy_review."
                        )
                    elif _stg_status == "timeout":
                        results.append(
                            "\u274c Staging rebuild timed out (5 min). "
                            "Check staging-watcher logs."
                        )
                    else:
                        # CB-132: Watcher health check may use wrong port.
                        # Fallback: direct health check via STAGING_URL (Docker internal).
                        _direct_ok = False
                        try:
                            async with _httpx_check.AsyncClient(timeout=15) as _hc:
                                _direct = await _hc.get(f"{_staging_url}/health")
                                if _direct.status_code == 200:
                                    _direct_commit = _direct.json().get("commit", "")[:8]
                                    results.append(
                                        f"\u2705 Staging boot verified via direct health check "
                                        f"(watcher reported {_stg_status}, but staging responds "
                                        f"at {_staging_url}, commit={_direct_commit})"
                                    )
                                    _staging_boot_ok = True
                                    _direct_ok = True
                        except Exception:
                            pass
                        if not _direct_ok:
                            results.append(
                                f"\u274c Staging rebuild failed: {_stg_status}. "
                                f"Fix before prod deploy."
                            )
                except Exception as _se:
                    results.append(f"\u274c Staging rebuild error: {_se}")

        if not _staging_boot_ok:
            has_errors = True
    except Exception as _gate6_exc:
        results.append(
            f"\u26a0\ufe0f Staging boot check error: {_gate6_exc}. "
            f"Skipped (infra issue, not code problem)."
        )
        _staging_boot_ok = True  # don't block on infra issues

    # Set or block the flag
    if not has_errors:
        from bot.hooks import mark_deploy_review_passed
        mark_deploy_review_passed(session_key=_review_sk)
        results.append("\u2705 Deploy review PASSED \u2014 deploy_bot is now unblocked")
    else:
        results.append("\u274c Deploy review FAILED \u2014 fix errors above before deploying")

    return _text_response({"passed": not has_errors, "results": results})



@tool("staging_qa_run", "Run QA test scenarios against staging and set the qa_staging_passed gate flag. Scenarios run IN PARALLEL (isolated chat IDs), fail-fast kills stuck sessions. Use regression=true for static regression suite (from tests/staging_scenarios.json — zero LLM cost), or pass dynamic scenarios JSON. Executes via HTTP — does NOT trust model-provided results.", {
    "regression": {"type": "boolean", "description": "Load static regression scenarios from tests/staging_scenarios.json. No qa-tester agent needed. Preferred for routine deploys."},
    "scenarios": {"type": "string", "description": "JSON array of test scenarios. Each: {name, input, expect_tools?, expect_no_tools?, expect_text_contains?, expect_text_not_contains?, expect_text_match_any?, is_admin?, timeout?}. Used for ad-hoc/dynamic tests."},
    "override": {"type": "boolean", "description": "Admin override for infra failures ONLY (staging down). NOT for test failures. Tool verifies staging is unreachable before allowing."},
    "reason": {"type": "string", "description": "Required when override=true. Why staging QA cannot run."},
})
async def staging_qa_run(args: dict[str, Any]) -> dict:
    """Execute QA scenarios against staging and set the hook gate flag.

    This tool runs tests ITSELF via httpx — it does not trust model-provided
    pass/fail reports. The qa-tester agent generates scenarios (planning),
    this tool executes them (verification).
    """
    try:
        return await _staging_qa_run_impl(args)
    except Exception as exc:
        log.error("staging_qa_run INTERNAL ERROR: %s", exc, exc_info=True)
        return _text_response({
            "passed": False,
            "error": f"Internal tool error: {type(exc).__name__}: {exc}",
            "hint": "The staging_qa_run tool itself crashed (not a test failure). "
                    "Use /deploy force to bypass gates, or fix the tool and retry.",
        })


async def _staging_qa_run_impl(args: dict[str, Any]) -> dict:
    """Inner implementation — separated so the wrapper can catch internal errors."""
    import httpx

    _qa_session_key = args.pop("_session_key", "")

    from config import STAGING_URL, STAGING_TEST_TOKEN, STAGING_QA_TIMEOUT

    override = _to_bool(args.get("override"), False)
    reason = (args.get("reason") or "").strip()
    scenarios_str = (args.get("scenarios") or "").strip()

    results: list[str] = []

    # --- Override path: only when staging is genuinely unreachable ---
    if override:
        if not reason:
            return _text_response({
                "passed": False,
                "error": "override requires a reason (explain why staging QA cannot run)",
            })
        # Verify staging is actually down before allowing override
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{STAGING_URL}/health")
                if resp.status_code == 200:
                    return _text_response({
                        "passed": False,
                        "error": "Override rejected: staging is reachable (health returned 200). "
                                 "Run QA tests instead of overriding.",
                    })
        except Exception as exc:
            log.info(f"Staging health check failed ({type(exc).__name__}: {exc}) — override allowed")
        log.warning(f"Staging QA OVERRIDE: {reason}")
        from bot.hooks import mark_qa_staging_passed
        mark_qa_staging_passed(session_key=_qa_session_key)
        return _text_response({
            "passed": True, "override": True, "reason": reason,
            "message": "QA gate overridden (staging unreachable). deploy_bot unblocked.",
        })

    # --- Normal path: load scenarios ---
    regression = _to_bool(args.get("regression"), False)

    if regression:
        # Load static regression scenarios from file
        scenarios_path = Path("/host-repo/tests/staging_scenarios.json")
        if not scenarios_path.exists():
            # Fallback to container path
            scenarios_path = Path("/app/tests/staging_scenarios.json")
        if not scenarios_path.exists():
            return _text_response({
                "passed": False,
                "error": "Regression scenarios file not found at tests/staging_scenarios.json",
            })
        try:
            scenarios_data = json.loads(scenarios_path.read_text())
            scenarios = scenarios_data.get("scenarios", [])
            log.info(f"Loaded {len(scenarios)} regression scenarios from {scenarios_path}")
        except (json.JSONDecodeError, KeyError) as e:
            return _text_response({"passed": False, "error": f"Invalid scenarios file: {e}"})
    elif scenarios_str:
        try:
            scenarios = json.loads(scenarios_str)
        except json.JSONDecodeError as e:
            return _text_response({"passed": False, "error": f"Invalid JSON: {e}"})
    else:
        return _text_response({
            "passed": False,
            "error": "Either regression=true or scenarios JSON is required. "
                     "Use regression=true for standard deploy gate.",
        })

    if not isinstance(scenarios, list) or len(scenarios) == 0:
        return _text_response({
            "passed": False,
            "error": "scenarios must be a non-empty JSON array",
        })

    if len(scenarios) > 50:
        return _text_response({
            "passed": False,
            "error": f"Too many scenarios ({len(scenarios)}). Maximum is 50.",
        })

    # Build auth headers
    headers = {"Content-Type": "application/json"}
    if STAGING_TEST_TOKEN:
        headers["Authorization"] = f"Bearer {STAGING_TEST_TOKEN}"

    passed = 0
    failed = 0
    skipped = 0

    # httpx timeout must cover the longest scenario (up to 900s) + buffer
    _max_scenario_timeout = max((s.get("timeout", STAGING_QA_TIMEOUT) for s in scenarios), default=STAGING_QA_TIMEOUT)
    _http_timeout = min(_max_scenario_timeout, 900) + 30
    async with httpx.AsyncClient(timeout=_http_timeout) as client:
        # Step 1: Health canary — verify staging is up + commit matches
        try:
            health_resp = await client.get(f"{STAGING_URL}/health", headers=headers)
            if health_resp.status_code != 200:
                return _text_response({
                    "passed": False,
                    "error": f"Staging health check failed (status {health_resp.status_code}). "
                             "Start staging first, or use override=true if it cannot be started.",
                })
            results.append("\u2705 Staging health check passed")

            # Step 1b: Verify staging is running the same code we're about to deploy.
            # This catches the case where staging wasn't rebuilt after code changes,
            # which would mean QA is testing stale code (the _harness incident).
            try:
                _local_head = (await _arun(
                    ["git", "rev-parse", "HEAD"],
                    timeout=5,
                )).stdout.strip()
                _staging_data = health_resp.json()
                _staging_commit = _staging_data.get("commit", "")
                # Compare using the shorter length (staging /health may return 7-char abbrev)
                _cmp_len = min(len(_local_head), len(_staging_commit), 12)
                if _staging_commit and _local_head[:_cmp_len] != _staging_commit[:_cmp_len]:
                    return _text_response({
                        "passed": False,
                        "error": (
                            f"Staging commit mismatch: local HEAD={_local_head[:8]}, "
                            f"staging={_staging_commit[:8]}. Staging is running STALE code. "
                            "Run deploy_staging first, then re-run staging_qa_run."
                        ),
                    })
                results.append(f"\u2705 Staging commit verified ({_staging_commit[:8]})")
            except Exception as _commit_err:
                # Non-fatal: warn but continue QA
                results.append(f"\u26a0\ufe0f Could not verify staging commit: {_commit_err}")
        except Exception as e:
            return _text_response({
                "passed": False,
                "error": f"Staging unreachable: {e}. Start staging or use override=true.",
            })

        # Wait for any in-progress deploy to finish before running QA.
        # QA runs do NOT hold the lock themselves — multiple parallel QA
        # sessions are allowed as long as no deploy is happening.
        # Scenarios are isolated by unique per-run chat ID prefixes.
        if _staging_deploy_lock.locked():
            try:
                await asyncio.wait_for(_staging_deploy_lock.acquire(), timeout=180)
                _staging_deploy_lock.release()  # Release immediately — just waited for deploy to finish
            except asyncio.TimeoutError:
                return _text_response({
                    "passed": False,
                    "error": "Staging deploy in progress and has not finished in 3 min. "
                             "Try again when deploy_staging completes.",
                })

        # Step 2: Clean up stale QA sessions from previous runs.
        try:
            cleanup = await client.post(
                f"{STAGING_URL}/test/reset-clients",
                headers=headers,
                json={"prefix": "qa_"},
            )
            cleanup_data = cleanup.json()
            cleaned = cleanup_data.get("count", 0)
            if cleaned:
                results.append(f"\U0001f9f9 Cleaned {cleaned} stale QA session(s)")
                await asyncio.sleep(1)
        except Exception:
            pass

        # Step 3: Run scenarios IN PARALLEL — each gets its own isolated chat session.
        # Per-scenario chat IDs (qa_<ts>_<i>) ensure complete isolation:
        #   - No admin state leaks between scenarios (separate SDK sessions)
        #   - Parallel execution: all scenarios fire concurrently, total time = max(individual)
        #   - Kill-on-timeout: each scenario resets its own session on failure to free resources
        # All IDs share a run-level prefix (qa_<ts>) so Step 4 can sweep them all clean.
        import time as _time_inner
        qa_run_ts = int(_time_inner.time())
        qa_chat_prefix = f"qa_{qa_run_ts}"
        _n_scenarios = len(scenarios)

        async def _run_one(i: int, scenario: dict) -> tuple:
            """Run one scenario in isolation. Returns (ok, name, fail_reasons, tool_names).
            ok=None means skipped; ok=True means passed; ok=False means failed.
            """
            qa_chat_id = f"{qa_chat_prefix}_{i}"
            if not isinstance(scenario, dict):
                return (None, f"scenario_{i}", ["not a dict"], [])
            name = scenario.get("name", f"scenario_{i}")
            if scenario.get("skip"):
                return (None, name, ["marked skip"], [])
            text = scenario.get("input", "")
            if not text:
                return (None, name, ["no input text"], [])

            is_admin = scenario.get("is_admin", False)
            timeout = min(scenario.get("timeout", STAGING_QA_TIMEOUT), 900)
            expect_tools = scenario.get("expect_tools", [])
            expect_no_tools = scenario.get("expect_no_tools", [])
            expect_text_contains = scenario.get("expect_text_contains", [])
            expect_text_not_contains = scenario.get("expect_text_not_contains", [])
            expect_text_match_any = scenario.get("expect_text_match_any", False)

            # Clear any stale responses from a prior inject on this chat_id
            try:
                await client.delete(
                    f"{STAGING_URL}/test/responses/{qa_chat_id}", headers=headers
                )
            except Exception:
                pass

            async def _kill_session() -> None:
                """Kill this scenario's staging session to free resources immediately."""
                try:
                    await client.post(
                        f"{STAGING_URL}/test/reset-clients",
                        headers=headers,
                        json={"prefix": qa_chat_id},
                    )
                    log.debug("QA kill_session: reset client %s", qa_chat_id)
                except Exception:
                    pass

            try:
                inject_body: dict[str, Any] = {
                    "text": text,
                    "chat_id": qa_chat_id,
                    "is_admin": is_admin,
                    "timeout": timeout,
                }
                # Pass harness config from scenario to staging inject.
                # harness_label assigns the harness; harness_config auto-creates
                # it if missing on this staging instance (harnesses.json is
                # per-volume, not git-tracked).
                if scenario.get("harness_label"):
                    inject_body["harness_label"] = scenario["harness_label"]
                if scenario.get("harness_config"):
                    inject_body["harness_config"] = scenario["harness_config"]

                resp = await client.post(
                    f"{STAGING_URL}/test/inject",
                    headers=headers,
                    json=inject_body,
                )
                data = resp.json()
                status = data.get("status", "")
                responses = data.get("responses", [])

                # Kill the staging session immediately on timeout or error —
                # don't leave it running and blocking staging resources.
                if status in ("timeout", "error"):
                    await _kill_session()

                tool_names = [r.get("tool", "") for r in responses]
                all_response_text = " ".join(
                    r.get("args", {}).get("text", "") for r in responses
                ).lower()

                ok = True
                fail_reasons: list[str] = []

                if status == "timeout":
                    ok = False
                    fail_reasons.append("timeout (no response)")
                elif status == "error":
                    ok = False
                    fail_reasons.append(f"staging error: {data.get('error', 'unknown')}")
                elif not responses:
                    ok = False
                    fail_reasons.append("empty response")

                for et in expect_tools:
                    if et not in tool_names:
                        ok = False
                        fail_reasons.append(f"expected tool {et} not called")

                for nt in expect_no_tools:
                    if nt in tool_names:
                        ok = False
                        fail_reasons.append(f"blocked tool {nt} was called")

                if expect_text_contains and all_response_text:
                    if expect_text_match_any:
                        if not any(pat.lower() in all_response_text for pat in expect_text_contains):
                            ok = False
                            fail_reasons.append(f"response missing any of: {expect_text_contains}")
                    else:
                        for pat in expect_text_contains:
                            if pat.lower() not in all_response_text:
                                ok = False
                                fail_reasons.append(f"response missing text: '{pat}'")

                for pat in expect_text_not_contains:
                    if pat.lower() in all_response_text:
                        ok = False
                        fail_reasons.append(f"response contains blocked text: '{pat}'")

                return (ok, name, fail_reasons, tool_names)

            except Exception as e:
                # Crash or network error — kill the session and report
                await _kill_session()
                return (False, name, [f"error: {type(e).__name__}: {e}"], [])

        # Fire all scenarios concurrently
        scenario_coros = [_run_one(i, s) for i, s in enumerate(scenarios)]
        raw_results = await asyncio.gather(*scenario_coros, return_exceptions=True)

        # Collect results and send per-scenario progress updates
        for i, r in enumerate(raw_results):
            scenario = scenarios[i] if i < len(scenarios) else {}
            if isinstance(r, BaseException):
                failed += 1
                _err_name = scenario.get("name", f"scenario_{i}") if isinstance(scenario, dict) else f"scenario_{i}"
                results.append(f"\u274c {_err_name}: internal error \u2014 {r}")
                continue

            ok, name, fail_reasons, tool_names = r
            if ok is None:
                # Skipped scenario
                _skip_reason = fail_reasons[0] if fail_reasons else "skip"
                if _skip_reason == "marked skip":
                    results.append(f"\u23ed\ufe0f {name}: skipped (marked skip)")
                    skipped += 1
                else:
                    results.append(f"SKIP {name}: {_skip_reason}")
                    skipped += 1
                continue

            if ok:
                passed += 1
                results.append(f"\u2705 {name}: passed (tools: {tool_names})")
            else:
                failed += 1
                results.append(f"\u274c {name}: {', '.join(fail_reasons)}")

            # Per-scenario progress removed (noisy) — final summary in tool result

        # Step 4: Clean up — disconnect all per-scenario QA sessions
        try:
            await client.post(
                f"{STAGING_URL}/test/reset-clients",
                headers=headers,
                json={"prefix": qa_chat_prefix},
            )
        except Exception:
            pass

    # Step 5: Set or block the gate flag (outside the httpx client context)
    total = passed + failed + skipped
    report = {
        "total": total, "passed": passed, "failed": failed, "skipped": skipped,
        "details": results,
    }

    if failed == 0 and passed > 0:
        from bot.hooks import mark_qa_staging_passed
        mark_qa_staging_passed(session_key=_qa_session_key)
        results.append(f"\u2705 Staging QA PASSED ({passed}/{total}) \u2014 deploy_bot unblocked")
        report["gate_set"] = True
    else:
        results.append(
            f"\u274c Staging QA FAILED ({failed} failures, {passed} passed, {skipped} skipped) "
            "\u2014 fix issues and re-run"
        )
        report["gate_set"] = False

    return _text_response(report)


# ═══════════════════════════════════════════════════════════════════════
# PIPELINE ORCHESTRATOR — Independent gate verification
# ═══════════════════════════════════════════════════════════════════════

@tool("pipeline_orchestrate", "Run the Pipeline Orchestrator — independent verification of coding pipeline stages. Mechanically verifies TCD gate completeness and agent evidence, then runs an independent AI code review (separate LLM invocation). Returns PASS/FAIL. PASS sets the orchestrator gate required for deploy. Call this AFTER completing all pipeline stages (implementation + reviews).", {
    "type": "object",
    "properties": {
        "ticket_id": {
            "type": ["integer", "string"],
            "description": "PM ticket ID to verify",
        },
    },
    "required": ["ticket_id"],
})
async def pipeline_orchestrate(args: dict[str, Any]) -> dict:
    """Pipeline orchestrator MCP tool — runs entirely server-side."""
    from bot.pm.orchestrator import run_pipeline_gate
    from bot.core.session_state import set_orchestrator_approval

    try:
        ticket_id = int(args["ticket_id"])
    except (ValueError, TypeError):
        return _text_response({"error": "ticket_id must be an integer"})

    _sk = args.pop("_session_key", "")

    log.info("Pipeline orchestrator starting for ticket %s (session=%s)", ticket_id, _sk)
    result = await run_pipeline_gate(ticket_id)

    if result.get("passed") and result.get("approval_token"):
        # Set the HMAC-signed approval in session state — only this tool can do this
        set_orchestrator_approval(_sk, result["approval_token"])
        log.info("Pipeline orchestrator PASSED for ticket %s — approval token set", ticket_id)

        # Auto-write the orchestrator gate to the ticket's TCD.
        # This prevents the model from having to manually add it — the commit hook
        # checks for this gate and blocks without it.
        try:
            from bot.pm.store import get_ticket, update_ticket
            import json as _json_orc
            _ticket = await get_ticket(ticket_id)
            if _ticket and _ticket.get("tcd"):
                _tcd = _json_orc.loads(_ticket["tcd"]) if isinstance(_ticket["tcd"], str) else _ticket["tcd"]
                _gates = _tcd.setdefault("pipeline_gates", {})
                _gates["orchestrator"] = {
                    "status": "passed",
                    "notes": f"Auto-set by pipeline_orchestrate, commit {result.get('commit_hash', '?')[:7]}",
                    "agent_id": "",
                }
                await update_ticket(ticket_id, tcd=_json_orc.dumps(_tcd))
                log.info("Orchestrator gate auto-written to TCD for ticket %s", ticket_id)
        except Exception as _orc_err:
            log.warning("Failed to auto-write orchestrator gate to TCD: %s", _orc_err)
    elif not result.get("passed"):
        log.info("Pipeline orchestrator FAILED for ticket %s", ticket_id)

    # Format human-readable response
    lines = []
    if result.get("error"):
        lines.append(f"❌ ERROR: {result['error']}")
        return _text_response({"passed": False, "error": result["error"]})

    mech = result.get("mechanical", {})
    ai = result.get("ai_review", {})

    lines.append(f"{'✅' if result['passed'] else '❌'} Pipeline Orchestrator: {'PASSED' if result['passed'] else 'FAILED'}")
    lines.append(f"Commit: {result.get('commit_hash', '?')[:7]}")
    lines.append("")

    # Mechanical results
    if mech.get("passed"):
        lines.append("✅ Mechanical verification: PASSED")
    else:
        lines.append("❌ Mechanical verification: FAILED")
        for f in mech.get("findings", []):
            lines.append(f"  • {f}")

    # AI review results
    verdict = ai.get("verdict", "?")
    lines.append(f"\n{'✅' if verdict == 'PASS' else '⏭️' if verdict == 'SKIP' else '❌'} AI Review: {verdict}")
    if ai.get("summary"):
        lines.append(f"  {ai['summary']}")
    for f in ai.get("findings", []):
        if isinstance(f, dict):
            sev = f.get("severity", "?")
            desc = f.get("description", "?")
            file = f.get("file", "")
            lines.append(f"  [{sev}] {desc}" + (f" ({file})" if file else ""))

    if result["passed"]:
        lines.append("\n🔓 Orchestrator gate APPROVED — deploy is unblocked.")
    else:
        lines.append("\n🔒 Fix the issues above and call pipeline_orchestrate again.")

    return _text_response({
        "passed": result["passed"],
        "mechanical_passed": mech.get("passed", False),
        "ai_verdict": verdict,
        "findings_count": len(mech.get("findings", [])) + len(ai.get("findings", [])),
        "details": "\n".join(lines),
    })


async def _create_pm_deploy_record(action: str, reason: str) -> dict[str, Any]:
    """Create a pm_deploys record and link tickets found via Ticket: trailers.

    Parses git log for the commit range (last deploy..HEAD) and extracts
    Ticket: PM-XX trailers. Non-blocking — failures are logged but never
    block the deploy itself.
    """
    import re as _re_pm

    from config import GIT_COMMIT

    # Determine commit range: last healthy → HEAD
    commit_from = GIT_COMMIT or ""
    try:
        head_result = await _arun(
            ["git", "rev-parse", "HEAD"],
            timeout=5,
        )
        commit_to = head_result.stdout.strip()
    except Exception:
        commit_to = "unknown"

    if not commit_from or commit_from == commit_to:
        return {"skipped": True, "reason": "no commit range (restart or same commit)"}

    # Parse Ticket: trailers from commit messages in range
    try:
        log_result = await _arun(
            ["git", "log", "--format=%B---END---", f"{commit_from}..{commit_to}"],
            timeout=10,
        )
        trailer_re = _re_pm.compile(r"Ticket:\s*PM-(\d+)", _re_pm.IGNORECASE)
        ticket_ids = sorted({int(m) for m in trailer_re.findall(log_result.stdout)})
    except Exception as e:
        log.warning("Failed to parse git trailers: %s", e)
        ticket_ids = []

    # Find active project for bot
    from bot.pm.store import get_or_create_project, create_deploy, link_tickets_to_deploy

    project = await get_or_create_project(
        chat_key="default",
        name="Corporate Bot",
        brief="Primary bot project",
    )

    deploy = await create_deploy(
        project_id=project["id"],
        commit_from=commit_from,
        commit_to=commit_to,
        action=action,
        reason=reason,
    )

    linked = 0
    if ticket_ids:
        linked = await link_tickets_to_deploy(deploy["id"], ticket_ids)

    return {
        "deploy_id": deploy["id"],
        "commit_range": f"{commit_from[:8]}..{commit_to[:8]}",
        "tickets_linked": ticket_ids[:20] if ticket_ids else [],
        "linked_count": linked,
    }


@tool("deploy_bot", "Trigger a Docker rebuild and restart of the bot. Writes a trigger file that the host watcher picks up. Actions: rebuild (default), restart (no rebuild), rebuild-all (all services).", {
    "action": {"type": "string", "description": "Action: rebuild, restart, or rebuild-all"},
    "reason": {"type": "string", "description": "Why this deploy is needed (shown in logs)"},
})
async def deploy_bot(args: dict[str, Any]) -> dict:
    """Trigger a Docker deploy via host watcher.

    Pre-deploy gates (for rebuild/rebuild-all):
    1. Syntax check all .py files in /host-repo
    2. Verify no uncommitted changes (everything must be committed)
    3. Verify commits are pushed to remote
    4. Run integration tests if available
    Only 'restart' skips these checks (no code changes involved).
    """
    active_key = args.pop("_session_key", "")
    from bot.hooks import get_current_user_ctx
    ctx = get_current_user_ctx(session_key=active_key) or {}
    from config import GIT_COMMIT
    log.warning(
        f"DEPLOY_TRIGGERED: user_id={ctx.get('user_id')} source={ctx.get('source')} "
        f"action={args.get('action')} reason={args.get('reason')} "
        f"current_commit={GIT_COMMIT}"
    )

    import glob as glob_mod
    from datetime import datetime

    action = args.get("action", "rebuild")
    reason = args.get("reason", "bot self-upgrade")

    if action not in ("rebuild", "restart", "rebuild-all"):
        return _text_response({"error": f"Unknown action: {action}. Use: rebuild, restart, rebuild-all"})

    # Use /deploy-triggers/ volume — shared between bot container and host watcher
    deploy_dir = Path("/deploy-triggers")
    trigger_path = deploy_dir / "deploy_trigger.json"
    result_path = deploy_dir / "deploy_result.json"

    # Check if a deploy is already in progress
    if trigger_path.exists():
        return _text_response({"error": "Deploy already triggered — waiting for host watcher to pick it up"})
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text())
            if result.get("status") == "in_progress":
                return _text_response({"status": "in_progress", "message": "Deploy is running on host..."})
        except Exception:
            pass

    # === PRE-DEPLOY GATES (skip for restart — no code changes) ===
    qa_results: list[str] = []

    # Check /deploy force flag — skip internal gates when set
    # (hooks already bypassed by force; this covers deploy_bot's own gates)
    from bot.core.session_state import _get_or_create_state
    _force = _get_or_create_state(active_key).get("deploy_force_flag", False)

    if action in ("rebuild", "rebuild-all") and not _force:
        # Gate 1: Syntax check all Python files
        py_files = glob_mod.glob("/host-repo/**/*.py", recursive=True)
        syntax_errors = []
        for py_file in py_files:
            try:
                with open(py_file) as f:
                    import ast
                    ast.parse(f.read(), filename=py_file)
            except SyntaxError as e:
                syntax_errors.append(f"{py_file}:{e.lineno} \u2014 {e.msg}")
        if syntax_errors:
            return _text_response({
                "error": "DEPLOY BLOCKED \u2014 syntax errors found",
                "syntax_errors": syntax_errors,
                "hint": "Fix these syntax errors before deploying.",
            })
        qa_results.append(f"\u2705 Syntax check passed ({len(py_files)} files)")

        # Gate 1b: Import check — catches NameError/ImportError that syntax check misses
        # Runs `python3 -c "import <module>"` for each top-level bot module
        import_errors = []
        bot_modules = [
            "config", "bot.agent", "bot.hooks", "bot.mcp_tools",
            "bot.storage.memory", "bot.storage.rag", "bot.prompts", "bot.scheduler",
            "bot.storage.media", "bot.storage.db", "bot.mcp_config",
            "bot.desktop_relay", "bot.relay.pool",
            "bot.md_to_tg_html", "bot.mcp_size_guard",
            "integrations.telegram",
            "integrations.gemini", "integrations.phone",
        ]
        for mod in bot_modules:
            try:
                result = await _arun(
                    ["python3", "-c", f"import {mod}"],
                    timeout=15,
                    env={**os.environ, "PYTHONPATH": "/host-repo"},
                )
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or "").strip()
                    # Extract just the last line (the actual error)
                    err_line = err.split("\n")[-1] if err else "unknown error"
                    import_errors.append(f"{mod}: {err_line}")
            except subprocess.TimeoutExpired:
                import_errors.append(f"{mod}: timeout")
            except Exception as e:
                import_errors.append(f"{mod}: {e}")
        if import_errors:
            return _text_response({
                "error": "DEPLOY BLOCKED \u2014 import errors found",
                "import_errors": import_errors,
                "hint": "Fix these import/NameError issues before deploying.",
            })
        qa_results.append(f"\u2705 Import check passed ({len(bot_modules)} modules)")

        # Gate 2: No uncommitted changes
        try:
            status = await _arun(
                ["git", "status", "--porcelain"],
                timeout=10,
            )
            if status.stdout.strip():
                return _text_response({
                    "error": "DEPLOY BLOCKED \u2014 uncommitted changes detected",
                    "uncommitted": status.stdout.strip(),
                    "hint": "Commit and push all changes before deploying.",
                })
            qa_results.append("\u2705 Working tree clean (all changes committed)")
        except Exception as e:
            qa_results.append(f"\u26a0\ufe0f Git status check failed: {e}")

        # Gate 3: All commits pushed to remote
        try:
            ahead = await _arun(
                ["git", "rev-list", "--count", "HEAD", "--not", "--remotes"],
                timeout=10,
            )
            unpushed = int(ahead.stdout.strip() or "0")
            if unpushed > 0:
                return _text_response({
                    "error": f"DEPLOY BLOCKED \u2014 {unpushed} unpushed commit(s)",
                    "hint": "Push to remote before deploying: git push origin main",
                })
            qa_results.append("\u2705 All commits pushed to remote")
        except Exception as e:
            qa_results.append(f"\u26a0\ufe0f Git push check failed: {e}")

        # Gate 4: Smoke tests (HARD BLOCK — catch import ordering, typos, missing params)
        smoke_files = [
            Path("/host-repo/tests/test_smoke_commands.py"),
            Path("/host-repo/tests/test_smoke_mcp.py"),
        ]
        existing_smoke = [f for f in smoke_files if f.exists()]
        if existing_smoke:
            try:
                smoke_result = await _arun(
                    ["python3", "-m", "pytest"] + [str(f) for f in existing_smoke] + ["-x", "-q", "--tb=short"],
                    timeout=120,
                )
                if smoke_result.returncode == 0:
                    qa_results.append(f"\u2705 Smoke tests passed ({len(existing_smoke)} files)")
                else:
                    return _text_response({
                        "error": "DEPLOY BLOCKED \u2014 smoke tests failed",
                        "output": smoke_result.stdout[-1000:],
                    })
            except subprocess.TimeoutExpired:
                qa_results.append("\u26a0\ufe0f Smoke tests timed out (120s)")
            except Exception as e:
                qa_results.append(f"\u26a0\ufe0f Smoke tests error: {e}")

        # Gate 5: Integration tests (non-blocking — warn but don't block)
        test_file = Path("/host-repo/tests/test_integration.py")
        if test_file.exists():
            try:
                test_result = await _arun(
                    ["python3", "-m", "pytest", str(test_file), "-x", "-q", "--tb=short"],
                    timeout=60,
                )
                if test_result.returncode == 0:
                    qa_results.append("\u2705 Integration tests passed")
                else:
                    # Tests failing is a warning, not a hard block
                    qa_results.append(f"\u26a0\ufe0f Integration tests failed:\n{test_result.stdout[-500:]}")
            except subprocess.TimeoutExpired:
                qa_results.append("\u26a0\ufe0f Integration tests timed out (60s)")
            except Exception as e:
                qa_results.append(f"\u26a0\ufe0f Integration tests error: {e}")

        log.info(f"deploy_bot pre-deploy QA: {'; '.join(qa_results)}")
    elif _force and action in ("rebuild", "rebuild-all"):
        qa_results.append("\u26a0\ufe0f All internal gates skipped (/deploy force)")
        log.warning("deploy_bot: /deploy force — skipping all internal pre-deploy gates")

    # Write deploy_pending marker — startup_recovery uses this to avoid re-queuing
    # the deploy task after restart (prevents deploy loop)
    deploy_marker = Path("/app/data/deploy_pending")
    deploy_marker.write_text(action)

    # --- PM deploy↔ticket linkage ---
    pm_deploy_info: dict[str, Any] = {}
    try:
        pm_deploy_info = await _create_pm_deploy_record(action, reason)
    except Exception as e:
        log.warning("PM deploy record creation failed (non-blocking): %s", e)
        pm_deploy_info = {"warning": f"PM linkage skipped: {e}"}

    # Write trigger
    trigger = {
        "action": action,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
        "qa_results": qa_results,
    }
    tmp_path = trigger_path.with_suffix('.tmp')
    tmp_path.write_text(json.dumps(trigger, indent=2))
    tmp_path.rename(trigger_path)
    log.info(f"deploy_bot triggered: action={action} reason={reason}")

    # Brief poll (15s) to detect pending_approval early — gives immediate feedback
    _result_msg = f"Deploy trigger written. Host watcher will {action} bot-core. Bot will restart in ~30-120s."
    result_path = Path("/deploy-triggers") / "deploy_result.json"
    for _poll in range(3):
        await asyncio.sleep(5)
        if result_path.exists():
            try:
                _poll_result = json.loads(result_path.read_text())
                _poll_status = _poll_result.get("status", "")
                if _poll_status == "pending_approval":
                    _sensitive = _poll_result.get("sensitive_files", [])
                    _files_str = ", ".join(_sensitive[:5]) if _sensitive else "infrastructure files"
                    _result_msg = (
                        f"\u26a0\ufe0f Deploy PAUSED \u2014 sensitive files detected ({_files_str}). "
                        f"Host-side approval required. Run approve-deploy.sh on the server.\n"
                        f"This shows the diff and asks for confirmation."
                    )
                    break
                elif _poll_status not in ("in_progress", "pending", ""):
                    break  # terminal status reached
            except Exception:
                pass

    return _text_response({
        "status": "triggered",
        "action": action,
        "reason": reason,
        "qa_results": qa_results,
        "pm_deploy": pm_deploy_info,
        "message": _result_msg,
    })


@tool("deploy_status", "Check the status of the last deploy triggered by deploy_bot. Includes failed startup logs if a rollback occurred.", {})
async def deploy_status(args: dict[str, Any]) -> dict:
    """Check deploy status from the result file + bot uptime context."""

    deploy_dir = Path("/deploy-triggers")
    # last_failed_startup.log is written by host-watcher to the deploy/ dir
    # (parent of triggers/), mapped to /host-repo/deploy/ in bot-core
    failed_log_path = Path("/host-repo/deploy/last_failed_startup.log")
    result_path = deploy_dir / "deploy_result.json"
    trigger_path = deploy_dir / "deploy_trigger.json"

    # Include bot startup time for context (detects recent restarts from deploy)
    from config import BOT_STARTED
    bot_started_str = BOT_STARTED.isoformat() if BOT_STARTED else "unknown"
    uptime_info = {"bot_started": bot_started_str}

    if trigger_path.exists():
        try:
            trigger = json.loads(trigger_path.read_text())
            return _text_response({"status": "pending", "trigger": trigger, **uptime_info,
                                   "message": "Trigger written, waiting for host watcher..."})
        except Exception:
            return _text_response({"status": "pending", **uptime_info,
                                   "message": "Trigger written, waiting for host watcher..."})

    if not result_path.exists():
        return _text_response({"status": "no_deploy", **uptime_info,
                               "message": "No recent deploy found. If a deploy was just triggered, "
                               "the watcher may still be processing — check again in 30s."})

    # Check result freshness — if result was written recently but status is not terminal,
    # a deploy might still be in progress (trigger consumed, build running)

    try:
        result = json.loads(result_path.read_text())
        result.update(uptime_info)
        # Detect stale "in_progress" — if result says in_progress but file is old (>5 min),
        # it might be a stuck deploy
        if result.get("status") == "in_progress":
            try:
                result_age = _time.time() - result_path.stat().st_mtime
                if result_age > 300:  # >5 min
                    result["warning"] = f"Deploy has been 'in_progress' for {int(result_age)}s — may be stuck"
            except OSError:
                pass

        # Mac-sync reminder: if deploy touched scripts/desktop-relay/*, remind
        # operator to manually sync guard.sh/daemon to Mac (FB-29).
        if result.get("status") == "success":
            try:
                from config import GIT_COMMIT
                deployed = result.get("commit", "")[:12]
                running = (GIT_COMMIT or "")[:12]
                if deployed and running and deployed != running:
                    # Check if deployed commit range touched relay scripts
                    diff_r = await _arun(
                        ["git", "-C", "/host-repo", "diff", "--name-only",
                         f"{running}..{deployed}", "--", "scripts/desktop-relay/"],
                        timeout=5,
                    )
                    changed = [f.strip() for f in diff_r.stdout.strip().splitlines() if f.strip()]
                    if changed:
                        result["mac_sync_needed"] = changed
                        result["mac_sync_hint"] = (
                            "⚠️ This deploy includes Mac-side script changes. "
                            "Sync to Mac: scp <file> mac:~/.relay/"
                        )
            except Exception:
                pass  # non-blocking

        # Attach failed startup logs for rolled-back or unhealthy deploys
        if result.get("status") in ("rolled_back", "unhealthy", "rollback_unhealthy"):
            if failed_log_path.exists():
                try:
                    raw = failed_log_path.read_text(errors="replace")
                    # Cap at 8000 chars to avoid blowing up context
                    if len(raw) > 8000:
                        raw = raw[:4000] + "\n\n... (truncated) ...\n\n" + raw[-4000:]
                    result["failed_startup_logs"] = raw
                except Exception as e:
                    result["failed_startup_logs_error"] = str(e)
            else:
                result["failed_startup_logs"] = "(no log captured — host-watcher may need updating)"

        return _text_response(result)
    except Exception as e:
        return _text_response({"error": f"Cannot read result: {e}", **uptime_info})


# ============================================================
# STAGING COORDINATION — prevents deploy races, allows parallel QA.
#
# _staging_deploy_lock (asyncio.Lock) is held exclusively by deploy_staging()
# for the full rebuild cycle (trigger write → watcher result, max 5 min).
# This prevents two sessions from racing to rebuild staging simultaneously.
#
# staging_qa_run() does NOT hold the lock. It only peeks: if a deploy is
# in progress, it waits for it to finish (max 3 min) before proceeding.
# Multiple parallel QA runs are allowed — they use unique per-run chat
# ID prefixes (qa_<ts>_<i>) for full isolation of sessions and responses.
#
# _staging_last_deployed_commit: set on successful deploy. Lets the next
# deploy_staging caller skip a rebuild if staging is already at HEAD.
# ============================================================

_staging_deploy_lock = asyncio.Lock()
_staging_last_deployed_commit: str = ""  # HEAD sha at last successful staging rebuild

# Staging repo path — bind-mounted from host for git sync.
# Prod bot syncs this repo before triggering the staging watcher, ensuring
# the staging clone is always at the same commit as prod's /host-repo.
_STAGING_REPO = Path("/staging-repo")


async def _sync_staging_repo() -> tuple[bool, str]:
    """Sync the staging repo to match prod's current HEAD.

    Returns (success, message). If staging repo is not mounted, returns
    (True, "not mounted") — falls back to watcher's own git pull.
    """
    if not _STAGING_REPO.exists() or not (_STAGING_REPO / ".git").exists():
        return True, "staging repo not mounted — watcher will git pull"

    prod_head = await _git_head("/host-repo")
    if not prod_head:
        return False, "could not determine prod HEAD"

    # Check if already at the right commit
    staging_head = await _git_head(str(_STAGING_REPO))
    if staging_head == prod_head:
        return True, f"already at {prod_head[:8]}"

    # Fetch + reset to match prod
    try:
        r = await _arun(
            ["git", "fetch", "origin"],
            cwd=str(_STAGING_REPO), timeout=30,
        )
        if r.returncode != 0:
            return False, f"git fetch failed: {r.stderr.strip()}"

        r = await _arun(
            ["git", "reset", "--hard", "origin/main"],
            cwd=str(_STAGING_REPO), timeout=10,
        )
        if r.returncode != 0:
            return False, f"git reset failed: {r.stderr.strip()}"

        new_head = await _git_head(str(_STAGING_REPO))
        if new_head[:12] == prod_head[:12]:
            log.info(f"_sync_staging_repo: synced to {prod_head[:8]}")
            return True, f"synced to {prod_head[:8]}"
        else:
            return False, f"sync mismatch: expected {prod_head[:8]}, got {new_head[:8]}"
    except subprocess.TimeoutExpired:
        return False, "git sync timed out"
    except Exception as e:
        return False, f"sync error: {e}"


async def _git_head(cwd: str = "/host-repo") -> str:
    """Return current HEAD sha, or '' on failure."""
    try:
        r = await _arun(
            ["git", "rev-parse", "HEAD"], cwd=cwd,
            timeout=10,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _staging_trigger_dir():
    """Staging trigger dir: /deploy-triggers volume (staging) or /host-repo/deploy/staging-triggers (prod).

    CRITICAL: Must check IS_STAGING, not path existence — /deploy-triggers exists in prod too
    (it's the prod watcher's bind mount). Using is_dir() caused deploy_staging to write to the
    prod trigger dir, rebuilding prod instead of staging.
    """
    from config import IS_STAGING
    if IS_STAGING:
        return Path("/deploy-triggers")
    return Path("/host-repo/deploy/staging-triggers")


@tool("deploy_staging", "Rebuild and restart the staging container. Admin only. Writes a trigger file that the staging-watcher picks up. Actions: rebuild (default), restart. No deploy gates needed — staging IS the QA target. For rebuild: acquires a cross-session lock so concurrent coding sessions don't race; if staging is already at the current HEAD commit, rebuild is skipped automatically.", {
    "action": {"type": "string", "description": "rebuild (default) or restart", "default": "rebuild"},
    "reason": {"type": "string", "description": "Why this staging deploy is needed"},
})
async def deploy_staging(args: dict[str, Any]) -> dict:
    """Trigger a staging container rebuild via the staging-watcher.

    For 'rebuild': serializes concurrent callers via _staging_deploy_lock so only
    one session deploys at a time. Once released, waiting sessions check if staging
    is already at the current HEAD commit — if so, they skip the rebuild and return
    'skipped' so they can proceed straight to staging_qa_run.
    For 'restart': no locking (no code changes involved).
    """
    import glob as glob_mod
    import ast
    from datetime import datetime

    action = args.get("action", "rebuild")
    reason = args.get("reason", "staging update")

    if action not in ("rebuild", "restart"):
        return _text_response({"error": f"Unknown action: {action}. Use: rebuild, restart"})

    staging_trigger_dir = _staging_trigger_dir()
    if not staging_trigger_dir.exists():
        staging_trigger_dir.mkdir(parents=True, exist_ok=True)

    trigger_path = staging_trigger_dir / "deploy_trigger.json"
    result_path = staging_trigger_dir / "deploy_result.json"

    # restart: no lock needed, no commit tracking
    if action == "restart":
        trigger = {"action": action, "reason": reason, "timestamp": datetime.now().isoformat()}
        tmp_path = trigger_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(trigger, indent=2))
        tmp_path.rename(trigger_path)
        log.info(f"deploy_staging restart triggered: {reason}")
        return _text_response({
            "status": "triggered", "action": "restart",
            "message": "Staging restart trigger written. Watcher will restart staging in ~15-30s.",
        })

    # rebuild: acquire cross-session lock (10-min timeout)
    global _staging_last_deployed_commit
    try:
        await asyncio.wait_for(_staging_deploy_lock.acquire(), timeout=600)
    except asyncio.TimeoutError:
        return _text_response({
            "status": "busy",
            "error": "Staging deploy lock timeout — another session has held staging for >10 min.",
            "hint": "Check deploy_staging_status for details.",
        })

    try:
        current_head = await _git_head()

        # Skip rebuild if staging is already at this commit (another session just deployed)
        if current_head and current_head == _staging_last_deployed_commit:
            log.info(f"deploy_staging: skipping rebuild — staging already at HEAD {current_head[:8]}")
            return _text_response({
                "status": "skipped",
                "commit": current_head[:8],
                "message": "Staging is already at current HEAD — rebuild skipped. Run staging_qa_run directly.",
            })

        qa_results: list[str] = []

        # Sync staging repo to current HEAD (if mounted)
        _sync_ok, _sync_msg = await _sync_staging_repo()
        if _sync_ok:
            qa_results.append(f"\u2705 Staging repo sync: {_sync_msg}")
        else:
            return _text_response({
                "error": f"STAGING DEPLOY BLOCKED \u2014 repo sync failed: {_sync_msg}",
                "hint": "Check staging repo mount and git SSH access.",
            })

        # Syntax check
        py_files = glob_mod.glob("/host-repo/**/*.py", recursive=True)
        syntax_errors = []
        for py_file in py_files:
            try:
                with open(py_file) as f:
                    ast.parse(f.read(), filename=py_file)
            except SyntaxError as e:
                syntax_errors.append(f"{py_file}:{e.lineno} \u2014 {e.msg}")
        if syntax_errors:
            return _text_response({
                "error": "STAGING DEPLOY BLOCKED \u2014 syntax errors",
                "syntax_errors": syntax_errors,
            })
        qa_results.append(f"\u2705 Syntax OK ({len(py_files)} files)")

        # Check uncommitted changes
        try:
            git_status = await _arun(
                ["git", "status", "--porcelain", "--", ".", ":(exclude)deploy/staging-triggers/", ":(exclude)deploy/triggers/"],
                timeout=10,
            )
            if git_status.stdout.strip():
                return _text_response({
                    "error": "STAGING DEPLOY BLOCKED \u2014 uncommitted changes",
                    "uncommitted": git_status.stdout.strip(),
                    "hint": "Commit and push before deploying staging.",
                })
            qa_results.append("\u2705 Working tree clean")
        except Exception as e:
            qa_results.append(f"\u26a0\ufe0f Git status check failed: {e}")

        # Check unpushed commits
        try:
            ahead = await _arun(
                ["git", "rev-list", "--count", "HEAD", "--not", "--remotes"],
                timeout=10,
            )
            unpushed = int(ahead.stdout.strip() or "0")
            if unpushed > 0:
                return _text_response({
                    "error": f"STAGING DEPLOY BLOCKED \u2014 {unpushed} unpushed commit(s)",
                    "hint": "Push to remote first: git push origin main",
                })
            qa_results.append("\u2705 All commits pushed")
        except Exception as e:
            qa_results.append(f"\u26a0\ufe0f Git push check failed: {e}")

        # Write trigger
        trigger = {
            "action": action,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
            "head_commit": current_head[:8] if current_head else "unknown",
        }
        tmp_path = trigger_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(trigger, indent=2))
        tmp_path.rename(trigger_path)
        log.info(f"deploy_staging triggered: action={action} head={current_head[:8] if current_head else '?'} reason={reason}")

        # Poll for watcher result (up to 5 minutes) while holding the lock.
        # This ensures waiting sessions see staging-at-HEAD and skip rebuild.
        final_status = "timeout"
        final_result: dict = {}
        poll_start = _time.time()
        while _time.time() - poll_start < 300:
            await asyncio.sleep(5)
            # Watcher consumes trigger then writes result
            if not trigger_path.exists() and result_path.exists():
                try:
                    final_result = json.loads(result_path.read_text())
                    s = final_result.get("status", "")
                    if s == "pending_approval":
                        final_status = "pending_approval"
                        break
                    if s not in ("in_progress", "pending", ""):
                        final_status = s
                        break
                except Exception:
                    pass

        # Update last-deployed commit only on success + verified commit match
        _commit_verified = False
        if final_status == "success" and current_head:
            # Verify staging is actually running the expected commit
            _result_commit = final_result.get("commit", "")
            if _result_commit and _result_commit[:12] == current_head[:12]:
                _staging_last_deployed_commit = current_head
                _commit_verified = True
                log.info(f"deploy_staging: success, commit verified {current_head[:8]}")
            elif _result_commit:
                # Watcher "succeeded" but deployed wrong commit
                qa_results.append(
                    f"\u26a0\ufe0f Commit mismatch: expected {current_head[:8]}, "
                    f"watcher deployed {_result_commit[:8]}. "
                    f"Staging repo may need manual sync."
                )
                log.warning(
                    f"deploy_staging: commit mismatch expected={current_head[:8]} "
                    f"actual={_result_commit[:8]}"
                )
            else:
                # No commit in result — older watcher format, trust it
                _staging_last_deployed_commit = current_head
                _commit_verified = True
                log.info(f"deploy_staging: success (no commit in result), trusting {current_head[:8]}")
        elif final_status != "success":
            log.warning(f"deploy_staging: finished with status={final_status}")

        return _text_response({
            "status": final_status,
            "action": action,
            "reason": reason,
            "commit": current_head[:8] if current_head else "unknown",
            "commit_verified": _commit_verified,
            "pre_checks": qa_results,
            "watcher_result": final_result,
            "message": (
                f"Staging rebuild complete: {final_status}."
                + (" Commit verified." if _commit_verified else "")
                if final_status != "timeout"
                else "Staging rebuild triggered but watcher result not received within 5 min — check deploy_staging_status."
            ),
        })

    finally:
        _staging_deploy_lock.release()


@tool("deploy_staging_status", "Check the status of the last staging deploy. Includes failed startup logs on build/health failures.", {})
async def deploy_staging_status(args: dict[str, Any]) -> dict:
    """Check staging deploy status."""
    staging_trigger_dir = _staging_trigger_dir()
    trigger_path = staging_trigger_dir / "deploy_trigger.json"
    result_path = staging_trigger_dir / "deploy_result.json"
    failed_log_path = staging_trigger_dir / "staging_last_failed_startup.log"

    if trigger_path.exists():
        try:
            trigger = json.loads(trigger_path.read_text())
            return _text_response({"status": "pending", "trigger": trigger,
                                   "message": "Staging trigger written, waiting for watcher..."})
        except Exception:
            return _text_response({"status": "pending", "message": "Waiting for staging watcher..."})

    if not result_path.exists():
        return _text_response({"status": "no_deploy", "message": "No recent staging deploy found."})

    try:
        result = json.loads(result_path.read_text())
        # Include failed logs on build or health failure
        if result.get("status", "") in ("build_failed", "unhealthy"):
            if failed_log_path.exists():
                try:
                    result["failed_logs"] = failed_log_path.read_text()[-3000:]
                except Exception:
                    pass
        return _text_response(result)
    except Exception as e:
        return _text_response({"error": f"Cannot read staging result: {e}"})


@tool("staging_command", "Run a command on the staging instance via /test/inject. Admin only. Use this to manage staging at runtime — e.g. switch_oauth, manage_oauth, check status. Returns staging bot's response text.", {
    "text": {"type": "string", "description": "Command text to send to staging (e.g. 'switch_oauth personal', 'manage_oauth status')"},
    "timeout": {"type": ["integer", "string"], "description": "Timeout in seconds (default 60)", "default": 60},
    "is_admin": {"type": "boolean", "description": "Run as admin on staging (default true)", "default": True},
})
async def staging_command(args: dict[str, Any]) -> dict:
    """Send an arbitrary command to the staging instance and return the response."""
    import httpx

    args.pop("_session_key", "")
    text = (args.get("text") or "").strip()
    if not text:
        return _text_response({"error": "text is required"})
    try:
        timeout = int(args.get("timeout") or 60)
    except (ValueError, TypeError):
        timeout = 60
    is_admin = args.get("is_admin", True)

    from config import STAGING_URL, STAGING_TEST_TOKEN

    headers = {}
    if STAGING_TEST_TOKEN:
        headers["Authorization"] = f"Bearer {STAGING_TEST_TOKEN}"

    # Check staging is reachable
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            health = await client.get(f"{STAGING_URL}/health", headers=headers)
            if health.status_code != 200:
                return _text_response({"error": f"Staging unreachable (HTTP {health.status_code})"})
    except Exception as exc:
        return _text_response({"error": f"Staging unreachable: {type(exc).__name__}: {exc}"})

    # Inject command
    chat_id = f"staging_cmd_{int(_time.time())}"
    try:
        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.post(
                f"{STAGING_URL}/test/inject",
                json={"text": text, "chat_id": chat_id, "is_admin": is_admin, "timeout": timeout},
                headers=headers,
                timeout=timeout + 10,
            )
            if resp.status_code != 200:
                return _text_response({"error": f"Staging inject failed (HTTP {resp.status_code}): {resp.text[:500]}"})
            data = resp.json()
    except httpx.TimeoutException:
        return _text_response({"error": f"Staging command timed out after {timeout}s"})
    except Exception as exc:
        return _text_response({"error": f"Staging inject error: {type(exc).__name__}: {exc}"})

    # Clean up session
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{STAGING_URL}/test/reset-clients",
                json={"prefix": chat_id},
                headers=headers,
            )
    except Exception as exc:
        log.warning("staging_command cleanup failed for %s: %s", chat_id, exc)

    # Extract response text from staging
    responses = data.get("responses", [])
    response_texts = []
    for r in responses:
        if isinstance(r, dict) and r.get("text"):
            response_texts.append(r["text"])
        elif isinstance(r, str):
            response_texts.append(r)

    return _text_response({
        "status": "ok",
        "command": text,
        "response": "\n---\n".join(response_texts) if response_texts else "(no text response)",
        "raw_responses": responses[:10],
    })


# ============================================================
# Tool list for server registration
# ============================================================

DEPLOY_TOOLS = [
    deploy_bot, deploy_review, staging_qa_run, deploy_status,
    deploy_staging, deploy_staging_status, staging_command,
    pipeline_orchestrate,
]

__all__ = [
    "deploy_bot", "deploy_review", "staging_qa_run", "deploy_status",
    "deploy_staging", "deploy_staging_status", "pipeline_orchestrate",
    "_staging_trigger_dir", "_staging_qa_run_impl",
    "DEPLOY_TOOLS",
]
