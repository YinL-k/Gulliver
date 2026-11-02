"""Hash chains over Gulliver's local state, so edits after the fact are visible.

  ledger.jsonl   every line carries `prev` (the hash of the line before) and `hash` (of itself)
  audit.jsonl    one chained line per saved receipt or approval, with the sha256 of the file as written

`gulliver audit verify` recomputes everything. It finds an edited, removed or reordered ledger line, and a
receipt or approval file that changed after Gulliver wrote it. Limits, stated plainly:
  - Without a key anyone who can write the folder can rebuild the whole chain. Set GULLIVER_AUDIT_KEY
    (kept away from the agent) to make the hashes HMACs that cannot be recomputed without it.
  - Removing the newest lines leaves a valid, shorter chain. Keep the `head` hash that verify prints
    somewhere else and pass it back with --head to prove nothing up to it was removed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
KEY_ENV = "GULLIVER_AUDIT_KEY"
HMAC = "hmac-sha256"


def _key() -> bytes | None:
    key = os.environ.get(KEY_ENV)
    return key.encode() if key else None


def content_hash(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def entry_hash(entry: dict[str, Any]) -> str | None:
    """sha256 of the entry, or its HMAC when the entry says it was keyed. None: keyed, but no key here."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    if entry.get("alg") == HMAC:
        key = _key()
        return hmac.new(key, data, hashlib.sha256).hexdigest() if key else None
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize appends between processes where the OS allows it (POSIX). Best effort elsewhere."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _last_line(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        size = min(end, 64 * 1024)
        fh.seek(end - size)
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    return lines[-1].decode(errors="replace") if lines else ""


def _last_hash(path: Path) -> str:
    try:
        return str(json.loads(_last_line(path)).get("hash") or GENESIS)
    except (json.JSONDecodeError, AttributeError):
        return GENESIS


def append(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Append one chained JSON line and return it (with prev and hash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        entry = {**entry, "prev": _last_hash(path)}
        if _key():
            entry["alg"] = HMAC
        entry["hash"] = entry_hash(entry)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def verify_chain(path: Path) -> dict[str, Any]:
    """Check one chained file. Lines written before chaining existed (no hash) are counted, not failed."""
    out: dict[str, Any] = {"file": path.name, "entries": 0, "chained": 0, "legacy": 0, "head": GENESIS,
                           "problems": [], "hashes": set()}
    if not path.is_file():
        return out
    prev = GENESIS
    started = keyed = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        out["entries"] += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            out["problems"].append(f"{path.name} line {number} is not valid JSON")
            continue
        if not isinstance(entry, dict) or "hash" not in entry:
            if started:
                out["problems"].append(f"{path.name} line {number} has no hash (added outside Gulliver?)")
            else:
                out["legacy"] += 1
            continue
        started = True
        out["chained"] += 1
        expected = entry_hash(entry)
        if entry.get("alg") == HMAC:
            keyed = True
        elif keyed:
            out["problems"].append(f"{path.name} line {number} is not keyed although earlier lines are "
                                   "(rewritten without GULLIVER_AUDIT_KEY?)")
        if expected is None:
            out["problems"].append(f"{path.name} line {number} is keyed: set {KEY_ENV} to verify it")
        elif expected != entry["hash"]:
            out["problems"].append(f"{path.name} line {number} was changed after it was written")
        if entry.get("prev") != prev:
            out["problems"].append(f"{path.name} line {number} does not follow the line before it "
                                   "(a line was removed, reordered or inserted)")
        prev = str(entry["hash"])
        out["hashes"].add(prev)
    out["head"] = prev
    return out
