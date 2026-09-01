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

from baseline.fixed_retry import FIXED_SCHEDULE_HOURS, run_baseline

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

# --- Unit economics -------------------------------------------------------
# Gross recovered rupees is the wrong scoreboard on its own: it treats gateway
# attempts and customer messages as free, which is exactly the assumption that
# makes spray-and-pray look good. These put a price on both.
#
# Both are ASSUMPTIONS, deliberately conservative, and overridable on the
# command line so a reviewer can substitute their own and re-run. They are not
# measured from this dataset -- nothing here could measure them.
#
#   attempt: roughly Indian PG per-transaction economics for a retried
#            authorisation. Excludes the indirect cost of a rising decline
#            ratio, which is real but not defensibly quantifiable here.
#   contact: the DIRECT send cost of a transactional email or SMS. Nothing
#            more. Messaging a customer about a failed payment also spends
#            goodwill, and that cost is real -- but it is not monetary, and
#            inventing a rupee figure for it would let us manufacture whatever
#            answer we wanted. Contacts therefore get their own column and are
#            judged on the count, not folded into the money.
COST_PER_ATTEMPT_PAISE = 250      # Rs 2.50 per gateway retry
COST_PER_CONTACT_PAISE = 100      # Rs 1.00 direct send cost, goodwill excluded

# Guard against a policy that defers forever. Each pass either advances the
# clock or acts; this caps how many passes one transaction may take.
#
# Raised from 12 when the contact rungs became reachable. A transaction can
# now spend its retries AND then walk the contact rungs, each of which may
# defer once for contact hours and once for the 24h spacing rule, before
# handing off. 12 truncated that tail silently -- the ceiling has to sit above
# the longest legitimate path or it becomes a stopping rule nobody wrote down.
MAX_PASSES = 24


def _ground_truth_index(path="data/failed_payments.json"):
    """Ground truth, keyed by transaction id. Scoring only.

    Loaded separately from the batch on purpose: the pipeline gets its data
    through load_batch(), which strips this, and only the executor's outcome
    resolution ever sees it.
    """
    data = json.loads(open(path, encoding="utf-8").read())
    return {t["transaction_id"]: t.get("_ground_truth", {})
            for t in data["transactions"]}


class CustomerLedger:
    """Per-customer, per-day counters shared across the whole batch.

    limits.max_attempts_per_customer_per_day has been in policy.yaml since
    phase 1 and had never once fired, because TransactionState carried the
    field and nothing ever populated it. Every transaction was decided as
    though its customer had a clean slate, so a customer with six failures
    was six separate first offences. The cap read as enforced in the document
    and was dead in the run -- the same failure as the unreachable ladder
    rungs, and worth naming as such rather than quietly fixing.

    Attempts are counted per IST calendar day, matching the wording of
    limits.max_attempts_per_customer_per_day. Contacts are counted over a
    ROLLING 24 hours, because that cap exists to stop a person being pestered
    and a calendar boundary is not something a pestered person experiences --
    measured by calendar day it let 20 message pairs through by straddling
    midnight.

    Honest limitation: transactions are worked to completion one at a time,
    so a customer's later transaction sees the contact history of the earlier
    ones but not the reverse. The ledger is therefore order-dependent. It is
    a real bound on a real resource, not an exact simulation of concurrent
    dunning, and the run reports the resulting distribution rather than
    asserting the cap held perfectly.
    """

    WINDOW = timedelta(hours=24)

    def __init__(self):
        self.attempts = collections.Counter()
        self.contacts = collections.defaultdict(list)

    @staticmethod
    def _day(transaction, when):
        return (transaction.get("customer_id"),
                datetime.fromisoformat(when).astimezone(IST).date())

    def load(self, state, transaction, when, cap=None):
        now = datetime.fromisoformat(when)
        state.attempts_today_for_customer = self.attempts[
            self._day(transaction, when)]

        sent = sorted(t for t in self.contacts[transaction.get("customer_id")]
                      if now - t < self.WINDOW)
        state.contacts_recent_for_customer = len(sent)
        # When the window frees up a slot: the moment the oldest message that
        # currently occupies one falls out of it. With a cap of 2 and 2 sent,
        # that is 24h after the older of the two.
        if cap and len(sent) >= int(cap):
            state.customer_contact_quota_resets_at = (
                sent[len(sent) - int(cap)] + self.WINDOW).isoformat()
        else:
            state.customer_contact_quota_resets_at = None

    def charge(self, transaction, when, consumes):
        if consumes == "attempt":
            self.attempts[self._day(transaction, when)] += 1
        elif consumes == "contact":
            self.contacts[transaction.get("customer_id")].append(
                datetime.fromisoformat(when))


