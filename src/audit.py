"""Append-only, tamper-evident audit trail.

Deliberately the first module built. It depends on nothing else in the
pipeline; everything else depends on it, never the reverse.

Design rules, in priority order:

1. Every decision is one JSON object on one line. Grep-able, diff-able,
   streamable. No nesting a reviewer has to unpick.
2. A decision NOT to act is still a decision and is still logged. A trail that
   records only retries cannot answer "why was this customer left alone?",
   which is exactly what a compliance reviewer asks.
3. Entries are hash-chained: each record commits to its predecessor, so
   editing or deleting a line in the middle of a finished run is detectable
   via verify_chain(). That is what makes this evidence rather than a log.
4. Writes are flushed and fsynced immediately. A crash mid-batch must leave
   the decisions made so far intact and verifiable.
5. The logger never raises into caller code. Losing an audit line is bad;
   crashing a live recovery run because the disk hiccuped is worse. Failures
   go to stderr and are counted in `write_failures`.

Required fields on every decision (the Track 3 bar):
    timestamp, transaction_id, decision, reason, policy_rule_applied
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64
SCHEMA_VERSION = 1

# Decisions the agent may record. Anything outside this set is a caller bug,
# not a new kind of decision -- the audit log is not where vocabulary is
# invented.
VALID_DECISIONS = {
    "retry_scheduled",      # a retry was queued for a specific future time
    "retry_executed",       # an attempt was actually sent to the gateway
    "retry_suppressed",     # eligible in principle, blocked by a rule
    "no_action_terminal",   # hard decline; nothing to do, by policy
    "escalated",            # moved up the escalation ladder
    "handed_off_to_human",  # agent withdrew, queued for ops
    "recovered",            # payment ultimately succeeded
    "abandoned",            # attempts exhausted without recovery
    "batch_aborted",        # circuit breaker tripped
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace.

    Two logically identical entries must hash identically across machines and
    Python versions, or chain verification means nothing.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_entry(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


class AuditLog:
    """Writes the decision trail for one batch run.

        with AuditLog("results/audit_log.jsonl", policy_id="recovery-policy-v1") as log:
            log.decision(
                txn_id,
                "retry_scheduled",
                "funds likely restored after salary credit",
                "retry_windows.INSUFFICIENT_FUNDS.delays_minutes[0]",
            )
    """

    def __init__(
        self,
        path="results/audit_log.jsonl",
        *,
        run_id: str | None = None,
        policy_id: str | None = None,
        policy_version: int | None = None,
        dry_run: bool = True,
        extra_run_context: dict | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.run_id = run_id or "run_" + stamp + "_" + uuid.uuid4().hex[:8]
        self.policy_id = policy_id
        self.policy_version = policy_version
        self.dry_run = dry_run
        self.extra_run_context = extra_run_context or {}

        self._seq = 0
        self._prev_hash = self._tail_hash()
        self._lock = threading.Lock()
        self._fh = None
        self.write_failures = 0
        self.counts: dict = {}

    # -- lifecycle -------------------------------------------------------

    def _tail_hash(self) -> str:
        """Resume from an existing file so re-runs append rather than fork."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS_HASH
        last = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return GENESIS_HASH
        try:
            return json.loads(last).get("entry_hash", GENESIS_HASH)
        except json.JSONDecodeError:
            sys.stderr.write("audit: trailing line is not valid JSON; starting a new chain\n")
            return GENESIS_HASH

    def open(self) -> "AuditLog":
        self._fh = self.path.open("a", encoding="utf-8")
        self._write_raw({
            "event": "run_started",
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "dry_run": self.dry_run,
            **self.extra_run_context,
        })
        return self

    def close(self, status: str = "completed") -> None:
        if self._fh is None:
            return
        self._write_raw({
            "event": "run_finished",
            "status": status,
            "entries_written": self._seq,
            "decision_counts": dict(self.counts),
            "write_failures": self.write_failures,
        })
        self._fh.close()
        self._fh = None

    def __enter__(self) -> "AuditLog":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close(status="failed" if exc_type else "completed")
        return False  # never swallow the caller's exception

    # -- writing ---------------------------------------------------------

    def _write_raw(self, body: dict):
        """Seal `body` into the hash chain and append it. Never raises."""
        with self._lock:
            self._seq += 1
            payload = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": self._seq,
                "timestamp": _utc_now_iso(),
            }
            payload.update(body)
            payload["prev_hash"] = self._prev_hash
            entry_hash = _hash_entry(self._prev_hash, payload)
            payload["entry_hash"] = entry_hash

            try:
                if self._fh is None:
                    raise RuntimeError("AuditLog used before open()")
                self._fh.write(_canonical(payload) + "\n")
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except Exception as exc:  # see module docstring, rule 5
                self.write_failures += 1
                self._seq -= 1
                sys.stderr.write("audit: FAILED to write entry: " + str(exc) + "\n")
                return None

            self._prev_hash = entry_hash
            return payload

    def decision(
        self,
        transaction_id: str,
        decision: str,
        reason: str,
        policy_rule_applied: str,
        **fields: Any,
    ):
        """Record one bounded decision about one transaction.

        `reason` is prose for a human. `policy_rule_applied` is the dotted path
        into policy.yaml that produced it. Together they let a reviewer
        re-derive the call without reading the source.
        """
        if decision not in VALID_DECISIONS:
            sys.stderr.write(
                "audit: unknown decision " + repr(decision)
                + " for " + str(transaction_id) + "\n"
            )
        self.counts[decision] = self.counts.get(decision, 0) + 1
        body = {
            "transaction_id": transaction_id,
            "decision": decision,
            "reason": reason,
            "policy_rule_applied": policy_rule_applied,
        }
        body.update(fields)
        return self._write_raw(body)

    def event(self, event: str, **fields: Any):
        """Record something that is not a per-transaction decision.

        Batch-level facts: issuer outage detected, breaker tripped, policy
        loaded. Kept distinct from decisions so money-affecting calls filter
        cleanly.
        """
        body = {"event": event}
        body.update(fields)
        return self._write_raw(body)


