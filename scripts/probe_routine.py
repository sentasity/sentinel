#!/usr/bin/env python3
"""Fire the probe routine and report what came back.

The routines API exposes no configuration read, so a fired session reporting
on itself is the only way to observe a routine's repository binding, connector
set, cloud environment, and network reach. Read this script's output together
with the session transcript: a missing POST plus a transcript showing the
attempt means egress is blocked, while no transcript means the session never
ran.

The probe is its own routine, holding its own prompt, so firing it never
displaces the investigator's. Its id and its token come from the deployment's
config and parameter store by default:

  scripts/probe_routine.py

A trigger token authorises exactly one routine, so the investigator's token
cannot fire the probe. Using the wrong one returns HTTP 401 and this script
reports a rejection.

Both values can be overridden for a routine that is not the configured one,
which is how you probe a routine before recording it:

  ROUTINE_ID=trig_... ROUTINE_TRIGGER_TOKEN=... scripts/probe_routine.py
"""

import json
import os
import sys

from receiver.config import ConfigError, get_secret, load_config
from receiver.routines import FireOutcome, RoutineClient


def resolve() -> tuple[str, str]:
    """The routine to fire and the token that authorises it.

    The environment wins so an unrecorded routine can still be probed. Config
    is the fallback rather than the only source, because a deployer probes a
    routine before they are confident enough to write its id down.
    """
    routine_id = os.environ.get("ROUTINE_ID", "")
    token = os.environ.get("ROUTINE_TRIGGER_TOKEN", "")
    if routine_id and token:
        return routine_id, token

    cfg = load_config()
    routine_id = routine_id or cfg.probe_routine_id
    if not routine_id:
        raise ConfigError(
            "no probe routine: set trigger.probe_routine_id in the config, or "
            "pass ROUTINE_ID"
        )
    token = token or get_secret(cfg.secret_name(cfg.probe_token_ref))
    return routine_id, token


def main() -> int:
    try:
        routine_id, token = resolve()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a missing parameter must read as one
        print(f"error: could not read the probe trigger token: {exc}", file=sys.stderr)
        return 2

    client = RoutineClient(routine_id, token)
    outcome, delay = client.fire({"probe": True})
    print(json.dumps({"outcome": outcome.value, "retry_after": delay}))

    if outcome is FireOutcome.FIRED:
        print(
            "fired. Read the session transcript and the receiver's log for "
            "/findings/probe."
        )
        return 0

    if outcome is FireOutcome.REJECTED:
        # The one rejection a deployer hits repeatedly, and the message the
        # API returns for it names the routine rather than the token, which
        # reads like the id is wrong when it is not.
        print(
            "probe did not fire: rejected. A trigger token authorises exactly "
            "one routine, so check this is the probe routine's own token and "
            "not the investigator's.",
            file=sys.stderr,
        )
        return 1

    print(f"probe did not fire: {outcome.value}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