def _work_transaction(policy, log, executor, transaction, diagnosis, signals,
                      horizon, ledger=None):
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
        # Refresh the cross-transaction counters for whatever day the clock
        # has reached, so the per-customer caps are evaluated against the
        # customer's real history rather than a blank slate.
        if ledger is not None:
            ledger.load(state, work, now,
                        cap=policy.limits.get("max_contacts_per_customer_per_24h"))
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

        # The budget was spent. Charge it to the RIGHT one and let the policy
        # decide again. `consumes` comes from the rung in policy.yaml; reading
        # it here instead of matching on action names means adding a rung to
        # the ladder does not require editing this loop, and means a contact
        # can never silently charge the attempt budget.
        if ledger is not None:
            ledger.charge(work, now, d.consumes)
        if d.consumes == "attempt":
            state.last_attempt_at = now
            work["attempt_number"] = int(work.get("attempt_number", 1)) + 1
        elif d.consumes == "contact":
            # Deliberately does NOT touch last_attempt_at. The retry cooldown
            # protects the customer's instrument from repeated authorisation
            # attempts; sending an email is not one, and letting a message
            # reset that clock would delay a legitimate retry for no reason.
            state.last_contact_at = now
        if d.customer_visible:
            state.contacts_used += 1
        if d.escalation_step:
            # Highest rung REACHED, never a forced advance past a rung that
            # is still eligible.
            state.escalation_step = max(state.escalation_step, d.escalation_step)
        if last_result.get("status") == "handed_off":
            break

    return last_decision, last_result


def run(log_path=DEFAULT_LOG, summary_path=DEFAULT_SUMMARY, live=False,
        limit=None, horizon=DEFAULT_HORIZON, data_path="data/failed_payments.json",
        policy_path="policy.yaml", quiet=False):
    # data/ is gitignored, so this is the FIRST thing a fresh clone hits if
    # the quickstart's generate_data step is skipped. A traceback here reads
    # as a broken project; it is just a missing input, and the fix is one
    # command.
    if not os.path.exists(data_path):
        raise SystemExit(
            "\nNo batch found at %s.\n\n"
            "The batch is generated, not committed -- data/ is gitignored so\n"
            "the repo carries no data anyone could mistake for real payments.\n\n"
            "Run this first:\n"
            "    python -m src.generate_data\n" % data_path
        )
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
        ledger = CustomerLedger()
        for t in txns:
            tid = t["transaction_id"]
            final_decision, final_result = _work_transaction(
                policy, log, executor, t, diagnoses[tid], signals, horizon,
                ledger=ledger,
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
            "attempts_spent": executor.stats["attempts"],
            "customers_contacted": len(executor.customers_contacted),
            "mode": "live_test_api" if live else "dry_run",
        }

        # --- the control arm --------------------------------------------
        # Same batch, same ground-truth windows, same scoring function. The
        # only difference between the arms is the strategy.
        #
        # Scored INSIDE the audit context on purpose: the comparison is part
        # of the run's result, so it belongs in the trail. Computing it after
        # the log closed left replay.py unable to rebuild it, and a number in
        # the summary that the log cannot reproduce is exactly the gap the
        # replay exists to catch.
        base = run_baseline(txns, ground_truth)
        ceiling = _ceiling(txns, ground_truth)
        summary["baseline"] = {k: v for k, v in base.items()
                               if k not in ("per_transaction", "recovered_ids")}
        summary["ceiling"] = {k: v for k, v in ceiling.items() if k != "ids"}
        summary["uplift_paise"] = (summary["recovered_paise"]
                                   - base["recovered_paise"])
        summary["costs"] = {
            "per_attempt_paise": COST_PER_ATTEMPT_PAISE,
            "per_contact_paise": COST_PER_CONTACT_PAISE,
        }
        # Cost is driven by MESSAGES SENT, not by people reached. 133 sends
        # across 88 customers costs 133 sends. Charging per unique customer
        # silently discounts every follow-up message, which is precisely the
        # kind of quiet understatement the cost model exists to prevent.
        # customers_contacted stays reported, because reach is the right
        # measure of the goodwill cost that is deliberately NOT monetised.
        summary["net_recovered_paise"] = _net(
            summary["recovered_paise"], summary["attempts_spent"],
            summary["contacts_stubbed"])
        summary["baseline"]["net_recovered_paise"] = _net(
            base["recovered_paise"], base["attempts_spent"],
            base["contacts_sent"])
        # The prices belong in the trail. A net figure is unreadable without
        # them, and a reviewer must be able to substitute their own and
        # recompute rather than take ours on faith.
        log.event("cost_model", **summary["costs"])
        log.event("recoverable_ceiling", **summary["ceiling"])
        log.event("baseline_scored", **summary["baseline"])
        log.event("run_summary", **summary)

    chain = verify_chain(log_path)
    summary["chain_ok"] = chain["ok"]
    summary["log_entries"] = chain["entries"]

    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    exceptions_path = os.path.join(
        os.path.dirname(log_path) or ".", "exceptions.md")
    write_exceptions(exceptions_path, txns, decisions, results, diagnoses,
                     ground_truth, base)

    if not quiet:
        _report(summary, chain, signals, outages, diagnoses, txns, live)
        _comparison(summary, base, ceiling, txns, ground_truth, results)
        print("\n  exception list written to %s" % exceptions_path)

    return summary


