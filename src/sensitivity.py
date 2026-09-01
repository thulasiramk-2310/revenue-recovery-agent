"""How the agent/baseline result moves with recovery-window width.

A single comparison at one window width is a point estimate dressed up as a
finding. The honest version is the curve: at what width does a bounded,
diagnosing agent stop being worth its complexity, and a blind retry schedule
start winning?

The mechanism is simple and worth stating plainly, because it is the whole
result. A recovery window is how long the opportunity to collect a payment
stays open. When windows are wide, any schedule that fires at all lands
inside one, so knowing WHEN to retry is worth nothing and the strategy with
the most attempts wins by arithmetic. When windows are narrow, most attempts
miss, and choosing the moment is the only thing that matters.

So this is not really a sweep over a data parameter. It is a sweep over how
much timing skill the world rewards.

Method
------
Window widths are rescaled in memory -- `closes = opens + (closes - opens) *
scale` -- so every other property of the batch is held fixed: the same
transactions, amounts, failure codes, issuers, and the same set of
recoverable payments. Only the width moves. Both arms are then scored by
`execute.resolve_outcome`, exactly as in a normal run.

    python -m src.sensitivity
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

from .audit import AuditLog
from .diagnose import diagnose_batch
from .execute import Executor
from .generate_data import load_batch
from .policy import load_policy
from .run_batch import (
    COST_PER_ATTEMPT_PAISE, COST_PER_CONTACT_PAISE, DEFAULT_HORIZON,
    _ground_truth_index, _work_transaction,
)
from baseline.fixed_retry import run_baseline

# Multipliers on the as-shipped widths. Chosen to span from "the opportunity
# is essentially instantaneous" to "the opportunity stands for a fortnight",
# which brackets every plausible real-world case.
SCALES = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0]


def _rescale(ground_truth, scale):
    """Widen or narrow every recovery window, holding the opening fixed."""
    out = {}
    for tid, gt in ground_truth.items():
        if not gt or not gt.get("is_recoverable"):
            out[tid] = gt
            continue
        opens = datetime.fromisoformat(gt["would_recover_if_retried_at"])
        closes = datetime.fromisoformat(gt["recovery_window_closes_at"])
        width = (closes - opens) * scale
        g = dict(gt)
        g["recovery_window_closes_at"] = (opens + width).isoformat()
        out[tid] = g
    return out


def _median_width_hours(ground_truth):
    ws = []
    for gt in ground_truth.values():
        if not gt or not gt.get("is_recoverable"):
            continue
        o = datetime.fromisoformat(gt["would_recover_if_retried_at"])
        c = datetime.fromisoformat(gt["recovery_window_closes_at"])
        ws.append((c - o).total_seconds() / 3600.0)
    ws.sort()
    return ws[len(ws) // 2] if ws else 0.0


def _run_agent(policy, txns, diagnoses, signals, gt):
    """One full agent pass against a given ground truth."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        with AuditLog(path, policy_id=policy.policy_id,
                      policy_version=policy.version, dry_run=True) as log:
            ex = Executor(audit=log, policy=policy, ground_truth=gt)
            recovered = 0
            for t in txns:
                _, res = _work_transaction(
                    policy, log, ex, t, diagnoses[t["transaction_id"]],
                    signals, DEFAULT_HORIZON)
                if (res or {}).get("recovered"):
                    recovered += t["amount_paise"]
        return {
            "recovered_paise": recovered,
            "attempts": ex.stats["attempts"],
            "contacts": len(ex.customers_contacted),
        }
    finally:
        os.unlink(path)


