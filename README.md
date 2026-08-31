# Payment Recovery Agent

Razorpay AI Buildathon — **Track 3, AI Revenue Recovery**.

Takes a batch of failed payments, works out why each one failed, picks a
bounded recovery action from a declarative policy, carries it out against
Razorpay test-mode APIs, and produces an audit trail complete enough to
reconstruct the entire run without the code.

---

## What is real and what is not

Read this first. Everything else depends on it.

| | Status |
|---|---|
| Failed-payment batch | **Synthetic.** 240 generated transactions with planted ground truth, so recovery can be measured honestly rather than asserted. |
| Order creation | **Real.** `POST /v1/orders` against Razorpay test mode. Order ids in the trail can be fetched back from the API. |
| Authorisation outcome | **Simulated.** Resolved against planted ground truth — see below. |
| Customer messages (email / SMS / in-app) | **Stubbed. Nothing is ever sent.** The full intended payload is logged and marked `delivered: false, stubbed: true`. |

### Why the authorisation outcome is simulated

Razorpay exposes no server-side way to drive a card authorisation with an
ordinary key pair. This was verified against the live test API rather than
assumed:

```
POST /v1/orders              -> 200   order created
POST /v1/payments  (S2S)     -> 401   Authentication failed (S2S not activated)
POST /v1/payments/create/upi -> 400   URL not found
```

Completing an order needs the client-side Checkout flow with a real
instrument. So the agent creates real orders and resolves *whether the attempt
would have succeeded* against the batch's ground truth: an attempt succeeds
only if it lands inside `[would_recover_if_retried_at,
recovery_window_closes_at]`.

Every audit line carries `gateway_call` (`real`/`none`) and `outcome_source`
(`gateway`/`ground_truth_simulation`/`stubbed`) so the two can never be
confused when reading the trail. **Nothing in this repo reports a captured
payment that did not happen.**

---

## Results

240 transactions, ₹542,522.78, seed 44, dry run:

```
recovered            34 txns / ₹75,192.07
degradation          7 windows detected vs 6 planted -- precision 0.71, recall 0.83
contacts             57 prepared, 0 sent (delivery is stubbed)
audit chain          VERIFIED over 1,356 entries
replay               reconstruction matches the run exactly
```

Live slice against the test API (25 transactions): 13 real gateway calls, 0
errors, 2 reconciliations, chain verified over 160 entries.

**Money deliberately not chased.** The generator plants `CARD_BLOCKED`
transactions that *would* clear on retry (temporary fraud holds).
`policy.yaml` forbids retrying `CARD_BLOCKED`. At the default seed that is
4 transactions / ₹12,296 knowingly forgone. Relaxing the rule would raise the
headline number and break the compliance requirement, so the report states it
rather than hiding it.

---

## Architecture

```
data/failed_payments.json      240 synthetic failures + hidden ground truth
        |
   detect.py      batch-level signals -- issuer degradation windows
        |
   diagnose.py    per-transaction cause (NOT the action)
        |
   policy.py      pure interpreter of policy.yaml -> bounded decision
        |
   execute.py     the only module allowed to touch Razorpay
        |
   run_batch.py   orchestrate -> results/run.log + run_summary.json
        |
   replay.py      rebuild the whole run from the log alone
```

Three rules hold the design together:

1. **`policy.py` contains no failure codes, delays, or thresholds.** They all
   live in `policy.yaml`. Every decision cites the dotted YAML path that
   produced it, so a reviewer can re-derive any call without reading code.
   A test scans the source to enforce this.
2. **Diagnosis is separate from action.** `diagnose.py` says what happened;
   only `policy.py` says what to do, and only within bounds the YAML sets.
3. **A decision *not* to act is still a decision** and still gets an audit
   line, with a reason and a rule path.

### The scheduling model

The agent works a queue rather than evaluating the batch at one instant. The
clock starts when a payment failed and advances only as the policy says to
wait: a `DEFER` or `HOLD` moves the clock to when the transaction becomes
actionable and asks again. A failed retry consumes an attempt and the loop
continues until the policy stops it, the money is recovered, or the horizon
passes.

This matters more than it sounds. Evaluating everything at a single fixed
"now" was the original implementation, and it clamped every scheduled retry to
that instant — a month after the failures — so all 82 candidates landed after
their recovery windows had closed and the agent recovered nothing at all.

