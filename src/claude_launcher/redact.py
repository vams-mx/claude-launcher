"""Redaction for anything cm displays: previews, --list, dry runs."""

from __future__ import annotations

import copy
import re
import shlex
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MASK = "‹redacted›"
_SECRETISH = re.compile(r"(key|token|secret|passw|pwd|auth|credential|bearer|cookie|session)", re.I)


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if not parts.scheme:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    query = MASK if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def redact_args(args: list[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for a in args:
        if hide_next:
            out.append(MASK)
            hide_next = False
            continue
        if a.startswith("-") and "=" in a:
            k, _ = a.split("=", 1)
            out.append(f"{k}={MASK}" if _SECRETISH.search(k) else a)
            continue
        if a.startswith("-") and _SECRETISH.search(a):
            out.append(a)
            hide_next = True
            continue
        if "://" in a:
            out.append(redact_url(a))
            continue
        out.append(a)
    return out


def redact_server(cfg: dict[str, Any]) -> dict[str, Any]:
    c = copy.deepcopy(cfg)
    for k in ("env", "headers"):
        if isinstance(c.get(k), dict):
            c[k] = {name: MASK for name in c[k]}
    if isinstance(c.get("url"), str):
        c["url"] = redact_url(c["url"])
    if isinstance(c.get("args"), list):
        c["args"] = redact_args([str(a) for a in c["args"]])
    if "headersHelper" in c:
        c["headersHelper"] = MASK
    return c


def redact_mcp_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {"mcpServers": {n: redact_server(c) for n, c in (doc.get("mcpServers") or {}).items()}}


def redact_settings(doc: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(doc)
    if isinstance(d.get("env"), dict):
        d["env"] = {k: MASK for k in d["env"]}
    for k in ("apiKeyHelper", "awsCredentialExport", "awsAuthRefresh", "otelHeadersHelper"):
        if k in d:
            d[k] = MASK
    return d


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
