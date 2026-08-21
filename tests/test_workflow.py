"""Invariants the repo's GitHub Actions workflows must never lose."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "autofix.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_workflow_parses_and_triggers_on_the_autofix_dispatch():
    doc = load()

    # PyYAML reads the top-level `on:` key as boolean True.
    assert doc[True] == {"repository_dispatch": {"types": ["autofix"]}}


def test_the_default_token_is_contents_read_only():
    assert load()["permissions"] == {"contents": "read"}


def test_one_run_per_issue_at_a_time():
    job = load()["jobs"]["autofix"]

    assert "sentry_issue_id" in job["concurrency"]["group"]
    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["timeout-minutes"] == 30


def test_only_the_session_step_sees_the_oauth_token():
    # The name appears exactly twice, both on the session step's one env
    # line: the env key and the secrets reference. A third occurrence means
    # the token leaked into another step.
    body = WORKFLOW.read_text()

    assert body.count("CLAUDE_CODE_OAUTH_TOKEN") == 2


def test_the_session_step_never_sees_the_app_token():
    job = load()["jobs"]["autofix"]
    session = next(s for s in job["steps"] if s.get("id") == "session")

    assert "GH_TOKEN" not in (session.get("env") or {})


def test_the_callback_always_fires():
    job = load()["jobs"]["autofix"]
    callback = next(s for s in job["steps"] if s.get("id") == "callback")

    assert callback["if"] == "always()"


def test_the_product_checkout_does_not_persist_credentials():
    job = load()["jobs"]["autofix"]
    checkout = next(s for s in job["steps"] if s.get("name") == "Checkout target repo")

    assert checkout["with"]["persist-credentials"] is False


def test_the_engine_checkout_does_not_persist_credentials():
    job = load()["jobs"]["autofix"]
    checkout = next(s for s in job["steps"] if s.get("name") == "Checkout engine repo")

    assert checkout["with"]["persist-credentials"] is False


def test_the_drift_diff_does_not_word_split_or_glob_cited_files():
    job = load()["jobs"]["autofix"]
    context = next(s for s in job["steps"] if s.get("id") == "context")
    run = context["run"]

    # The naive form the fix replaced: unquoted command substitution over
    # cited.txt let the shell word-split entries with spaces and let git
    # glob-expand entries with wildcards.
    assert "$(cat" not in run
    # The safe form: cited.txt is read into a quoted array (no shell word
    # splitting) and GIT_LITERAL_PATHSPECS=1 turns off git's own wildcard
    # matching on the pathspecs.
    assert "GIT_LITERAL_PATHSPECS=1" in run
    assert 'CITED_FILES+=("$cited_path")' in run
    assert 'git diff "${RELEASE_SHA}"..HEAD -- "${CITED_FILES[@]}"' in run


def test_an_empty_cited_files_list_produces_an_empty_drift_patch_not_a_whole_tree_diff():
    job = load()["jobs"]["autofix"]
    context = next(s for s in job["steps"] if s.get("id") == "context")
    run = context["run"]

    # Without this guard, a zero-element CITED_FILES array expands to
    # nothing and `git diff RELEASE_SHA..HEAD -- "${CITED_FILES[@]}"`
    # collapses to a bare trailing `--`, which diffs the entire repo
    # instead of nothing.
    assert 'if [ "${#CITED_FILES[@]}" -eq 0 ]; then' in run
    assert ": > ../drift.patch" in run


def test_the_publish_step_sets_up_git_auth_before_publishing():
    job = load()["jobs"]["autofix"]
    publish = next(s for s in job["steps"] if s.get("id") == "publish")

    assert "gh auth setup-git" in publish["run"]
    # The auth setup must happen before the publish script runs.
    assert publish["run"].index("gh auth setup-git") < publish["run"].index("autofix_publish.py")


def test_the_payload_file_never_contains_the_callback_secrets():
    job = load()["jobs"]["autofix"]
    context = next(s for s in job["steps"] if s.get("id") == "context")

    assert "del(.callback_url, .callback_token)" in context["run"]


def test_every_action_is_pinned_to_a_full_commit_sha():
    doc = load()
    steps = doc["jobs"]["autofix"]["steps"]
    uses_lines = [s["uses"] for s in steps if "uses" in s]

    assert uses_lines, "expected at least one uses: step"

    for uses in uses_lines:
        assert "@" in uses, f"{uses} is not pinned at all"
        _, ref = uses.split("@", 1)
        sha = ref.split(" ", 1)[0]
        assert len(sha) == 40, f"{uses} is not pinned to a full 40-character commit SHA"
        assert all(c in "0123456789abcdef" for c in sha), f"{uses} ref is not a hex SHA"


def test_the_target_repository_comes_from_repository_variables():
    """A fork points the workflow at its own repo by setting variables,
    never by editing this file."""
    body = WORKFLOW.read_text()

    assert "sentasity" not in body.lower()
    for name in ("AUTOFIX_TARGET_OWNER", "AUTOFIX_TARGET_NAME", "AUTOFIX_TARGET_BRANCH"):
        assert f"vars.{name}" in body, f"the workflow never reads vars.{name}"


WORKFLOW_DIR = WORKFLOW.parent
SECRET_SCAN = WORKFLOW_DIR / "secret-scan.yml"


def test_every_workflow_pins_its_actions_to_a_full_commit_sha():
    """The assertion above covers autofix.yml by name. This one covers whatever
    else lands in the directory, so a second workflow cannot quietly introduce
    a floating tag."""
    workflows = sorted(WORKFLOW_DIR.glob("*.yml"))

    assert len(workflows) >= 2, "expected more than just autofix.yml"

    for path in workflows:
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            for step in job["steps"]:
                if "uses" not in step:
                    continue
                sha = step["uses"].split("@", 1)[1].split(" ", 1)[0]
                assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), (
                    f"{path.name}: {step['uses']} is not pinned to a full commit SHA"
                )


def test_the_secret_scan_runs_on_push_and_pull_request():
    doc = yaml.safe_load(SECRET_SCAN.read_text())

    # PyYAML reads the top-level `on:` key as boolean True.
    assert set(doc[True]) == {"push", "pull_request"}


def scan_step():
    """The step that actually runs gitleaks, not the comments describing it."""
    doc = yaml.safe_load(SECRET_SCAN.read_text())
    steps = doc["jobs"]["gitleaks"]["steps"]
    return next(s for s in steps if "gitleaks detect" in s.get("run", ""))


def test_the_secret_scan_cannot_write_and_cannot_print_a_finding():
    """A scan that leaks its own findings into a public log is worse than none."""
    doc = yaml.safe_load(SECRET_SCAN.read_text())
    checkout = next(s for s in doc["jobs"]["gitleaks"]["steps"] if "uses" in s)

    assert doc["permissions"] == {"contents": "read"}
    assert checkout["with"]["persist-credentials"] is False
    assert "--redact" in scan_step()["run"]


def test_the_gitleaks_download_is_version_pinned_and_checksummed():
    """The binary is fetched at run time, so the pin is the only thing standing
    between this job and an unreviewed executable."""
    body = SECRET_SCAN.read_text()

    assert re.search(r"GITLEAKS_VERSION: \d+\.\d+\.\d+", body)
    assert re.search(r"GITLEAKS_SHA256: [0-9a-f]{64}", body)
    assert "sha256sum -c -" in body


def test_the_scan_uses_the_committed_config():
    """Without --config the allowlist is ignored and the job fails on a known
    false positive, which would train everyone to ignore it."""
    assert "--config .gitleaks.toml" in scan_step()["run"]
    assert (WORKFLOW_DIR.parent.parent / ".gitleaks.toml").is_file()
