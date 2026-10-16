"""Non-destructive health checks (GET only, short timeout)."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request

from .catalog import LocalServer

DEFAULT_AGENTMEMORY_URL = "http://localhost:3111"
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_refs(value: str, env: dict[str, str] | None = None) -> str:
    """Expand ${VAR} / ${VAR:-default} like Claude Code does for --mcp-config
    (verified on 2.1.281: an unset ${VAR} without default stays literal)."""
    env = os.environ if env is None else env

    def sub(m: re.Match[str]) -> str:
        val = env.get(m.group(1))
        if val:
            return val
        if m.group(2) is not None:
            return m.group(2)
        return m.group(0) if val is None else val

    return _REF_RE.sub(sub, value)


def agentmemory_url(srv: LocalServer, env: dict[str, str] | None = None) -> str | None:
    cfg_env = srv.config.get("env") or {}
    args = " ".join(str(a) for a in srv.config.get("args", []))
    if "AGENTMEMORY_URL" in cfg_env or "agentmemory" in args or srv.name == "agentmemory":
        url = expand_refs(str(cfg_env.get("AGENTMEMORY_URL") or ""), env).strip()
        if not url or "${" in url:
            url = DEFAULT_AGENTMEMORY_URL
        return url.rstrip("/")
    return None


def livez(base: str, timeout: float = 0.8) -> str:
    """'backend up' / 'backend down' / 'backend HTTP <code>' for an AgentMemory base URL.
    /agentmemory/livez is unauthenticated in every AgentMemory version."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/agentmemory/livez", timeout=timeout) as r:  # noqa: S310
            return "backend up" if r.status == 200 else f"backend HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        return f"backend HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError):
        return "backend down"


def check(srv: LocalServer, timeout: float = 0.8) -> str | None:
    """-> 'backend up' / 'backend down' / None when no check applies.
    Only AgentMemory has a known liveness endpoint (/agentmemory/livez)."""
    base = agentmemory_url(srv)
    return livez(base, timeout) if base else None
