"""Synthetic batch of failed Indian card payments, with hidden ground truth.

Why this file matters more than it looks: the credibility of the final
"rupees recovered" number is entirely determined here. If the batch is
uniform noise, any strategy scores the same and the headline number means
nothing. So the generator bakes in the structure a real recovery agent is
supposed to exploit:

  * Failure codes are non-uniform and bank-dependent.
  * ISSUER_DOWN is clustered into real outage windows per bank, and those
    windows also inflate that bank's overall failure volume -- the
    degradation signal src/detect.py is meant to find.
  * Recoverability is code-dependent and TIME-dependent. A retry only works
    if it lands inside the transaction's recovery window. Retrying at the
    wrong moment fails even when the payment was recoverable in principle.

That last point is what stops "retry everything, often" from trivially
winning, and is what gives a policy-driven scheduler something to beat the
fixed-retry baseline with.

Ground truth lives under the `_ground_truth` key on each record. Any consumer
must strip it -- use load_batch(), which does so by default.

Deterministic: same --seed gives byte-identical output.

    python -m src.generate_data --count 240 --seed 44
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

# Anchor so runs are reproducible regardless of wall clock.
BATCH_END = datetime(2026, 8, 31, 0, 0, 0, tzinfo=IST)
BATCH_DAYS = 30

# Rough retail card-issuing mix, normalised over these five.
BANKS = {"HDFC": 0.26, "SBI": 0.24, "ICICI": 0.21, "Axis": 0.16, "Kotak": 0.13}

FAILURE_CODES = [
    "INSUFFICIENT_FUNDS", "NETWORK_TIMEOUT", "DO_NOT_HONOR", "ISSUER_DOWN",
    "CARD_EXPIRED", "INVALID_CVV", "CARD_BLOCKED", "ACCOUNT_CLOSED",
]

# Baseline mix OUTSIDE any outage. ISSUER_DOWN is deliberately rare here --
# almost all of it arrives via clustered outages, which is the point.
BASE_MIX = {
    "INSUFFICIENT_FUNDS": 0.360,
    "NETWORK_TIMEOUT": 0.155,
    "DO_NOT_HONOR": 0.145,
    "CARD_EXPIRED": 0.100,
    "CARD_BLOCKED": 0.090,
    "INVALID_CVV": 0.080,
    "ACCOUNT_CLOSED": 0.050,
    "ISSUER_DOWN": 0.020,
}

# Multiplicative per-bank tilts, applied then renormalised. Keeps banks from
# being interchangeable so per-issuer analysis has something to find.
BANK_TILT = {
    "SBI":   {"INSUFFICIENT_FUNDS": 1.35, "NETWORK_TIMEOUT": 1.20, "CARD_EXPIRED": 0.75},
    "HDFC":  {"INSUFFICIENT_FUNDS": 0.75, "DO_NOT_HONOR": 1.20, "NETWORK_TIMEOUT": 0.85},
    "ICICI": {"DO_NOT_HONOR": 1.25, "INVALID_CVV": 1.15},
    "Axis":  {"CARD_EXPIRED": 1.30, "CARD_BLOCKED": 1.20},
    "Kotak": {"NETWORK_TIMEOUT": 1.30, "INSUFFICIENT_FUNDS": 0.90},
}

# Probability a retry can EVER succeed, by code. The four terminal codes are
# zero by construction -- except a deliberate sliver on CARD_BLOCKED (see
# note in _ground_truth below).
RECOVERABLE_RATE = {
    "NETWORK_TIMEOUT": 0.86,
    "ISSUER_DOWN": 0.92,
    "INSUFFICIENT_FUNDS": 0.52,
    "DO_NOT_HONOR": 0.24,
    "CARD_BLOCKED": 0.12,
    "CARD_EXPIRED": 0.0,
    "INVALID_CVV": 0.0,
    "ACCOUNT_CLOSED": 0.0,
}

# Recurring plan prices actually seen in Indian D2C/SaaS, in paise.
SUB_TIERS = [
    14900, 19900, 24900, 29900, 39900, 49900, 59900,
    79900, 99900, 129900, 149900, 199900, 299900, 499900,
]

# Hour-of-day weights, IST. Lunch bump, strong 8-11pm peak.
HOUR_WEIGHTS = [
    0.010, 0.006, 0.004, 0.003, 0.003, 0.006,  # 00-05
    0.012, 0.022, 0.035, 0.046, 0.052, 0.055,  # 06-11
    0.062, 0.068, 0.055, 0.048, 0.046, 0.052,  # 12-17
    0.062, 0.078, 0.092, 0.089, 0.062, 0.032,  # 18-23
]


def _weighted(rng, mapping):
    keys = list(mapping)
    return rng.choices(keys, weights=[mapping[k] for k in keys], k=1)[0]


def _mix_for_bank(bank):
    tilt = BANK_TILT.get(bank, {})
    raw = {c: BASE_MIX[c] * tilt.get(c, 1.0) for c in FAILURE_CODES}
    total = sum(raw.values())
    return {c: v / total for c, v in raw.items()}


# -- amounts -------------------------------------------------------------

def _realistic_amount(rng, is_subscription):
    """Amounts that look like a real cart, not round test numbers.

    One-off purchases get psychological pricing (ends 99/49/95), then a
    plausible mangling: GST on some categories, shipping on small carts,
    a coupon on a few. Net effect is the untidy paise values a real ledger
    shows, with an AOV around INR 1,200-1,500 and a genuine long tail.
    """
    if is_subscription:
        return rng.choice(SUB_TIERS)

    bucket = rng.choices(
        ["food", "fashion", "general", "electronics", "highticket"],
        weights=[0.27, 0.31, 0.26, 0.13, 0.03], k=1,
    )[0]
    if bucket == "food":
        rupees = rng.randint(129, 749)
    elif bucket == "fashion":
        rupees = rng.choice([399, 499, 599, 699, 799, 899, 999, 1199, 1299, 1499, 1799, 1999, 2499])
    elif bucket == "general":
        rupees = rng.randint(199, 2999)
    elif bucket == "electronics":
        rupees = rng.choice([2499, 2999, 3499, 4499, 5999, 6999, 7999, 9999, 12999, 15999])
    else:
        rupees = rng.choice([18999, 22999, 27999, 32999, 44999, 54999])

    if bucket in ("food", "general") and rng.random() < 0.55:
        rupees = rupees - (rupees % 10) + rng.choice([9, 5, 9, 9])

    paise = rupees * 100

    if bucket in ("food", "general") and rng.random() < 0.42:      # GST 18%
        paise = int(round(paise * 1.18))
    elif bucket == "fashion" and rng.random() < 0.25:              # GST 5%
        paise = int(round(paise * 1.05))
    if paise < 60000 and rng.random() < 0.38:                      # shipping
        paise += rng.choice([3000, 4000, 4900, 5900])
    if rng.random() < 0.12:                                        # coupon
        paise = int(round(paise * rng.choice([0.9, 0.85, 0.8])))

    return max(paise, 4900)


# -- outages -------------------------------------------------------------

def _build_outages(rng, start, end):
    """Discrete per-bank degradation windows.

    Windows are the ground truth for detect.py. They deliberately vary in
    length (a 25-minute blip vs a 3-hour incident) so detection cannot just
    threshold on window size, and two banks may overlap.
    """
    outages = []
    n = rng.randint(5, 7)
    span = int((end - start).total_seconds())
    for i in range(n):
        bank = _weighted(rng, BANKS)
        begin = start + timedelta(seconds=rng.randint(0, max(span - 14400, 1)))
        # Outages cluster in evening peak more often than not.
        if rng.random() < 0.6:
            begin = begin.replace(hour=rng.choice([19, 20, 21, 22]), minute=rng.randint(0, 59))
        minutes = rng.choice([25, 40, 55, 75, 95, 120, 150, 180])
        outages.append({
            "outage_id": "outage_%02d" % (i + 1),
            "issuer_bank": bank,
            "start": begin.isoformat(),
            "end": (begin + timedelta(minutes=minutes)).isoformat(),
            "duration_minutes": minutes,
            "severity": round(rng.uniform(0.72, 0.94), 2),  # P(ISSUER_DOWN | in window)
        })
    outages.sort(key=lambda o: o["start"])
    return outages


def _outage_for(outages, bank, ts):
    for o in outages:
        if o["issuer_bank"] != bank:
            continue
        if datetime.fromisoformat(o["start"]) <= ts <= datetime.fromisoformat(o["end"]):
            return o
    return None


# -- ground truth --------------------------------------------------------

def _next_salary_window(rng, after):
    """Next 1st-7th of a month strictly after `after`, at a plausible hour."""
    probe = after + timedelta(days=1)
    for _ in range(40):
        if 1 <= probe.day <= 7:
            return probe.replace(hour=rng.randint(9, 21), minute=rng.randint(0, 59),
                                 second=0, microsecond=0)
        probe += timedelta(days=1)
    return after + timedelta(days=7)


def _ground_truth(rng, code, failed_at, outage, width_rng=None):
    """When -- if ever -- a retry would actually clear, and for how long.

    Returns the opening and closing of the recovery window. A retry outside
    [opens, closes] fails even though the payment was recoverable, which is
    what makes retry TIMING worth optimising rather than just retry COUNT.

    WINDOW WIDTHS COME FROM A SEPARATE RANDOM STREAM
    -----------------------------------------------
    `width_rng` is deliberately independent of `rng`, and the main stream
    still makes its original draw at its original range even where the value
    is discarded.

    This is not fussiness. random.randint consumes a variable number of
    underlying bits depending on how wide its range is, so simply editing a
    range desynchronises every later draw in the shared stream. When the
    widths were first corrected that changed the batch itself -- the
    recoverable ceiling moved from 112 transactions to 94 and total value
    from Rs 542,522.78 to Rs 485,292.13 -- which would have made the
    before/after comparison meaningless, because two things changed at once.

    Isolating the width draws means a width can never alter WHICH
    transactions exist, which are recoverable, or what they are worth. That
    is what WINDOW_MODEL.md commits to, so it is enforced here rather than
    hoped for.
    """
    side = width_rng or rng
    if rng.random() >= RECOVERABLE_RATE.get(code, 0.0):
        reason = {
            "ACCOUNT_CLOSED": "account permanently closed; no retry can succeed",
            "CARD_EXPIRED": "instrument expired; needs replacement, not a retry",
            "INVALID_CVV": "stored credential wrong; needs customer re-entry",
            "CARD_BLOCKED": "issuer block is permanent for this instrument",
            "INSUFFICIENT_FUNDS": "balance never recovers within the observation period",
            "DO_NOT_HONOR": "issuer refusal is a hard risk decline",
            "NETWORK_TIMEOUT": "underlying authorisation was genuinely rejected",
            "ISSUER_DOWN": "transaction also had a second, non-transient problem",
        }.get(code, "not recoverable by retry")
        return None, None, reason

    if code == "NETWORK_TIMEOUT":
        opens = failed_at + timedelta(minutes=rng.randint(1, 20))
        # Minutes, not days. See WINDOW_MODEL.md: the transport fault is gone
        # almost immediately, and what remains is a live checkout session that
        # ends when the customer gives up and leaves.
        rng.randint(5, 14)            # stream-preserving; value discarded
        closes = opens + timedelta(minutes=side.randint(5, 30))
        return opens, closes, "transient network fault; clears almost immediately"

    if code == "ISSUER_DOWN":
        if outage:
            opens = datetime.fromisoformat(outage["end"]) + timedelta(minutes=rng.randint(5, 45))
            why = "issuer %s recovered after outage %s" % (outage["issuer_bank"], outage["outage_id"])
        else:
            opens = failed_at + timedelta(minutes=rng.randint(30, 180))
            why = "unflagged short issuer degradation; self-resolves"
        # Bounded by the incident and the customer's patience, not by a
        # multi-day slab. This is what makes holding through an outage worth
        # anything -- see WINDOW_MODEL.md.
        rng.randint(4, 12)            # stream-preserving; value discarded
        return opens, opens + timedelta(minutes=side.randint(30, 240)), why

    if code == "INSUFFICIENT_FUNDS":
        # Split deliberately: not everyone waits for payday. The fast half is
        # what a +1d/+3d fixed retry can catch; the salary-aligned half is the
        # headroom a scheduler that reads the calendar can claim.
        if rng.random() < 0.45:
            opens = failed_at + timedelta(hours=rng.randint(14, 96))
            why = "balance topped up ad hoc (UPI transfer / partial credit)"
        else:
            opens = _next_salary_window(rng, failed_at)
            why = "balance restored at monthly salary credit"
        # A credit event, not a span: the money lands and is spent within a
        # day or so. A five-day window described the opposite of the liquidity
        # pattern that caused the failure. See WINDOW_MODEL.md.
        rng.randint(3, 9)             # stream-preserving; value discarded
        return opens, opens + timedelta(hours=side.randint(18, 36)), why

    if code == "DO_NOT_HONOR":
        # Deliberately left WIDE. Issuer discretion genuinely is not tightly
        # bounded, and a narrow value here would be false precision invented
        # to help the agent. Holding one code wide is a check on the whole
        # correction -- see WINDOW_MODEL.md.
        opens = failed_at + timedelta(hours=rng.randint(6, 96))
        rng.randint(3, 10)            # stream-preserving; value discarded
        return opens, opens + timedelta(days=side.randint(3, 10)), "soft velocity/risk block lifted by issuer"

    if code == "CARD_BLOCKED":
        # Deliberate honesty knob: a small slice of blocks are temporary fraud
        # holds that clear. Policy still refuses to retry CARD_BLOCKED, so this
        # money is knowingly left on the table. The report should say so rather
        # than quietly tune the policy to grab it.
        opens = failed_at + timedelta(hours=rng.randint(12, 120))
        rng.randint(2, 6)             # stream-preserving; value discarded
        return opens, opens + timedelta(days=side.randint(2, 6)), "temporary fraud hold released by issuer"

    return None, None, "not recoverable by retry"


# -- generation ----------------------------------------------------------

def generate(count=240, seed=42):
    rng = random.Random(seed)
    # Recovery-window WIDTHS are drawn from their own stream, so that
    # changing a width cannot perturb the batch itself. Derived from the same
    # seed, so a run stays fully reproducible from `--seed` alone.
    # See the note in _ground_truth for why this is necessary rather than tidy.
    width_rng = random.Random(seed * 7919 + 13)
    start = BATCH_END - timedelta(days=BATCH_DAYS)
    outages = _build_outages(rng, start, BATCH_END)

    # A customer pool smaller than the batch, so repeat offenders exist and
    # per-customer attempt caps are actually exercised.
    n_customers = int(count * 0.62)
    customers = ["cust_%05d" % (rng.randint(10000, 99999)) for _ in range(n_customers)]
    customers = list(dict.fromkeys(customers))
    chronic = set(rng.sample(customers, k=max(1, len(customers) // 12)))
    cust_bank = {c: _weighted(rng, BANKS) for c in customers}

    records = []
    span = int((BATCH_END - start).total_seconds())

    def sample_ts():
        base = start + timedelta(seconds=rng.randint(0, span))
        hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
        return base.replace(hour=hour, minute=rng.randint(0, 59),
                            second=rng.randint(0, 59), microsecond=0)

    # Pass 1: organic failures across the whole window.
    n_organic = int(count * 0.90)
    for _ in range(n_organic):
        cust = rng.choice(customers)
        bank = cust_bank[cust]
        ts = sample_ts()
        outage = _outage_for(outages, bank, ts)
        if outage and rng.random() < outage["severity"]:
            code = "ISSUER_DOWN"
        else:
            code = _weighted(rng, _mix_for_bank(bank))
        records.append((cust, bank, ts, code, outage))

    # Pass 2: outage-driven excess volume. A degraded issuer does not merely
    # change the failure MIX, it lifts that bank's failure COUNT -- which is
    # the signal detect.py keys on.
    while len(records) < count:
        outage = rng.choice(outages)
        bank = outage["issuer_bank"]
        o_start = datetime.fromisoformat(outage["start"])
        o_end = datetime.fromisoformat(outage["end"])
        ts = o_start + timedelta(seconds=rng.randint(0, int((o_end - o_start).total_seconds())))
        pool = [c for c in customers if cust_bank[c] == bank] or customers
        cust = rng.choice(pool)
        code = "ISSUER_DOWN" if rng.random() < outage["severity"] else _weighted(rng, _mix_for_bank(bank))
        records.append((cust, bank, ts, code, outage))

    records.sort(key=lambda r: r[2])

    transactions = []
    per_customer = Counter()
    for i, (cust, bank, ts, code, outage) in enumerate(records, start=1):
        per_customer[cust] += 1

        # Retry-of-a-retry is common for soft declines, rare for hard ones.
        if code in ("INSUFFICIENT_FUNDS", "NETWORK_TIMEOUT", "DO_NOT_HONOR", "ISSUER_DOWN"):
            attempt = rng.choices([1, 2, 3, 4], weights=[0.62, 0.24, 0.10, 0.04], k=1)[0]
        else:
            attempt = rng.choices([1, 2, 3], weights=[0.86, 0.11, 0.03], k=1)[0]
        if cust in chronic:
            attempt = min(4, attempt + rng.choice([0, 1, 1]))

        is_sub = rng.random() < (0.42 if cust in chronic else 0.25)
        amount = _realistic_amount(rng, is_sub)
        opens, closes, why = _ground_truth(rng, code, ts, outage, width_rng)

        transactions.append({
            "transaction_id": "pay_%s%04d" % (format(seed, "x")[:3].upper(), i),
            "amount_paise": amount,
            "timestamp": ts.isoformat(),
            "issuer_bank": bank,
            "failure_code": code,
            "customer_id": cust,
            "attempt_number": attempt,
            "is_subscription": is_sub,
            "_ground_truth": {
                "would_recover_if_retried_at": opens.isoformat() if opens else None,
                "recovery_window_closes_at": closes.isoformat() if closes else None,
                "is_recoverable": opens is not None,
                "recovery_reason": why,
                "in_outage": outage["outage_id"] if outage else None,
            },
        })

    return {
        "metadata": {
            "generated_by": "src/generate_data.py",
            "seed": seed,
            "count": len(transactions),
            "window_start": start.isoformat(),
            "window_end": BATCH_END.isoformat(),
            "currency": "INR",
            "amount_unit": "paise",
            "note": (
                "_ground_truth is hidden truth for scoring only. It must never "
                "be read by detect/diagnose/policy/execute. Use load_batch()."
            ),
        },
        "outage_windows": outages,
        "transactions": transactions,
    }


def load_batch(path="data/failed_payments.json", strip_ground_truth=True):
    """Load the batch the way the pipeline must see it: no ground truth."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    txns = data["transactions"]
    if strip_ground_truth:
        txns = [{k: v for k, v in t.items() if k != "_ground_truth"} for t in txns]
    return data["metadata"], data["outage_windows"], txns


