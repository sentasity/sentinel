"""Sentry alert-workflow migration: transforms, backup, apply, rollback."""

import copy
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts import migrate_rules as mr
from tests.conftest import load_fixture

DETECTORS = load_fixture("sentry-detectors.json")
WORKFLOWS = load_fixture("sentry-workflows-checkout.json")
WORKFLOW_ACTION = load_fixture("sentry-workflow-action.json")

SCANNERS_PROJECT_ID = next(d["projectId"] for d in DETECTORS if str(d["id"]) == "1000038")

# The two staging workflows were migrated before this port landed.
PENDING_IDS = ["1000008", "1000010"]


def wf_by_name(name):
    return next(w for w in WORKFLOWS if w["name"] == name)


def actions_of(workflow):
    return [a for af in workflow["actionFilters"] for a in af["actions"]]


def conditions_of(workflow):
    return [c for af in workflow["actionFilters"] for c in af["conditions"]]


def test_workflows_are_scoped_to_a_project_through_detectors():
    found = mr.workflows_for_project(WORKFLOWS, DETECTORS, SCANNERS_PROJECT_ID)

    assert {w["name"] for w in found} == {
        "[Scanners] New Issue - Prod",
        "[Scanners] New Issue - Staging",
        "[Scanners] New Issue - Dev",
        "[Scanners] Regression - Prod",
        "[Scanners] Regression - Staging",
        "[Scanners] Regression - Dev",
    }


def test_workflows_for_another_project_are_excluded():
    other = next(str(d["projectId"]) for d in DETECTORS if str(d["id"]) == "1000032")

    assert mr.workflows_for_project(WORKFLOWS, DETECTORS, other) == []


def test_a_workflow_spanning_two_projects_is_refused():
    workflow = copy.deepcopy(wf_by_name("[Scanners] New Issue - Prod"))
    workflow["detectorIds"] = ["1000038", "1000032"]

    with pytest.raises(mr.MigrationError, match="spans projects"):
        mr.workflows_for_project([workflow], DETECTORS, SCANNERS_PROJECT_ID)


def test_put_payload_keeps_only_writable_keys():
    payload = mr.put_payload(wf_by_name("[Scanners] New Issue - Prod"))

    assert set(payload) == {
        "name",
        "enabled",
        "environment",
        "config",
        "detectorIds",
        "triggers",
        "actionFilters",
        "owner",
    }
    assert payload["config"]["frequency"] == 300


def test_put_payload_always_carries_enabled():
    """A PUT that omits `enabled` switches a disabled alert back on."""
    workflow = copy.deepcopy(wf_by_name("[Scanners] New Issue - Prod"))
    workflow["enabled"] = False

    assert mr.put_payload(workflow)["enabled"] is False


def test_swap_workflow_action_replaces_the_msteams_action():
    swapped = mr.swap_workflow_action(
        wf_by_name("[Scanners] New Issue - Prod"), WORKFLOW_ACTION
    )

    actions = actions_of(swapped)
    assert len(actions) == 1
    assert actions[0]["type"] == mr.WEBHOOK_ACTION_TYPE
    assert actions[0]["config"]["targetIdentifier"] == "sentinel-a1b2c3"


def test_swap_workflow_action_ignores_the_server_assigned_id_when_deduping():
    workflow = copy.deepcopy(wf_by_name("[Scanners] New Issue - Prod"))
    already_there = copy.deepcopy(WORKFLOW_ACTION)
    already_there["id"] = "99999999"
    workflow["actionFilters"][0]["actions"].append(already_there)

    swapped = mr.swap_workflow_action(workflow, WORKFLOW_ACTION)

    assert len(actions_of(swapped)) == 1


def test_swap_workflow_action_keeps_an_unrelated_action():
    workflow = copy.deepcopy(wf_by_name("[Scanners] New Issue - Prod"))
    workflow["actionFilters"][0]["actions"].append(
        {"type": "email", "config": {"targetType": "user", "targetIdentifier": "56789"}}
    )

    swapped = mr.swap_workflow_action(workflow, WORKFLOW_ACTION)

    assert sorted(a["type"] for a in actions_of(swapped)) == ["email", "webhook"]


