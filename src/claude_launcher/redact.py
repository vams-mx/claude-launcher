"""Redaction for anything cm displays: previews, --list, dry runs."""

from __future__ import annotations

import copy
import re
import shlex
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MASK = "‹redacted›"
_SECRETISH = re.compile(r"(key|token|secret|passw|pwd|auth|credential|bearer|cookie|session)", re.I)
_KV = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$", re.S)  # GITHUB_TOKEN=ghp_…
_HEADER = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", re.S)  # Authorization: Bearer …
_SCHEME = re.compile(r"^(bearer|basic|token)\s+\S", re.I)  # a bare "Bearer sk-…"


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


def _redact_value(a: str) -> str:
    """A non-flag argument: KEY=value with a secret-looking key, a secret
    header ("Authorization: Bearer …"), a bare auth scheme value, or a URL."""
    if m := _KV.match(a):
        k, v = m.groups()
        if _SECRETISH.search(k):
            return f"{k}={MASK}"
        return f"{k}={redact_url(v)}" if "://" in v else a
    if "://" in a:
        return redact_url(a)
    if (m := _HEADER.match(a)) and _SECRETISH.search(m.group(1)):
        return f"{m.group(1)}: {MASK}"
    if _SCHEME.match(a):
        return MASK
    return a


def redact_args(args: list[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for a in args:
        if hide_next:
            out.append(MASK)
            hide_next = False
            continue
        if a.startswith("-") and "=" in a:
            k, v = a.split("=", 1)
            out.append(f"{k}={MASK}" if _SECRETISH.search(k) else f"{k}={_redact_value(v)}")
            continue
        if a.startswith("-") and _SECRETISH.search(a):
            out.append(a)
            hide_next = True
            continue
        out.append(_redact_value(a))
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
