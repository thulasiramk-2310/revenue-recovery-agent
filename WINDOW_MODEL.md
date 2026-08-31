# Recovery window model — pre-registration

**Written and committed BEFORE the generator was changed or any result was
re-measured.** The git history of this file is the evidence: if the commit that
introduces it does not precede the commit that changes
`src/generate_data.py`, this document is worthless and should be treated as
such.

---

## Why this file exists

Phase 1 gave every recoverable transaction a recovery window several days
wide. Measured after the fact:

| | |
|---|---|
| windows wider than 48h | **111 of 112 — 99.1%** |
| median width | 156h (6.5 days) |
| narrowest | 48h |
| widest | 312h (13 days) |

That is a modeling error, and it invalidates the central claim the ground
truth was built to support.

The recovery **window** exists so that retry *timing* matters and not merely
retry *count*. A window seven days wide cannot do that: any schedule that
fires at all within the week succeeds, so a blind fixed-retry baseline is
optimal by construction. The mechanism defeats its own purpose.

The consequence is measurable. On the phase-1 windows the naive baseline
captures 86.0% of the recoverable ceiling against the agent's 51.4%, and
recovers ₹69,906.62 more gross. A large part of that is not the agent being
worse — it is the data making discrimination impossible.

**The problem: correcting this will probably make the agent look better, and
a correction that helps the author is indistinguishable from tuning unless it
is fixed in advance and justified independently.** Hence this document.

---

## The rule this file binds

1. Every width below is justified by how the underlying real-world condition
   behaves — not by what it does to the result.
2. The values are fixed here **before** `src/generate_data.py` is touched and
   before anything is re-run.
3. The generator is run **once** with these values. Whatever comes out is
   reported, including if the agent still loses.
4. Results on both the original and corrected windows are published side by
   side, plus a sensitivity sweep across widths. The old numbers are not
   deleted or replaced.
5. If a value later needs to change, that is a new pre-registration with its
   own justification and its own commit — never an edit to this one.

---

## The windows

`opens` is measured from the original failure. `width` is how long the window
stays open before the recovery opportunity is gone.

### NETWORK_TIMEOUT — opens 1–20 min, width **5–30 min**

*Was: width 5–14 days.*

A network or gateway timeout is a transient transport fault. The condition
that caused it — a dropped connection, a slow issuer response, a saturated
link — is gone within minutes. What follows is not a lingering opportunity
but a fork: either the customer's session is still alive and a prompt retry
captures the payment, or they have closed the tab and gone elsewhere.

A seven-day window implies a customer who remains willing to be charged all
week without doing anything, which is not how checkout abandonment works.
This is the single largest correction and it is the one most clearly wrong
before.

### ISSUER_DOWN — opens at outage end + 5–45 min, width **30 min – 4 h**

*Was: width 4–12 days.*

Tied to the incident, which is the whole point of detecting it. Once the
issuer recovers, the payment is authorisable again — but the customer's
intent decays on the same timescale as any checkout. The width is now
anchored to the planted outage duration (25–180 min) rather than being an
unrelated multi-day slab.

This is what makes issuer-degradation detection worth doing: hold the attempt
through the outage, then spend it inside a few-hour window. Under the old
model, holding gained nothing because the window stayed open for a week
regardless.

### INSUFFICIENT_FUNDS — an event, not a span. width **18–36 h**

*Was: width 3–9 days.*

Balance-driven failures do not recover gradually. They recover at a credit
event — salary, a UPI transfer, a partial deposit — and the money is spent
soon after it lands. Modelling this as a five-day slab says a customer's
account stays funded for most of a week, which is the opposite of the
liquidity pattern that caused the failure in the first place.

Kept as a two-mode split, unchanged in structure:

* **ad hoc top-up (45%)** — opens 14–96 h after failure, width 18–36 h
* **salary credit (55%)** — opens at the next 1st–7th payday window, width
  18–36 h

The split stays because both patterns are real. Narrowing the width is what
makes calendar-aware scheduling worth anything: hitting a 24-hour payday
window is a genuine skill, hitting a 120-hour one is not.

### DO_NOT_HONOR — opens 6–96 h, width **3–10 days (UNCHANGED)**

Deliberately left wide, and this is the honest exception.

`DO_NOT_HONOR` is an opaque catch-all covering issuer risk holds, velocity
limits, and internal scoring. When such a block lifts is genuinely
unpredictable and genuinely not tightly bounded — an issuer may relax a
velocity counter over days. A narrow window here would be false precision
invented to help the agent.

Leaving one code wide is a check on this whole exercise: if every width had
been narrowed, the corrections would look like they were chosen for their
effect rather than their realism. This one costs the agent and stays as it is.

### CARD_BLOCKED — opens 12–120 h, width **2–6 days (UNCHANGED)**

Temporary fraud holds released by the issuer, on the issuer's own timescale.
Unchanged because the agent never retries this code anyway — `policy.yaml`
lists `CARD_BLOCKED` in `stop_immediately_on` — so its width cannot flatter
the agent. It only affects how much the *baseline* collects, and narrowing it
would shrink the compliance carve-out that currently counts **against** us.

---

## Summary of changes

| code | old width | new width | direction | affects agent |
|---|---|---|---|---|
| NETWORK_TIMEOUT | 5–14 days | 5–30 min | much narrower | helps |
| ISSUER_DOWN | 4–12 days | 30 min – 4 h | much narrower | helps |
| INSUFFICIENT_FUNDS | 3–9 days | 18–36 h | narrower | helps |
| DO_NOT_HONOR | 3–10 days | unchanged | — | neutral |
| CARD_BLOCKED | 2–6 days | unchanged | — | counts against |

Three narrowed, two held. The two held are the two where narrowing would
either be false precision or would conveniently shrink a result that
currently counts against the agent.

## What is NOT being changed

- Which transactions are recoverable at all (`RECOVERABLE_RATE`) — untouched.
  The ceiling of 112 transactions / ₹202,447.60 must not move.
- When windows **open**. Only widths change, except where the opening was
  already tied to an outage or payday.
- The failure mix, amounts, banks, outage windows, or the seed.
- `policy.yaml`. The agent's own retry schedule is not being re-tuned to suit
  the new windows; that would be a second, separate change and would need its
  own justification.

## The prediction, recorded in advance

Stated now so it can be checked against the outcome rather than rationalised
afterwards.

The agent should close much of the gap, because narrow windows are precisely
what a scheduler can exploit and a blind sprayer cannot. It may still lose on
gross: the baseline's 611 attempts give it more chances to land inside any
window, and it will still collect the ₹12,296.20 of `CARD_BLOCKED` fraud
holds that the agent is forbidden to touch.

**If the agent still loses, that is the reported result.** The cost table,
the compliance carve-out and the sensitivity sweep make a bounded agent a
coherent engineering position whether or not it wins on gross rupees.