def test_swap_workflow_action_leaves_the_source_workflow_untouched():
    workflow = wf_by_name("[Scanners] New Issue - Prod")

    mr.swap_workflow_action(workflow, WORKFLOW_ACTION)

    assert actions_of(workflow)[0]["type"] == mr.MSTEAMS_ACTION_TYPE


def test_swap_workflow_action_preserves_triggers_frequency_and_environment():
    workflow = wf_by_name("[Scanners] Regression - Prod")

    swapped = mr.swap_workflow_action(workflow, WORKFLOW_ACTION)

    assert swapped["triggers"] == workflow["triggers"]
    assert swapped["config"] == workflow["config"]
    assert swapped["environment"] == "prod"
    assert swapped["detectorIds"] == workflow["detectorIds"]
    assert swapped["owner"] == workflow["owner"]


def test_swap_workflow_action_refuses_a_workflow_with_no_msteams_action():
    workflow = copy.deepcopy(wf_by_name("[Scanners] New Issue - Prod"))
    workflow["actionFilters"][0]["actions"] = []

    with pytest.raises(mr.MigrationError, match="no Msteams action"):
        mr.swap_workflow_action(workflow, WORKFLOW_ACTION)


def test_prod_workflows_lose_the_level_condition():
    planned = mr.plan_workflow(wf_by_name("[Scanners] New Issue - Prod"), WORKFLOW_ACTION)

    assert [c["type"] for c in conditions_of(planned)] == []


def test_non_prod_workflows_keep_their_level_condition():
    """The level condition is dropped for prod only, so a gate-less strip fails here.

    Every non-prod workflow in the fixture happens to carry no conditions at all,
    which would make this assertion pass even if `plan_workflow` stripped them
    unconditionally. So the condition is added here rather than found.
    """
    source = copy.deepcopy(wf_by_name("[Scanners] New Issue - Dev"))
    level = copy.deepcopy(
        next(c for c in conditions_of(wf_by_name("[Scanners] New Issue - Prod")))
    )
    source["actionFilters"][0]["conditions"].append(level)

    planned = mr.plan_workflow(source, WORKFLOW_ACTION)

    assert [c["type"] for c in conditions_of(planned)] == ["level"]


def test_classify_separates_migrated_from_pending():
    pending = wf_by_name("[Scanners] New Issue - Prod")
    migrated = wf_by_name("[Scanners] New Issue - Staging")

    assert mr.classify(pending, WORKFLOW_ACTION) == "pending"
    assert mr.classify(migrated, WORKFLOW_ACTION) == "migrated"


def test_classify_flags_a_workflow_with_neither_action():
    workflow = copy.deepcopy(wf_by_name("[Scanners] New Issue - Prod"))
    workflow["actionFilters"][0]["actions"] = []

    assert mr.classify(workflow, WORKFLOW_ACTION) == "unexpected"


def fake_client():
    client = MagicMock()
    client.list_workflows.return_value = json.loads(json.dumps(WORKFLOWS))
    client.list_detectors.return_value = json.loads(json.dumps(DETECTORS))
    client.project_id.return_value = str(SCANNERS_PROJECT_ID)
    return client


def test_apply_writes_a_backup_before_touching_anything(tmp_path):
    client = fake_client()
    backup = tmp_path / "backup.json"

    mr.apply(client, "checkout", WORKFLOW_ACTION, backup, environments=("prod",))

    saved = json.loads(backup.read_text())
    assert [w["name"] for w in saved] == [
        "[Scanners] New Issue - Prod",
        "[Scanners] Regression - Prod",
    ]
    assert actions_of(saved[0])[0]["type"] == mr.MSTEAMS_ACTION_TYPE


