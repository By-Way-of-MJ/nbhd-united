#!/usr/bin/env python3
"""Fire a real container cron and report the full delivered database rows."""

import argparse
import json
import sys
import time
from datetime import UTC, datetime

from stack import STATE, docker, runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    args = parser.parse_args()
    started = time.monotonic()
    # Signed share sync publishes real runtime IDs; never invent a job payload.
    job = None
    while time.monotonic() - started < 90:
        path = STATE / "share/nbhd-cron-state.json"
        if path.exists():
            job = next((row for row in json.loads(path.read_text())["jobs"] if row["name"] == args.name), None)
            if job:
                break
        time.sleep(2)
    if not job:
        raise RuntimeError("Named job is not present in the container's real cron state")
    since = datetime.now(UTC).isoformat()
    result = docker(
        "exec",
        "nbhd-stage-assistant",
        "openclaw",
        "cron",
        "run",
        job["id"],
        "--expect-final",
        "--wait",
        "--wait-timeout",
        "10m",
        "--timeout",
        "600000",
        capture=True,
    )
    rows = json.loads(runtime("outbound", since))
    print(
        json.dumps(
            {
                "status": "delivered" if rows else "nothing_sent",
                "job": args.name,
                "seconds_taken": round(time.monotonic() - started, 2),
                "outbound": rows,
                "runtime_exit_code": result.returncode,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
