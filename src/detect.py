"""Batch-level signal detection, run once before any per-transaction decision.

Its job is to find conditions that should change how the whole batch is
treated, chiefly issuer degradation: a window in which one bank's failures
spike and issuer-fault codes cluster. That matters because retrying into a
degraded issuer burns attempts against the per-transaction cap for a reason
that has nothing to do with the customer.

WHAT "FAILURE RATE" MEANS HERE
------------------------------
The batch contains only failures -- there is no success stream to divide by,
so a true failure rate is not computable from this input. Two things that ARE
computable stand in for it, and the field names below say which is which:

  * mix        share of one bank's failures in a window that carry an
               issuer-fault code (ISSUER_DOWN, NETWORK_TIMEOUT) versus that
               bank's own baseline share. This is the primary signal: during a
               real outage the *reason* customers fail changes.
  * volume     that bank's failures per hour in the window versus its baseline
               failures per hour. Corroborating only -- volume alone rises for
               dull reasons like a payday traffic peak.

Both are ordinary significance tests against the bank's own baseline, computed
outage-free (see `_baseline_fault_share`). No model, no training, no fitting.

SCORING, NOT ASSERTING
----------------------
The generator plants real outage windows (data/failed_payments.json ->
outage_windows). Detection must never read them; `score_detections` below is
the grader and takes them as an explicit argument. It is not called by the
pipeline.

Contract
--------
detect_issuer_degradation(transactions) -> list[dict]
    Each: {issuer_bank, window_start, window_end, observed_failure_rate,
           baseline_failure_rate, confidence, evidence_transaction_ids}

Every detection is written to the audit log via
AuditLog.event("issuer_degradation_detected", ...) before it influences any
decision.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

# Codes that point at the issuer or the link to it, rather than at the
# customer's balance or instrument. A cluster of these from one bank in a
# short window is what an outage looks like from the merchant's side.
ISSUER_FAULT_CODES = frozenset({"ISSUER_DOWN", "NETWORK_TIMEOUT"})

# Detection parameters. These are detection tuning, not recovery policy, so
# they live here rather than in policy.yaml -- policy.yaml decides what to DO
# about a degraded issuer, this file only decides whether one is degraded.
DEFAULTS = {
    # Two issuer-fault failures more than this far apart are treated as
    # separate incidents. Planted outages run 25-180 min; a gap wider than a
    # short outage would merge two distinct ones.
    "cluster_gap_minutes": 75,
    # Minimum issuer-fault failures before we will call an outage at all. At 1
    # a single stray timeout becomes an "outage"; the significance test cannot
    # rescue you from n=1.
    "min_evidence": 2,
    # One-sided significance threshold on the mix test. 2.0 is ~2.3% under the
    # normal approximation.
    "z_threshold": 2.0,
    # Real outages end some time after the last failure we happen to observe;
    # the last customer to fail is rarely the last second of the incident.
    # Padding the window makes HOLD decisions cover the tail.
    "window_pad_minutes": 20,
    # Floor on window length when computing a per-hour rate, so a two-minute
    # cluster does not report a meaningless 60/hour.
    "min_window_minutes": 20,
}

# Ceiling on reported confidence. See where it is applied for why.
CONFIDENCE_CAP = 0.99

# Chosen by sweeping min_evidence x z_threshold over 20 independent seeds
# (240 txns each) and scoring against planted outages:
#
#   min_evidence=2, z=2.0   recall 0.872 +- 0.118   precision 0.854
#   min_evidence=3, z=2.0   recall 0.682 +- 0.188   precision 1.000
#   min_evidence=2, z=4.0   recall 0.493 +- 0.182   precision 0.961
#
# Recall is weighted above precision deliberately. A false positive holds a
# transaction that did not need holding: we retry a little later and lose
# nothing but time. A false negative retries INTO a live outage and burns an
# attempt against limits.max_attempts that we never get back. The asymmetry
# runs one way, so the parameters lean that way.
#
# On the default seed alone, min_evidence=2/z=4.0 scored a better f1 (0.800 vs
# 0.769). Across 20 seeds it collapses to 0.626. That config was fitting one
# draw, which is exactly what the multi-seed sweep exists to catch.


# -- small numeric helpers ------------------------------------------------

def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _norm_cdf(z: float) -> float:
    """P(Z <= z) for the standard normal. Avoids a scipy dependency."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _binomial_z(k: int, n: int, p0: float) -> float:
    """One-sided z for observing k of n successes against baseline share p0.

    Returns 0.0 rather than raising when the test is undefined (n == 0, or a
    degenerate baseline), so a caller never has to special-case it.
    """
    if n <= 0 or p0 <= 0.0 or p0 >= 1.0:
        return 0.0
    se = math.sqrt(p0 * (1.0 - p0) / n)
    if se == 0.0:
        return 0.0
    return ((k / n) - p0) / se