def test_apply_updates_only_the_named_environment(tmp_path):
    client = fake_client()

    mr.apply(client, "checkout", WORKFLOW_ACTION, tmp_path / "b.json", environments=("prod",))

    updated_ids = [call.args[0] for call in client.update.call_args_list]
    assert sorted(updated_ids) == PENDING_IDS


def test_apply_never_touches_the_dev_workflows(tmp_path):
    """The per-developer rules keep the stock integration and the dev channel."""
    client = fake_client()

    mr.apply(client, "checkout", WORKFLOW_ACTION, tmp_path / "b.json")

    updated_ids = [call.args[0] for call in client.update.call_args_list]
    assert sorted(updated_ids) == PENDING_IDS


def test_apply_skips_the_already_migrated_staging_workflows(tmp_path):
    """2 of 18 were migrated before the port, so a slice holding them must proceed."""
    client = fake_client()

    result = mr.apply(client, "checkout", WORKFLOW_ACTION, tmp_path / "b.json")

    assert sorted(result["skipped"]) == [
        "[Scanners] New Issue - Staging",
        "[Scanners] Regression - Staging",
    ]
    assert sorted(result["updated"]) == PENDING_IDS


def test_apply_sends_the_planned_payload(tmp_path):
    client = fake_client()

    mr.apply(client, "checkout", WORKFLOW_ACTION, tmp_path / "b.json", environments=("prod",))

    payloads = [call.args[1] for call in client.update.call_args_list]
    assert all(actions_of(p)[0]["type"] == mr.WEBHOOK_ACTION_TYPE for p in payloads)
    assert all(conditions_of(p) == [] for p in payloads)
    assert all("id" not in p for p in payloads)
    assert all(p["detectorIds"] == ["1000038"] for p in payloads)


def test_apply_refuses_a_workflow_in_an_unknown_shape(tmp_path):
    client = fake_client()
    workflows = json.loads(json.dumps(WORKFLOWS))
    for workflow in workflows:
        if workflow["name"] == "[Scanners] New Issue - Prod":
            workflow["actionFilters"][0]["actions"] = []
    client.list_workflows.return_value = workflows
    backup = tmp_path / "b.json"

    with pytest.raises(mr.MigrationError, match="unknown shape"):
        mr.apply(client, "checkout", WORKFLOW_ACTION, backup, environments=("prod",))

    assert not backup.exists()
    client.update.assert_not_called()


def test_apply_refuses_when_the_backup_path_is_not_writable(tmp_path):
    client = fake_client()

    with pytest.raises(mr.MigrationError, match="cannot write backup"):
        mr.apply(
            client,
            "checkout",
            WORKFLOW_ACTION,
            tmp_path / "missing-dir" / "b.json",
            environments=("prod",),
        )

    client.update.assert_not_called()


def test_rollback_puts_every_backed_up_workflow_back(tmp_path):
    client = MagicMock()
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps([w for w in WORKFLOWS if w["environment"] == "prod"]))

    restored = mr.rollback(client, backup)

    assert sorted(restored) == PENDING_IDS
    payloads = [call.args[1] for call in client.update.call_args_list]
    assert all(actions_of(p)[0]["type"] == mr.MSTEAMS_ACTION_TYPE for p in payloads)


def test_rollback_restores_the_prod_level_condition(tmp_path):
    client = MagicMock()
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps([wf_by_name("[Scanners] New Issue - Prod")]))

    mr.rollback(client, backup)

    payload = client.update.call_args.args[1]
    assert [c["type"] for c in conditions_of(payload)] == [mr.LEVEL_CONDITION_TYPE]


def test_rollback_refuses_an_empty_backup(tmp_path):
    client = MagicMock()
    backup = tmp_path / "backup.json"
    backup.write_text("[]")

    with pytest.raises(mr.MigrationError, match="backup is empty"):
        mr.rollback(client, backup)


# The backup is the only rollback artifact for 18 live production alert rules,
# and docs/SETUP-SENTRY.md walks the project slices back to back on the same day.


def test_default_backup_path_separates_environment_slices():
    staging = mr.default_backup_path("checkout", ("staging",))
    prod = mr.default_backup_path("checkout", ("prod",))

    assert staging != prod
    assert "staging" in staging.name
    assert "prod" in prod.name


