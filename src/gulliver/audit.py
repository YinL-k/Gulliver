"""Hash chains over Gulliver's local state, so edits after the fact are visible.

  ledger.jsonl   every line carries `prev` (the hash of the line before) and `hash` (of itself)
  audit.jsonl    one chained line per saved receipt or approval, with the sha256 of the file as written

`gulliver audit verify` recomputes everything. It finds an edited, removed or reordered ledger line, and a
receipt or approval file that changed after Gulliver wrote it. Limits, stated plainly:
  - Without a key anyone who can write the folder can rebuild the whole chain. Set GULLIVER_AUDIT_KEY
    (kept away from the agent) to make the hashes HMACs that cannot be recomputed without it.
    When the first keyed line of a file is written, a marker (`<file>.keyed`, itself an HMAC) records how
    many lines came before it. With the key set, verify requires every later line to be keyed, so a chain
    rebuilt as plain sha256, or stripped of its hashes, fails; a deleted marker means "keyed from line 1".
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


def _marker_path(path: Path) -> Path:
    return path.with_name(path.name + ".keyed")


def _marker_mac(key: bytes, name: str, before: int, first: str) -> str:
    return hmac.new(key, f"{name}|{before}|{first}".encode(), hashlib.sha256).hexdigest()


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def read_marker(path: Path) -> dict[str, Any] | None:
    """The keyed-genesis marker of a chained file: {"before": lines before the first keyed line, "first": its
    hash, "valid": the MAC checks out (None: no key here)}. None when there is no (readable) marker."""
    try:
        data = json.loads(_marker_path(path).read_text(encoding="utf-8"))
        before, first, mac = int(data["before"]), str(data["first"]), str(data["mac"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    key = _key()
    valid = hmac.compare_digest(_marker_mac(key, path.name, before, first), mac) if key else None
    return {"before": before, "first": first, "valid": valid}


def append(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Append one chained JSON line and return it (with prev and hash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _key()
    with _locked(path):
        last = _last_line(path)
        entry = {**entry, "prev": _last_hash(path)}
        if key:
            entry["alg"] = HMAC
        entry["hash"] = entry_hash(entry)
        if key and not _marker_path(path).exists():
            try:
                last_keyed = json.loads(last).get("alg") == HMAC if last else False
            except (json.JSONDecodeError, AttributeError):
                last_keyed = False
            if not last_keyed:   # the first keyed line of this file: record where keying started
                before = _count_lines(path)
                _marker_path(path).write_text(json.dumps({
                    "before": before, "first": entry["hash"],
                    "mac": _marker_mac(key, path.name, before, entry["hash"])}), encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def verify_chain(path: Path) -> dict[str, Any]:
    """Check one chained file. Lines written before chaining existed (no hash) are counted, not failed.

    With GULLIVER_AUDIT_KEY set, every line after the keyed-genesis marker's `before` (all lines, when there is
    no valid marker) must be keyed: an unkeyed or hashless line there is a problem, not legacy."""
    out: dict[str, Any] = {"file": path.name, "entries": 0, "chained": 0, "legacy": 0, "head": GENESIS,
                           "problems": [], "hashes": set(), "first_at": None, "legacy_at": []}
    marker = read_marker(path)
    strict = _key() is not None
    before = 0
    if marker is not None:
        if marker["valid"] is False:
            out["problems"].append(f"{path.name}.keyed (where keying started) was changed or written without "
                                   "the key")
        else:
            before = marker["before"]
    # no (valid) marker: with a key set, every line must be keyed from line 1
    if not path.is_file():
        if marker is not None:
            out["problems"].append(f"{path.name} is missing although keyed lines were written to it")
        return out
    prev = GENESIS
    started = keyed = False
    must_key = strict or marker is not None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        out["entries"] += 1
        after_key = must_key and out["entries"] > before
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            out["problems"].append(f"{path.name} line {number} is not valid JSON")
            continue
        if not isinstance(entry, dict) or "hash" not in entry:
            if after_key:
                out["problems"].append(f"{path.name} line {number} has no hash although {KEY_ENV} was set "
                                       "when it was written (stripped or added outside Gulliver?)")
            elif started:
                out["problems"].append(f"{path.name} line {number} has no hash (added outside Gulliver?)")
            else:
                out["legacy"] += 1
                if isinstance(entry, dict) and entry.get("at"):
                    out["legacy_at"].append(str(entry["at"]))
            continue
        if not started and isinstance(entry.get("at"), str):
            out["first_at"] = entry["at"]
        started = True
        out["chained"] += 1
        expected = entry_hash(entry)
        if entry.get("alg") == HMAC:
            keyed = True
            if marker is not None and out["entries"] == before + 1 and entry.get("hash") != marker["first"]:
                out["problems"].append(f"{path.name} line {number} is not the first keyed line Gulliver wrote "
                                       "(the lines before it were rewritten)")
        elif after_key:
            out["problems"].append(f"{path.name} line {number} is not keyed although {KEY_ENV} was set when it "
                                   "was written (the chain was rebuilt without the key?)")
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
    if marker is not None and marker["valid"] is not False and out["entries"] <= before:
        out["problems"].append(f"{path.name} has no keyed line although {path.name}.keyed says line "
                               f"{before + 1} was keyed (the newest lines were removed)")
    out["head"] = prev
    return out
