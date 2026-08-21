"""CloudFormation stack for the Sentry alert receiver."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
)
from constructs import Construct

from receiver.config import DEFAULT_CONFIG_FILENAME, ReceiverConfig
from receiver.observability import (
    AUTOFIX_FAILED_MARKER,
    DELIVERY_FAILURE_MARKER,
    FINDINGS_REJECTED_MARKER,
    WINDOW_EXHAUSTED_MARKER,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
METRIC_NAMESPACE = "Sentinel"


class ReceiverStack(Stack):
    """Receiver Lambda, its alert table, and its health alarming."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: ReceiverConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

        self.table = dynamodb.Table(
            self,
            "AlertTable",
            table_name=cfg.table_name,
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            # The investigation engine extends this table with investigation state; losing posted
            # messageIds would orphan every open thread.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Sparse: `due_pk` exists only while a row waits for a fire or for
        # findings, so the index is empty at steady state and a busy minute
        # holds a handful of rows. Nothing ever scans the table.
        self.table.add_global_secondary_index(
            index_name="due-index",
            partition_key=dynamodb.Attribute(
                name="due_pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="due_at", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Kept after delivery, unlike `due_pk`, so a session retrying its POST
        # for an already-delivered batch stays distinguishable from a token
        # that was never issued: the first is a 200 with no repost, the second
        # a 401. Projects ALL because `claim_batch` fans out straight from this
        # query and would otherwise need a follow-up read per row.
        #
        # DynamoDB refuses more than one GSI creation per stack update, and
        # the receiver's table ships with none, so the first investigation-engine deploy must add
        # due-index alone: run `cdk deploy -c skip_token_index=1`, then deploy
        # again without the flag. Inert once both indexes exist.
        if not self.node.try_get_context("skip_token_index"):
            self.table.add_global_secondary_index(
                index_name="token-index",
                partition_key=dynamodb.Attribute(
                    name="reply_token_hash", type=dynamodb.AttributeType.STRING
                ),
                projection_type=dynamodb.ProjectionType.ALL,
            )

        self.log_group = logs.LogGroup(
            self,
            "ReceiverLogs",
            log_group_name="/aws/lambda/sentinel-receiver",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = lambda_.Function(
            self,
            "Receiver",
            function_name="sentinel-receiver",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="receiver.handler.lambda_handler",
            memory_size=256,
            timeout=Duration.seconds(30),
            log_group=self.log_group,
            environment={
                "RECEIVER_CONFIG": f"/var/task/config/{DEFAULT_CONFIG_FILENAME}",
                "SENTRY_DSN": cfg.automation_dsn,
                "SENTASITY_ENV": "prod",
                "TABLE_NAME": cfg.table_name,
            },
            code=lambda_.Code.from_asset(
                str(REPO_ROOT),
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    # The function runs on x86_64, so the bundle must be built
                    # for it. Without this, an arm64 build host (any Apple
                    # Silicon Mac) installs aarch64 wheels and the function
                    # dies at import with Runtime.ImportModuleError. Pure
                    # Python deps hid this; cryptography, pulled in by
                    # PyJWT[crypto], has a native module and no fallback.
                    platform="linux/amd64",
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements-lambda.txt -t /asset-output "
                        "&& cp -r receiver config /asset-output",
                    ],
                ),
            ),
        )

        self.function_url = self.function.add_function_url(
            # Platform auth is NONE because Sentry signs its own deliveries and
            # every route verifies that signature itself.
            auth_type=lambda_.FunctionUrlAuthType.NONE
        )

        # The debounce window IS this interval: a row enqueued at any point in
        # a minute is picked up by the next pass, so there is no timer to
        # manage and no request held open. EventBridge invokes the function
        # directly rather than over the Function URL, so the sweep path carries
        # no HMAC and is not reachable from the internet.
        self.sweep_rule = events.Rule(
            self,
            "InvestigationSweep",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            targets=[events_targets.LambdaFunction(self.function)],
        )

        self.table.grant_read_write_data(self.function)

        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter{cfg.ssm_prefix}/*"
                ],
            )
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                # The SecureStrings use the AWS-managed SSM key, whose ARN is not
                # knowable at synth; the ViaService condition is the scope instead.
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"ssm.{self.region}.amazonaws.com"
                    }
                },
            )
        )

        self.alarm_topic = sns.Topic(
            self, "ReceiverAlarms", display_name="sentinel receiver alarms"
        )
        if cfg.alarm_email:
            self.alarm_topic.add_subscription(
                sns_subscriptions.EmailSubscription(cfg.alarm_email)
            )

        self.error_alarm = cloudwatch.Alarm(
            self,
            "ReceiverErrors",
            alarm_description=(
                "The Sentry alert receiver errored. Teams alerts may be silent."
            ),
            metric=self.function.metric_errors(
                period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        self.error_alarm.add_alarm_action(cloudwatch_actions.SnsAction(self.alarm_topic))

        # The alarm above cannot see a failed Teams post. The handler catches
        # BotError and returns a 500 response, which is a *successful*
        # invocation, so AWS/Lambda Errors stays at zero. That is the one
        # failure where the receiver is up, answering, and silently delivering
        # nothing, which since the rule migration is the whole alerting path.
        self.delivery_failures = self.log_group.add_metric_filter(
            "DeliveryFailures",
            filter_pattern=logs.FilterPattern.literal(
                f'"{DELIVERY_FAILURE_MARKER}"'
            ),
            metric_namespace=METRIC_NAMESPACE,
            metric_name="DeliveryFailures",
            metric_value="1",
            # Without this the metric reports nothing on healthy invocations,
            # and an alarm with no datapoints sits in INSUFFICIENT_DATA rather
            # than OK. A zero per matched-nothing period keeps it evaluating.
            default_value=0,
        )

        self.delivery_alarm = cloudwatch.Alarm(
            self,
            "ReceiverDeliveryFailures",
            alarm_description=(
                "The receiver could not post a card to Teams. The alert that "
                "triggered it was not delivered."
            ),
            metric=self.delivery_failures.metric(
                period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        self.delivery_alarm.add_alarm_action(
            cloudwatch_actions.SnsAction(self.alarm_topic)
        )

        # Two more log-derived metrics, both alarmed. Deliberately absent: any
        # alarm on a paused routine. A pause is the operator's own budget kill
        # switch, and paging somebody about a thing they just did would train
        # them to ignore the exhaustion alarm below, which nobody chose.
        for construct_id, marker, description in (
            (
                "WindowExhausted",
                WINDOW_EXHAUSTED_MARKER,
                "The Claude subscription window is spent. Investigations are "
                "being skipped.",
            ),
            (
                "FindingsRejected",
                FINDINGS_REJECTED_MARKER,
                "A session's findings failed schema validation. The stored "
                "prompt and the receiver disagree, and the thread got no findings.",
            ),
            (
                "AutofixFailures",
                AUTOFIX_FAILED_MARKER,
                "An autofix dispatch failed or never called back. The thread "
                "was promised a fix attempt and got a failure line.",
            ),
        ):
            metric_filter = self.log_group.add_metric_filter(
                construct_id,
                filter_pattern=logs.FilterPattern.literal(f'"{marker}"'),
                metric_namespace=METRIC_NAMESPACE,
                metric_name=construct_id,
                metric_value="1",
                # Same reason as DeliveryFailures: without a zero per
                # matched-nothing period the alarm sits in INSUFFICIENT_DATA
                # rather than OK.
                default_value=0,
            )
            alarm = cloudwatch.Alarm(
                self,
                f"Receiver{construct_id}",
                alarm_description=description,
                metric=metric_filter.metric(
                    period=Duration.minutes(5), statistic="Sum"
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(cloudwatch_actions.SnsAction(self.alarm_topic))

        CfnOutput(self, "FunctionUrl", value=self.function_url.url)
