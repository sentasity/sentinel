"""Invariants the repo's GitHub Actions workflows must never lose.

Covers the workflows that still exist: the secret scan's trigger, its
gitleaks pin, and its findings-cannot-leak guard; the test suite's trigger,
interpreter matrix, and no-credentials-on-a-fork guard; and one check that
isn't about any single workflow, that every `uses:` step in the directory
pins to a full commit SHA, so a workflow landing later cannot quietly
introduce a floating tag.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SECRET_SCAN = WORKFLOW_DIR / "secret-scan.yml"
TESTS_WORKFLOW = WORKFLOW_DIR / "tests.yml"


def test_every_workflow_pins_its_actions_to_a_full_commit_sha():
    """A floating tag or short SHA on any `uses:` step is a supply-chain gap:
    the action's maintainer, not this repo, would then control what code runs
    in CI. This walks every workflow file rather than naming one, so a new
    workflow landing without a pin fails here automatically."""
    workflows = sorted(WORKFLOW_DIR.glob("*.yml"))

    assert len(workflows) >= 2, "expected more than one workflow in the directory"

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
    assert (REPO_ROOT / ".gitleaks.toml").is_file()


def test_the_suite_runs_on_push_and_pull_request():
    doc = yaml.safe_load(TESTS_WORKFLOW.read_text())

    # PyYAML reads the top-level `on:` key as boolean True.
    assert set(doc[True]) == {"push", "pull_request"}


def test_the_suite_runs_on_both_the_deploy_and_development_interpreters():
    """The Lambda runs 3.12 while development happens on something newer, so a
    matrix over only one of them leaves the other tested by luck."""
    doc = yaml.safe_load(TESTS_WORKFLOW.read_text())
    versions = doc["jobs"]["pytest"]["strategy"]["matrix"]["python-version"]

    assert "3.12" in versions, "3.12 is the Lambda runtime"
    assert len(versions) > 1, "a single-version matrix defeats the point"


def test_the_lambda_runtime_is_in_the_test_matrix():
    """Pins the matrix to the runtime the stack actually declares, so bumping
    one without the other fails here rather than in production."""
    stack = (REPO_ROOT / "infra" / "stacks" / "receiver_stack.py").read_text()
    runtime = re.search(r"Runtime\.PYTHON_(\d+)_(\d+)", stack)

    assert runtime, "could not find the Lambda runtime in the stack"
    declared = f"{runtime.group(1)}.{runtime.group(2)}"
    versions = yaml.safe_load(TESTS_WORKFLOW.read_text())["jobs"]["pytest"]["strategy"]["matrix"]["python-version"]

    assert declared in versions, f"the stack deploys {declared}; the matrix does not test it"


def test_the_test_job_needs_no_credentials():
    """A suite that needs secrets cannot run on a fork's pull request, where
    GitHub withholds them. One step is allowed to read one: the private ban
    list is a secret by design, so that step must be guarded to skip a fork's
    run, and the suite must stay green without it."""
    doc = yaml.safe_load(TESTS_WORKFLOW.read_text())

    assert doc["permissions"] == {"contents": "read"}
    for step in doc["jobs"]["pytest"]["steps"]:
        if "secrets." not in yaml.safe_dump(step):
            continue
        assert step["name"] == "Materialize the private ban list"
        assert (
            "github.event.pull_request.head.repo.full_name == github.repository"
            in step["if"]
        )
