#!/usr/bin/env python3
"""Check that a shadow deployment cannot reach live, and refuse to continue if it can.

    python3 scripts/run_shadow_env.py --verify          # report the verdict
    python3 scripts/run_shadow_env.py --verify --strict # non-zero on any failure

This is the boot guard for the shadow process. Run it *before* starting uvicorn
in the shadow, not after -- a shadow that is already running against the live
database has already answered the question the guard asks, and the answer was
"yes, it writes live".

**Why this is a script and not a middleware.** The claim being made is that the
shadow cannot write live, and a middleware can only make claims about requests it
sees. The actual mechanism is boring and structural: the shadow process reads
its target from ``CSERVICE_SHADOW_DATABASE_URL``, which has no fallback to
``DATABASE_URL``, and this guard compares the two identities and exits non-zero
if they are the same database. The identity is ``(host, port, database)`` --
deliberately not the host alone, because the expensive part of this schema is
the schema and not the server, and a guard that demanded a separate host would
be refused by every developer running both locally.

**Why the check fails closed.** Any isolation check whose input nobody measured
is reported as failed. A guard that treats "we did not check" as "fine" converts
an unmeasured risk into a published safety claim, and the published number is
what an operator trusts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import shadow_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify", action="store_true", help="run the checks (the default)")
    parser.add_argument(
        "--strict", action="store_true", help="treat an advisory failure as a failure too"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    parser.add_argument(
        "--shadow-url",
        default="",
        help="verify this url instead of CSERVICE_SHADOW_DATABASE_URL (useful for testing the guard)",
    )
    args = parser.parse_args()

    observations = shadow_env.build_process_observations(
        shadow_url=args.shadow_url or None
    )
    verdict = shadow_env.evaluate_isolation(observations)
    directions = shadow_env.feed_direction_report()

    failed = verdict["isolated"] is False
    advisories = bool(verdict["advisory_failures"])
    # --strict is for CI, where an unmeasured check should stop the build rather
    # than wait for somebody to read the advisory list.
    exit_code = 1 if failed or (args.strict and advisories) else 0

    if args.json:
        print(
            json.dumps(
                {"verdict": verdict, "directions": directions, "exit_code": exit_code},
                indent=2,
                sort_keys=True,
            )
        )
        return exit_code

    live_url = shadow_env.live_database_url()
    shadow_url = shadow_env.shadow_database_url() or args.shadow_url
    live_id = shadow_env.database_identity(live_url)
    shadow_id = shadow_env.database_identity(shadow_url)

    print(f"live   url : {live_url or '(unset)'}")
    print(f"live   db  : {live_id.get('host')}:{live_id.get('port')}/{live_id.get('database')}")
    print(f"shadow url : {shadow_url or '(unset)'}")
    print(f"shadow db  : {shadow_id.get('host')}:{shadow_id.get('port')}/{shadow_id.get('database')}")
    print(f"process env: {shadow_env.process_environment_name() or '(unset)'}")
    print()

    for check in verdict["checks"]:
        mark = "ok  " if check["passed"] else "FAIL"
        print(f"  [{mark}] {check['check_id']:22} ({check['severity']}) {check['detail']}")
        if not check["passed"]:
            print(f"         remedy: {check['remedy']}")
    print()

    print(f"channels    : {directions['channels']} ({directions['forbidden_direction']} forbidden)")
    print(f"one-way     : {directions['one_way']}")
    for path in shadow_env.leakage_paths():
        print(f"  LEAKAGE  {path}")
    print()

    if verdict["isolated"]:
        print("isolated: no blocking check failed.")
        if advisories:
            print(f"advisories : {', '.join(verdict['advisory_failures'])} (advisory; --strict would fail)")
        print()
        print("Safe to start the shadow process. It still cannot reach live by construction;")
        print("this guard is the evidence, not the mechanism.")
        return exit_code

    print("NOT ISOLATED. Do not start the shadow process.")
    print(f"blocking failures: {', '.join(verdict['blocking_failures'])}")
    print()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
