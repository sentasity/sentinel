"""Shared transcript rendering for the unattended runner sessions.

Every runner in this directory streams one Claude Agent SDK session's
messages into a GitHub Actions step log as it runs, because the step log is
the only window a human has into an unattended session. The rendering has to
guard against the same thing regardless of which runner is streaming it:
message content is untrusted (it derives from whatever the session read, not
from this repository), so it could contain a line that forges a workflow
command. One implementation of that guard is what keeps it from drifting
between runners; this module is that implementation, imported by every
runner that streams a transcript.
"""

from __future__ import annotations

import json
import secrets

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
    workflow, not of the tests, and each runner's own run_session imports it
    lazily for that same reason.
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
