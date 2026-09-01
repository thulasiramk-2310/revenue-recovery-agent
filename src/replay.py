"""Reconstruct a complete run from results/run.log and nothing else.

This is a test, not a viewer. The claim the audit trail makes is that a
reviewer can re-derive what the agent did and why, without the code, without
the input batch, and without trusting the agent's own summary. This module
tries to do exactly that and reports honestly where it cannot.

It reads ONLY the log. Not data/failed_payments.json, not policy.yaml, not
run_summary.json (except in --compare, where the comparison is the point).
If a number cannot be rebuilt from the trail, that is a gap in the trail and
it is reported as one rather than quietly filled in from another source.

What a passing replay establishes
---------------------------------
  1. the hash chain is intact, so no line was edited, inserted or removed
  2. every decision carries a reason and a resolvable policy rule path
  3. every executed attempt has an outcome, a latency and a stated source
  4. the money, action and outcome totals rebuilt from the log match what
     the run reported at the time

Usage
-----
    python -m src.replay                      # rebuild and print
    python -m src.replay --compare            # rebuild and diff vs run_summary.json
    python -m src.replay --transaction pay_2C0001   # one transaction's story
    python -m src.replay --log results/run.log
"""

from __future__ import annotations

import argparse
import collections
import json
import sys

from .audit import read_entries, verify_chain

# Fields the AuditLog wraps every entry in. Stripped when an event's payload
# is rebuilt as data, so a reconstruction is compared on content rather than
# on the envelope that carried it.
ENVELOPE = {"event", "hash", "prev_hash", "entry_hash", "ts", "timestamp",
            "run_id", "seq", "schema", "schema_version"}

DEFAULT_LOG = "results/run.log"
DEFAULT_SUMMARY = "results/run_summary.json"

# Audit decisions that mean money actually moved.
RECOVERY_DECISIONS = {"recovered"}

# How an execution line maps to the per-transaction FINAL status the run
# reports. Two different questions get asked of this log and both have to be
# answerable from it alone: "what happened in total" (every attempt) and
# "where did each transaction end up" (the last thing that happened to it).
FINAL_STATUS_FROM_DECISION = {
    "recovered": "recovered",
    "retry_executed": "failed",
    "escalated": "contact_stubbed",
    "retry_suppressed": "suppressed_already_paid",
}
FINAL_STATUS_FROM_EVENT = {
    "execution_skipped": "not_attempted",
    "handed_off": "handed_off",
}


def _customers(contacts):
    """Unique customers contacted, rebuilt from the contact lines alone.

    Counted by customer rather than by message: two emails to one person is
    one person bothered, and that is the number the baseline comparison needs.
    The id lives inside the logged payload, which is the point of logging the
    payload in full.
    """
    seen = set()
    for c in contacts:
        payload = c.get("intended_payload") or {}
        cid = (payload.get("to") or {}).get("customer_id")
        if cid:
            seen.add(cid)
    return len(seen)


