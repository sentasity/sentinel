"""CDK template assertions for the receiver stack."""

from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.stacks.receiver_stack import ReceiverStack
from receiver import observability
from receiver.config import ReceiverConfig

CONFIG = ReceiverConfig(
    environments=("prod", "staging"),
    account="123456789012",
    region="us-east-1",
    table_name="sentinel-alerts",
    alarm_email="ops@example.com",
    tenant_id="tenant-123",
    service_url="https://smba.trafficmanager.net/amer/",
    bot_app_id="app-456",
    channels={"prod": "19:prod@thread.tacv2", "staging": "19:staging@thread.tacv2"},
    sentry_org="sentasity",
    ssm_prefix="/sentinel",
    automation_dsn="https://key@o0.ingest.sentry.io/1",
)


@pytest.fixture(scope="module")
def stack():
    # An empty bundling-stacks list skips Docker asset bundling, so these
    # assertions run offline in CI and on a laptop with no daemon running.
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    return ReceiverStack(
        app,
        "sentinel-receiver",
        cfg=CONFIG,
        env=cdk.Environment(account=CONFIG.account, region=CONFIG.region),
    )


@pytest.fixture(scope="module")
def template(stack):
    return Template.from_stack(stack)


def test_stack_targets_shared_services(stack):
    assert stack.account == "123456789012"
    assert stack.region == "us-east-1"
    assert stack.stack_name == "sentinel-receiver"


def test_alert_table_is_on_demand_with_the_documented_key_schema(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "sentinel-alerts",
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        },
    )


def test_alert_table_is_retained_on_stack_delete(template):
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain"})


def test_receiver_function_runtime_and_limits(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "sentinel-receiver",
            "Runtime": "python3.12",
            "Handler": "receiver.handler.lambda_handler",
            "MemorySize": 256,
            "Timeout": 30,
        },
    )


def test_receiver_function_environment_carries_config_and_dsn(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "RECEIVER_CONFIG": "/var/task/config/receiver.yaml",
                        "SENTRY_DSN": "https://key@o0.ingest.sentry.io/1",
                        "SENTASITY_ENV": "prod",
                        "TABLE_NAME": "sentinel-alerts",
                    }
                )
            }
        },
    )


def test_function_url_is_unauthenticated_at_the_platform(template):
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "NONE"})


def test_function_url_permission_is_granted(template):
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"},
    )


def test_log_retention_is_bounded(template):
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": "/aws/lambda/sentinel-receiver",
            "RetentionInDays": 90,
        },
    )


def policy_statements(template):
    statements = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def test_lambda_may_read_only_its_own_ssm_prefix(template):
    wanted = [
        s
        for s in policy_statements(template)
        if "ssm:GetParameter" in str(s.get("Action"))
    ]

    assert len(wanted) == 1
    assert wanted[0]["Effect"] == "Allow"
    assert "parameter/sentinel/*" in str(wanted[0]["Resource"])


def test_kms_decrypt_statement_is_conditioned_on_ssm(template):
    wanted = [
        s for s in policy_statements(template) if "kms:Decrypt" in str(s.get("Action"))
    ]

    assert len(wanted) == 1
    condition = wanted[0]["Condition"]["StringEquals"]
    assert condition["kms:ViaService"] == "ssm.us-east-1.amazonaws.com"


def test_lambda_may_write_the_alert_table(template):
    wanted = [
        s
        for s in policy_statements(template)
        if "dynamodb:PutItem" in str(s.get("Action"))
    ]

    assert wanted, "expected a DynamoDB grant on the receiver's role"


def test_error_alarm_fires_on_a_single_failure(template):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "Errors",
            "Namespace": "AWS/Lambda",
            "Statistic": "Sum",
            "Threshold": 1,
            "EvaluationPeriods": 1,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "TreatMissingData": "notBreaching",
        },
    )


def test_every_alarm_action_points_at_the_email_topic(template):
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    topic_ids = list(template.find_resources("AWS::SNS::Topic"))

    assert len(topic_ids) == 1
    assert alarms, "expected at least one alarm"
    for alarm in alarms.values():
        assert alarm["Properties"]["AlarmActions"] == [{"Ref": topic_ids[0]}]


def test_a_failed_teams_post_is_extracted_from_the_logs_as_a_metric(template):
    """Lambda's Errors metric cannot see a delivery failure.

    The handler catches BotError and returns a 500 response, so the invocation
    succeeds and Errors stays at zero. The log marker is the only signal, and
    this filter is what turns it into something alarmable.
    """
    template.has_resource_properties(
        "AWS::Logs::MetricFilter",
        {
            "FilterPattern": f'"{observability.DELIVERY_FAILURE_MARKER}"',
            "MetricTransformations": [
                {
                    "MetricName": "DeliveryFailures",
                    "MetricNamespace": "Sentinel",
                    "MetricValue": "1",
                    "DefaultValue": 0,
                }
            ],
        },
    )


def test_delivery_failure_alarm_fires_on_a_single_failure(template):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "DeliveryFailures",
            "Namespace": "Sentinel",
            "Statistic": "Sum",
            "Threshold": 1,
            "EvaluationPeriods": 1,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "TreatMissingData": "notBreaching",
        },
    )


def test_topic_has_the_configured_email_subscription(template):
    template.has_resource_properties(
        "AWS::SNS::Subscription",
        {"Protocol": "email", "Endpoint": "ops@example.com"},
    )


def test_the_table_carries_the_sparse_due_index(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "GlobalSecondaryIndexes": Match.array_with(
                [
                    Match.object_like(
                        {
                            "IndexName": "due-index",
                            "KeySchema": [
                                {"AttributeName": "due_pk", "KeyType": "HASH"},
                                {"AttributeName": "due_at", "KeyType": "RANGE"},
                            ],
                        }
                    )
                ]
            )
        },
    )


