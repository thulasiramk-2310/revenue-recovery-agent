"""Orchestrator. Wires the loop and writes the run log.

    load batch
      -> detect batch-level signals (issuer degradation)
      -> for each transaction: diagnose -> policy.decide -> execute
      -> write results/run.log (hash-chained) + results/run_summary.json

Everything that happens goes to the trail as it happens, not afterwards. The
summary written at the end is the orchestrator's own view; src/replay.py
rebuilds the same summary from the log alone, and the two must agree. If they
do not, the trail is missing something, and a trail that cannot reproduce the
run is not an audit trail.

The audit chain is verified at the end of every run. A broken chain fails the
run regardless of how much money was recovered.

Usage
-----
    python -m src.run_batch                  # dry run, no network
    python -m src.run_batch --live           # real orders against TEST mode
    python -m src.run_batch --live --limit 5 # small live slice
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from .audit import AuditLog, verify_chain
from .diagnose import diagnose_batch, summarise_diagnoses
from .detect import score_detections
from .execute import Executor, LiveKeyRefused, mask
from .generate_data import load_batch
from .policy import decide_and_log, load_policy

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_LOG = "results/run.log"
DEFAULT_SUMMARY = "results/run_summary.json"
# The horizon: how far the simulated clock may run past the batch window
# before we stop waiting and call it. Fixed rather than wall-clock so runs are
# reproducible. This is NOT a global "now" -- see _work_transaction.
DEFAULT_HORIZON = "2026-10-15T00:00:00+05:30"

# Guard against a policy that defers forever. Each pass either advances the
# clock or acts; this caps how many passes one transaction may take.
MAX_PASSES = 12


def _ground_truth_index(path="data/failed_payments.json"):
    """Ground truth, keyed by transaction id. Scoring only.

    Loaded separately from the batch on purpose: the pipeline gets its data
    through load_batch(), which strips this, and only the executor's outcome
    resolution ever sees it.
    """
    data = json.loads(open(path, encoding="utf-8").read())
    return {t["transaction_id"]: t.get("_ground_truth", {})
            for t in data["transactions"]}


def _work_transaction(policy, log, executor, transaction, diagnosis, signals,
                      horizon):
    """Work one transaction the way an agent actually would: as a queue item.

    The clock starts when the payment failed and advances only as the policy
    says to wait. A DEFER or a HOLD is not the end of the story -- it moves
    the clock to when the transaction becomes actionable and asks again. An
    executed retry that fails consumes an attempt and the loop continues until
    the policy stops it, the money is recovered, or the horizon passes.

    Evaluating the whole batch at one fixed instant instead of this was the
    original bug: every retry got clamped to that instant, which was a month
    after the failures, so all 82 landed after their recovery windows had
    closed and nothing was ever recovered.
    """
    from .policy import DEFER, HOLD, STOP, TransactionState

    state = TransactionState()
    work = dict(transaction)
    now = work["timestamp"]
    horizon_dt = datetime.fromisoformat(horizon)
    last_decision = last_result = None

    for _ in range(MAX_PASSES):
        d = decide_and_log(policy, log, work, diagnosis,
                           state=state, batch_signals=signals, now=now)
        last_decision = d

        if d.action == STOP:
            last_result = executor.attempt(work, d)
            break

        if d.action in (DEFER, HOLD):
            if not d.scheduled_time:
                break
            nxt = datetime.fromisoformat(d.scheduled_time)
            # The clock must strictly advance, or this is an infinite wait.
            if nxt <= datetime.fromisoformat(now) or nxt > horizon_dt:
                log.event("wait_abandoned", transaction_id=work["transaction_id"],
                          reason=("scheduled beyond the run horizon"
                                  if nxt > horizon_dt
                                  else "policy did not advance the clock"),
                          scheduled_time=d.scheduled_time, horizon=horizon)
                break
            now = d.scheduled_time
            continue

        # Something executable.
        if d.scheduled_time:
            if datetime.fromisoformat(d.scheduled_time) > horizon_dt:
                log.event("wait_abandoned", transaction_id=work["transaction_id"],
                          reason="scheduled beyond the run horizon",
                          scheduled_time=d.scheduled_time, horizon=horizon)
                break
            now = d.scheduled_time

        last_result = executor.attempt(work, d)
        if last_result.get("recovered"):
            break

        # The attempt was spent. Record it and let the policy decide again.
        state.last_attempt_at = now
        if d.customer_visible:
            state.contacts_used += 1
        if d.escalation_step:
            state.escalation_step = d.escalation_step
        if d.action in ("silent_retry", "retry_with_updated_instrument"):
            work["attempt_number"] = int(work.get("attempt_number", 1)) + 1
        if last_result.get("status") == "handed_off":
            break

    return last_decision, last_result


def run(log_path=DEFAULT_LOG, summary_path=DEFAULT_SUMMARY, live=False,
        limit=None, horizon=DEFAULT_HORIZON, data_path="data/failed_payments.json",
        policy_path="policy.yaml", quiet=False):
    meta, outages, txns = load_batch(data_path)
    if limit:
        txns = txns[:limit]
    policy = load_policy(policy_path)
    ground_truth = _ground_truth_index(data_path)

    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    if os.path.exists(log_path):
        os.remove(log_path)

    with AuditLog(log_path, policy_id=policy.policy_id,
                  policy_version=policy.version, dry_run=not live,
                  extra_run_context={
                      "data_file": data_path,
                      "data_seed": meta.get("seed"),
                      "batch_size": len(txns),
                      "horizon": horizon,
                      "mode": "live_test_api" if live else "dry_run",
                  }) as log:

        executor = Executor(audit=log, policy=policy, live=live,
                            ground_truth=ground_truth)
        log.event("run_configured", mode="live_test_api" if live else "dry_run",
                  key_id=mask(executor.key_id) if live else None,
                  batch_size=len(txns), horizon=horizon,
                  gateway_semantics=(
                      "orders are created for real against test mode; the "
                      "authorisation outcome is resolved from ground truth "
                      "because the test API exposes no server-side way to "
                      "drive an authorisation"
                  ) if live else "no network calls")

        diagnoses, signals = diagnose_batch(txns, audit=log)

        results, decisions = {}, {}
        for t in txns:
            tid = t["transaction_id"]
            final_decision, final_result = _work_transaction(
                policy, log, executor, t, diagnoses[tid], signals, horizon
            )
            decisions[tid] = final_decision
            results[tid] = final_result

        recovered_paise = sum(
            t["amount_paise"] for t in txns
            if results[t["transaction_id"]].get("recovered")
        )
        summary = {
            "batch_size": len(txns),
            "total_value_paise": sum(t["amount_paise"] for t in txns),
            "detections": len(signals),
            "actions": dict(collections.Counter(d.action for d in decisions.values())),
            # Two views of the same run, both needed: where each transaction
            # ENDED UP, and everything that happened along the way. Reporting
            # only the first hides the intermediate attempts; only the second
            # hides the outcome. replay.py rebuilds both and diffs them.
            "final_status": dict(collections.Counter(
                (r or {}).get("status", "no_result") for r in results.values())),
            "execution_events": dict(executor.audit_decisions),
            "recovered_count": sum(1 for r in results.values() if r.get("recovered")),
            "recovered_paise": recovered_paise,
            "contacts_stubbed": executor.stats["contacts_stubbed"],
            "gateway_calls": executor.stats["gateway_calls"],
            "gateway_errors": executor.stats["gateway_errors"],
            "reconciles": executor.stats["reconciles"],
            "mode": "live_test_api" if live else "dry_run",
        }
        log.event("run_summary", **summary)

    chain = verify_chain(log_path)
    summary["chain_ok"] = chain["ok"]
    summary["log_entries"] = chain["entries"]

    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    if not quiet:
        _report(summary, chain, signals, outages, diagnoses, txns, live)

    return summary


def _report(summary, chain, signals, outages, diagnoses, txns, live):
    r = lambda p: "Rs " + format(p / 100.0, ",.2f")
    print("\n" + "=" * 64)
    print("  RUN COMPLETE  (%s)" % summary["mode"])
    print("=" * 64)
    print("  batch                %d txns worth %s"
          % (summary["batch_size"], r(summary["total_value_paise"])))

    ds = summarise_diagnoses(diagnoses)
    print("  diagnosed            SOFT %d / HARD %d, %d inside a degraded window"
          % (ds["by_severity"].get("SOFT", 0), ds["by_severity"].get("HARD", 0),
             ds["issuer_degraded"]))

    # Detection scoring is meaningless on a sliced batch: the planted
    # outages span the whole month, so grading a 25-txn slice against
    # all six understates recall by construction. Report the count,
    # withhold the score, and say why.
    if summary.get("sliced"):
        print("  degradation          %d detected  (NOT scored: %d-txn slice"
              % (len(signals), summary["batch_size"]))
        print("                       against outages spanning the full month)")
    else:
        sc = score_detections(signals, outages)
        print("  degradation          %d detected, %d planted -> precision %.2f recall %.2f"
              % (sc["detected"], sc["planted"], sc["precision"], sc["recall"]))

    print("\n  decisions")
    for a, n in sorted(summary["actions"].items(), key=lambda x: -x[1]):
        print("      %-30s %4d" % (a, n))

    print("\n  execution")
    for s, n in sorted(summary["final_status"].items(), key=lambda x: -x[1]):
        print("      %-30s %4d" % (s, n))

    print("\n  recovered            %d txns / %s"
          % (summary["recovered_count"], r(summary["recovered_paise"])))
    print("  contacts             %d prepared, 0 sent (delivery is stubbed)"
          % summary["contacts_stubbed"])
    if live:
        print("  gateway              %d calls, %d errors, %d reconciles"
              % (summary["gateway_calls"], summary["gateway_errors"],
                 summary["reconciles"]))
    print("  audit chain          %s over %d entries"
          % ("VERIFIED" if chain["ok"] else "BROKEN", chain["entries"]))
    print("=" * 64)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the recovery batch.")
    ap.add_argument("--live", action="store_true",
                    help="make real order calls against Razorpay TEST mode")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N transactions")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--summary", default=DEFAULT_SUMMARY)
    ap.add_argument("--horizon", default=DEFAULT_HORIZON)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        s = run(log_path=args.log, summary_path=args.summary, live=args.live,
                limit=args.limit, horizon=args.horizon, quiet=args.quiet)
    except LiveKeyRefused as e:
        sys.stderr.write("REFUSED: %s\n" % e)
        return 2
    return 0 if s["chain_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