def _poisson_z(observed: int, expected: float) -> float:
    """One-sided z for a count against a Poisson expectation."""
    if expected <= 0.0:
        return 0.0
    return (observed - expected) / math.sqrt(expected)


def _cluster(txns: list, gap_minutes: float) -> list:
    """Split time-sorted transactions wherever the gap exceeds gap_minutes."""
    if not txns:
        return []
    gap = timedelta(minutes=gap_minutes)
    groups = [[txns[0]]]
    for t in txns[1:]:
        if _parse(t["timestamp"]) - _parse(groups[-1][-1]["timestamp"]) <= gap:
            groups[-1].append(t)
        else:
            groups.append([t])
    return groups


# -- baseline -------------------------------------------------------------

def _baseline_fault_share(bank_txns: list, candidate_ids: set) -> float:
    """That bank's ordinary issuer-fault share, measured OUTSIDE candidates.

    Computing the baseline over the whole batch would include the outages
    themselves, inflating the baseline and shrinking the very spike we are
    testing for -- the test would partly cancel itself out. So candidates are
    found first, then excluded here, then tested against what is left.

    Falls back to the all-inclusive share when exclusion leaves too little to
    measure, which is the conservative direction: a higher baseline makes a
    detection harder to claim, not easier.
    """
    quiet = [t for t in bank_txns if t["transaction_id"] not in candidate_ids]
    pool = quiet if len(quiet) >= 8 else bank_txns
    if not pool:
        return 0.0
    faults = sum(1 for t in pool if t["failure_code"] in ISSUER_FAULT_CODES)
    return faults / len(pool)


# -- main detector --------------------------------------------------------

def detect_issuer_degradation(transactions, audit=None, **kwargs):
    """Find windows where one issuer's failures look like a degradation.

    Pure with respect to `transactions`: nothing is mutated. `audit`, when
    given, receives one "issuer_degradation_detected" event per detection
    before any decision can consume it.
    """
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in kwargs.items() if k in DEFAULTS})

    if not transactions:
        return []

    by_bank = defaultdict(list)
    for t in transactions:
        by_bank[t["issuer_bank"]].append(t)

    all_times = [_parse(t["timestamp"]) for t in transactions]
    batch_hours = max(
        (max(all_times) - min(all_times)).total_seconds() / 3600.0, 1.0
    )

    detections = []

    for bank, bank_txns in by_bank.items():
        bank_txns = sorted(bank_txns, key=lambda t: t["timestamp"])
        bank_hourly_rate = len(bank_txns) / batch_hours

        faults = [t for t in bank_txns if t["failure_code"] in ISSUER_FAULT_CODES]
        clusters = [
            c for c in _cluster(faults, cfg["cluster_gap_minutes"])
            if len(c) >= cfg["min_evidence"]
        ]
        if not clusters:
            continue

        candidate_ids = {t["transaction_id"] for c in clusters for t in c}
        p0 = _baseline_fault_share(bank_txns, candidate_ids)

        for cluster in clusters:
            start = _parse(cluster[0]["timestamp"])
            end = _parse(cluster[-1]["timestamp"])

            # Judge the window on ALL of that bank's traffic inside it, not
            # just the fault codes -- otherwise the mix share is 100% by
            # construction and the test proves nothing.
            in_window = [
                t for t in bank_txns if start <= _parse(t["timestamp"]) <= end
            ]
            n = len(in_window)
            k = sum(
                1 for t in in_window
                if t["failure_code"] in ISSUER_FAULT_CODES
            )

            z_mix = _binomial_z(k, n, p0)

            span_minutes = max(
                (end - start).total_seconds() / 60.0, cfg["min_window_minutes"]
            )
            span_hours = span_minutes / 60.0
            observed_rate = n / span_hours
            expected_count = bank_hourly_rate * span_hours
            z_volume = _poisson_z(n, expected_count)

            if z_mix < cfg["z_threshold"]:
                continue

            pad = timedelta(minutes=cfg["window_pad_minutes"])
            # A z of 5 on four transactions is real, but the normal
            # approximation is not trustworthy to seven decimal places at that
            # n, and reporting "confidence 1.000" off four data points is an
            # overclaim a reviewer would rightly pick apart. Cap it, and
            # publish evidence_count so the reader can judge the n themselves.
            confidence = round(min(_norm_cdf(z_mix), CONFIDENCE_CAP), 4)

            detections.append({
                "signal": "ISSUER_DEGRADED",
                "issuer_bank": bank,
                "window_start": (start - pad).isoformat(),
                "window_end": (end + pad).isoformat(),
                "observed_window_start": start.isoformat(),
                "observed_window_end": end.isoformat(),
                # Failures per hour, not a true failure rate -- see module
                # docstring. Named _failure_rate to satisfy the pinned
                # contract; the *_per_hour aliases say what they really are.
                "observed_failure_rate": round(observed_rate, 3),
                "baseline_failure_rate": round(bank_hourly_rate, 3),
                "observed_failures_per_hour": round(observed_rate, 3),
                "baseline_failures_per_hour": round(bank_hourly_rate, 3),
                "observed_fault_share": round(k / n, 4) if n else 0.0,
                "baseline_fault_share": round(p0, 4),
                "z_mix": round(z_mix, 3),
                "z_volume": round(z_volume, 3),
                "confidence": confidence,
                "evidence_transaction_ids": [
                    t["transaction_id"] for t in in_window
                    if t["failure_code"] in ISSUER_FAULT_CODES
                ],
                "affected_transaction_ids": [
                    t["transaction_id"] for t in in_window
                ],
                "method": "binomial-z on issuer-fault mix vs outage-free bank baseline",
            })

    detections.sort(key=lambda d: (d["window_start"], d["issuer_bank"]))

    if audit is not None:
        for d in detections:
            audit.event(
                "issuer_degradation_detected",
                issuer_bank=d["issuer_bank"],
                window_start=d["window_start"],
                window_end=d["window_end"],
                confidence=d["confidence"],
                z_mix=d["z_mix"],
                z_volume=d["z_volume"],
                observed_fault_share=d["observed_fault_share"],
                baseline_fault_share=d["baseline_fault_share"],
                evidence_count=len(d["evidence_transaction_ids"]),
                evidence_transaction_ids=d["evidence_transaction_ids"],
                method=d["method"],
            )

    return detections