# -- verification --------------------------------------------------------

def read_entries(path) -> Iterator[dict]:
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def verify_chain(path) -> dict:
    """Recompute the hash chain and report any break.

    `ok` is True only if every entry's recorded hash matches a recomputation
    over its own contents and its predecessor. False means a line was edited,
    reordered, inserted, or removed after the fact.
    """
    path = Path(path)
    report: dict = {
        "path": str(path),
        "ok": True,
        "entries": 0,
        "runs": [],
        "errors": [],
        "decision_counts": {},
    }
    if not path.exists():
        report["ok"] = False
        report["errors"].append("audit file does not exist")
        return report

    prev_by_run: dict = {}
    seen_runs: list = []

    for lineno, entry in enumerate(read_entries(path), start=1):
        report["entries"] += 1
        run_id = entry.get("run_id", "<missing>")
        if run_id not in prev_by_run:
            prev_by_run[run_id] = None
            seen_runs.append(run_id)

        recorded = entry.get("entry_hash")
        prev = entry.get("prev_hash")
        body = {k: v for k, v in entry.items() if k != "entry_hash"}

        if recorded is None or prev is None:
            report["ok"] = False
            report["errors"].append("line " + str(lineno) + ": missing hash fields")
            continue

        if _hash_entry(prev, body) != recorded:
            report["ok"] = False
            report["errors"].append(
                "line " + str(lineno)
                + ": content does not match its hash (entry altered after writing)"
            )

        last = prev_by_run.get(run_id)
        if last is not None and prev != last:
            report["ok"] = False
            report["errors"].append(
                "line " + str(lineno) + ": chain break -- prev_hash does not match "
                "the previous entry of run " + str(run_id)
                + " (an entry was removed or reordered)"
            )
        prev_by_run[run_id] = recorded

        d = entry.get("decision")
        if d:
            report["decision_counts"][d] = report["decision_counts"].get(d, 0) + 1

    report["runs"] = seen_runs
    return report


def summarise(path) -> dict:
    """Decision tallies per run, for the final report."""
    runs: dict = {}
    for e in read_entries(path):
        r = runs.setdefault(
            e.get("run_id", "<missing>"),
            {"decisions": {}, "events": {}, "entries": 0},
        )
        r["entries"] += 1
        if "decision" in e:
            r["decisions"][e["decision"]] = r["decisions"].get(e["decision"], 0) + 1
        elif "event" in e:
            r["events"][e["event"]] = r["events"].get(e["event"], 0) + 1
    return runs


def _cli(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "verify"
    path = argv[2] if len(argv) > 2 else "results/audit_log.jsonl"

    if cmd == "verify":
        rep = verify_chain(path)
        state = "INTACT" if rep["ok"] else "BROKEN"
        print("audit chain: " + state + "  (" + str(rep["entries"])
              + " entries, " + str(len(rep["runs"])) + " run(s))")
        for err in rep["errors"]:
            print("  !", err)
        if rep["decision_counts"]:
            print("  decisions:")
            for k, v in sorted(rep["decision_counts"].items(), key=lambda x: -x[1]):
                print("    " + k.ljust(24) + str(v))
        return 0 if rep["ok"] else 1

    if cmd == "tail":
        for e in list(read_entries(path))[-20:]:
            label = e.get("decision") or e.get("event", "")
            print(str(e.get("timestamp", "")) + "  "
                  + str(e.get("transaction_id", "-")).rjust(18) + "  "
                  + str(label).ljust(24) + "  " + str(e.get("reason", "")))
        return 0

    if cmd == "stats":
        print(json.dumps(summarise(path), indent=2))
        return 0

    print("usage: python -m src.audit [verify|tail|stats] [path]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
