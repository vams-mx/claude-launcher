"""Non-destructive health checks (GET only, short timeout)."""

from __future__ import annotations

import urllib.error
import urllib.request

from .catalog import LocalServer


def agentmemory_url(srv: LocalServer) -> str | None:
    env = srv.config.get("env") or {}
    args = " ".join(str(a) for a in srv.config.get("args", []))
    if "AGENTMEMORY_URL" in env or "agentmemory" in args or srv.name == "agentmemory":
        return str(env.get("AGENTMEMORY_URL") or "http://localhost:3111").rstrip("/")
    return None


def check(srv: LocalServer, timeout: float = 0.8) -> str | None:
    """-> 'backend up' / 'backend down' / None when no check applies.
    Only AgentMemory has a known liveness endpoint (/agentmemory/livez)."""
    base = agentmemory_url(srv)
    if not base:
        return None
    try:
        with urllib.request.urlopen(base + "/agentmemory/livez", timeout=timeout) as r:  # noqa: S310
            return "backend up" if r.status == 200 else f"backend HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        return f"backend HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError):
        return "backend down"
