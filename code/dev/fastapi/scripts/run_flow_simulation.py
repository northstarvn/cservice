#!/usr/bin/env python3
"""Run every real-life flow, print the blockages, and append them to BLOCKAGES.md.

    python3 scripts/run_flow_simulation.py                  # report only
    python3 scripts/run_flow_simulation.py --append         # also append to the log
    python3 scripts/run_flow_simulation.py --json          # machine-readable
    python3 scripts/run_flow_simulation.py --append --allow-repeat

Exit status is 1 when there is a blocker, so it can gate a pipeline. Warnings
alone exit 0: a warning is a reason to read the output, not a reason to stop a
release, and a gate that treats both as fatal is a gate people start ignoring.

**Appending is opt-in, and second-guarded.** Two reasons, both learned from
BLOCKAGES.md itself:

1. A simulator that appended on every run would fill the log with identical
   dated sections. After two runs nobody reads it, and a log nobody reads is
   indistinguishable from no log at all.
2. A wrong finding appended with the same authority as a real one is worse than
   no finding, because the next person cannot tell which is which. Every entry
   therefore carries its evidence, and ``--append`` is a deliberate act.

The runner writes nothing else, contacts nothing, and needs no database: the
probes call the engines in-process. That is deliberate -- a flow simulation that
required a running server would not run often enough to be worth having.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import real_life_flows as flows  # noqa: E402


def simulate(environment: str = "offline") -> tuple[list, list, dict]:
    runs = flows.run_all_flows(environment=environment)
    blockages = flows.collect_blockages(runs)
    return runs, blockages, flows.flow_gate_measurements(runs)


def render(runs, blockages, measurements, comparison=None) -> str:
    return flows.render_blockages_markdown(
        runs, blockages, comparison=comparison
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--append", action="store_true", help="append the findings to BLOCKAGES.md")
    parser.add_argument(
        "--allow-repeat",
        action="store_true",
        help="allow a second dated section even if one is already present",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    parser.add_argument(
        "--environment",
        default="offline",
        help="label recorded against the run; a shadow deployment passes its own name",
    )
    parser.add_argument(
        "--blockages",
        default="",
        help="path to BLOCKAGES.md (defaults to the one beside the fastapi root)",
    )
    args = parser.parse_args()

    runs, blockages, measurements = simulate(args.environment)
    markdown = render(runs, blockages, measurements)

    append_result = None
    if args.append:
        append_result = flows.append_to_blockages_md(
            markdown,
            path=Path(args.blockages) if args.blockages else None,
            allow_repeat=args.allow_repeat,
        )

    blockers = [row for row in blockages if row.severity == "blocker"]
    warnings = [row for row in blockages if row.severity == "warning"]

    if args.json:
        print(
            json.dumps(
                {
                    "environment": args.environment,
                    "measurements": measurements,
                    "blockages": [row.as_dict() for row in blockages],
                    "append": append_result,
                    "exit_code": 1 if blockers else 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if blockers else 0

    print(f"flows run    : {measurements['flows_run']}")
    print(f"flows failed : {measurements['flows_failed']}")
    print(f"subflows     : {measurements['subflows_run']} ({measurements['subflows_failed']} failed)")
    print(f"blockers     : {len(blockers)}")
    print(f"warnings     : {len(warnings)}")
    print()

    if not blockages:
        print("no blockage: every flow held its invariants across every persona it names.")
        print(
            "Read that as 'the engines still branch', not as 'one input works' -- "
            "the personas span loyal, abandoned, at-risk and dormant."
        )
    for row in blockages:
        print(f"[{row.severity:8}] {row.category} -- {row.blockage_id}")
        print(f"           {row.evidence.get('summary', '')}")
        print(f"           suggestion: {row.suggestion}")
        if row.error:
            print(f"           error: {row.error}")
        print()

    if append_result is not None:
        if append_result["written"]:
            print(f"appended {append_result['bytes']} bytes to {append_result['path']}")
        else:
            print(f"not appended: {append_result['reason']}")
        print()

    if append_result is None:
        print("nothing written. Pass --append to add these findings to BLOCKAGES.md.")

    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
