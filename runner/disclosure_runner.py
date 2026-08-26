"""Unattended disclosure and security scan of the public tree.

Runs one read-only Claude Agent SDK session over the files a change touches,
or over the whole tracked tree on a manual full sweep, and reports what a
public reader should not have been able to read.

It complements two deterministic gates rather than replacing either:
tests/test_public_tree.py bans strings someone already thought to ban, and
the secret-scan workflow runs gitleaks over the tree. Both match patterns.
This catches the class of thing neither can express, the clearest case being
a runbook that narrates one real deployment: no single token in it looks like
a credential, and together they are a map.

The session holds no credential beyond the subscription token the SDK reads
for itself, cannot write files, and cannot run commands. Its entire output is
one JSON block carrying a per-session token.

Env contract (set by .github/workflows/disclosure-scan.yml):
  DISCLOSURE_SCOPE           "changed" or "full", framing for the prompt
  DISCLOSURE_FILES_FILE      newline-delimited paths in scope
  DISCLOSURE_DIFF_FILE       the diff under review, empty on a full sweep
  DISCLOSURE_ALLOWLIST_FILE  accepted disclosures, prose
  DISCLOSURE_PROMPT_DIR      directory holding the two prompt files
  DISCLOSURE_WORKSPACE       the checkout the session reads, its cwd
  DISCLOSURE_MODEL           optional model override, empty to use the default
  CLAUDE_CODE_OAUTH_TOKEN    subscription auth, read by the SDK itself
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from pathlib import Path

# The transcript rendering is the same problem in both runners: stream every
# SDK message into the step log as a collapsible group, with workflow-command
# parsing suspended inside each body because the bodies are untrusted. Sharing
# it keeps one implementation of that guard rather than two that drift.
from runner.autofix_runner import emit_section, render_message

# Read-only by construction. The session's job is to look, and a scan that can
# edit the tree it is judging could be talked into editing the finding away.
SCAN_TOOLS = ["Read", "Glob", "Grep"]

# allowed_tools does not, on its own, keep the harness's built-ins out of the
# session. ReportFindings is the one that matters here: it is a tool built for
# exactly this shape of work, so a session handed it will reach for it and
# report into a channel this runner cannot read. The first live run did
# precisely that, ending with a clean review and no result block. It is denied
# by name, and the task template also tells the session that no tool reports
# for it, because the next harness may add another one.
DENIED_TOOLS = [
    "AskUserQuestion",
    "Bash",
    "Edit",
    "ReportFindings",
    "WebFetch",
    "WebSearch",
    "Write",
]
MAX_TURNS = 80

# The only keys read off a finding. Everything else the session emits is
# dropped before anything is printed. This job's log, its step summary, and
# the pull request it annotates are all public, so a value smuggled into an
# "excerpt" or "value" field would republish exactly what the finding is
# complaining about, in a place with no history to rewrite. The prompt says
# not to quote values; this is the half that does not depend on the model
# having listened.
FINDING_KEYS = ("file", "line", "severity", "kind", "title", "why", "fix")

# Severities that do not fail the check. Anything unrecognized is read as
# "high", so a typo or an invented severity can never downgrade a finding.
SOFT_SEVERITIES = ("medium", "low")
FIELD_MAX_CHARS = 300

FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

# The template's substitution markers, matched in one pass. See
# build_task_prompt for why one pass is the whole point.
MARKER_RE = re.compile(r"<<(SCOPE|FILE_LIST|DIFF|ALLOWLIST|SESSION_TOKEN)>>")


def flat(text, limit: int = FIELD_MAX_CHARS) -> str:
    """One capped single-line field, safe to print in the Actions log.

    `::` is neutralized because every rendered field lands on a ::error:: or
    ::warning:: line, outside the ::stop-commands:: guard the transcript uses,
    where a finding could otherwise forge a workflow command.
    """
    value = " ".join(str(text if text is not None else "").split())
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value.replace("::", ":.:")


def build_task_prompt(
    *,
    template_path: Path,
    scope: str,
    files_text: str,
    diff_text: str,
    allowlist_text: str,
    session_token: str,
) -> str:
    """Fill the task template in a single pass.

    One pass is the security property, not a tidiness preference. The content
    being reviewed routinely contains this template's own markers: a diff that
    touches these prompt files carries a literal `<<SESSION_TOKEN>>`. Replacing
    the markers in sequence would substitute into text that an earlier
    substitution had just inserted, stamping the live session token into the
    untrusted region. Content could then close a data block or open a
    `Steps [<token>]:` list of its own, which is precisely the attack the token
    exists to make impossible. `re.sub` never rescans what it wrote, so a
    marker arriving inside the diff stays a literal marker.

    Substitution is by marker rather than str.format because the template
    embeds a JSON example, and every brace in it would have to be doubled to
    survive format. A prompt whose literal text differs from what a reader
    sees in the file is a prompt nobody will keep correct.
    """
    values = {
        "SCOPE": scope,
        "FILE_LIST": files_text.strip() or "(none)",
        "DIFF": diff_text.strip() or "(no diff: this is a full sweep)",
        "ALLOWLIST": allowlist_text.strip() or "(none)",
        "SESSION_TOKEN": session_token,
    }
    return MARKER_RE.sub(lambda match: values[match.group(1)], template_path.read_text())


def build_options_kwargs(*, system_prompt_path: Path, workspace: Path, model: str = "") -> dict:
    """ClaudeAgentOptions kwargs, read-only and unattended by construction.

    setting_sources=[] is load-bearing twice over: it keeps the checkout's
    CLAUDE.md, skills, and hooks out of the session, and the checkout under
    review is exactly where an injected instruction would be waiting.
    """
    kwargs = {
        "system_prompt": {"type": "file", "path": str(system_prompt_path)},
        "allowed_tools": SCAN_TOOLS,
        "disallowed_tools": DENIED_TOOLS,
        "permission_mode": "dontAsk",
        "setting_sources": [],
        "max_turns": MAX_TURNS,
        "cwd": str(workspace),
    }
    if model:
        kwargs["model"] = model
    return kwargs


def extract_result(text: str, session_token: str) -> dict | None:
    """The last JSON object carrying this session's token, or None.

    The token is generated after the tree under review already existed, so no
    file the session read could have been written to contain it. That is what
    makes this safe to run over untrusted content: a findings block planted in
    a doc, however well-formed, cannot claim the run's result.
    """
    found = None
    for blob in list(FENCE_RE.findall(text)) + [text]:
        try:
            parsed = json.loads(blob)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("session_token") == session_token:
            found = parsed
    return found


def normalize(finding) -> dict | None:
    """One finding reduced to the fields that may be printed."""
    if not isinstance(finding, dict):
        return None
    out = {key: flat(finding.get(key)) for key in FINDING_KEYS}

    severity = out["severity"].lower()
    out["severity"] = severity if severity in SOFT_SEVERITIES else "high"

    digits = re.sub(r"\D", "", out["line"])
    out["line"] = digits[:9] if digits else ""

    out["file"] = out["file"] or "(unspecified)"
    out["title"] = out["title"] or "(untitled finding)"
    return out


def report(result: dict) -> int:
    """Annotate, summarize, and return the exit code.

    High findings fail the check. Medium and low are annotations only: they are
    judgment calls worth reading in review, and a nondeterministic gate that
    blocked on them would teach people to re-run until it went green.
    """
    findings = [f for f in map(normalize, result.get("findings") or []) if f]
    high = [f for f in findings if f["severity"] == "high"]

    for finding in findings:
        level = "error" if finding["severity"] == "high" else "warning"
        location = f"file={finding['file']}"
        if finding["line"]:
            location += f",line={finding['line']}"
        detail = " ".join(part for part in (finding["why"], finding["fix"]) if part)
        print(
            f"::{level} {location},title=disclosure-scan: {finding['title']}::{detail}",
            flush=True,
        )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## Disclosure scan", "", flat(result.get("summary"), 500) or "(no summary)", ""]
        if findings:
            lines += ["| Severity | Kind | Where | Finding | Fix |", "|---|---|---|---|---|"]
            for finding in findings:
                where = finding["file"] + (f":{finding['line']}" if finding["line"] else "")
                cells = (
                    finding["severity"],
                    finding["kind"],
                    where,
                    finding["title"],
                    finding["fix"],
                )
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        else:
            lines.append("No findings.")
        Path(summary_path).write_text("\n".join(lines) + "\n")

    print(
        f"::notice::disclosure scan: {len(findings)} finding(s), {len(high)} at high severity",
        flush=True,
    )
    return 1 if high else 0


async def run_session(task_prompt: str, options_kwargs: dict) -> str:
    """One SDK session, streamed to the step log; returns the assistant text."""
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(**options_kwargs)
    transcript: list[str] = []
    section = 0
    async for message in query(prompt=task_prompt, options=options):
        for title, body in render_message(message):
            section += 1
            emit_section(section, title, body)
        # Dispatched on class name for the same reason render_message is: this
        # module has to import without claude-agent-sdk present, because the
        # SDK is a dependency of the workflow and not of the tests.
        kind = type(message).__name__
        if kind == "AssistantMessage":
            transcript += [
                getattr(block, "text", "")
                for block in (message.content or [])
                if type(block).__name__ == "TextBlock"
            ]
        elif kind == "ResultMessage":
            print(
                f"::notice::disclosure session ended subtype={getattr(message, 'subtype', '')}",
                flush=True,
            )
            print(
                json.dumps({"disclosure_usage": getattr(message, "usage", None)}, default=str),
                flush=True,
            )
    return "\n".join(transcript)


def main() -> int:
    scope = os.environ.get("DISCLOSURE_SCOPE", "changed")
    files_text = Path(os.environ["DISCLOSURE_FILES_FILE"]).read_text()
    prompt_dir = Path(os.environ["DISCLOSURE_PROMPT_DIR"])
    workspace = Path(os.environ["DISCLOSURE_WORKSPACE"])

    if not files_text.strip():
        print("::notice::disclosure scan: nothing in scope, skipping the session", flush=True)
        return 0

    def optional(name: str) -> str:
        path = os.environ.get(name)
        return Path(path).read_text() if path and Path(path).is_file() else ""

    # Generated after every input above is already fixed, so nothing the
    # session reads could have been authored to contain it.
    session_token = secrets.token_hex(8)
    task_prompt = build_task_prompt(
        template_path=prompt_dir / "disclosure-task.md",
        scope=scope,
        files_text=files_text,
        diff_text=optional("DISCLOSURE_DIFF_FILE"),
        allowlist_text=optional("DISCLOSURE_ALLOWLIST_FILE"),
        session_token=session_token,
    )
    options_kwargs = build_options_kwargs(
        system_prompt_path=prompt_dir / "disclosure-scan.md",
        workspace=workspace,
        model=os.environ.get("DISCLOSURE_MODEL", ""),
    )

    try:
        transcript = asyncio.run(run_session(task_prompt, options_kwargs))
    except Exception as exc:  # noqa: BLE001 - a crashed scan must not read as a pass
        print(f"::error::disclosure session crashed: {exc}", flush=True)
        return 1

    result = extract_result(transcript, session_token)
    if result is None:
        # A scan that cannot report is not a scan that found nothing. Failing
        # here is re-runnable; passing here would be a green check nobody ran.
        print(
            "::error::disclosure scan: the session produced no result block carrying this "
            "run's session token, so nothing was reviewed. Re-run the job.",
            flush=True,
        )
        return 1

    return report(result)


if __name__ == "__main__":
    sys.exit(main())
