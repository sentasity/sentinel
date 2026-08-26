"""Deterministic publish: branch, commit, push, PR. No AI in this path."""

import json
from unittest.mock import MagicMock, call, patch

from scripts.autofix_publish import branch_name, main, pr_body, pr_title


def test_branch_name_is_stable_and_lowercase():
    assert branch_name("CHECKOUT-4B2", "d1b2c3d4-rest") == "autofix/checkout-4b2-d1b2c3"


def test_pr_title_carries_the_marker_and_short_id():
    assert pr_title("CHECKOUT-4B2", "Swallowed ClientError in recover flow") == (
        "[autofix] Swallowed ClientError in recover flow (CHECKOUT-4B2)"
    )


def test_pr_body_appends_the_provenance_footer():
    body = pr_body(
        summary="## Root cause\nswallowed error",
        dispatch_id="d-1",
        release_sha="79bad4b7" + "0" * 32,
        develop_sha="abc123",
        run_url="https://run",
    )

    assert body.startswith("## Root cause")
    assert "unattended autofix run" in body
    assert "d-1" in body and "abc123" in body and "https://run" in body


@patch("scripts.autofix_publish.subprocess.run")
def test_a_non_verified_status_publishes_nothing(run, tmp_path):
    workspace = tmp_path
    (workspace / ".autofix").mkdir()
    (workspace / ".autofix" / "result.json").write_text(json.dumps({"status": "aborted_drift"}))

    assert main(workspace=workspace, env={}) == "aborted_drift"
    run.assert_not_called()


@patch("scripts.autofix_publish.subprocess.run")
def test_a_verified_empty_diff_is_failed(run, tmp_path):
    workspace = tmp_path
    (workspace / ".autofix").mkdir()
    (workspace / ".autofix" / "result.json").write_text(json.dumps({"status": "verified"}))
    (workspace / ".autofix" / "summary.md").write_text("## Root cause\nx")
    run.return_value = MagicMock(returncode=0, stdout="")  # git status --porcelain: empty

    assert main(workspace=workspace, env={"AUTOFIX_SHORT_ID": "S-1"}) == "failed"


def _cp(returncode=0, stdout="", stderr=""):
    """A minimal stand-in for subprocess.CompletedProcess."""
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _seed_verified_workspace(tmp_path, summary="## Root cause\nswallowed error"):
    workspace = tmp_path
    (workspace / ".autofix").mkdir()
    (workspace / ".autofix" / "result.json").write_text(json.dumps({"status": "verified"}))
    (workspace / ".autofix" / "summary.md").write_text(summary)
    return workspace


@patch("scripts.autofix_publish.subprocess.run")
def test_a_verified_diff_runs_the_full_publish_sequence(run, tmp_path, capsys):
    workspace = _seed_verified_workspace(tmp_path)
    run.side_effect = [
        _cp(stdout=" M src/foo.py\n"),  # git status --porcelain
        _cp(stdout="devsha123\n"),  # git rev-parse HEAD
        _cp(stdout="12345\n"),  # gh api bot id lookup
        _cp(),  # git checkout -b
        _cp(),  # git add --all
        _cp(),  # git commit
        _cp(),  # git push
        _cp(stdout="https://github.com/acme-tools/checkout/pull/42\n"),  # gh pr create
    ]
    env = {
        "AUTOFIX_SHORT_ID": "CHECKOUT-4B2",
        "AUTOFIX_DISPATCH_ID": "d1b2c3d4-rest",
        "AUTOFIX_RELEASE_SHA": "abc",
        "AUTOFIX_RUN_URL": "https://run",
        # No default any more: the workflow passes this through from the
        # AUTOFIX_TARGET_* repository variables, so the test must too.
        "AUTOFIX_TARGET_REPO": "acme-tools/checkout",
    }

    assert main(workspace=workspace, env=env) == "pr_opened"
    assert run.call_count == 8

    out = capsys.readouterr().out
    assert "pr_url=https://github.com/acme-tools/checkout/pull/42" in out

    pr_create_args = run.call_args_list[-1].args[0]
    assert pr_create_args[:3] == ["gh", "pr", "create"]
    assert "acme-tools/checkout" in pr_create_args


