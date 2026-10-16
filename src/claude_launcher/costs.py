"""Context-cost estimates.

Numbers are *measured*, not guessed: `cm --measure` launches one headless
probe per item (plus a baseline with nothing selected) and records the
increase in input tokens of the first request, and the number of tools the
item adds. With Tool Search on, most MCP tools arrive as deferred names, so
the upfront cost is small; the full schema of a tool is paid only when Claude
searches for it. Items never measured show as "not measured".
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import probe as probe_mod
from .catalog import Catalog
from .paths import config_dir
from .plan import Selection, build_plan


@dataclass
class Cost:
    tokens: int  # upfront, Tool Search on (deferred tool names only)
    tools: int
    measured_at: str = ""
    full_tokens: int | None = None  # same item with ENABLE_TOOL_SEARCH=false (every schema in context)


def costs_path() -> Path:
    return config_dir() / "costs.json"


def load() -> dict[str, Cost]:
    try:
        doc = json.loads(costs_path().read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, Cost] = {}
    for k, v in (doc.get("items") or {}).items():
        try:
            full = v.get("full_tokens")
            out[k] = Cost(int(v["tokens"]), int(v.get("tools", 0)), v.get("measured_at", ""),
                          int(full) if full is not None else None)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save(items: dict[str, Cost], baseline: int | None = None) -> None:
    path = costs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"note": "input-token delta of the first request vs. an empty session, measured on Haiku with Tool Search",
           "baseline_tokens": baseline,
           "items": {k: {"tokens": c.tokens, "tools": c.tools, "full_tokens": c.full_tokens,
                         "measured_at": c.measured_at} for k, c in items.items()}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)


def item_keys(cat: Catalog) -> list[str]:
    keys = [s.key for s in cat.local] + [c.key for c in cat.connectors if c.verified]
    keys += [p.key for p in cat.plugins]
    if cat.cmux.available:
        keys.append("cmux")
    return keys


def selection_for(cat: Catalog, key: str) -> Selection:
    kind, _, name = key.partition(":")
    if kind == "mcp":
        return Selection(local=[name])
    if kind == "connector":
        return Selection(connectors=[c.name for c in cat.connectors if c.short == name])
    if kind == "plugin":
        return Selection(plugins=[name])
    if key == "cmux":
        return Selection(cmux=True)
    raise ValueError(key)


def selection_keys(cat: Catalog, sel: Selection) -> list[str]:
    keys = [f"mcp:{n}" for n in sel.local]
    keys += [c.key for c in cat.connectors if c.name in sel.connectors]
    keys += [f"plugin:{p}" for p in sel.plugins]
    if sel.cmux:
        keys.append("cmux")
    return keys


def estimate(costs: dict[str, Cost], keys: Iterable[str]) -> tuple[int, int, list[str]]:
    """-> (tokens, tools, unmeasured keys)"""
    tok = tools = 0
    missing: list[str] = []
    for k in keys:
        c = costs.get(k)
        if c is None:
            missing.append(k)
        else:
            tok += c.tokens
            tools += c.tools
    return tok, tools, missing


def measure(
    cat: Catalog,
    keys: list[str],
    *,
    claude_bin: str,
    progress: Callable[[str], None] = lambda _m: None,
    run_probe: Callable[..., probe_mod.ProbeResult] | None = None,
    full: bool = True,
) -> tuple[dict[str, Cost], int]:
    run_probe = run_probe or probe_mod.run
    costs = load()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def one(sel: Selection, no_tool_search: bool = False) -> probe_mod.ProbeResult:
        with tempfile.TemporaryDirectory(prefix="cm-measure-") as tmp:
            from .launcher import write_session_files

            plan = build_plan(cat, sel, claude_bin=claude_bin, session_dir=Path(tmp))
            write_session_files(plan)
            env = dict(plan.env_overrides)
            if no_tool_search:
                env["ENABLE_TOOL_SEARCH"] = "false"
            return run_probe(plan.argv, env, cwd=cat.cwd)

    progress("baseline (nothing selected)")
    base = one(Selection())
    if base.returncode != 0 or not base.input_tokens:
        raise RuntimeError(f"baseline probe failed: {base.error.strip()[:200]}")
    base_tools = len(base.mcp_tools)
    base_full = one(Selection(), True) if full else None
    for k in keys:
        progress(k)
        r = one(selection_for(cat, k))
        if r.returncode != 0 or not r.input_tokens:
            progress(f"  ! {k} failed: {r.error.strip()[:120]}")
            continue
        # tools = MCP tools the item adds (built-in MCP resource helpers excluded)
        c = Cost(max(0, r.input_tokens - base.input_tokens), max(0, len(r.mcp_tools) - base_tools), now)
        if base_full is not None and base_full.input_tokens and (c.tools or k.startswith("connector:")):
            rf = one(selection_for(cat, k), True)
            if rf.returncode == 0 and rf.input_tokens:
                c.full_tokens = max(0, rf.input_tokens - base_full.input_tokens)
        costs[k] = c
    save(costs, base.input_tokens)
    return costs, base.input_tokens


def fmt(c: Cost | None) -> str:
    if c is None:
        return "not measured"
    def k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    full = f" ({k(c.full_tokens)} if all loaded)" if c.full_tokens and c.full_tokens > c.tokens else ""
    return f"~{k(c.tokens)} tok{full} · {c.tools} tools"
