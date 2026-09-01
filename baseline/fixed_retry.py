"""The control arm. Without it, "we recovered Rs X" compares to nothing.

Deliberately naive, and deliberately NOT a strawman: this is what a competent
team actually ships first. Retry every failed payment on a fixed schedule, no
diagnosis, no issuer awareness, no calendar awareness, no stopping rules.

    retry at +1h, +24h, +72h from the original failure
    the same schedule for every failure code
    stop after 3 retries, or on success

Being a fair opponent matters more than being an easy one. Three things are
done specifically to avoid rigging the comparison:

  1. It is scored by the SAME function as the agent -- execute.resolve_outcome
     -- against the same ground-truth windows on the same batch. Neither arm
     marks its own homework.
  2. The schedule is a reasonable one. +1h/+24h/+72h is a real pattern used in
     production dunning, not a deliberately bad choice.
  3. Its structural advantages are reported, not buried. It never contacts a
     customer, and it is free to retry transactions the agent's compliance
     rules forbid it from touching. Where the baseline wins, the table says so.

Where the headroom is, if the agent is to earn its complexity:

  * terminal codes -- the baseline spends 3 attempts each on cards that are
    expired, blocked, or attached to closed accounts, and never recovers one
  * issuer outages -- it retries into a bank that is currently down, burning
    attempts on a fault that has nothing to do with the customer
  * salary timing -- INSUFFICIENT_FUNDS is balance-driven, and +1h/+24h/+72h
    mostly re-hits an empty account rather than waiting for the reload

Contract
--------
run_baseline(transactions, ground_truth) -> dict
"""

from __future__ import annotations

import collections
from datetime import datetime, timedelta

from src.execute import resolve_outcome

# The schedule, in hours from the ORIGINAL failure.
FIXED_SCHEDULE_HOURS = [1, 24, 72]


def _parse(ts):
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def run_baseline(transactions, ground_truth, schedule_hours=None):
    """Retry everything on a fixed schedule and score it honestly.

    Returns the same shape of result the agent reports, so the two can be put
    side by side without either being reshaped to fit.
    """
    schedule = schedule_hours or FIXED_SCHEDULE_HOURS

    recovered_ids = set()
    recovered_paise = 0
    attempts_spent = 0
    wasted_on_terminal = 0
    wasted_on_terminal_attempts = 0
    per_transaction = {}
    attempts_by_code = collections.Counter()

    for t in transactions:
        tid = t["transaction_id"]
        gt = ground_truth.get(tid, {})
        failed_at = _parse(t["timestamp"])

        trail = []
        recovered_here = False
        for offset in schedule:
            attempted_at = (failed_at + timedelta(hours=offset)).isoformat()
            attempts_spent += 1
            attempts_by_code[t["failure_code"]] += 1

            ok, why, _ = resolve_outcome(gt, attempted_at)
            trail.append({
                "attempted_at": attempted_at,
                "offset_hours": offset,
                "recovered": ok,
                "reason": why,
            })
            if ok:
                recovered_here = True
                recovered_ids.add(tid)
                recovered_paise += t["amount_paise"]
                break

        # No diagnosis means no way to know an attempt was pointless. Counted
        # so the waste can be reported rather than inferred.
        if not gt.get("is_recoverable"):
            wasted_on_terminal += 1
            wasted_on_terminal_attempts += len(trail)

        per_transaction[tid] = {
            "recovered": recovered_here,
            "attempts": len(trail),
            "amount_paise": t["amount_paise"],
            "failure_code": t["failure_code"],
            "trail": trail,
        }

    return {
        "strategy": "fixed_retry_%s" % "_".join("%dh" % h for h in schedule),
        "schedule_hours": list(schedule),
        "recovered_count": len(recovered_ids),
        "recovered_paise": recovered_paise,
        "recovered_ids": sorted(recovered_ids),
        "attempts_spent": attempts_spent,
        # A fixed-retry baseline has no escalation ladder, so it never sends
        # a customer anything. That is a genuine advantage on the contact
        # axis and the comparison reports it as one.
        "customers_contacted": 0,
        "contacts_sent": 0,
        "attempts_on_unrecoverable": wasted_on_terminal_attempts,
        "unrecoverable_transactions_retried": wasted_on_terminal,
        "attempts_by_failure_code": dict(attempts_by_code),
        "per_transaction": per_transaction,
    }