def rebuild(log_path=DEFAULT_LOG):
    """Rebuild the whole run from the log. Returns a reconstruction dict."""
    chain = verify_chain(log_path)

    run_context = {}
    final_status = {}
    ceiling = baseline = costs = None
    detections, diagnoses = [], {}
    decisions, executions = {}, {}
    contacts, gateway_calls, backoffs, reconciles = [], [], [], []
    logged_summary = None
    timeline = []
    gaps = []

    for e in read_entries(log_path):
        kind = e.get("event")
        tid = e.get("transaction_id")
        timeline.append(e)

        if kind in ("run_started", "run_configured"):
            run_context.update({k: v for k, v in e.items()
                                if k not in ("event", "hash", "prev_hash")})
        elif kind == "issuer_degradation_detected":
            detections.append(e)
        elif kind == "diagnosed":
            diagnoses[tid] = e
        elif kind == "gateway_call":
            gateway_calls.append(e)
        elif kind == "gateway_backoff":
            backoffs.append(e)
        elif kind == "reconciled":
            reconciles.append(e)
        elif kind == "contact_stubbed":
            contacts.append(e)
        elif kind in FINAL_STATUS_FROM_EVENT:
            final_status[tid] = FINAL_STATUS_FROM_EVENT[kind]
        elif kind == "cost_model":
            costs = {k: v for k, v in e.items() if k not in ENVELOPE}
        elif kind == "recoverable_ceiling":
            ceiling = {k: v for k, v in e.items() if k not in ENVELOPE}
        elif kind == "baseline_scored":
            baseline = {k: v for k, v in e.items() if k not in ENVELOPE}
        elif kind == "run_summary":
            logged_summary = {k: v for k, v in e.items() if k not in ENVELOPE}
        elif e.get("decision"):
            # A decision line. The policy layer and the execution layer both
            # write these; the execution ones carry an outcome_source.
            if e.get("outcome_source") or e.get("attempted_at"):
                executions.setdefault(tid, []).append(e)
                mapped = FINAL_STATUS_FROM_DECISION.get(e["decision"])
                if mapped:
                    final_status[tid] = mapped
            else:
                decisions[tid] = e

    # --- rebuild the aggregates purely from the lines above -------------
    actions = collections.Counter(
        d.get("action") for d in decisions.values() if d.get("action")
    )
    recovered_ids, recovered_paise = set(), 0
    exec_status = collections.Counter()
    latencies = []

    for tid, entries in executions.items():
        for e in entries:
            exec_status[e.get("decision")] += 1
            if e.get("latency_ms") is not None:
                latencies.append(e["latency_ms"])
            if e.get("decision") in RECOVERY_DECISIONS or e.get("recovered") is True:
                if tid not in recovered_ids:
                    recovered_ids.add(tid)
                    amt = e.get("amount_paise")
                    if amt is None:
                        gaps.append(
                            "recovered %s carries no amount_paise, so its "
                            "value cannot be rebuilt from the log" % tid
                        )
                    else:
                        recovered_paise += amt

    total_value = sum(
        d.get("amount_paise") or 0 for d in decisions.values()
    )
    missing_amounts = [tid for tid, d in decisions.items()
                       if d.get("amount_paise") is None]
    if missing_amounts:
        gaps.append("%d decisions carry no amount_paise; batch value is "
                    "incomplete" % len(missing_amounts))

    # --- integrity of the trail itself ----------------------------------
    if not chain["ok"]:
        gaps.append("hash chain is BROKEN: %s" % "; ".join(chain["errors"][:3]))
    no_reason = [tid for tid, d in decisions.items() if not d.get("reason")]
    if no_reason:
        gaps.append("%d decisions have no reason" % len(no_reason))
    no_rule = [tid for tid, d in decisions.items()
               if not d.get("policy_rule_applied")]
    if no_rule:
        gaps.append("%d decisions cite no policy rule" % len(no_rule))
    undiagnosed = [tid for tid in decisions if tid not in diagnoses]
    if undiagnosed:
        gaps.append("%d transactions were decided with no diagnosis logged "
                    "beforehand" % len(undiagnosed))
    unexplained = [e for entries in executions.values() for e in entries
                   if not e.get("outcome_source")]
    if unexplained:
        gaps.append("%d executions do not state whether their outcome came "
                    "from the gateway or from simulation" % len(unexplained))

    return {
        "log_path": log_path,
        "chain": chain,
        "run_context": run_context,
        "counts": {
            "entries": chain["entries"],
            "detections": len(detections),
            "diagnoses": len(diagnoses),
            "decisions": len(decisions),
            "executed": sum(len(v) for v in executions.values()),
            "gateway_calls": len(gateway_calls),
            "gateway_backoffs": len(backoffs),
            "reconciles": len(reconciles),
            "contacts_stubbed": len(contacts),
        },
        "rebuilt_summary": {
            "batch_size": len(decisions),
            "total_value_paise": total_value,
            "detections": len(detections),
            "actions": dict(actions),
            "execution_status": dict(exec_status),
            "recovered_count": len(recovered_ids),
            "recovered_paise": recovered_paise,
            "contacts_stubbed": len(contacts),
            "gateway_calls": len(gateway_calls),
            "final_status": dict(collections.Counter(final_status.values())),
            "execution_events": dict(exec_status),
            "customers_contacted": _customers(contacts),
            # Gateway attempts the agent spent, rebuilt by counting the
            # execution lines rather than trusting a reported total.
            "attempts_spent": (exec_status.get("retry_executed", 0)
                               + exec_status.get("recovered", 0)),
            "costs": costs,
            # Net is RECOMPUTED from the rebuilt attempt and contact counts
            # using the logged prices -- not copied from the run's claim.
            # Reading the claimed net back would test nothing.
            "net_recovered_paise": (
                recovered_paise
                - (exec_status.get("retry_executed", 0)
                   + exec_status.get("recovered", 0))
                * costs["per_attempt_paise"]
                - _customers(contacts) * costs["per_contact_paise"]
            ) if costs else None,
            "ceiling": ceiling,
            "baseline": baseline,
            "uplift_paise": ((recovered_paise - baseline["recovered_paise"])
                             if baseline else None),
        },
        "logged_summary": logged_summary,
        "latency_ms": {
            "n": len(latencies),
            "mean": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "max": round(max(latencies), 1) if latencies else None,
        },
        "detections": detections,
        "decisions": decisions,
        "diagnoses": diagnoses,
        "executions": executions,
        "contacts": contacts,
        "gaps": gaps,
    }


