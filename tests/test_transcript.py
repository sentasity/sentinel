"""Shared transcript rendering: the SDK-message-to-step-log guard.

Moved out of the autofix runner's test module when the Actions autofix path
was deleted. The rendering itself stayed: the disclosure scan streams the
same untrusted-content-into-a-step-log problem, through the same shared
module, so the guard against a transcript forging a workflow command still
needs this coverage.
"""

import pytest

from runner.transcript import BODY_MAX_LINES, emit_section, render_message

# --- Transcript rendering ---------------------------------------------------
#
# The renderer dispatches on the SDK's class names, so the fakes below carry
# the real names. They are deliberately dumb: claude-agent-sdk is not a test
# dependency (nothing else in this file imports it either), and the shapes it
# yields are small enough to restate.


class TextBlock:
    def __init__(self, text):
        self.text = text


class ThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking
        self.signature = "sig"


class ToolUseBlock:
    def __init__(self, name, input, id="toolu_0123456789"):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, content, is_error=False, tool_use_id="toolu_0123456789"):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content):
        self.content = content
        self.model = "claude-opus-5"


class UserMessage:
    def __init__(self, content):
        self.content = content


class SystemMessage:
    def __init__(self, subtype, data):
        self.subtype = subtype
        self.data = data


class ResultMessage:
    def __init__(self, subtype="success"):
        self.subtype = subtype
        self.usage = {"output_tokens": 1}


def test_an_assistant_turn_renders_its_prose_and_its_tool_call(tmp_path):
    message = AssistantMessage(
        [
            TextBlock("Reproducing the crash first."),
            ToolUseBlock("Bash", {"command": "pytest -q tests/test_provisioning.py"}),
        ]
    )

    sections = render_message(message)

    assert [title for title, _ in sections] == [
        "assistant",
        "tool · Bash · pytest -q tests/test_provisioning.py",
    ]
    assert sections[0][1] == ["Reproducing the crash first."]
    assert '"command"' in "\n".join(sections[1][1])


def test_thinking_is_rendered_because_the_session_thinks(tmp_path):
    sections = render_message(AssistantMessage([ThinkingBlock("weighing two causes")]))

    assert sections == [("thinking", ["weighing two causes"])]


def test_a_tool_result_is_clipped_and_says_what_it_dropped():
    body = "\n".join(f"line {n}" for n in range(500))

    sections = render_message(UserMessage([ToolResultBlock(body)]))

    (title, lines), = sections
    assert title.startswith("result")
    assert len(lines) == BODY_MAX_LINES + 1
    assert lines[0] == "line 0"
    assert "truncated" in lines[-1] and "500 lines" in lines[-1]


def test_a_failed_tool_result_is_flagged():
    sections = render_message(UserMessage([ToolResultBlock("boom", is_error=True)]))

    assert "ERROR" in sections[0][0]


def test_a_list_shaped_tool_result_is_flattened():
    sections = render_message(UserMessage([ToolResultBlock([{"type": "text", "text": "hi"}])]))

    assert sections[0][1] == ["hi"]


def test_the_init_banner_is_rendered():
    sections = render_message(SystemMessage("init", {"model": "claude-opus-5"}))

    assert sections[0][0] == "system · init"
    assert any("claude-opus-5" in line for line in sections[0][1])


def test_the_result_message_renders_no_section_because_the_notice_covers_it():
    assert render_message(ResultMessage()) == []


def test_a_body_cannot_forge_a_workflow_command(capsys):
    emit_section(3, "result", ["::error::the session was hijacked", "::add-mask::x"])

    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "::group::[3] result"
    assert printed[1].startswith("::stop-commands::")
    token = printed[1].removeprefix("::stop-commands::")
    assert printed[2:4] == ["::error::the session was hijacked", "::add-mask::x"]
    assert printed[4] == f"::{token}::"
    assert printed[5] == "::endgroup::"


def test_a_group_title_is_one_line_and_cannot_forge_a_command(capsys):
    emit_section(1, "tool · Bash · ::error::x\nsecond line", [])

    printed = capsys.readouterr().out.splitlines()
    assert printed == ["::group::[1] tool · Bash · :.:error:.:x second line", "::endgroup::"]


def test_the_renderer_matches_the_real_sdk_shapes():
    """Pin the class names render_message dispatches on to the SDK's own.

    Skipped wherever claude-agent-sdk is absent, which is most checkouts: it
    is a dependency of the workflow, not of the tests. Where it is installed,
    this is what catches an SDK rename, which would otherwise turn the whole
    transcript into silence again without failing anything.
    """
    sdk = pytest.importorskip("claude_agent_sdk")

    assistant = sdk.AssistantMessage(
        content=[
            sdk.ThinkingBlock(thinking="weighing two causes", signature="sig"),
            sdk.TextBlock(text="hi"),
            sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
        ],
        model="claude-opus-5",
    )
    user = sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="t1", content="out")])
    system = sdk.SystemMessage(subtype="init", data={"model": "claude-opus-5"})

    assert [title for title, _ in render_message(assistant)] == [
        "thinking",
        "assistant",
        "tool · Bash · ls",
    ]
    assert render_message(user)[0][1] == ["out"]
    assert render_message(system)[0][0] == "system · init"
