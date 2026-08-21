"""Unattended autofix session for the GitHub Actions workflow.

Runs one Claude Agent SDK session that judges drift, re-verifies the root
cause, and writes the fix in the product checkout. The session holds no
GitHub credential and never publishes; scripts/autofix_publish.py does.

Every message the session produces is streamed to the Actions step log as a
collapsible group, which is the only place anyone can watch a run; see the
transcript rendering section below for why bodies are guarded and capped.

Env contract (set by .github/workflows/autofix.yml):
  AUTOFIX_PAYLOAD_FILE   JSON file holding the dispatch client_payload
  AUTOFIX_DRIFT_FILE     unified diff of cited files, release..develop
  AUTOFIX_WORKSPACE      the product checkout the session works in
  AUTOFIX_PROMPT_DIR     directory holding autofix-system.md / autofix-task.md
  CLAUDE_CODE_OAUTH_TOKEN  subscription auth, read by the SDK itself
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

# The statuses the session may report; anything else becomes "failed".
# The session only ever reports one of these four. "verified" never appears
# in receiver.autofix.CALLBACK_STATUSES: scripts/autofix_publish.py is what
# maps a "verified" session result to the callback's "pr_opened", once a PR
# is actually opened. "failed" is likewise produced only by the workflow,
# never by the session. So the real invariant is: RESULT_STATUSES minus
# "verified" equals receiver.autofix.CALLBACK_STATUSES minus
# {"pr_opened", "failed"}.
RESULT_STATUSES = ("verified", "aborted_drift", "not_reproducible", "declined_in_session")

SESSION_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
MAX_TURNS = 150

# Payload fields interpolated into the task prompt. callback_url and
# callback_token are deliberately absent: the session must not know them.
PROMPT_FIELDS = (
    "dispatch_id",
    "sentry_issue_id",
    "sentry_short_id",
    "project",
    "environment",
    "release_sha",
    "cited_files",
)


# --- Transcript rendering -------------------------------------------------
#
# The Actions step log is the only window a human has into this session, so
# every message the SDK yields is streamed there as it arrives. Streamed, not
# buffered into a job summary: the 30-minute job timeout and any crash both
# have to leave the transcript behind, and a buffered one would be lost in
# exactly the runs worth reading.
#
# Every rendered body is untrusted. Assistant text, tool inputs, and tool
# results all derive from findings_md (attacker-influenceable, the same reason
# the prompt fences it) or from the product checkout, so any of them could
# contain a line reading "::add-mask::" or "::error::" and forge a workflow
# command in this repo's log. GitHub's own answer is ::stop-commands::<token>,
# which suspends command parsing until the matching ::<token>:: line. Bodies
# go between that pair; ::group:: and ::endgroup:: stay outside it so the
# groups themselves still register.
STOP_COMMANDS_TOKEN = secrets.token_hex(8)

# A 150-turn session that reads a few large files can emit tens of MB, and
# past its own ceiling GitHub truncates a step log from the end. That costs
# the tail, which is the part usually worth reading, so cap each body here
# instead and say what was dropped.
BODY_MAX_LINES = 40
BODY_MAX_CHARS = 4000
TOOL_INPUT_MAX_CHARS = 2000
TITLE_MAX_CHARS = 100

# Checked in order for the one-line preview that makes a collapsed group
# scannable at a glance ("tool · Bash · pytest -q tests/...").
PREVIEW_KEYS = ("command", "file_path", "path", "pattern", "url", "description")


def _title(text: str) -> str:
    """One safe line for a ::group:: header, which sits outside the guard."""
    flat = " ".join(str(text).split())
    if len(flat) > TITLE_MAX_CHARS:
        flat = flat[: TITLE_MAX_CHARS - 1] + "…"
    return flat.replace("::", ":.:")


def _clip(text, *, max_lines: int = BODY_MAX_LINES, max_chars: int = BODY_MAX_CHARS) -> list:
    """Cap a body to a readable size, noting how much was dropped."""
    raw = str(text)
    lines = raw.splitlines()
    kept = lines[:max_lines]
    body = "\n".join(kept)
    if len(body) > max_chars:
        body = body[:max_chars]
        kept = body.splitlines()
    if len(kept) < len(lines) or len(body) < len(raw):
        kept.append(
            f"… truncated: showing {len(kept)} of {len(lines)} lines, "
            f"{len(body)} of {len(raw)} chars"
        )
    return kept


def _render_tool_use(block) -> tuple:
    name = getattr(block, "name", "?")
    params = getattr(block, "input", None)
    if isinstance(params, dict):
        preview = next((str(params[key]) for key in PREVIEW_KEYS if params.get(key)), "")
        body = json.dumps(params, indent=2, sort_keys=True, default=str)
    else:
        preview, body = "", str(params or "")
    title = f"tool · {name}" + (f" · {preview}" if preview else "")
    return title, _clip(body, max_chars=TOOL_INPUT_MAX_CHARS)


def _render_tool_result(block) -> tuple:
    content = getattr(block, "content", None)
    if isinstance(content, list):
        content = "\n".join(
            (item.get("text") or json.dumps(item, sort_keys=True, default=str))
            if isinstance(item, dict)
            else str(item)
            for item in content
        )
    title = "result"
    # Parallel tool calls arrive as a run of tool_use blocks followed by a run
    # of results, so position alone does not pair them; the id tail does.
    tool_use_id = str(getattr(block, "tool_use_id", "") or "")
    if tool_use_id:
        title += f" · {tool_use_id[-8:]}"
    if getattr(block, "is_error", False):
        title += " · ERROR"
    return title, _clip(content or "")


def _render_blocks(blocks) -> list:
    sections = []
    for block in blocks:
        kind = type(block).__name__
        if kind == "TextBlock":
            sections.append(("assistant", _clip(getattr(block, "text", ""))))
        elif kind == "ThinkingBlock":
            sections.append(("thinking", _clip(getattr(block, "thinking", ""))))
        elif kind == "ToolUseBlock":
            sections.append(_render_tool_use(block))
        elif kind == "ToolResultBlock":
            sections.append(_render_tool_result(block))
        else:
            sections.append((f"block · {kind}", _clip(repr(block))))
    return sections


def render_message(message) -> list:
    """(title, body lines) sections for one SDK message.

    Dispatches on class name rather than isinstance so this module still
    imports without claude-agent-sdk installed: the SDK is a dependency of the
    workflow, not of the tests, and run_session imports it lazily for that
    same reason.
    """
    kind = type(message).__name__
    if kind == "SystemMessage":
        data = getattr(message, "data", None) or {}
        body = json.dumps(data, indent=2, sort_keys=True, default=str)
        return [(f"system · {getattr(message, 'subtype', '')}", _clip(body))]
    if kind in ("AssistantMessage", "UserMessage"):
        content = getattr(message, "content", None)
        if isinstance(content, list):
            return _render_blocks(content)
        return [(kind.replace("Message", "").lower(), _clip(str(content or "")))]
    # ResultMessage, and anything the SDK adds later: run_session already
    # summarizes the result, and an unknown message is not worth guessing at.
    return []


def emit_section(index: int, title: str, body_lines) -> None:
    """Print one collapsible group with command parsing off inside it."""
    print(f"::group::[{index}] {_title(title)}", flush=True)
    if body_lines:
        print(f"::stop-commands::{STOP_COMMANDS_TOKEN}", flush=True)
        try:
            for line in body_lines:
                print(line, flush=True)
        finally:
            # Always re-enable: ::endgroup:: below, and the ::notice:: and
            # ::error:: lines further on, are workflow commands too.
            print(f"::{STOP_COMMANDS_TOKEN}::", flush=True)
    print("::endgroup::", flush=True)


def build_options_kwargs(*, system_prompt_path: Path, workspace: Path) -> dict:
    """ClaudeAgentOptions kwargs, unattended by construction.

    setting_sources=[] is load-bearing: it keeps the product checkout's
    .mcp.json, CLAUDE.md, skills, and hooks out of the session.
    """
    return {
        "system_prompt": {"type": "file", "path": str(system_prompt_path)},
        "allowed_tools": SESSION_TOOLS,
        "disallowed_tools": ["AskUserQuestion"],
        "permission_mode": "dontAsk",
        "setting_sources": [],
        "max_turns": MAX_TURNS,
        "cwd": str(workspace),
    }


def build_task_prompt(*, template_path: Path, payload: dict, drift_path: Path) -> str:
    """Fill the task template. Payload data lands fenced; secrets never do.

    session_token is generated here, after findings_md (untrusted, and
    possibly attacker-controlled) is already fixed, so nothing inside
    findings_md, payload, or drift could have been made to contain it. The
    template uses this token to mark the one Steps list that governs.
    """
    fields = {k: payload.get(k) for k in PROMPT_FIELDS}
    drift = drift_path.read_text() if drift_path.is_file() else "(no drift: cited files unchanged)"
    session_token = secrets.token_hex(8)
    return template_path.read_text().format(
        payload_json=json.dumps(fields, indent=2, sort_keys=True),
        findings_md=str(payload.get("findings_md") or ""),
        drift=drift,
        session_token=session_token,
    )


def finalize_result(result_path: Path) -> str:
    """Normalize .autofix/result.json to a status the workflow can branch on."""
    status = ""
    try:
        parsed = json.loads(result_path.read_text())
    except (OSError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        status = parsed.get("status", "")
    if status not in RESULT_STATUSES:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({"status": "failed"}))
        return "failed"
    return status


async def run_session(task_prompt: str, options_kwargs: dict) -> None:
    """One SDK session, streamed to the step log; exact usage logged for cost."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(**options_kwargs)
    section = 0
    async for message in query(prompt=task_prompt, options=options):
        for title, body in render_message(message):
            section += 1
            emit_section(section, title, body)
        if isinstance(message, ResultMessage):
            print(f"::notice::autofix session ended subtype={message.subtype}", flush=True)
            print(
                json.dumps({"autofix_usage": getattr(message, "usage", None)}, default=str),
                flush=True,
            )


def main() -> int:
    payload = json.loads(Path(os.environ["AUTOFIX_PAYLOAD_FILE"]).read_text())
    workspace = Path(os.environ["AUTOFIX_WORKSPACE"])
    prompt_dir = Path(os.environ["AUTOFIX_PROMPT_DIR"])
    drift_path = Path(os.environ["AUTOFIX_DRIFT_FILE"])
    result_path = workspace / ".autofix" / "result.json"

    task_prompt = build_task_prompt(
        template_path=prompt_dir / "autofix-task.md", payload=payload, drift_path=drift_path
    )
    options_kwargs = build_options_kwargs(
        system_prompt_path=prompt_dir / "autofix-system.md", workspace=workspace
    )

    try:
        asyncio.run(run_session(task_prompt, options_kwargs))
    except Exception as exc:  # noqa: BLE001 - the result file is the contract
        print(f"::error::autofix session crashed: {exc}")

    status = finalize_result(result_path)
    print(f"::notice::autofix result status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
