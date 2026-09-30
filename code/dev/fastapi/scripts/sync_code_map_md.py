#!/usr/bin/env python3
"""Sync the multi-line rendering in CODE_MAP.md with CODE_MAP.newick.

Rule 4 says the single-line Newick file is the source of truth and that
`CODE_MAP.md` may keep a readable multi-line rendering. A *stale* rendering is
worse than none: a reader who trusts it is reading a tree that no longer
exists, and the sixth pass found the rendering had silently drifted by seven
leaves. So the rendering is generated, and this script verifies token equality
rather than asking anyone to eyeball it.

Usage:
    python3 scripts/sync_code_map_md.py           # rewrite the block in CODE_MAP.md
    python3 scripts/sync_code_map_md.py --check   # verify only, non-zero on drift
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
NEWICK = ROOT / "CODE_MAP.newick"
MARKDOWN = ROOT / "CODE_MAP.md"

START = "## Current Map — `CODE_MAP.newick`"
FENCE_OPEN = "```\n"
FENCE_CLOSE = "\n```"

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tree_body() -> str:
    for line in NEWICK.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            return line.strip()
    raise SystemExit("CODE_MAP.newick has no tree line")


def render() -> str:
    """Render the single-line Newick as the multi-line form used in the doc.

    A node's opening paren is emitted at the current indent and its first child
    continues on the same line, so `((a,b),c)d` reads as three indented lines
    rather than doubling the line count with lone parens.
    """
    body = _tree_body()
    lines: list[str] = []
    buffer = ""
    depth = 0
    for char in body:
        if char == "(":
            if buffer.strip():
                lines.append("  " * depth + buffer)
            buffer = "("
            depth += 1
        elif char == ",":
            lines.append("  " * depth + buffer + ",")
            buffer = ""
        elif char == ")":
            lines.append("  " * depth + buffer + ")")
            depth -= 1
            buffer = ""
        else:
            buffer += char
    if buffer.strip():
        lines.append("  " * depth + buffer)
    return "\n".join(lines)


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _locate_block(text: str) -> tuple[int, int]:
    """Line indices of the fenced block that holds the rendering."""
    start = text.index(START)
    fence = text.index(FENCE_OPEN, start) + len(FENCE_OPEN)
    close = text.index(FENCE_CLOSE, fence)
    return fence, close


def main() -> int:
    rendered = render()
    text = MARKDOWN.read_text(encoding="utf-8")
    begin, end = _locate_block(text)
    current = text[begin:end]

    if "--check" in sys.argv:
        if _tokens(current) != _tokens(rendered):
            missing = set(_tokens(current)) - set(_tokens(rendered))
            extra = set(_tokens(rendered)) - set(_tokens(current))
            print(
                "CODE_MAP.md rendering has drifted from CODE_MAP.newick\n"
                f"  only in the document: {sorted(missing)}\n"
                f"  only in the newick:   {sorted(extra)}"
            )
            return 1
        print(
            f"in sync: {len(_tokens(rendered))} tokens "
            f"({len(rendered.splitlines())} rendered lines)"
        )
        return 0

    MARKDOWN.write_text(
        text[:begin] + rendered + text[end:], encoding="utf-8"
    )
    print(
        f"synced {len(_tokens(rendered))} tokens "
        f"({len(rendered.splitlines())} rendered lines)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
