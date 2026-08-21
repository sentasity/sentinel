#!/usr/bin/env python3
"""Fire the probe routine and report what came back.

The routines API exposes no configuration read, so a fired session reporting
on itself is the only way to observe a routine's repository binding, connector
set, cloud environment, and network reach. Read this script's output together
with the session transcript: a missing POST plus a transcript showing the
attempt means egress is blocked, while no transcript means the session never
ran.

  ROUTINE_ID=trig_... ROUTINE_TRIGGER_TOKEN=... scripts/probe_routine.py
"""

import json
import os
import sys

from receiver.routines import FireOutcome, RoutineClient


def main() -> int:
    routine_id = os.environ.get("ROUTINE_ID", "")
    token = os.environ.get("ROUTINE_TRIGGER_TOKEN", "")
    if not routine_id or not token:
        print(
            "error: ROUTINE_ID and ROUTINE_TRIGGER_TOKEN must both be set",
            file=sys.stderr,
        )
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

    print(f"probe did not fire: {outcome.value}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