def _net(recovered_paise, attempts, contacts):
    """Recovered rupees minus what it cost to go and get them."""
    return (recovered_paise
            - attempts * COST_PER_ATTEMPT_PAISE
            - contacts * COST_PER_CONTACT_PAISE)


def _break_even_attempt_cost_paise(summary, base):
    """What an attempt would have to cost for the agent to win on net.

    The single most useful number in the comparison. If the agent recovers
    less gross but spends far fewer attempts, the whole case rests on
    attempts being expensive -- so state the price at which that case starts
    working, and let the reader judge whether it is plausible.

    Returns None when the agent already wins on gross.
    """
    gross_gap = base["recovered_paise"] - summary["recovered_paise"]
    attempt_gap = base["attempts_spent"] - summary["attempts_spent"]
    contact_gap = summary["contacts_stubbed"] - base["contacts_sent"]
    if gross_gap <= 0 or attempt_gap <= 0:
        return None
    return (gross_gap + contact_gap * COST_PER_CONTACT_PAISE) / attempt_gap


def _ceiling(txns, ground_truth):
    """The most any strategy could possibly recover on this batch.

    Straight from would_recover_if_retried_at: a transaction is in the
    ceiling if ground truth says a well-timed retry would have cleared it.
    Every capture rate below is measured against this, not against the batch
    total, because the batch total includes money nobody could ever get.
    """
    ids = [t["transaction_id"] for t in txns
           if ground_truth.get(t["transaction_id"], {}).get("is_recoverable")]
    by_id = {t["transaction_id"]: t for t in txns}
    return {
        "count": len(ids),
        "paise": sum(by_id[i]["amount_paise"] for i in ids),
        "ids": ids,
    }


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
        # Print the confusion matrix, not just the two ratios. "7 detected,
        # 6 planted -> precision 0.71" reads like an arithmetic error: the
        # obvious mental bound is 6/7 = 0.86, which only holds if all six
        # planted outages were among the seven found. One was missed, so the
        # true split is 5 correct of 7 found and 5 found of 6 planted. Making
        # a reader re-derive that is a good way to have them assume a bug.
        print("  degradation          %d detected vs %d planted"
              % (sc["detected"], sc["planted"]))
        print("                       %d correct, %d false, %d missed"
              % (sc["true_positives"], sc["false_positives"],
                 sc["false_negatives"]))
        print("                       precision %.2f (%d/%d)  recall %.2f (%d/%d)"
              % (sc["precision"], sc["true_positives"], sc["detected"],
                 sc["recall"], sc["true_positives"], sc["planted"]))

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