# -- reporting -----------------------------------------------------------

def _bar(n, total, width=34):
    filled = int(round(width * n / total)) if total else 0
    return "#" * filled + "." * (width - filled)


def print_distribution(data):
    txns = data["transactions"]
    n = len(txns)
    total = sum(t["amount_paise"] for t in txns)

    def rupees(p):
        return "INR {:,.2f}".format(p / 100)

    print("=" * 74)
    print("BATCH: %d failed payments   %s   seed=%s"
          % (n, rupees(total), data["metadata"]["seed"]))
    print("window: %s -> %s" % (data["metadata"]["window_start"][:10],
                                data["metadata"]["window_end"][:10]))
    print("=" * 74)

    print("\nFAILURE CODE")
    codes = Counter(t["failure_code"] for t in txns)
    val = defaultdict(int)
    rec = Counter()
    for t in txns:
        val[t["failure_code"]] += t["amount_paise"]
        if t["_ground_truth"]["is_recoverable"]:
            rec[t["failure_code"]] += 1
    print("  %-20s %5s %6s  %-34s %14s %8s" % ("code", "n", "share", "", "value", "recov%"))
    for c, k in codes.most_common():
        print("  %-20s %5d %5.1f%%  %s %14s %7.0f%%"
              % (c, k, 100 * k / n, _bar(k, n), rupees(val[c]), 100 * rec[c] / k))

    print("\nISSUER BANK")
    banks = Counter(t["issuer_bank"] for t in txns)
    for b, k in banks.most_common():
        dom = Counter(t["failure_code"] for t in txns if t["issuer_bank"] == b).most_common(1)[0]
        print("  %-20s %5d %5.1f%%  %s  top: %s (%d)"
              % (b, k, 100 * k / n, _bar(k, n, 22), dom[0], dom[1]))

    print("\nISSUER_DOWN CLUSTERING  (the degradation signal detect.py must find)")
    idown = [t for t in txns if t["failure_code"] == "ISSUER_DOWN"]
    inside = sum(1 for t in idown if t["_ground_truth"]["in_outage"])
    print("  %d ISSUER_DOWN events, %d (%.0f%%) inside a declared outage window"
          % (len(idown), inside, 100 * inside / max(len(idown), 1)))
    for o in data["outage_windows"]:
        hits = [t for t in txns if t["_ground_truth"]["in_outage"] == o["outage_id"]]
        idn = sum(1 for t in hits if t["failure_code"] == "ISSUER_DOWN")
        print("    %s  %-6s %s  %3dmin  %2d txns (%2d ISSUER_DOWN)"
              % (o["outage_id"], o["issuer_bank"], o["start"][:16].replace("T", " "),
                 o["duration_minutes"], len(hits), idn))

    print("\nAMOUNTS")
    amts = sorted(t["amount_paise"] for t in txns)
    def pct(p):
        return amts[min(len(amts) - 1, int(len(amts) * p))]
    print("  min %s   p25 %s   median %s" % (rupees(amts[0]), rupees(pct(.25)), rupees(pct(.5))))
    print("  p75 %s   p95 %s   max %s" % (rupees(pct(.75)), rupees(pct(.95)), rupees(amts[-1])))
    print("  mean %s" % rupees(total / n))
    round_100 = sum(1 for a in amts if a % 10000 == 0)
    print("  ends in a round INR 100: %d (%.1f%%)  <- low is realistic" % (round_100, 100 * round_100 / n))
    print("  sample: " + ", ".join(rupees(t["amount_paise"]) for t in txns[:6]))

    print("\nATTEMPTS / SUBSCRIPTION / CUSTOMERS")
    att = Counter(t["attempt_number"] for t in txns)
    print("  attempt_number: " + "  ".join("%d:%d (%.0f%%)" % (k, att[k], 100 * att[k] / n)
                                           for k in sorted(att)))
    subs = sum(1 for t in txns if t["is_subscription"])
    print("  subscriptions: %d (%.1f%%)   one-off: %d" % (subs, 100 * subs / n, n - subs))
    cc = Counter(t["customer_id"] for t in txns)
    print("  unique customers: %d   repeat: %d   worst: %d failures"
          % (len(cc), sum(1 for v in cc.values() if v > 1), max(cc.values())))

    print("\nGROUND TRUTH  (the ceiling any strategy can reach)")
    r = [t for t in txns if t["_ground_truth"]["is_recoverable"]]
    rv = sum(t["amount_paise"] for t in r)
    print("  recoverable:     %d/%d (%.1f%%)   %s of %s (%.1f%%)"
          % (len(r), n, 100 * len(r) / n, rupees(rv), rupees(total), 100 * rv / total))
    term = [t for t in txns if t["failure_code"] in
            ("ACCOUNT_CLOSED", "CARD_EXPIRED", "INVALID_CVV", "CARD_BLOCKED")]
    tr = [t for t in term if t["_ground_truth"]["is_recoverable"]]
    print("  terminal-coded:  %d (%s) -- policy never retries these"
          % (len(term), rupees(sum(t["amount_paise"] for t in term))))
    print("  ...of which recoverable anyway: %d (%s) -- knowingly forgone,"
          % (len(tr), rupees(sum(t["amount_paise"] for t in tr))))
    print("     must be reported as left-on-table, not quietly harvested")
    print("=" * 74)


def main():
    ap = argparse.ArgumentParser(description="Generate a synthetic failed-payment batch.")
    ap.add_argument("--count", type=int, default=240)
    ap.add_argument("--seed", type=int, default=44)
    ap.add_argument("--out", default="data/failed_payments.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.count < 200:
        ap.error("count must be at least 200 (the brief requires 200+)")

    data = generate(count=args.count, seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.quiet:
        print_distribution(data)
    print("\nwrote %s  (%d transactions, %.1f KB)"
          % (out, len(data["transactions"]), out.stat().st_size / 1024))


if __name__ == "__main__":
    main()
