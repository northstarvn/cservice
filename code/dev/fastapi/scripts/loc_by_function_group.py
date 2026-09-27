#!/usr/bin/env python3
"""Rank backend function groups by lines of code (LOC).

Companion to ``scripts/build_code_map.py``. The code map answers *what business
surfaces exist*; this script answers *how big each function group is*, so thin
groups (the ones that are under-configured and most likely to need expansion)
can be found mechanically instead of by eye.

A "function group" is a module of ``app/`` mapped to the node label it occupies
in ``CODE_MAP.newick`` (layer.module, e.g. ``infra.db`` or ``routers.chat``).
Groups sharing a parent domain are also reported, because a thin *domain* can
be made of several thin modules.

Usage:
    python3 scripts/loc_by_function_group.py                 # full ranking
    python3 scripts/loc_by_function_group.py --top 20        # thinnest 20
    python3 scripts/loc_by_function_group.py --json          # machine readable
    python3 scripts/loc_by_function_group.py --min-loc 400   # only thin ones

Counts are physical source lines (blank lines and comments included), so the
number is comparable across modules but slightly overstates *code*. Blank-line
and docstring shares are reported separately to keep the ranking honest.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

# module path (relative to app/) -> code-map node label
MODULE_NODES: dict[str, str] = {
    "main.py": "main",
    "db.py": "db",
    "security.py": "security",
    "deps.py": "auth_deps",
    "i18n.py": "localization",
    "tenant_router.py": "tenant_router",
    "partition_manager.py": "partition_manager",
    "hsm_signer.py": "hsm_signer",
    "biometric_vault.py": "biometric_vault",
    "risk_evaluator.py": "risk_evaluator",
    "cell_matrix.py": "cell_matrix",
    "high_throughput_pipeline.py": "high_throughput_pipeline",
    "protobuf_transaction_spec.py": "protobuf_transaction_spec",
    "model_versioning.py": "model_versioning",
    "simulation_engine.py": "simulation_engine",
    "explainability.py": "explainability",
    "optimistic_locking.py": "optimistic_locking",
    "rule_engine.py": "rule_engine",
    "regional_policy.py": "regional_policy",
    "model_bases.py": "model_bases",
    "models.py": "models",
    "routers/users.py": "users",
    "routers/bookings.py": "bookings",
    "routers/chat.py": "chat",
    "routers/topics.py": "topics",
    "routers/audit.py": "audit",
    "services/chat_analytics.py": "chat_analytics",
    "services/topics.py": "topics",
    "services/bookings.py": "bookings",
    "services/policy_scoring.py": "policy_scoring",
    "services/retention.py": "retention",
    "services/retention_snapshots.py": "retention_snapshot_ops",
    "services/loyalty_journey.py": "loyalty_journey",
    "services/activity_tree.py": "activity_tree",
    "services/communication_strategy.py": "communication_strategy",
    "services/efficiency_audit.py": "efficiency_audit",
    "services/arrears_payments.py": "arrears_payments",
    "services/points_exchange.py": "points_exchange",
    "services/recovery_playbooks.py": "recovery_playbooks",
    "services/audit_log.py": "audit_log",
    "schemas/schemas.py": "schemas_core",
    "schemas/chat.py": "schemas_chat",
    "schemas/audit.py": "schemas_audit",
}

# module -> layer, and module -> parent domain inside that layer
LAYER_OF: dict[str, str] = {}
for _mod in MODULE_NODES:
    if "/" not in _mod:
        _layer = "infra"
    elif _mod.startswith("routers/"):
        _layer = "routers"
    elif _mod.startswith("services/"):
        _layer = "services"
    elif _mod.startswith("schemas/"):
        _layer = "schemas"
    else:
        _layer = "models"
    LAYER_OF[_mod] = _layer

# parent domain for the multi-module nodes (matches build_code_map.py nesting)
PARENT_OF: dict[str, str] = {
    "tenant_router": "multi_tenant",
    "partition_manager": "multi_tenant",
    "hsm_signer": "zero_trust",
    "biometric_vault": "zero_trust",
    "risk_evaluator": "zero_trust",
    "cell_matrix": "zero_trust",
    "high_throughput_pipeline": "event_pipeline",
    "protobuf_transaction_spec": "event_pipeline",
    "model_versioning": "decision_intelligence",
    "simulation_engine": "decision_intelligence",
    "explainability": "decision_intelligence",
    "optimistic_locking": "decision_intelligence",
    "rule_engine": "business_rules",
    "regional_policy": "business_rules",
    "model_bases": "models",
}


@dataclass
class GroupStat:
    layer: str
    domain: str
    node: str
    module: str
    total: int
    code: int
    blank: int
    comment: int
    public_defs: int
    classes: int
    config_tables: int


def _line_stats(path: Path) -> dict[str, int]:
    """Split a module into blank / comment / code lines without executing it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return {"total": 0, "code": 0, "blank": 0, "comment": 0}
    import io
    import tokenize

    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    blank = {i for i, text in enumerate(lines, start=1) if not text.strip()}
    doc_lines: set[int] = set()
    comment: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            comment.add(tok.start[0])
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc_lines.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    total = len(lines)
    blank_n = len(blank)
    comment_n = len(comment - blank)
    doc_n = len(doc_lines - blank - comment)
    return {
        "total": total,
        "code": max(0, total - blank_n - comment_n - doc_n),
        "blank": blank_n,
        "comment": comment_n + doc_n,
    }