def _comparison(summary, base, ceiling, txns, ground_truth, results):
    """The headline table. Both arms, same scoring, costs made explicit.

    Gross recovered rupees on its own is a misleading scoreboard: it prices
    gateway attempts and customer messages at zero, which is precisely the
    assumption that makes spray-and-pray look free. Cost and net are shown
    alongside gross so the reader can see both, and the break-even line says
    what an attempt would have to cost for the bounded approach to win.
    """
    rup = lambda p: "Rs " + format(p / 100.0, ",.2f")
    pct = lambda n, d: ("%.1f%%" % (100.0 * n / d)) if d else "n/a"

    a_gross, b_gross = summary["recovered_paise"], base["recovered_paise"]
    a_att, b_att = summary["attempts_spent"], base["attempts_spent"]
    a_con, b_con = summary["customers_contacted"], base["customers_contacted"]
    a_msg, b_msg = summary["contacts_stubbed"], base["contacts_sent"]
    a_net = summary["net_recovered_paise"]
    b_net = summary["baseline"]["net_recovered_paise"]
    a_cost, b_cost = a_gross - a_net, b_gross - b_net
    cap = ceiling["paise"]

    print("\n" + "=" * 72)
    print("  AGENT vs FIXED-RETRY BASELINE")
    print("  same batch, same ground-truth windows, same scoring function")
    print("=" * 72)
    print("  %-24s %16s %16s" % ("", "Agent", "Baseline"))
    print("  " + "-" * 58)
    print("  %-24s %16s %16s" % ("Gross recovered", rup(a_gross), rup(b_gross)))
    print("  %-24s %16s %16s"
          % ("", "%d txns" % summary["recovered_count"],
             "%d txns" % base["recovered_count"]))
    print("  %-24s %16d %16d" % ("Attempts", a_att, b_att))
    print("  %-24s %16d %16d" % ("Messages sent", a_msg, b_msg))
    print("  %-24s %16d %16d" % ("Customers contacted", a_con, b_con))
    print("  %-24s %16s %16s" % ("Cost", rup(a_cost), rup(b_cost)))
    print("  %-24s %16s %16s" % ("Net recovered", rup(a_net), rup(b_net)))
    print("  %-24s %16s %16s"
          % ("Rs per attempt", rup(a_gross // a_att if a_att else 0),
             rup(b_gross // b_att if b_att else 0)))
    print("  %-24s %16s %16s"
          % ("Capture vs ceiling", pct(a_gross, cap), pct(b_gross, cap)))
    print("  " + "-" * 58)
    print("  recoverable ceiling      %d txns / %s (%s of batch value)"
          % (ceiling["count"], rup(cap), pct(cap, summary["total_value_paise"])))
    print("  costed at                %s per attempt, %s per contact"
          % (rup(COST_PER_ATTEMPT_PAISE), rup(COST_PER_CONTACT_PAISE)))
    print("                           (assumptions, not measurements; goodwill")
    print("                           cost of a contact is NOT monetised)")

    gross_d, net_d = a_gross - b_gross, a_net - b_net
    print("\n" + "-" * 58)
    if gross_d >= 0:
        print("  GROSS   agent ahead by %s" % rup(gross_d))
    else:
        print("  GROSS   AGENT LOSES by %s  (%.1fx fewer attempts: %d vs %d)"
              % (rup(-gross_d), (b_att / a_att) if a_att else 0, a_att, b_att))
    if net_d >= 0:
        print("  NET     agent ahead by %s" % rup(net_d))
    else:
        print("  NET     AGENT LOSES by %s -- costing attempts does NOT"
              % rup(-net_d))
        print("          close the gap on this batch.")

    be = _break_even_attempt_cost_paise(summary, base)
    if be is not None:
        print("\nBREAK-EVEN   an attempt would have to cost %s for the agent"
              % rup(be))
        print("               to win on net. That is %.0fx the %s assumed here,"
              % (be / COST_PER_ATTEMPT_PAISE, rup(COST_PER_ATTEMPT_PAISE)))
        print("               and far above real Indian PG economics. The")
        print("               efficiency argument does not carry the result.")

    print("\nagent wastes 0 attempts on unrecoverable transactions;")
    print("  the baseline spends %d across %d of them and recovers none."
          % (base["attempts_on_unrecoverable"],
             base["unrecoverable_transactions_retried"]))
    print("=" * 72)


def write_exceptions(path, txns, decisions, results, diagnoses, ground_truth,
                     base):
    """Every transaction the agent did not recover, and why.

    Not a footnote. An honest exception list is the difference between a
    result and a claim, and the buckets below are kept apart because they
    mean very different things:

      * knowingly forgone   -- recoverable, and policy forbids chasing it
      * tried and missed    -- we acted and still lost the money
      * correctly declined  -- ground truth agrees nothing would have worked
      * other               -- attempts exhausted, or handed to a human

    The first bucket is the uncomfortable one, so it is listed first.
    """
    rup = lambda p: "Rs " + format(p / 100.0, ",.2f")

    forgone, missed, declined, other = [], [], [], []
    for t in txns:
        tid = t["transaction_id"]
        res = results.get(tid) or {}
        if res.get("recovered"):
            continue
        d = decisions.get(tid)
        gt = ground_truth.get(tid, {})
        recoverable = bool(gt.get("is_recoverable"))
        rule = getattr(d, "policy_rule_applied", "?") or "?"
        row = {
            "tid": tid, "amount": t["amount_paise"], "code": t["failure_code"],
            "bank": t["issuer_bank"], "action": getattr(d, "action", "?"),
            "rule": rule, "reason": getattr(d, "reason", "") or "",
            "status": res.get("status", "no_result"),
            "note": res.get("note") or "",
        }
        if recoverable and rule.startswith("stop_immediately_on"):
            forgone.append(row)
        elif recoverable:
            missed.append(row)
        elif getattr(d, "terminal", False):
            declined.append(row)
        else:
            other.append(row)

    buckets = [
        ("Knowingly forgone", forgone,
         "Ground truth says a retry **would have cleared these**, and "
         "`policy.yaml` forbids retrying the code. Chasing them would raise "
         "the headline number and break the compliance requirement. Listed "
         "first because they are what a reviewer should push on."),
        ("Tried and missed", missed,
         "Recoverable, the agent acted, and the money was still lost - "
         "usually because the retry landed outside the recovery window. This "
         "is the real remaining headroom, and the honest measure of how much "
         "better the timing could get."),
        ("Correctly declined", declined,
         "Hard declines where ground truth agrees no retry would have "
         "worked. The baseline spends %d attempts on transactions in this "
         "class and recovers none of them." % base["attempts_on_unrecoverable"]),
        ("Other unresolved", other,
         "Attempts exhausted, handed to a human, or scheduled beyond the run "
         "horizon."),
    ]

    total_n = sum(len(b[1]) for b in buckets)
    total_p = sum(r["amount"] for b in buckets for r in b[1])

    L = ["# Exceptions: what the agent did not recover", ""]
    L.append("Every unresolved transaction in the batch, with its reason. "
             "Nothing is omitted")
    L.append("or aggregated away.")
    L.append("")
    L.append("**%d transactions / %s unresolved, out of %d in the batch.**"
             % (total_n, rup(total_p), len(txns)))
    L.append("")
    L.append("| bucket | txns | value | meaning |")
    L.append("|---|---:|---:|---|")
    meanings = {
        "Knowingly forgone": "recoverable, policy forbids chasing",
        "Tried and missed": "we acted and still lost it",
        "Correctly declined": "nothing would have worked",
        "Other unresolved": "attempts exhausted or escalated out",
    }
    for title, rows, _ in buckets:
        L.append("| %s | %d | %s | %s |"
                 % (title, len(rows), rup(sum(r["amount"] for r in rows)),
                    meanings[title]))
    L.append("")

    for title, rows, blurb in buckets:
        L.append("")
        L.append("## %s - %d transactions, %s"
                 % (title, len(rows), rup(sum(r["amount"] for r in rows))))
        L.append("")
        L.append(blurb)
        L.append("")
        if not rows:
            L.append("_None._")
            continue
        L.append("| transaction | amount | code | bank | outcome | rule applied |")
        L.append("|---|---:|---|---|---|---|")
        for r in sorted(rows, key=lambda x: -x["amount"]):
            L.append("| `%s` | %s | %s | %s | %s | `%s` |"
                     % (r["tid"], rup(r["amount"]), r["code"], r["bank"],
                        r["status"], r["rule"]))

    L += ["", "---", "",
          "Full reasoning for any single transaction is in `results/run.log`:",
          "", "```bash", "python -m src.replay --transaction <transaction_id>",
          "```", ""]

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return {"forgone": len(forgone), "missed": len(missed),
            "declined": len(declined), "other": len(other)}


if __name__ == "__main__":
    raise SystemExit(main())