@patch("scripts.autofix_publish.subprocess.run")
def test_a_failing_git_step_stops_the_sequence_and_names_it(run, tmp_path, capsys):
    workspace = _seed_verified_workspace(tmp_path)
    run.side_effect = [
        _cp(stdout=" M src/foo.py\n"),  # git status --porcelain
        _cp(stdout="devsha123\n"),  # git rev-parse HEAD
        _cp(stdout="12345\n"),  # gh api bot id lookup
        _cp(),  # git checkout -b
        _cp(),  # git add --all
        _cp(returncode=1, stderr="nothing to commit"),  # git commit fails
    ]
    env = {"AUTOFIX_SHORT_ID": "S-1", "AUTOFIX_DISPATCH_ID": "d-1"}

    assert main(workspace=workspace, env=env) == "failed"
    assert run.call_count == 6  # push and gh pr create never attempted

    out = capsys.readouterr().out
    assert "::error::git commit failed" in out


@patch("scripts.autofix_publish.subprocess.run")
def test_gh_pr_create_failing_is_reported_and_failed(run, tmp_path, capsys):
    workspace = _seed_verified_workspace(tmp_path)
    run.side_effect = [
        _cp(stdout=" M src/foo.py\n"),
        _cp(stdout="devsha123\n"),
        _cp(stdout="12345\n"),
        _cp(),
        _cp(),
        _cp(),
        _cp(),
        _cp(returncode=1, stderr="permission denied"),  # gh pr create fails
    ]
    env = {"AUTOFIX_SHORT_ID": "S-1", "AUTOFIX_DISPATCH_ID": "d-1"}

    assert main(workspace=workspace, env=env) == "failed"
    assert run.call_count == 8

    out = capsys.readouterr().out
    assert "::error::gh pr create failed" in out


@patch("scripts.autofix_publish.subprocess.run")
def test_a_failing_bot_identity_lookup_fails_loudly_not_silently(run, tmp_path, capsys):
    workspace = _seed_verified_workspace(tmp_path)
    run.side_effect = [
        _cp(stdout=" M src/foo.py\n"),  # git status --porcelain
        _cp(stdout="devsha123\n"),  # git rev-parse HEAD
        _cp(returncode=1, stderr="404 Not Found"),  # gh api bot id lookup fails
    ]
    env = {"AUTOFIX_SHORT_ID": "S-1", "AUTOFIX_DISPATCH_ID": "d-1"}

    assert main(workspace=workspace, env=env) == "failed"
    assert run.call_count == 3  # checkout/add/commit/push never attempted

    out = capsys.readouterr().out
    assert "::error::" in out
    assert "sentasity-sentinel[bot]" in out


@patch("scripts.autofix_publish.subprocess.run")
def test_the_empty_diff_check_uses_the_same_pathspec_as_git_add(run, tmp_path):
    workspace = _seed_verified_workspace(tmp_path)
    run.side_effect = [
        _cp(stdout=" M src/foo.py\n"),
        _cp(stdout="devsha123\n"),
        _cp(stdout="12345\n"),
        _cp(),
        _cp(),
        _cp(),
        _cp(),
        _cp(stdout="https://github.com/acme-tools/checkout/pull/1\n"),
    ]
    env = {"AUTOFIX_SHORT_ID": "S-1", "AUTOFIX_DISPATCH_ID": "d-1"}

    assert main(workspace=workspace, env=env) == "pr_opened"

    status_call, add_call = run.call_args_list[0], run.call_args_list[4]
    status_pathspec = status_call.args[0][-3:]
    add_pathspec = add_call.args[0][-3:]
    assert status_pathspec == ["--", ".", ":!.autofix"]
    assert status_pathspec == add_pathspec
    run.assert_has_calls(
        [
            call(status_call.args[0], **status_call.kwargs),
            call(add_call.args[0], **add_call.kwargs),
        ],
        any_order=True,
    )
