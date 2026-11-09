"""Forward the receiver's CloudWatch alarms to an ntfy topic.

The alarm topic's email is easy to miss, and an alarm is only useful if
someone acts on it. ntfy delivers to every device subscribed to a topic, so
an operator can route these to a phone or a desktop alert that stays on
screen. This function is deployed only when the configuration names an ntfy
server; without one, alarms go to email alone, as before.

What it sends is the alarm's own text: its description (which lives in this
repository's stack definition), its name, and when it fired. No Sentry data,
no thread or issue links, nothing from a payload. On a public ntfy server the
topic name is the only access control, so it is kept in SSM as a
SecureString, and anyone who learns it can read these messages and publish
to the topic.

Deliberately standalone: it imports nothing from the rest of the receiver,
so it runs small and never loads the receiver's configuration or SDKs.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

# ntfy's "high": a subscriber can treat it as needing attention now.
PRIORITY = 4
TIMEOUT_SECONDS = 10

_topic: str | None = None


def _ssm_client():
    return boto3.client("ssm")


def _topic_name() -> str:
    """The topic, read once per container."""
    global _topic
    if _topic is None:
        _topic = _ssm_client().get_parameter(
            Name=os.environ["NTFY_TOPIC_PARAMETER"], WithDecryption=True
        )["Parameter"]["Value"].strip()
    return _topic


def _first_sentence(text: str) -> str:
    return text.split(". ", 1)[0].rstrip(".")


def build_message(alarm: dict, topic: str) -> dict | None:
    """The ntfy publish body for one alarm notification, or None when the
    notification is not a move into ALARM."""
    if alarm.get("NewStateValue") != "ALARM":
        return None
    description = str(alarm.get("AlarmDescription") or "A receiver alarm fired.")
    return {
        "topic": topic,
        "title": f"Sentinel: {_first_sentence(description)}",
        "message": (
            f"{description}\n\n"
            f"Alarm: {alarm.get('AlarmName', '')}\n"
            f"At: {alarm.get('StateChangeTime', '')}"
        ),
        "priority": PRIORITY,
        "tags": ["sentinel"],
    }


def _publish(server: str, message: dict) -> None:
    request = urllib.request.Request(
        server,
        data=json.dumps(message).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
        pass


def lambda_handler(event: dict, context) -> dict:
    """Publish every ALARM notification in an SNS event.

    A record that is not an alarm notification is skipped rather than raised:
    raising would retry the whole event and alarm on this function, for a
    message that will never parse. A failed publish does raise, so Lambda's
    asynchronous retries apply; the email subscription is unaffected.
    """
    server = os.environ["NTFY_SERVER"]
    published = skipped = 0
    for record in event.get("Records") or []:
        try:
            alarm = json.loads(record["Sns"]["Message"])
        except (KeyError, TypeError, ValueError):
            LOG.warning("skipping an SNS record that is not an alarm notification")
            skipped += 1
            continue
        message = build_message(alarm, _topic_name()) if isinstance(alarm, dict) else None
        if message is None:
            skipped += 1
            continue
        _publish(server, message)
        LOG.info("published alarm %s", alarm.get("AlarmName", ""))
        published += 1
    return {"published": published, "skipped": skipped}