def sweep(scales=None):
    scales = scales or SCALES
    _, _, txns = load_batch()
    policy = load_policy()
    base_gt = _ground_truth_index()
    # Diagnosis never sees ground truth, so it is computed once and reused --
    # the agent's behaviour must not vary with the thing it cannot observe.
    diagnoses, signals = diagnose_batch(txns)

    rows = []
    for scale in scales:
        gt = _rescale(base_gt, scale)
        agent = _run_agent(policy, txns, diagnoses, signals, gt)
        base = run_baseline(txns, gt)
        a_net = (agent["recovered_paise"]
                 - agent["attempts"] * COST_PER_ATTEMPT_PAISE
                 - agent["contacts"] * COST_PER_CONTACT_PAISE)
        b_net = (base["recovered_paise"]
                 - base["attempts_spent"] * COST_PER_ATTEMPT_PAISE
                 - base["customers_contacted"] * COST_PER_CONTACT_PAISE)
        # The baseline is free to retry codes policy.yaml forbids the agent
        # from touching (fraud holds). That money is a compliance decision,
        # not a strategy difference, so the like-for-like column removes it
        # and compares the two arms on the ground they actually share.
        carve = sum(
            t["amount_paise"] for t in txns
            if t["failure_code"] in policy.stop_immediately_on
            and base["per_transaction"][t["transaction_id"]]["recovered"]
        )
        rows.append({
            "carve_out_paise": carve,
            "like_for_like_delta": (agent["recovered_paise"]
                                    - (base["recovered_paise"] - carve)),
            "scale": scale,
            "median_width_h": _median_width_hours(gt),
            "agent_paise": agent["recovered_paise"],
            "baseline_paise": base["recovered_paise"],
            "agent_attempts": agent["attempts"],
            "baseline_attempts": base["attempts_spent"],
            "agent_net": a_net,
            "baseline_net": b_net,
            "gross_delta": agent["recovered_paise"] - base["recovered_paise"],
            "net_delta": a_net - b_net,
        })
    return rows


def crossover(rows, key="net_delta"):
    """Median width at which the agent stops winning, by linear interpolation.

    Reported as a range between the two bracketing runs rather than a single
    number, because interpolating between two simulations is not a precise
    measurement and should not be dressed up as one.
    """
    ordered = sorted(rows, key=lambda r: r["median_width_h"])
    for a, b in zip(ordered, ordered[1:]):
        if (a[key] >= 0) != (b[key] >= 0):
            return a["median_width_h"], b["median_width_h"]
    return None


def main():
    rows = sweep()
    rup = lambda p: format(p / 100.0, ",.0f")
    print("\n" + "=" * 78)
    print("  SENSITIVITY: result vs recovery-window width")
    print("  everything else held fixed -- same txns, amounts, codes, ceiling")
    print("=" * 78)
    print("  %6s %10s %12s %12s %9s %9s  %s"
          % ("scale", "median h", "agent Rs", "baseline Rs", "agent n",
             "base n", "verdict"))
    print("  " + "-" * 74)
    for r in rows:
        verdict = ("AGENT +%s" % rup(r["gross_delta"])) if r["gross_delta"] > 0 \
            else ("baseline +%s" % rup(-r["gross_delta"]))
        print("  %6.2f %10.1f %12s %12s %9d %9d  %s"
              % (r["scale"], r["median_width_h"], rup(r["agent_paise"]),
                 rup(r["baseline_paise"]), r["agent_attempts"],
                 r["baseline_attempts"], verdict))

    for key, label in (("gross_delta", "GROSS"), ("net_delta", "NET"),
                       ("like_for_like_delta", "LIKE-FOR-LIKE")):
        c = crossover(rows, key)
        print("\n  %s crossover:" % label, end=" ")
        if c is None:
            wins = all(r[key] > 0 for r in rows)
            print("none in the swept range -- the agent %s throughout"
                  % ("wins" if wins else "loses"))
        else:
            print("between %.1fh and %.1fh median width" % c)
            print("         below that the agent leads; above it, spraying wins")
    print("=" * 78)
    print("  Read this as the operating range. A bounded agent earns its")
    print("  complexity where opportunities are short-lived. Where they stay")
    print("  open for days, nothing beats making more attempts.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
