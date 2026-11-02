"""Who is asking: the agent, its session and the task, stamped on every ledger entry and query tag.

Sources, the first one that is set wins:
  agent    GULLIVER_AGENT (set by the human)  >  MCP clientInfo.name  >  CLI --agent  >  "cli" / "local"
  session  MCP: Mcp-Session-Id header  >  GULLIVER_SESSION  >  server process + client name
           CLI: GULLIVER_SESSION, else none (each CLI call stands alone)
  task     GULLIVER_TASK (set by the human)

All of this is self-reported by the client, so per-agent and per-session budgets are accounting,
not a security boundary. The global limits (per query, per day) always apply to everyone.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Identity:
    agent: str
    session: str | None = None
    task: str | None = None
    source: str = "default"   # mcp | cli | env | default

    def fields(self) -> dict[str, str]:
        """The ledger fields (no empty values)."""
        return {k: v for k, v in asdict(self).items() if k != "source" and v}


_CURRENT: ContextVar[Identity | None] = ContextVar("gulliver_identity", default=None)
_UNSAFE = re.compile(r"[^\w.@:-]+")


def clean(text: Any, limit: int = 64) -> str | None:
    """A short, printable label: no spaces, quotes or comment markers."""
    if text is None:
        return None
    value = _UNSAFE.sub("-", str(text).strip()).strip("-")
    return value[:limit] or None


def from_env(agent: str | None = None, default_agent: str = "local", source: str = "default") -> Identity:
    """Identity outside MCP (CLI, library). GULLIVER_AGENT, set by the human, wins over a flag."""
    env_agent = clean(os.environ.get("GULLIVER_AGENT"))
    name = env_agent or clean(agent) or default_agent
    src = "env" if env_agent else (source if agent else "default")
    return Identity(agent=name, session=clean(os.environ.get("GULLIVER_SESSION")),
                    task=clean(os.environ.get("GULLIVER_TASK"), 120), source=src)


def current() -> Identity:
    return _CURRENT.get() or from_env()


@contextmanager
def use(identity: Identity) -> Iterator[Identity]:
    token = _CURRENT.set(identity)
    try:
        yield identity
    finally:
        _CURRENT.reset(token)


# -- MCP ----------------------------------------------------------------------------------
_PROCESS = secrets.token_hex(4)  # one MCP server process


def from_mcp(context: Any) -> Identity:
    """Identity for one MCP tool call: the client's self-reported name, and as session the Mcp-Session-Id
    header (HTTP) or else this server process + the client name (stdio: one client per process)."""
    env_agent = clean(os.environ.get("GULLIVER_AGENT"))
    session = headers = None
    if context is not None:
        try:
            session = context.session
            headers = context.headers
        except Exception:  # outside a request, or an older mcp version
            pass
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
    client_name = clean(getattr(info, "name", None))
    agent = env_agent or client_name or "mcp"
    header_sid = clean(headers.get("mcp-session-id")) if headers else None
    sid = header_sid or clean(os.environ.get("GULLIVER_SESSION")) or f"mcp-{_PROCESS}-{client_name or agent}"
    return Identity(agent=agent, session=sid, task=clean(os.environ.get("GULLIVER_TASK"), 120),
                    source="env" if env_agent else "mcp")
