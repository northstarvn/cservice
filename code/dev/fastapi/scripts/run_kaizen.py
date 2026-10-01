#!/usr/bin/env python3
"""Every kaizen check, one command, or on a timer.

    python3 scripts/run_kaizen.py                  # report, read-only
    python3 scripts/run_kaizen.py --level l4_live  # as if promoting to live
    python3 scripts/run_kaizen.py --append         # also record in BLOCKAGES.md
    python3 scripts/run_kaizen.py --json           # machine-readable
    python3 scripts/run_kaizen.py --daemon         # run on a loop instead of once
    python3 scripts/run_kaizen.py --status         # what the timer has been doing

Exit status is the answer to one question -- *can this change be released at the
level I asked about?*

* ``0`` nothing measured is wrong.
* ``1`` something measured is wrong: a flow failed, a part is a stub, a mutating
  public route is unlisted, the shadow can reach live, or a promotion gate failed
  **with evidence**.
* ``2`` the sweep could not complete, so the answer is unknown.

The third is the one people get wrong. A gate that treats "no blockers" and "I
could not tell" as the same number is a gate that has stopped running, and it is
separated here for exactly that reason.

**A gate that was never measured is not a failed gate.** On a developer checkout
there is no canary serving traffic and no deployment ledger, so most gates are
unmeasured by definition. A sweep that reported "no" every day would be a sweep
people learned to ignore. Unmeasured gates are listed in the output so their
absence is visible; only gates that were measured *and failed* change the exit
status.

Requires no database and starts no server: the probes call the engines
in-process. A check that needed a running deployment would not run often enough
to be worth having.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import kaizen_runner as runner  # noqa: E402


def _print_findings(payload: dict, limit: int) -> None:
    """The findings, in prose. Bounded, because an unbounded list is a scroll."""
    items = (payload.get("blockages") or {}).get("items") or []
    if not items:
        print("no blockage found.")
        return
    for row in items[:limit]:
        print(f"[{row.get('severity'):8}] {row.get('category')} -- {row.get('blockage_id')}")
        evidence = row.get("evidence") or {}
        for line in evidence.get("evidence") or ([evidence.get("detail")] if evidence.get("detail") else []):
            if line:
                print(f"           {line}")
        print(f"           conclusion: {row.get('conclusion')}")
        print(f"           suggestion: {row.get('suggestion')}")
        if row.get("error"):
            print(f"           error: {row['error']}")
        print()
    if len(items) > limit:
        print(f"... {len(items) - limit} more. Re-run with --json for all of them.")
        print()


def _print_capabilities(payload: dict) -> None:
    rows = (payload.get("completeness") or {}).get("capabilities") or []
    unfinished = [row for row in rows if row.get("state") != "complete"]
    if not unfinished:
        print("completeness   every declared part is complete.")
        return
    print("completeness, by state:")
    for row in unfinished:
        print(f"  [{row.get('state'):13}] {row.get('kind'):8} {row.get('capability_id')}")
        for line in row.get("evidence") or []:
            if line:
                print(f"                  {line}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Exit status")[1].split("Requires no database")[0],
    )
    parser.add_argument(
        "--level",
        default="l3_canary",
        help="grade completeness and gates for this maturity level (default: l3_canary)",
    )
    parser.add_argument("--append", action="store_true", help="append findings to BLOCKAGES.md")
    parser.add_argument(
        "--allow-repeat",
        action="store_true",
        help="allow a second dated section even if one is already present",
    )
    parser.add_argument(
        "--blockages",
        default="",
        help="path to BLOCKAGES.md (defaults to the one beside the fastapi root)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    parser.add_argument(
        "--no-shadow", action="store_true", help="skip the isolation checks"
    )
    parser.add_argument(
        "--environment",
        default="offline",
        help="label recorded against the run; a shadow deployment passes its own name",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="how many findings to print before truncating (default: 25)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="run on a loop instead of once; see CSERVICE_KAIZEN_INTERVAL",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="seconds between sweeps in --daemon mode (default: CSERVICE_KAIZEN_INTERVAL)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print what the in-process timer has done and exit",
    )
    args = parser.parse_args()

    if args.status:
        print(runner.to_json(runner.autostatus()))
        return 0

    if args.daemon:
        interval = args.interval or runner.KAIZEN_INTERVAL_SECONDS

        def _announce(payload: dict) -> None:
            print(f"--- sweep {payload.get('generated_at')} exit={payload.get('exit_code')}")
            print(runner.summarize(payload))
            print(flush=True)

        print(f"sweeping every {interval:g}s. Ctrl-C to stop.", flush=True)
        try:
            asyncio.run(
                runner.sweep_forever(
                    environment=args.environment,
                    for_level=args.level,
                    interval=interval,
                    on_result=_announce,
                )
            )
        except KeyboardInterrupt:
            print("\nstopped.")
        return 0

    payload = runner.run_sweep(
        environment=args.environment,
        include_shadow=not args.no_shadow,
        for_level=args.level,
        append=args.append,
        blockages_path=args.blockages or None,
        allow_repeat=args.allow_repeat,
    )

    if args.json:
        print(runner.to_json(payload))
        return int(payload.get("exit_code") or 0)

    print(runner.summarize(payload))
    print()
    _print_capabilities(payload)
    _print_findings(payload, max(0, args.limit))
    if not args.append:
        print(
            "nothing written. Pass --append to add these findings to BLOCKAGES.md; "
            "the timer never writes there unless CSERVICE_KAIZEN_APPEND=1."
        )
    return int(payload.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())