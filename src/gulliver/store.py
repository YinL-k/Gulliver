"""Local state in the .gulliver folder: receipts, the spend ledger and human approvals.

Everything is plain JSON so people (and other tools) can read it.
  receipts/<id>.json    one per preflight, rehearsal and run
  ledger.jsonl          money and bytes actually spent, one line per execution
  approvals/<id>.json   written by `gulliver approve <id>`, or by the MCP server when the human approves in
                        the client (only with [approval] channel = "elicitation")
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APPROVAL_TTL = timedelta(hours=24)
REHEARSAL_TTL = timedelta(hours=24)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


class Store:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir)

    # -- paths ---------------------------------------------------------------------
    @property
    def receipts_dir(self) -> Path:
        return self.root / "receipts"

    @property
    def approvals_dir(self) -> Path:
        return self.root / "approvals"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.jsonl"

    def _ensure(self) -> None:
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.approvals_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("# Gulliver local state (receipts may contain SQL and result previews)\n*\n")

    # -- receipts ------------------------------------------------------------------
    def new_id(self, action: str) -> str:
        return f"gv-{now_utc().strftime('%Y%m%d-%H%M%S')}-{action[:3]}-{secrets.token_hex(2)}"

    def save_receipt(self, receipt: dict[str, Any]) -> str:
        self._ensure()
        rid = receipt.setdefault("receipt_id", self.new_id(receipt.get("action", "rec")))
        receipt.setdefault("created_at", iso(now_utc()))
        path = self.receipts_dir / f"{rid}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(receipt, indent=2, default=str))
        os.replace(tmp, path)
        return rid

    def load_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        safe = Path(receipt_id).name
        path = self.receipts_dir / f"{safe}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text())

    def receipts(self) -> list[dict[str, Any]]:
        if not self.receipts_dir.is_dir():
            return []
        out = []
        # newest first; ids only have one-second resolution, so ties are broken by the file's write time
        paths = sorted(self.receipts_dir.glob("*.json"), key=lambda p: (p.name[:18], _mtime(p), p.name), reverse=True)
        for path in paths:
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def latest_rehearsal(self, sql_hash: str, connection: str) -> dict[str, Any] | None:
        cutoff = now_utc() - REHEARSAL_TTL
        for rec in self.receipts():
            if (rec.get("action") == "rehearse" and rec.get("sql_hash") == sql_hash
                    and rec.get("connection") == connection and rec.get("status") in ("ok", "warn", "block")
                    and parse_iso(rec["created_at"]) >= cutoff):
                return rec
        return None

    # -- approvals -----------------------------------------------------------------
    def approve(self, receipt_id: str, note: str = "", channel: str = "cli",
                client: str | None = None) -> dict[str, Any]:
        receipt = self.load_receipt(receipt_id)
        if receipt is None:
            raise FileNotFoundError(f"No receipt {receipt_id}")
        self._ensure()
        approval = {
            "receipt_id": receipt_id,
            "sql_hash": receipt.get("sql_hash"),
            "connection": receipt.get("connection"),
            "approved_at": iso(now_utc()),
            "expires_at": iso(now_utc() + APPROVAL_TTL),
            "max_cost_usd": receipt.get("gate", {}).get("cost_for_gate_usd"),
            "note": note,
            "channel": channel,   # cli: `gulliver approve`; elicitation: the human answered in the MCP client
        }
        if client:
            approval["client"] = client
        (self.approvals_dir / f"{Path(receipt_id).name}.json").write_text(json.dumps(approval, indent=2))
        return approval

    def find_approval(self, sql_hash: str, connection: str) -> dict[str, Any] | None:
        if not self.approvals_dir.is_dir():
            return None
        now = now_utc()
        for path in sorted(self.approvals_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if (data.get("sql_hash") == sql_hash and data.get("connection") == connection
                    and parse_iso(data["expires_at"]) > now):
                return data
        return None

    # -- ledger --------------------------------------------------------------------
    def record_spend(self, entry: dict[str, Any]) -> None:
        self._ensure()
        entry = {"at": iso(now_utc()), **entry}
        with self.ledger_path.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def spent_today(self) -> dict[str, float]:
        today = now_utc().date()
        usd = 0.0
        runs = 0
        bytes_total = 0
        if self.ledger_path.is_file():
            for line in self.ledger_path.read_text().splitlines():
                try:
                    entry = json.loads(line)
                    if parse_iso(entry["at"]).date() != today:
                        continue
                except (ValueError, KeyError, json.JSONDecodeError):
                    continue
                usd += float(entry.get("cost_usd") or 0.0)
                bytes_total += int(entry.get("bytes") or 0)
                runs += 1
        return {"usd": round(usd, 6), "executions": runs, "bytes": bytes_total}
