"""One command, or one timer: run every kaizen check and say what it found.

There are three subsystems in this area and each grew its own script, which is
the standard way a codebase ends up with a command nobody runs. The flow
simulation has ``run_flow_simulation.py``; the shadow has ``run_shadow_env.py``;
the ladder has no script at all and is reachable only over HTTP by an admin
holding a token. A maintainer asking "is this change safe to ship" had three
answers, two of which required remembering a filename and one of which required
being logged in as somebody.

So there is one entry point, :func:`run_sweep`, and everything else is a flag.
The CLI and the background worker both call it, which is the point: a check that
only runs when a human remembers it is a check whose staleness nobody can see.

**The worker is read-only by default, and that is not a default worth changing
blindly.** A timer that appends to ``BLOCKAGES.md`` fills the log with dated
sections nobody wrote, and the second-append guard would refuse every one of
them -- so an "automatic" writer is either a no-op or a flood, and neither is
useful. Automatic means *measured on a schedule and readable at an endpoint*;
writing to a file stays an act a person performs. ``CSERVICE_KAIZEN_APPEND=1``
opts into it for the pipelines that genuinely want it, and it is off here.

Gated by ``CSERVICE_KAIZEN_AUTORUN=1``, on the same idiom as the three existing
optional workers (``CSERVICE_PARTITION_WORKER``, ``CSERVICE_AUTO_RECOVERY``,
``CSERVICE_PIPELINE_AUTOSTART``): off by default so a developer boot and the
test suite are untouched, and enabled per deployment.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

#: Opt in per deployment. Same shape and same default as the existing workers.
KAIZEN_AUTORUN = os.getenv("CSERVICE_KAIZEN_AUTORUN", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
#: How often, once enabled. Generous on purpose: a sweep runs 21 flow-persona
#: pairs across 12 flows, resolves twelve modules and walks the route table, and
#: there is no value in re-deciding on a loop tight enough to show up in a
#: profile. (21 is pairs, not flows. The two numbers were conflated in a comment
#: here for a while, which is harmless until somebody greps for "21 flows".)
KAIZEN_INTERVAL_SECONDS = float(os.getenv("CSERVICE_KAIZEN_INTERVAL", "900"))
#: Whether the scheduled run may write to BLOCKAGES.md. Off by default; see the
#: module docstring for why an automatic writer is worse than no writer.
KAIZEN_APPEND = os.getenv("CSERVICE_KAIZEN_APPEND", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

#: The last sweep's result, plus a bounded history. In-process and per-worker on
#: purpose: a sweep result is a statement about *this* build, and persisting it
#: would invite a reader to trust a result from a process that is no longer
#: running. A stale answer that says it is stale beats a persisted answer that
#: does not.
_LAST: Optional[dict[str, Any]] = None
_HISTORY: list[dict[str, Any]] = []
#: A sweep is CPU-bound and does not need the event loop, but it must not run
#: concurrently with itself: two sweeps would double the CPU and race on
#: ``_LAST``. An asyncio lock is the cheapest thing that is actually correct.
_LOCK: Optional[asyncio.Lock] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def run_sweep(
    *,
    environment: str = "offline",
    include_shadow: bool = True,
    include_authz: bool = True,
    for_level: str = "l3_canary",
    append: bool = False,
    blockages_path: Optional[str] = None,
    allow_repeat: bool = False,
) -> dict[str, Any]:
    """Run every check, and return one payload describing all of them.

    Read-only unless ``append`` is passed. The three subsystems are run in the
    order a reader would want them: what is built, then whether it behaves, then
    whether it can be released safely.

    Never raises. A sweep that dies halfway leaves the reader with a partial
    picture and no indication that it is partial, which is worse than a sweep
    that finishes and reports one section as failed -- so each section is
    attempted independently and carries its own error.
    """
    from app import capability_audit, real_life_flows, release_ladder, shadow_env

    started = _now_iso()
    payload: dict[str, Any] = {
        "generated_at": started,
        "environment": str(environment),
        "for_level": str(for_level),
        "kaizen_autoload_version": 1,
    }

    # --- 1. flows, and what they could not exercise
    flows_section: dict[str, Any] = {}
    runs: list[Any] = []
    try:
        runs = real_life_flows.run_all_flows(environment=environment)
        flows_section["measurements"] = real_life_flows.flow_gate_measurements(runs)
        flows_section["ok"] = True
    except Exception as exc:  # noqa: BLE001 - a sweep must finish
        flows_section["ok"] = False
        flows_section["error"] = f"{type(exc).__name__}: {exc}"
        runs = []

    # --- 1b. what would change between two runs of the same tree.
    #
    # This exists because `shadow_divergence_within_tolerance` was unreachable
    # from anywhere the product runs: it needs `compare_runs`, and nothing in
    # `app/` called it. Every sweep therefore reported the gate as `unmeasured`,
    # `GATE_OPS` folds unmeasured to *fail*, and the canary rung could not be
    # reached by construction -- while an unmeasured gate and a passing one look
    # identical in the daily summary.
    #
    # **What this measurement is, stated exactly:** with no candidate tree to
    # compare against, both sides are the same tree, so this is a *reproducibility*
    # check, not a live-versus-shadow comparison. It catches a probe that reads
    # the wall clock, iterates a set, or depends on module state another flow
    # left behind -- all of which are real defects, and all of which would make a
    # later real comparison meaningless. It does **not** tell you a change is
    # safe.
    #
    # The `kind` key is on the payload so nobody has to guess which of the two
    # they are reading, and the measurements are only folded into the ladder when
    # the comparison actually ran -- failing to compare must not hand the gate a
    # zero it did not earn.
    divergence_section: dict[str, Any] = {}
    try:
        comparison = real_life_flows.compare_runs(
            runs, real_life_flows.run_all_flows(environment=environment)
        )
        divergence_section = {
            "ok": True,
            "kind": "self_reproducibility",
            "note": (
                "both sides are the same tree: this measures whether the flows "
                "reproduce, which is a precondition for a real shadow comparison "
                "and is not one"
            ),
            "comparison": comparison,
            "measurements": real_life_flows.divergence_measurements(comparison),
        }
    except Exception as exc:  # noqa: BLE001 - a sweep must finish
        divergence_section = {
            "ok": False,
            "kind": "self_reproducibility",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # --- 2. completeness, from the same run set
    completeness_section: dict[str, Any] = {}
    report: dict[str, Any] = {}
    try:
        report = real_life_flows.capability_report(runs)
        completeness_section = {
            "ok": True,
            "total": report["total"],
            "counts": report["counts"],
            "complete_share": report["complete_share"],
            "capabilities": report["capabilities"],
            "orphan_probes": report["orphan_probes"],
            "blind_probes": report["blind_probes"],
            "untested": report["untested"],
            "measurements": real_life_flows.completeness_measurements(
                report, for_level=for_level
            ),
        }
    except Exception as exc:  # noqa: BLE001
        completeness_section = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    # --- 3. blockages: flow findings and completeness findings, merged
    blockages_section: dict[str, Any] = {}
    try:
        flow_blockages = real_life_flows.collect_blockages(runs)
        completeness_blockages = (
            real_life_flows.completeness_blockages(report) if report else []
        )
        merged = real_life_flows.collect_blockages(runs)
        seen = {row.blockage_id for row in merged}
        for row in completeness_blockages:
            if row.blockage_id not in seen:
                merged.append(row)
        blockers = [row for row in merged if row.severity == "blocker"]
        warnings = [row for row in merged if row.severity == "warning"]
        blockages_section = {
            "ok": True,
            "blockers": len(blockers),
            "warnings": len(warnings),
            "flow_findings": len(flow_blockages),
            "completeness_findings": len(completeness_blockages),
            "items": [row.as_dict() for row in merged],
        }
    except Exception as exc:  # noqa: BLE001
        blockages_section = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- 4. shadow isolation
    shadow_section: dict[str, Any] = {}
    if include_shadow:
        try:
            verdict = shadow_env.evaluate_isolation(
                shadow_env.build_process_observations()
            )
            shadow_section = {
                "ok": True,
                "isolated": bool(verdict["isolated"]),
                "blocking_failures": list(verdict["blocking_failures"]),
                "advisory_failures": list(verdict["advisory_failures"]),
                "checks": verdict["checks"],
            }
        except Exception as exc:  # noqa: BLE001
            shadow_section = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- 5. authorization, because it is cheap and it has been wrong before
    authz_section: dict[str, Any] = {}
    if include_authz:
        try:
            authz_section = {
                "ok": True,
                **release_ladder.authorization_measurements(_live_routes()),
            }
        except Exception as exc:  # noqa: BLE001
            authz_section = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- 6. the ladder's own verdict at the requested rung
    ladder_section: dict[str, Any] = {}
    try:
        measurements: dict[str, Any] = {}
        measurements.update(flows_section.get("measurements") or {})
        measurements.update(completeness_section.get("measurements") or {})
        measurements.update(
            {
                key: value
                for key, value in (authz_section or {}).items()
                if key not in ("ok",)
            }
        )
        if shadow_section.get("ok"):
            measurements["shadow_isolated"] = bool(shadow_section["isolated"])
        # The divergence ratio reaches the ladder only when the comparison ran.
        # A sweep that could not compare leaves the gate unmeasured, which is the
        # honest outcome: a gate fed a default zero here would read as "the
        # candidate and live agreed" when in fact nothing was compared.
        if divergence_section.get("ok"):
            measurements.update(divergence_section["measurements"])
        candidate = release_ladder.ReleaseCandidate(
            candidate_id="sweep",
            code_version="code:this-process",
            data_version="data:this-process",
            level="l2_shadow",
            maintainer=os.getenv("CSERVICE_KAIZEN_MAINTAINER", "sweep"),
            measured=measurements,
        )
        report_gates = release_ladder.evaluate_gates(candidate, for_level=for_level)
        blocking = list(report_gates["blocking_failures"])
        unmeasured = list(report_gates.get("unmeasured_gates") or [])
        # A gate that was never measured is not a failed gate. On a developer
        # checkout almost everything is unmeasured -- there is no canary serving
        # traffic and no deployment ledger -- and a sweep that called that "no"
        # every day would be a sweep people learned to ignore, which is the same
        # as no sweep at all. Only gates that were measured and *failed* are
        # counted; the unmeasured ones are reported so their absence is visible.
        measured_failures = [name for name in blocking if name not in set(unmeasured)]
        ladder_section = {
            "ok": True,
            "for_level": str(for_level),
            "safe": bool(report_gates["safe"]),
            "blocking_failures": blocking,
            "measured_failures": measured_failures,
            "unmeasured_gates": unmeasured,
            "warnings": list(report_gates.get("warnings") or []),
            "gates": report_gates["gates"],
        }
    except Exception as exc:  # noqa: BLE001
        ladder_section = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    payload["flows"] = flows_section
    payload["divergence"] = divergence_section
    payload["completeness"] = completeness_section
    payload["blockages"] = blockages_section
    payload["shadow"] = shadow_section
    payload["authorization"] = authz_section
    payload["ladder"] = ladder_section

    # --- 7. optional: append the findings, which stays a person's decision
    if append and blockages_section.get("ok"):
        try:
            markdown = real_life_flows.render_blockages_markdown(
                runs,
                [
                    real_life_flows.Blockage(**_blockage_fields(item))
                    for item in blockages_section["items"]
                ],
            )
            payload["append"] = real_life_flows.append_to_blockages_md(
                markdown,
                path=None if not blockages_path else __import__("pathlib").Path(blockages_path),
                allow_repeat=allow_repeat,
            )
        except Exception as exc:  # noqa: BLE001
            payload["append"] = {"written": False, "reason": f"{type(exc).__name__}: {exc}"}

    payload["exit_code"] = exit_code(payload)
    payload["duration_ms"] = round(
        (
            datetime.fromisoformat(_now_iso()) - datetime.fromisoformat(started)
        ).total_seconds()
        * 1000.0,
        3,
    )
    payload["generated_at"] = _now_iso()
    return payload


def _blockage_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "blockage_id": str(item.get("blockage_id") or ""),
        "flow_id": str(item.get("flow_id") or ""),
        "persona_id": str(item.get("persona_id") or ""),
        "subflow_id": str(item.get("subflow_id") or ""),
        "severity": str(item.get("severity") or "warning"),
        "category": str(item.get("category") or ""),
        "conclusion": str(item.get("conclusion") or ""),
        "suggestion": str(item.get("suggestion") or ""),
        "evidence": dict(item.get("evidence") or {}),
        "error": str(item.get("error") or ""),
    }


def _live_routes() -> Any:
    from app.main import app  # local: app.main imports the routers

    return app.routes


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def exit_code(payload: Mapping[str, Any]) -> int:
    """0 clean, 1 a blocker, 2 the sweep itself could not complete.

    Three outcomes, because "no blockers" and "I could not tell" must never
    return the same number. A CI job that reads 0 as both green and broken is a
    CI job that has stopped running.
    """
    sections = (
        "flows",
        "divergence",
        "completeness",
        "blockages",
        "shadow",
        "authorization",
        "ladder",
    )
    for name in sections:
        section = payload.get(name)
        if isinstance(section, Mapping) and section.get("ok") is False:
            return 2
    blockages = payload.get("blockages") or {}
    if int(blockages.get("blockers") or 0) > 0:
        return 1
    shadow = payload.get("shadow") or {}
    if shadow.get("ok") and not bool(shadow.get("isolated")):
        return 1
    ladder = payload.get("ladder") or {}
    if ladder.get("ok") and (ladder.get("measured_failures") or []):
        return 1
    return 0


def summarize(payload: Mapping[str, Any]) -> str:
    """One screen of prose, for a terminal and for the commit message."""
    lines: list[str] = []
    flows = payload.get("flows") or {}
    if flows.get("ok"):
        m = flows.get("measurements") or {}
        lines.append(
            f"flows          {m.get('flows_run')} run, {m.get('flows_failed')} failed, "
            f"{m.get('subflows_exercised')}/{m.get('subflows_run')} subflows exercised"
            + (
                f", {m.get('subflows_deferred')} deferred (part not built)"
                if int(m.get("subflows_deferred") or 0)
                else ""
            )
        )
    else:
        lines.append(f"flows          FAILED: {flows.get('error')}")

    # The reproducibility check gets its own line, and names what kind of
    # comparison it is. Without the label a reader sees "divergence 0.0" and
    # concludes a candidate was compared against live, which nothing did.
    divergence = payload.get("divergence") or {}
    if divergence.get("ok"):
        dm = divergence.get("measurements") or {}
        lines.append(
            f"divergence     {dm.get('shadow_divergence_ratio')} "
            f"(self-reproducibility, tolerance {dm.get('divergence_tolerance')}, "
            f"{dm.get('outcome_flips')} outcome flip(s))"
        )
    else:
        lines.append(f"divergence     FAILED: {divergence.get('error')}")

    comp = payload.get("completeness") or {}
    if comp.get("ok"):
        counts = comp.get("counts") or {}
        parts = [f"{name}={count}" for name, count in counts.items() if count]
        lines.append(
            f"completeness   {comp.get('total')} parts, share="
            f"{comp.get('complete_share')}: {', '.join(parts)}"
        )
        measured = comp.get("measurements") or {}
        lines.append(
            f"               blocking at {measured.get('for_level')}: "
            f"{measured.get('capability_blocking')}"
            + (
                f" ({', '.join(measured.get('capability_blocking_detail') or [])})"
                if measured.get("capability_blocking_detail")
                else ""
            )
        )
        if comp.get("orphan_probes"):
            lines.append(
                f"               {len(comp['orphan_probes'])} probe(s) registered but "
                f"invoked by no flow: {', '.join(comp['orphan_probes'])}"
            )
        if comp.get("blind_probes"):
            lines.append(
                f"               {len(comp['blind_probes'])} probe(s) every persona "
                "answers alike, so a constant engine would pass them"
            )
    else:
        lines.append(f"completeness   FAILED: {comp.get('error')}")

    blockages = payload.get("blockages") or {}
    if blockages.get("ok"):
        lines.append(
            f"blockages      {blockages.get('blockers')} blocker(s), "
            f"{blockages.get('warnings')} warning(s) "
            f"({blockages.get('flow_findings')} from flows, "
            f"{blockages.get('completeness_findings')} from completeness)"
        )
    else:
        lines.append(f"blockages      FAILED: {blockages.get('error')}")

    shadow = payload.get("shadow") or {}
    if shadow:
        if shadow.get("ok"):
            lines.append(
                f"shadow         {'isolated' if shadow.get('isolated') else 'NOT ISOLATED'}"
                + (
                    f", blocking: {', '.join(shadow.get('blocking_failures') or [])}"
                    if not shadow.get("isolated")
                    else ""
                )
            )
        else:
            lines.append(f"shadow         FAILED: {shadow.get('error')}")

    authz = payload.get("authorization") or {}
    if authz.get("ok"):
        lines.append(
            f"authorization in_sync={authz.get('authz_in_sync')} "
            f"unlisted_public_writes={authz.get('unlisted_public_writes')} "
            f"({authz.get('public_write_count')} public write(s) on the record)"
        )
    elif authz:
        lines.append(f"authorization  FAILED: {authz.get('error')}")

    ladder = payload.get("ladder") or {}
    if ladder.get("ok"):
        measured = ladder.get("measured_failures") or []
        unmeasured = ladder.get("unmeasured_gates") or []
        lines.append(
            f"ladder         {ladder.get('for_level')}: "
            f"{'BLOCKED' if measured else 'no measured failure'}"
            + (f" by {', '.join(measured)}" if measured else "")
            + (
                f" (not measured here: {', '.join(unmeasured)})"
                if unmeasured
                else ""
            )
        )
    else:
        lines.append(f"ladder         FAILED: {ladder.get('error')}")

    append_result = payload.get("append")
    if isinstance(append_result, Mapping):
        lines.append(
            f"appended       {append_result.get('bytes')} bytes to {append_result.get('path')}"
            if append_result.get("written")
            else f"appended       not written: {append_result.get('reason')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The automatic trigger
# ---------------------------------------------------------------------------


async def run_sweep_async(**kwargs: Any) -> dict[str, Any]:
    """Serialise sweeps and keep the last result readable.

    The lock matters more than it looks: ``run_sweep`` resolves twelve modules
    and walks 274 route paths, and an interval shorter than that duration would
    otherwise stack runs on the event loop.
    """
    async with _lock():
        payload = await asyncio.to_thread(run_sweep, **kwargs)
    return _remember(payload)


def _remember(payload: Mapping[str, Any]) -> dict[str, Any]:
    global _LAST
    record = dict(payload)
    _LAST = record
    _HISTORY.append(
        {
            "generated_at": record.get("generated_at"),
            "exit_code": record.get("exit_code"),
            "for_level": record.get("for_level"),
            "blockers": (record.get("blockages") or {}).get("blockers"),
            "warnings": (record.get("blockages") or {}).get("warnings"),
            "complete_share": (record.get("completeness") or {}).get("complete_share"),
        }
    )
    # Bounded, because an unbounded log in a long-lived process is a leak with a
    # misleading name. Fifty sweeps at a quarter of an hour is over a day.
    del _HISTORY[:-50]
    return record


def last_sweep() -> Optional[dict[str, Any]]:
    """The most recent sweep, or ``None`` if none has run in this process."""
    return dict(_LAST) if _LAST is not None else None


def sweep_history() -> list[dict[str, Any]]:
    return [dict(row) for row in _HISTORY]


def autostatus() -> dict[str, Any]:
    """What the automatic trigger is doing, and whether it has ever done it.

    Publishes the configuration as well as the result, because the useful
    question is not "did it pass" but "is it even turned on" -- and a worker that
    silently never started looks exactly like a healthy one.
    """
    last = last_sweep()
    return {
        "autoload_version": 1,
        "autorun_enabled": KAIZEN_AUTORUN,
        "interval_seconds": KAIZEN_INTERVAL_SECONDS,
        "append_enabled": KAIZEN_APPEND,
        "maintainer": os.getenv("CSERVICE_KAIZEN_MAINTAINER", "sweep"),
        "runs": len(_HISTORY),
        "last": last,
        "history": sweep_history(),
        "staleness_seconds": (
            round(
                (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(str(last.get("generated_at")))
                ).total_seconds(),
                1,
            )
            if last and last.get("generated_at")
            else None
        ),
        "note": (
            "the scheduled sweep never writes to BLOCKAGES.md unless "
            "CSERVICE_KAIZEN_APPEND=1: an automatic writer fills the log with dated "
            "sections nobody wrote, and the duplicate guard would refuse every one of "
            "them. Automatic means measured on a schedule and readable here; appending "
            "stays an act a person performs."
        ),
    }


async def sweep_forever(
    *,
    stop: Optional[asyncio.Event] = None,
    environment: str = "offline",
    for_level: str = "l3_canary",
    include_shadow: bool = True,
    interval: Optional[float] = None,
    on_result: Optional[Callable[[Mapping[str, Any]], Any]] = None,
) -> None:
    """Run a sweep, wait, repeat. Cancelled by cancelling the task.

    Runs **immediately** on start rather than after the first interval. A worker
    that first sleeps means a deployment which enables it and then crashes inside
    the interval leaves no evidence it ever ran, and the failure looks like
    health.
    """
    seconds = float(interval if interval is not None else KAIZEN_INTERVAL_SECONDS)
    appended = bool(KAIZEN_APPEND)
    while True:
        try:
            payload = await run_sweep_async(
                environment=environment,
                include_shadow=include_shadow,
                for_level=for_level,
                # Read-only unless the deployment opted in. See autostatus().
                append=appended,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad run
            payload = {
                "generated_at": _now_iso(),
                "exit_code": 2,
                "error": f"{type(exc).__name__}: {exc}",
                "for_level": for_level,
            }
            _remember(payload)
        if on_result is not None:
            try:
                result = on_result(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - a logger must not kill the loop
                pass
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=seconds)
                return
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(seconds)


def to_json(payload: Mapping[str, Any]) -> str:
    """Stable JSON, for `--json` and for anything consuming the endpoint."""
    return json.dumps(payload, indent=2, sort_keys=True, default=str)