"""PHASE 2 -- NOT IMPLEMENTED.

The control arm. Without it, "we recovered INR X" is a number with nothing to
compare against and is not evidence of anything.

Deliberately naive, and deliberately NOT a strawman: it is what a competent
team actually ships first -- retry every failed payment on a fixed schedule,
no diagnosis, no issuer awareness, no calendar awareness.

    retry at +1h, +24h, +72h from the original failure
    same schedule for every failure code
    stop after 3 retries or on success

It therefore wastes attempts on terminal codes, retries into live issuer
outages, and misses salary-credit timing on INSUFFICIENT_FUNDS. Those three
gaps are exactly the headroom the policy-driven agent has to convert. If the
agent cannot beat this, it is not earning its complexity.

Scored identically to the agent, against the same ground-truth windows, on
the same batch, with the same attempt accounting -- otherwise the comparison
is rigged.

Contract
--------
run_baseline(transactions) -> {recovered_paise, recovered_count,
                               attempts_spent, per_transaction}
"""

from __future__ import annotations

FIXED_SCHEDULE_HOURS = [1, 24, 72]


def run_baseline(transactions):
    raise NotImplementedError("phase 2")
