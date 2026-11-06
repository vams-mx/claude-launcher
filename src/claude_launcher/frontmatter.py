"""A tolerant reader for the YAML frontmatter of agent and skill files (`---` … `---`).

Only top-level `key: value` pairs are read: plain, quoted, or a folded/literal block
(`>` / `|`) on the indented lines that follow. Anything else is ignored. Reads are bounded and
never raise: a missing, unreadable or odd file gives {} (no frontmatter)."""

from __future__ import annotations

import re
from pathlib import Path

MAX_READ = 64 * 1024
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s?(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        return inner.replace("''", "'") if v[0] == "'" else inner.replace('\\"', '"')
    return v


def parse(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    key: str | None = None
    block: list[str] | None = None
    folded = False

    def flush() -> None:
        nonlocal key, block
        if key is not None and block is not None:
            out[key] = (" ".join(x.strip() for x in block if x.strip()) if folded
                        else "\n".join(x.strip() for x in block)).strip()
        key, block = None, None

    for line in lines[1:]:
        if line.strip() == "---":
            flush()
            return out
        if block is not None:
            if line.startswith((" ", "\t")) or not line.strip():
                block.append(line)
                continue
            flush()
        m = _KEY.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if v in (">", "|", ">-", "|-", ">+", "|+"):
            key, block, folded = k, [], v.startswith(">")
            continue
        out[k] = _unquote(v)
    return {}  # no closing ---: not frontmatter


def read(path: Path) -> dict[str, str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_READ)
    except OSError:
        return {}
    try:
        return parse(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - a broken file is "no frontmatter"
        return {}