def test_the_due_index_name_matches_what_the_store_queries():
    """A renamed index the store never queries is an index that does nothing."""
    from receiver.store import DUE_INDEX

    assert DUE_INDEX == "due-index"


def test_the_table_carries_the_token_index(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "GlobalSecondaryIndexes": Match.array_with(
                [
                    Match.object_like(
                        {
                            "IndexName": "token-index",
                            "KeySchema": [
                                {"AttributeName": "reply_token_hash", "KeyType": "HASH"}
                            ],
                        }
                    )
                ]
            )
        },
    )


def test_the_token_index_projects_everything_the_fan_out_reads():
    """claim_batch replies straight from this query; KEYS_ONLY would not do."""
    from infra.stacks.receiver_stack import ReceiverStack  # noqa: F401

    import aws_cdk as cdk_local
    from aws_cdk.assertions import Template as T

    app = cdk_local.App(context={"aws:cdk:bundling-stacks": []})
    stack = ReceiverStack(
        app, "probe", cfg=CONFIG,
        env=cdk_local.Environment(account=CONFIG.account, region=CONFIG.region),
    )
    table = list(T.from_stack(stack).find_resources("AWS::DynamoDB::Table").values())[0]
    indexes = {i["IndexName"]: i for i in table["Properties"]["GlobalSecondaryIndexes"]}

    assert indexes["token-index"]["Projection"]["ProjectionType"] == "ALL"


def test_the_token_index_name_matches_what_the_store_queries():
    from receiver.store import TOKEN_INDEX

    assert TOKEN_INDEX == "token-index"


def test_a_one_minute_rule_invokes_the_receiver(template):
    template.has_resource_properties(
        "AWS::Events::Rule",
        {"ScheduleExpression": "rate(1 minute)", "State": "ENABLED"},
    )


def test_exactly_one_schedule_exists(template):
    """Two rules would double every sweep's fire budget."""
    template.resource_count_is("AWS::Events::Rule", 1)


def test_eventbridge_may_invoke_the_function(template):
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Principal": "events.amazonaws.com", "Action": "lambda:InvokeFunction"},
    )


@pytest.mark.parametrize(
    "metric_name, marker",
    [
        ("WindowExhausted", observability.WINDOW_EXHAUSTED_MARKER),
        ("FindingsRejected", observability.FINDINGS_REJECTED_MARKER),
    ],
)
def test_each_marker_has_a_metric_filter(template, metric_name, marker):
    template.has_resource_properties(
        "AWS::Logs::MetricFilter",
        {
            "FilterPattern": f'"{marker}"',
            "MetricTransformations": Match.array_with(
                [Match.object_like({"MetricName": metric_name, "DefaultValue": 0})]
            ),
        },
    )


@pytest.mark.parametrize("alarm_metric", ["WindowExhausted", "FindingsRejected"])
def test_each_new_metric_is_alarmed(template, alarm_metric):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"MetricName": alarm_metric, "Namespace": "Sentinel", "Threshold": 1},
    )


def test_a_paused_routine_is_never_alarmed(template):
    """A pause is deliberate. Alarming on it teaches people to ignore alarms."""
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    names = [a["Properties"].get("MetricName", "") for a in alarms.values()]

    assert "RoutinePaused" not in names


def test_an_autofix_failure_is_extracted_and_alarmed(template):
    template.has_resource_properties(
        "AWS::Logs::MetricFilter",
        Match.object_like(
            {
                "FilterPattern": '"AUTOFIX_FAILED"',
                "MetricTransformations": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "MetricName": "AutofixFailures",
                                "MetricNamespace": "Sentinel",
                                "DefaultValue": 0,
                            }
                        )
                    ]
                ),
            }
        ),
    )
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "AutofixFailures",
                "Namespace": "Sentinel",
                "Threshold": 1,
            }
        ),
    )


def test_the_lambda_bundle_is_built_for_the_function_architecture():
    """The bundle's platform must match the architecture the function runs on.

    An arm64 build host (any Apple Silicon Mac) otherwise installs aarch64
    wheels into an x86_64 function, which dies at import with
    Runtime.ImportModuleError. Pure-Python dependencies hid this for a long
    time; cryptography, pulled in by PyJWT[crypto], has a native module and
    no fallback, so it took the receiver down on deploy 2026-08-17.

    The bundling platform is a build-time input and never reaches the
    synthesized template, so this asserts on the stack source and pins it
    against the architecture the template does carry.
    """
    source = (Path(__file__).resolve().parent.parent / "infra" / "stacks" / "receiver_stack.py").read_text()

    assert 'platform="linux/amd64"' in source, (
        "the Lambda bundling must pin platform=linux/amd64; without it an "
        "arm64 build host ships aarch64 wheels to an x86_64 function"
    )


def test_the_function_architecture_matches_the_pinned_bundle_platform(template):
    """If the function ever moves to arm64, the bundling platform must follow."""
    source = (Path(__file__).resolve().parent.parent / "infra" / "stacks" / "receiver_stack.py").read_text()

    # x86_64 is the CDK default, so the template omits Architectures unless
    # it was set explicitly. Either way the two must agree.
    functions = template.find_resources("AWS::Lambda::Function")
    architectures = {
        tuple(body["Properties"].get("Architectures", ["x86_64"]))
        for body in functions.values()
    }

    assert architectures == {("x86_64",)}, (
        f"function architecture changed to {architectures}; update the "
        f"bundling platform in receiver_stack.py to match"
    )
    assert 'platform="linux/amd64"' in source