# -- lookup used by diagnose.py and policy.py -----------------------------

def signal_for_transaction(transaction, detections):
    """Return the degradation signal covering this transaction, or None.

    Matches on bank AND time: a transaction is only inside an outage if it is
    the same issuer and falls in the window.
    """
    if not detections:
        return None
    ts = _parse(transaction["timestamp"])
    bank = transaction["issuer_bank"]
    for d in detections:
        if d["issuer_bank"] != bank:
            continue
        if _parse(d["window_start"]) <= ts <= _parse(d["window_end"]):
            return d
    return None


# -- grading (never called by the pipeline) -------------------------------

def score_detections(detections, outage_windows, tolerance_minutes=45):
    """Grade detections against the generator's planted outages.

    SCORING ONLY. `outage_windows` is ground truth; the detector never sees
    it. A detection counts as a hit when it names the right bank and its
    window overlaps the planted one (within a tolerance, since we can only
    observe an outage through the failures it happens to produce).
    """
    tol = timedelta(minutes=tolerance_minutes)
    planted = [
        {
            "outage_id": o["outage_id"],
            "issuer_bank": o["issuer_bank"],
            "start": _parse(o["start"]),
            "end": _parse(o["end"]),
        }
        for o in outage_windows
    ]

    matched_planted, matched_detections = set(), set()
    pairs = []
    for i, d in enumerate(detections):
        ds, de = _parse(d["window_start"]), _parse(d["window_end"])
        for p in planted:
            if p["issuer_bank"] != d["issuer_bank"]:
                continue
            if ds <= p["end"] + tol and p["start"] - tol <= de:
                matched_planted.add(p["outage_id"])
                matched_detections.add(i)
                pairs.append((d, p["outage_id"]))
                break

    tp = len(matched_detections)
    fp = len(detections) - tp
    fn = len(planted) - len(matched_planted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "planted": len(planted),
        "detected": len(detections),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "matched_outage_ids": sorted(matched_planted),
        "missed_outage_ids": sorted(
            p["outage_id"] for p in planted if p["outage_id"] not in matched_planted
        ),
        "unmatched_detections": [
            {
                "issuer_bank": d["issuer_bank"],
                "window_start": d["window_start"],
                "window_end": d["window_end"],
                "z_mix": d["z_mix"],
                "confidence": d["confidence"],
            }
            for i, d in enumerate(detections) if i not in matched_detections
        ],
    }