def story(rec, transaction_id):
    """Everything the trail knows about one transaction, in order.

    This is the reviewer's question -- "why did it do that to THIS payment?"
    -- and it must be answerable from the log alone.
    """
    lines = []
    d = rec["diagnoses"].get(transaction_id)
    if d:
        lines.append("DIAGNOSED  %s / %s  (%s, transient=%s, retryable=%s)"
                     % (d.get("failure_code"), d.get("severity"),
                        d.get("cause"), d.get("is_transient"), d.get("retryable")))
        for ev in d.get("evidence") or []:
            lines.append("           evidence: %s" % ev)
    dec = rec["decisions"].get(transaction_id)
    if dec:
        lines.append("DECIDED    %s  -> %s" % (dec.get("action"), dec.get("decision")))
        lines.append("           rule: %s" % dec.get("policy_rule_applied"))
        lines.append("           why : %s" % dec.get("reason"))
        if dec.get("scheduled_time"):
            lines.append("           when: %s" % dec["scheduled_time"])
        for r in dec.get("rungs_passed_over") or []:
            lines.append("           ladder rung %s (%s) passed over: %s"
                         % (r.get("step"), r.get("action"), "; ".join(r.get("unmet", []))))
    for e in rec["executions"].get(transaction_id, []):
        lines.append("EXECUTED   %s  (%s, %sms)"
                     % (e.get("decision"), e.get("outcome_source"),
                        e.get("latency_ms")))
        if e.get("order_id"):
            lines.append("           order: %s  http %s"
                         % (e["order_id"], e.get("http_status")))
        lines.append("           why : %s" % e.get("reason"))
    return lines


def compare(rec, summary_path=DEFAULT_SUMMARY):
    """Diff the rebuilt summary against what the run claimed at the time."""
    try:
        with open(summary_path, encoding="utf-8") as fh:
            claimed = json.load(fh)
    except FileNotFoundError:
        return None, ["%s not found" % summary_path]

    rebuilt = rec["rebuilt_summary"]
    diffs = []
    for key in sorted(set(rebuilt) | set(claimed)):
        if key in ("mode", "chain_ok", "log_entries", "gateway_errors",
                   "reconciles", "execution_status", "sliced"):
            continue
        a, b = rebuilt.get(key), claimed.get(key)
        if a != b:
            diffs.append("%-22s rebuilt=%s  claimed=%s" % (key, a, b))
    return claimed, diffs


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reconstruct a run from its audit log alone.")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--summary", default=DEFAULT_SUMMARY)
    ap.add_argument("--compare", action="store_true",
                    help="diff the reconstruction against run_summary.json")
    ap.add_argument("--transaction", help="print one transaction's full story")
    args = ap.parse_args(argv)

    try:
        rec = rebuild(args.log)
    except FileNotFoundError:
        sys.stderr.write("no log at %s -- run `python -m src.run_batch` first\n"
                         % args.log)
        return 2

    if args.transaction:
        lines = story(rec, args.transaction)
        if not lines:
            print("nothing in the trail for %s" % args.transaction)
            return 1
        print("\n=== %s, reconstructed from %s ===" % (args.transaction, args.log))
        for l in lines:
            print("  " + l)
        return 0

    c = rec["chain"]
    print("\n" + "=" * 64)
    print("  REPLAY  %s" % args.log)
    print("=" * 64)
    print("  chain              %s  (%d entries, %d run%s)"
          % ("VERIFIED" if c["ok"] else "BROKEN", c["entries"],
             len(c["runs"]), "" if len(c["runs"]) == 1 else "s"))
    ctx = rec["run_context"]
    if ctx:
        print("  run                %s  policy=%s v%s  mode=%s"
              % (ctx.get("run_id", "?"), ctx.get("policy_id"),
                 ctx.get("policy_version"), ctx.get("mode", "?")))

    print("\n  reconstructed from the log alone")
    for k, v in rec["counts"].items():
        print("      %-22s %6d" % (k, v))

    s = rec["rebuilt_summary"]
    print("\n  decisions")
    for a, n in sorted(s["actions"].items(), key=lambda x: -x[1]):
        print("      %-30s %4d" % (a, n))
    print("\n  outcomes")
    for a, n in sorted(s["execution_status"].items(), key=lambda x: -x[1]):
        print("      %-30s %4d" % (a, n))
    print("\n  batch value        Rs %s" % format(s["total_value_paise"] / 100.0, ",.2f"))
    print("  recovered          %d txns / Rs %s"
          % (s["recovered_count"], format(s["recovered_paise"] / 100.0, ",.2f")))
    lat = rec["latency_ms"]
    if lat["n"]:
        print("  latency            mean %sms, max %sms over %d attempts"
              % (lat["mean"], lat["max"], lat["n"]))

    exit_code = 0
    if args.compare:
        claimed, diffs = compare(rec, args.summary)
        print("\n  " + "-" * 60)
        if claimed is None:
            print("  COMPARE: %s" % diffs[0])
            exit_code = 1
        elif diffs:
            print("  COMPARE: MISMATCH -- the log does not reproduce the run")
            for d in diffs:
                print("      " + d)
            exit_code = 1
        else:
            print("  COMPARE: the reconstruction matches the run's own summary")
            print("           exactly. The trail is sufficient to reproduce")
            print("           the batch without the code or the input data.")

    if rec["gaps"]:
        print("\n  GAPS IN THE TRAIL (things the log could not answer)")
        for g in rec["gaps"]:
            print("      - " + g)
        exit_code = 1
    else:
        print("\n  no gaps: every decision has a reason, a rule path, a prior")
        print("  diagnosis, and every execution states its outcome source.")
    print("=" * 64)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