---

## Stopping rules

Enforced in strict order. A later rule can only ever be more restrictive.

| # | Rule | Outcome |
|---|---|---|
| 1 | customer opt-out | STOP |
| 2 | `failure_code` in `stop_immediately_on` | STOP |
| 3 | `attempt_number >= max_attempts` | STOP |
| 4 | inside the cooldown window | DEFER |
| 5 | inside an `ISSUER_DEGRADED` window | HOLD until it clears |
| 6 | otherwise | `retry_windows` + `escalation_ladder` |

Opt-out leads because it is the one refusal no amount of recoverable money may
override. A rule that can be outvoted by a large enough number is not a rule.

`DEFER` versus `STOP` is load-bearing: STOP discards a recoverable payment
permanently, DEFER only postpones it. `HOLD` preserves an attempt instead of
spending it on an issuer-side fault.

---

## Issuer degradation detection

Ordinary significance testing, no model and no training.

The batch contains only failures, so a true failure rate is not computable.
Two things that are computable stand in for it:

* **mix** — the share of one bank's failures in a window carrying an
  issuer-fault code, against that bank's own baseline measured *outside* the
  candidate windows. This is the primary signal: during an outage the *reason*
  customers fail changes.
* **volume** — that bank's failures per hour against its baseline.
  Corroborating only, since volume rises for dull reasons like a payday peak.

Parameters were chosen by sweeping over **20 independent seeds**, not tuned on
the one being reported:

| min_evidence | z | recall | precision |
|---|---|---|---|
| **2** | **2.0** | **0.872 ± 0.118** | **0.854** |
| 3 | 2.0 | 0.682 ± 0.188 | 1.000 |
| 2 | 4.0 | 0.493 ± 0.182 | 0.961 |

Recall is weighted above precision deliberately: a false positive holds a
transaction that did not need holding and costs only time, while a false
negative retries into a live outage and burns an attempt that never comes
back.

On the default seed alone, `min_evidence=2 / z=4.0` scored a better F1 (0.800
vs 0.769). Across 20 seeds it collapses to 0.626 — it was fitting one draw,
which is what the multi-seed sweep exists to catch.

---

## The audit trail is the deliverable

`results/run.log` is append-only JSONL, hash-chained: each entry commits to
the previous one, so an edited, inserted or deleted line is detectable.

`replay.py` rebuilds a complete run **from the log alone** — not from the
input batch, not from `policy.yaml`, not from the run's own summary — and
diffs its reconstruction against what the run claimed at the time.

```bash
python -m src.replay --compare
python -m src.replay --transaction pay_2C0011   # one payment's full story
```

If a number cannot be rebuilt from the trail, replay reports it as a gap
rather than quietly filling it in from another source. A trail that cannot
reproduce the run is not an audit trail.

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # add your test-mode keys

python -m src.generate_data              # regenerate the batch + distribution
python -m src.run_batch                  # dry run, no network
python -m src.run_batch --live --limit 25    # real orders, TEST mode only
python -m src.replay --compare           # rebuild the run from its log
python -m unittest discover -s tests     # 84 tests
```

### Safety

* The executor **refuses to start** if `RAZORPAY_KEY_ID` is not a test key.
  A live key is a hard abort, not a warning.
* `dry_run` is the default; touching the network takes an explicit `--live`.
* The API secret is read once for auth and never enters a log line. Every
  payload is scanned for it before it is written, and the check raises rather
  than redacting silently.
* `.env` and any key file are gitignored, and this was verified with
  `git check-ignore` and by scanning committed blobs — not assumed.

---

## Layout

```
src/generate_data.py   synthetic batch + planted ground truth and outages
src/detect.py          issuer degradation, with a scorer for grading it
src/diagnose.py        SOFT/HARD taxonomy + batch-context rewriting
src/policy.py          pure interpreter of policy.yaml
src/execute.py         the only module that touches Razorpay
src/audit.py           hash-chained append-only trail
src/replay.py          rebuild a run from its log alone
src/run_batch.py       orchestrator
policy.yaml            every limit, delay, terminal code and compliance rail
tests/                 84 tests, stdlib unittest
```

Dependencies: PyYAML and python-dotenv. The gateway client is stdlib `urllib`,
so rate limiting, backoff and latency measurement are all visible in the code
rather than hidden in a library.