def test_apply_refuses_to_overwrite_an_existing_backup(tmp_path):
    client = fake_client()
    backup = tmp_path / "backup.json"
    backup.write_text("[]")

    with pytest.raises(mr.MigrationError, match="backup already exists"):
        mr.apply(client, "checkout", WORKFLOW_ACTION, backup, environments=("prod",))

    assert backup.read_text() == "[]"
    client.update.assert_not_called()


def test_dry_run_plans_without_writing(tmp_path):
    client = fake_client()
    backup = tmp_path / "backup.json"

    result = mr.apply(client, "checkout", WORKFLOW_ACTION, backup, dry_run=True)

    client.update.assert_not_called()
    assert not backup.exists()
    assert result["updated"] == []
    assert sorted(result["planned"]) == [
        "[Scanners] New Issue - Prod",
        "[Scanners] Regression - Prod",
    ]


def test_dry_run_still_reports_what_it_would_skip(tmp_path):
    client = fake_client()

    result = mr.apply(client, "checkout", WORKFLOW_ACTION, tmp_path / "b.json", dry_run=True)

    assert sorted(result["skipped"]) == [
        "[Scanners] New Issue - Staging",
        "[Scanners] Regression - Staging",
    ]


def test_apply_names_the_workflows_it_already_updated_when_one_fails(tmp_path):
    client = fake_client()
    client.update.side_effect = [None, mr.MigrationError("PUT returned HTTP 500")]

    with pytest.raises(mr.MigrationError, match="1000008"):
        mr.apply(
            client, "checkout", WORKFLOW_ACTION, tmp_path / "b.json", environments=("prod",)
        )


def test_a_dropped_connection_still_names_what_it_already_updated(tmp_path):
    """A dead socket is the likeliest mid-flight failure, not a tidy HTTP status."""
    client = fake_client()
    client.update.side_effect = [None, requests.exceptions.ConnectionError("reset by peer")]

    with pytest.raises(mr.MigrationError, match="1000008"):
        mr.apply(
            client, "checkout", WORKFLOW_ACTION, tmp_path / "b.json", environments=("prod",)
        )


def test_client_transport_errors_surface_as_migration_errors():
    client = mr.SentryWorkflows("acme-tools", "tok")

    with patch.object(
        client.session, "get", side_effect=requests.exceptions.ConnectTimeout("timed out")
    ):
        with pytest.raises(mr.MigrationError, match="timed out"):
            client.list_workflows()

    with patch.object(
        client.session, "put", side_effect=requests.exceptions.ConnectionError("reset")
    ):
        with pytest.raises(mr.MigrationError, match="reset"):
            client.update("1000008", {})


def test_the_client_only_calls_org_scoped_endpoints():
    """The legacy project-rules endpoints return HTTP 410 as of 17 August 2026."""
    client = mr.SentryWorkflows("acme-tools", "tok")
    ok = MagicMock(ok=True)
    ok.json.return_value = []

    with patch.object(client.session, "get", return_value=ok) as get:
        client.list_workflows()
        client.list_detectors()
    with patch.object(client.session, "put", return_value=ok) as put:
        client.update("1000008", {})

    called = [c.args[0] for c in get.call_args_list] + [c.args[0] for c in put.call_args_list]
    assert called == [
        "https://sentry.io/api/0/organizations/acme-tools/workflows/",
        "https://sentry.io/api/0/organizations/acme-tools/detectors/",
        "https://sentry.io/api/0/organizations/acme-tools/workflows/1000008/",
    ]


def test_rollback_names_what_it_already_restored_when_one_fails(tmp_path):
    client = MagicMock()
    client.update.side_effect = [None, requests.exceptions.ConnectionError("reset by peer")]
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps([w for w in WORKFLOWS if w["environment"] == "prod"]))

    with pytest.raises(mr.MigrationError, match="1000008"):
        mr.rollback(client, backup)