def _def_counts(path: Path) -> tuple[int, int, int]:
    """(# public defs, # classes, # module-level UPPER_CASE config tables)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return 0, 0, 0
    public = classes = tables = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                public += 1
        elif isinstance(node, ast.ClassDef):
            classes += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper() and isinstance(
                    node.value, (ast.List, ast.Dict, ast.Set, ast.Tuple)
                ):
                    tables += 1
    return public, classes, tables


def collect() -> list[GroupStat]:
    stats: list[GroupStat] = []
    for module, node in MODULE_NODES.items():
        path = APP / module
        if not path.exists():
            print(f"warning: missing module {module}", file=sys.stderr)
            continue
        lines = _line_stats(path)
        public, classes, tables = _def_counts(path)
        layer = LAYER_OF[module]
        stats.append(
            GroupStat(
                layer=layer,
                domain=PARENT_OF.get(node, layer),
                node=node,
                module=module,
                total=lines["total"],
                code=lines["code"],
                blank=lines["blank"],
                comment=lines["comment"],
                public_defs=public,
                classes=classes,
                config_tables=tables,
            )
        )
    return sorted(stats, key=lambda s: s.total)


def render(groups: list[GroupStat], top: int | None) -> str:
    rows = groups[:top] if top else groups
    out = [
        f"{'LOC':>6} {'code':>6} {'blnk':>5} {'cmnt':>5} {'defs':>5} {'cls':>4} {'cfg':>4}  "
        f"{'code-map node':<42} module",
        "-" * 128,
    ]
    for g in rows:
        out.append(
            f"{g.total:>6} {g.code:>6} {g.blank:>5} {g.comment:>5} {g.public_defs:>5} "
            f"{g.classes:>4} {g.config_tables:>4}  "
            f"{g.layer + '.' + g.node:<42} app/{g.module}"
        )
    out.append("-" * 128)
    out.append(
        f"{sum(g.total for g in groups):>6} {' ':>6} {' ':>5} {' ':>5} "
        f"{sum(g.public_defs for g in groups):>5} {sum(g.classes for g in groups):>4} "
        f"{sum(g.config_tables for g in groups):>4}  {len(groups)} function groups"
    )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=None, help="show only the N thinnest")
    ap.add_argument("--min-loc", type=int, default=0, help="only groups at or below this LOC")
    ap.add_argument("--domain", action="store_true", help="roll modules up into their domain")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    groups = collect()
    if args.min_loc:
        groups = [g for g in groups if g.total <= args.min_loc]

    if args.domain:
        rolled: dict[tuple[str, str], GroupStat] = {}
        for g in groups:
            key = (g.layer, g.domain)
            if key not in rolled:
                rolled[key] = GroupStat(
                    layer=g.layer, domain=g.domain, node=g.domain, module="",
                    total=0, code=0, blank=0, comment=0,
                    public_defs=0, classes=0, config_tables=0,
                )
            acc = rolled[key]
            acc.total += g.total
            acc.code += g.code
            acc.blank += g.blank
            acc.comment += g.comment
            acc.public_defs += g.public_defs
            acc.classes += g.classes
            acc.config_tables += g.config_tables
        groups = sorted(rolled.values(), key=lambda s: s.total)

    if args.json:
        print(json.dumps([asdict(g) for g in groups], indent=2))
    else:
        print(render(groups, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
