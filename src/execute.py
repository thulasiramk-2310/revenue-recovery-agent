"""The only module permitted to touch the Razorpay API or move money.

WHAT IS REAL HERE AND WHAT IS NOT
---------------------------------
This is the first thing to read, because getting it wrong would make every
number downstream a lie.

  REAL     Order creation. POST /v1/orders is a genuine authenticated call to
           Razorpay test mode. The order_id, HTTP status, request id and
           latency in the audit trail all came off the wire. You can paste an
           order_id into the Razorpay dashboard and find it.

  NOT REAL The authorisation outcome. Razorpay does not expose a server-side
           way to drive a card authorisation with a plain key pair: the
           Server-to-Server payments API returns 401 unless S2S is activated
           on the account, and completing an order otherwise requires the
           client-side Checkout flow with a real instrument. Verified against
           the live test API, not assumed:
               POST /v1/orders             -> 200
               POST /v1/payments (S2S)     -> 401 Authentication failed
               POST /v1/payments/create/upi-> 400 URL not found

           So whether an attempt SUCCEEDS is resolved against the batch's
           planted ground truth, exactly as the scoring model intends: an
           attempt succeeds only if it lands inside
           [would_recover_if_retried_at, recovery_window_closes_at].

Every audit line carries `gateway_call` (real/none) and `outcome_source`
(gateway/ground_truth_simulation) so the two can never be confused when
reading the trail. Nothing in this file reports a captured payment that did
not happen.

Customer contact is STUBBED. No email, SMS or in-app nudge is ever sent. The
full intended payload is logged and the line is marked delivered=false,
stubbed=true. See README.md, which says the same thing.

SAFETY RAILS, NON-NEGOTIABLE
----------------------------
* Refuses to run unless RAZORPAY_KEY_ID starts with the test prefix. A live
  key is a hard abort, not a warning.
* dry_run is the DEFAULT. Touching the network is an explicit argument.
* The secret is read once for auth and never enters a log line; every payload
  is scanned for it before it is written.
* Never re-attempts without reconciling first where the policy says so
  (retry_windows[code].require_reconcile_before_retry). A NETWORK_TIMEOUT may
  mean the authorisation actually succeeded; retrying blind risks
  double-charging a customer, which is worse than not recovering.

Contract
--------
Executor(...).attempt(transaction, decision) -> dict
    {status, gateway_payment_id, http_status, error_code, attempted_at,
     latency_ms, raw}
"""

from __future__ import annotations

import base64
import collections
import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

API_BASE = "https://api.razorpay.com/v1"
TEST_KEY_PREFIX = "rzp_test_"
LIVE_KEY_PREFIX = "rzp_live_"

# Actions that mean "talk to the gateway". Read from the decision, which got
# them from policy.yaml's escalation_ladder -- not a policy list of our own.
GATEWAY_ACTIONS = {"silent_retry", "retry_with_updated_instrument"}
CONTACT_ACTIONS = {"notify_customer", "request_instrument_update"}
NO_OP_ACTIONS = {"STOP", "DEFER", "HOLD"}


class LiveKeyRefused(RuntimeError):
    """Raised when a production key is present. Never downgraded to a warning."""


class SecretLeak(RuntimeError):
    """Raised if a log payload would contain the API secret."""


# -- credentials ----------------------------------------------------------

def load_credentials(env_path=".env"):
    """Read keys from .env. Returns (key_id, key_secret, mode).

    The secret is returned for auth only. It must not be stored on any object
    that gets serialised, printed, or logged.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:
        # Minimal fallback so a missing dotenv is not a hard stop.
        p = Path(env_path)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    mode = os.environ.get("RAZORPAY_MODE", "test").strip()
    return key_id, secret, mode


def mask(key_id: str) -> str:
    """A key id safe to put in a log: enough to identify, not enough to use."""
    if not key_id:
        return "<unset>"
    if len(key_id) <= 12:
        return key_id[:4] + "***"
    return key_id[:12] + "***" + key_id[-2:]


# -- rate limiting --------------------------------------------------------

class RateLimiter:
    """Minimum spacing between gateway calls, plus exponential backoff.

    Two separate concerns deliberately kept together, because they are the
    same concern seen from both sides: don't send too fast, and when told to
    slow down, actually slow down.
    """

    def __init__(self, min_interval_seconds=0.35, max_retries=4,
                 base_backoff=0.8, max_backoff=30.0, jitter=0.3):
        self.min_interval = min_interval_seconds
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.jitter = jitter
        self._last_call = 0.0
        self.sleeps = 0
        self.total_sleep = 0.0

    def pace(self):
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            self._sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    def backoff_for(self, attempt, retry_after=None):
        if retry_after:
            try:
                return min(float(retry_after), self.max_backoff)
            except (TypeError, ValueError):
                pass
        delay = min(self.base_backoff * (2 ** attempt), self.max_backoff)
        # Jitter so parallel runs do not synchronise into a thundering herd.
        return delay * (1.0 + random.uniform(-self.jitter, self.jitter))

    def _sleep(self, seconds):
        if seconds <= 0:
            return
        self.sleeps += 1
        self.total_sleep += seconds
        time.sleep(seconds)

    sleep = _sleep


# -- outcome resolution (shared by the agent and the baseline) ------------

def resolve_outcome(gt_entry, attempted_at):
    """Did an attempt at this instant recover the payment?

    Ground truth, read ONLY to score, never to choose what to do.

    The recovery WINDOW is the whole point. Money arrives and is then spent
    again, so an attempt outside [would_recover_if_retried_at,
    recovery_window_closes_at] fails even for a payment that was genuinely
    recoverable. Without that, "retry everything constantly" wins trivially
    and beating the baseline would mean nothing.

    Deliberately a free function, not a method: src/run_batch.py scores the
    agent through this, and baseline/fixed_retry.py scores the control arm
    through the same call. Identical accounting for both arms is what makes
    the comparison evidence rather than assertion.

    Returns (recovered: bool, why: str, window: dict|None).
    """
    if not gt_entry:
        return False, "no ground truth for this transaction", None
    if not gt_entry.get("is_recoverable"):
        return False, gt_entry.get("recovery_reason") or "not recoverable", None

    opens = gt_entry.get("would_recover_if_retried_at")
    closes = gt_entry.get("recovery_window_closes_at")
    if not opens:
        return False, "no recovery window", None

    t = _parse(attempted_at)
    o = _parse(opens)
    c = _parse(closes) if closes else None
    window = {"window_opens": opens, "window_closes": closes}

    if t < o:
        return False, "attempted %.1fh before the recovery window opened" % (
            (o - t).total_seconds() / 3600.0), window
    if c and t > c:
        return False, "attempted %.1fh after the recovery window closed" % (
            (t - c).total_seconds() / 3600.0), window
    return True, gt_entry.get("recovery_reason") or "inside the recovery window", window


# -- the executor ---------------------------------------------------------

class Executor:
    """Carries out one decision. Defaults to touching nothing."""

    def __init__(self, audit=None, policy=None, dry_run=True, live=False,
                 env_path=".env", ground_truth=None, timeout=15,
                 rate_limiter=None, receipt_prefix="rcv"):
        self.audit = audit
        self.policy = policy
        self.dry_run = dry_run and not live
        self.live = bool(live)
        self.timeout = timeout
        self.limiter = rate_limiter or RateLimiter()
        self.receipt_prefix = receipt_prefix
        self.ground_truth = ground_truth or {}

        self.key_id, self._secret, self.mode = load_credentials(env_path)
        self._auth_header = None

        self.stats = {
            "gateway_calls": 0, "gateway_errors": 0, "retries_after_backoff": 0,
            "contacts_stubbed": 0, "attempts": 0, "recovered": 0,
            "reconciles": 0, "skipped": 0,
        }
        # Audit decisions written by THIS layer only. The shared AuditLog
        # counter cannot be used for this: the policy layer writes lines with
        # the same names (a policy "escalated" and the executor's "escalated"
        # for the same contact are two lines), so counting from there
        # double-counts and the replay comparison rightly fails.
        self.audit_decisions = collections.Counter()
        # Unique customers who received a (stubbed) message. Counted by
        # customer, not by contact: two messages to one person is one person
        # bothered, and "customers contacted" is the number that matters when
        # comparing against a baseline that contacts nobody.
        self.customers_contacted = set()

        if self.live:
            self._assert_test_mode()
            token = base64.b64encode(
                ("%s:%s" % (self.key_id, self._secret)).encode("utf-8")
            ).decode("ascii")
            self._auth_header = "Basic " + token

    # -- safety ---------------------------------------------------------

    def _assert_test_mode(self):
        if not self.key_id:
            raise LiveKeyRefused(
                "RAZORPAY_KEY_ID is not set. Refusing to run live with no "
                "credentials."
            )
        if self.key_id.startswith(LIVE_KEY_PREFIX):
            raise LiveKeyRefused(
                "RAZORPAY_KEY_ID is a LIVE key (%s). This agent moves money; "
                "it will not run against production. Hard abort."
                % mask(self.key_id)
            )
        if not self.key_id.startswith(TEST_KEY_PREFIX):
            raise LiveKeyRefused(
                "RAZORPAY_KEY_ID (%s) does not carry the test prefix %r. "
                "Refusing to guess." % (mask(self.key_id), TEST_KEY_PREFIX)
            )
        if self.mode and self.mode.lower() != "test":
            raise LiveKeyRefused(
                "RAZORPAY_MODE is %r, not 'test'. Refusing to run."
                % self.mode
            )
        if not self._secret:
            raise LiveKeyRefused("RAZORPAY_KEY_SECRET is not set.")

    def _assert_no_secret(self, payload):
        """Last line of defence before anything reaches the trail."""
        if not self._secret:
            return
        blob = json.dumps(payload, default=str)
        if self._secret in blob:
            raise SecretLeak(
                "an audit payload contained the API secret; refusing to write"
            )

    def _log(self, kind, **fields):
        self._assert_no_secret(fields)
        if self.audit is not None:
            self.audit.event(kind, **fields)

    # -- gateway --------------------------------------------------------

    def _request(self, method, path, body=None):
        """One HTTP call with pacing, backoff and full timing.

        Returns (http_status, parsed_body, latency_ms, error_text). Never
        raises for an HTTP-level failure: a failed call is data the trail
        needs, not an exception the caller has to guess about.
        """
        url = API_BASE + path
        data = json.dumps(body).encode("utf-8") if body is not None else None

        last = (0, None, 0.0, "no attempt made")
        for attempt in range(self.limiter.max_retries + 1):
            self.limiter.pace()
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", self._auth_header)
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "recovery-agent/1.0 (buildathon; test-mode)")

            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    latency = (time.monotonic() - started) * 1000.0
                    self.stats["gateway_calls"] += 1
                    try:
                        parsed = json.loads(raw) if raw else {}
                    except ValueError:
                        parsed = {"_unparsed": raw[:500]}
                    return resp.status, parsed, latency, None

            except urllib.error.HTTPError as e:
                latency = (time.monotonic() - started) * 1000.0
                raw = e.read().decode("utf-8", "replace") if e.fp else ""
                self.stats["gateway_calls"] += 1
                self.stats["gateway_errors"] += 1
                try:
                    parsed = json.loads(raw) if raw else {}
                except ValueError:
                    parsed = {"_unparsed": raw[:500]}
                last = (e.code, parsed, latency, raw[:300])

                # 429 and 5xx are worth another go; 4xx is our fault and
                # retrying just burns quota to get the same answer.
                if e.code == 429 or 500 <= e.code < 600:
                    if attempt < self.limiter.max_retries:
                        delay = self.limiter.backoff_for(
                            attempt, e.headers.get("Retry-After")
                        )
                        self.stats["retries_after_backoff"] += 1
                        self._log(
                            "gateway_backoff", http_status=e.code,
                            attempt=attempt + 1, sleeping_seconds=round(delay, 2),
                            reason="rate limited" if e.code == 429 else "server error",
                        )
                        self.limiter.sleep(delay)
                        continue
                return last

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                latency = (time.monotonic() - started) * 1000.0
                self.stats["gateway_errors"] += 1
                last = (0, None, latency, "transport: %s" % e)
                if attempt < self.limiter.max_retries:
                    delay = self.limiter.backoff_for(attempt)
                    self.stats["retries_after_backoff"] += 1
                    self._log(
                        "gateway_backoff", http_status=0, attempt=attempt + 1,
                        sleeping_seconds=round(delay, 2), reason="transport error",
                    )
                    self.limiter.sleep(delay)
                    continue
                return last

        return last

    def create_order(self, transaction):
        """Create a real order for this transaction. Returns a result dict."""
        receipt = "%s_%s" % (self.receipt_prefix, transaction["transaction_id"])
        body = {
            "amount": int(transaction["amount_paise"]),
            "currency": "INR",
            "receipt": receipt[:40],
            "notes": {
                "transaction_id": transaction["transaction_id"],
                "failure_code": transaction["failure_code"],
                "issuer_bank": transaction["issuer_bank"],
                "recovery_attempt": str(transaction.get("attempt_number", 1) + 1),
            },
        }
        status, parsed, latency, err = self._request("POST", "/orders", body)
        parsed = parsed or {}
        return {
            "http_status": status,
            "order_id": parsed.get("id"),
            "latency_ms": round(latency, 1),
            "error": err,
            "error_code": (parsed.get("error") or {}).get("code") if isinstance(parsed, dict) else None,
            "amount_paise": parsed.get("amount"),
            "raw": parsed,
        }

    def reconcile(self, transaction, order_id=None):
        """Check status before re-attempting, where the policy demands it.

        A NETWORK_TIMEOUT is ambiguous: the authorisation may have gone
        through. Retrying blind risks charging a customer twice, which is a
        worse outcome than never recovering the payment at all.
        """
        self.stats["reconciles"] += 1
        if not (self.live and order_id):
            return {"reconciled": True, "already_paid": False,
                    "source": "simulated", "http_status": None}
        status, parsed, latency, err = self._request("GET", "/orders/%s" % order_id)
        parsed = parsed or {}
        return {
            "reconciled": status == 200,
            "already_paid": parsed.get("status") == "paid",
            "amount_paid": parsed.get("amount_paid"),
            "source": "gateway",
            "http_status": status,
            "latency_ms": round(latency, 1),
            "error": err,
        }

    # -- outcome resolution ---------------------------------------------

    def _resolve_outcome(self, transaction, attempted_at):
        """Did this attempt recover the payment?

        Delegates to the module-level resolver so that the agent and the
        fixed-retry baseline are scored by literally the same code. Two
        parallel implementations would be free to drift, and a comparison
        where each arm marks its own homework proves nothing.
        """
        return resolve_outcome(
            self.ground_truth.get(transaction["transaction_id"]), attempted_at
        )

    # -- the public entry point ------------------------------------------

    def attempt(self, transaction, decision):
        """Carry out one decision. Always returns a result dict; never raises
        for an expected failure."""
        action = getattr(decision, "action", None) or decision.get("action")
        scheduled = (getattr(decision, "scheduled_time", None)
                     or (decision.get("scheduled_time") if isinstance(decision, dict) else None))
        attempted_at = scheduled or transaction["timestamp"]
        txn_id = transaction["transaction_id"]

        if action in NO_OP_ACTIONS or action is None:
            self.stats["skipped"] += 1
            result = {
                "status": "not_attempted", "action": action,
                "attempted_at": attempted_at, "gateway_call": "none",
                "outcome_source": "policy", "latency_ms": 0.0,
                "gateway_payment_id": None, "http_status": None,
                "error_code": None, "raw": None,
            }
            self._log("execution_skipped", transaction_id=txn_id, action=action,
                      reason="policy returned a non-executing action")
            return result

        if action in CONTACT_ACTIONS:
            return self._send_contact(transaction, decision, attempted_at)
        if action in GATEWAY_ACTIONS:
            return self._attempt_retry(transaction, decision, attempted_at)

        # hand_off_to_human, or anything else the ladder introduces later.
        self.stats["skipped"] += 1
        self._log("handed_off", transaction_id=txn_id, action=action,
                  queue="merchant_ops", amount_paise=transaction["amount_paise"])
        return {
            "status": "handed_off", "action": action,
            "attempted_at": attempted_at, "gateway_call": "none",
            "outcome_source": "policy", "latency_ms": 0.0,
            "gateway_payment_id": None, "http_status": None,
            "error_code": None, "raw": None,
        }

    # -- retry path ------------------------------------------------------

    def _attempt_retry(self, transaction, decision, attempted_at):
        txn_id = transaction["transaction_id"]
        self.stats["attempts"] += 1
        needs_reconcile = bool(getattr(decision, "requires_reconcile", False))
        order = None
        recon = None
        started = time.monotonic()

        if self.live:
            order = self.create_order(transaction)
            self._log(
                "gateway_call", transaction_id=txn_id, method="POST",
                endpoint="/v1/orders", http_status=order["http_status"],
                order_id=order["order_id"], latency_ms=order["latency_ms"],
                amount_paise=transaction["amount_paise"],
                key_id=mask(self.key_id), error=order["error"],
                error_code=order["error_code"],
            )
            if needs_reconcile and order["order_id"]:
                recon = self.reconcile(transaction, order["order_id"])
                self._log(
                    "reconciled", transaction_id=txn_id,
                    order_id=order["order_id"],
                    already_paid=recon.get("already_paid"),
                    http_status=recon.get("http_status"),
                    latency_ms=recon.get("latency_ms"),
                    rationale="policy requires reconcile before retry; a "
                              "timeout may mean the charge already succeeded",
                )
                if recon.get("already_paid"):
                    # Refusing to double-charge outranks recovering the money.
                    self._audit_outcome(
                        txn_id, "retry_suppressed",
                        "Reconciliation shows the order is already paid. "
                        "Not re-attempting: double-charging a customer is a "
                        "worse outcome than not recovering.",
                        decision, transaction, attempted_at, order, 0.0,
                        outcome_source="gateway",
                    )
                    return self._result("suppressed_already_paid", order,
                                        attempted_at, 0.0, "gateway")
        else:
            recon = {"reconciled": True, "already_paid": False,
                     "source": "simulated"} if needs_reconcile else None

        recovered, why, window = self._resolve_outcome(transaction, attempted_at)
        latency = (time.monotonic() - started) * 1000.0
        if recovered:
            self.stats["recovered"] += 1

        self._audit_outcome(
            txn_id, "recovered" if recovered else "retry_executed",
            ("Recovered: %s" % why) if recovered
            else ("Attempt did not recover: %s" % why),
            decision, transaction, attempted_at, order, latency,
            outcome_source="ground_truth_simulation",
            recovered=recovered, window=window,
            reconciled=bool(recon), amount_paise=transaction["amount_paise"],
        )
        return self._result(
            "recovered" if recovered else "failed", order, attempted_at,
            latency, "ground_truth_simulation", recovered=recovered, note=why,
        )

    # -- contact path ----------------------------------------------------

    def _send_contact(self, transaction, decision, attempted_at):
        """STUBBED. Builds and logs the real payload; sends nothing.

        The payload is complete and real -- what would actually go out. Only
        the transmission is absent, and the trail says so on every line.
        """
        txn_id = transaction["transaction_id"]
        channel = getattr(decision, "channel", None) or "email"
        self.stats["contacts_stubbed"] += 1
        self.customers_contacted.add(transaction.get("customer_id"))

        rupees = transaction["amount_paise"] / 100.0
        payload = {
            "channel": channel,
            "template": "payment_failed_recovery_v1",
            "transactional": True,
            "to": {"customer_id": transaction["customer_id"]},
            "scheduled_for": attempted_at,
            "subject": "Your payment of Rs %s could not be completed" % format(rupees, ",.2f"),
            "body": (
                "We could not process your payment of Rs %s. "
                "This usually clears on its own; you can also complete it "
                "here: {{secure_fix_link}}" % format(rupees, ",.2f")
            ),
            "variables": {
                "amount_rupees": round(rupees, 2),
                "issuer_bank": transaction["issuer_bank"],
                "is_subscription": bool(transaction.get("is_subscription")),
            },
            "compliance": {
                "within_contact_hours": True,
                "consent_on_file": True,
                "marketing_content": False,
            },
        }

        self._log(
            "contact_stubbed", transaction_id=txn_id, channel=channel,
            action=getattr(decision, "action", None),
            escalation_step=getattr(decision, "escalation_step", None),
            delivered=False, stubbed=True,
            provider="none - no message was sent",
            intended_payload=payload,
            note="Payload is what would be sent. Transmission is deliberately "
                 "not implemented; see README.md.",
        )
        self._audit_outcome(
            txn_id, "escalated",
            "Customer contact prepared on channel %s and logged in full. "
            "NOT sent: message delivery is stubbed in this build." % channel,
            decision, transaction, attempted_at, None, 0.0,
            outcome_source="stubbed", channel=channel, delivered=False,
        )
        return {
            "status": "contact_stubbed", "action": getattr(decision, "action", None),
            "attempted_at": attempted_at, "gateway_call": "none",
            "outcome_source": "stubbed", "latency_ms": 0.0,
            "gateway_payment_id": None, "http_status": None,
            "error_code": None, "delivered": False, "channel": channel,
            "raw": payload,
        }

    # -- helpers ---------------------------------------------------------

    def _audit_outcome(self, txn_id, audit_decision, reason, decision,
                       transaction, attempted_at, order, latency,
                       outcome_source, **extra):
        if self.audit is None:
            return
        fields = {
            "action": getattr(decision, "action", None),
            "attempted_at": attempted_at,
            "latency_ms": round(latency, 1),
            "gateway_call": "real" if (self.live and order) else "none",
            "outcome_source": outcome_source,
            "order_id": (order or {}).get("order_id"),
            "http_status": (order or {}).get("http_status"),
            "error_code": (order or {}).get("error_code"),
            "gateway_latency_ms": (order or {}).get("latency_ms"),
            "mode": "live_test_api" if self.live else "dry_run",
            "key_id": mask(self.key_id) if self.live else None,
        }
        fields.update(extra)
        self._assert_no_secret(fields)
        self.audit_decisions[audit_decision] += 1
        self.audit.decision(
            transaction_id=txn_id,
            decision=audit_decision,
            reason=reason,
            policy_rule_applied=getattr(decision, "policy_rule_applied", "n/a"),
            **fields
        )

    def _result(self, status, order, attempted_at, latency, outcome_source,
                recovered=False, note=None):
        return {
            "status": status,
            "gateway_payment_id": (order or {}).get("order_id"),
            "http_status": (order or {}).get("http_status"),
            "error_code": (order or {}).get("error_code"),
            "attempted_at": attempted_at,
            "latency_ms": round(latency, 1),
            "gateway_call": "real" if order else "none",
            "outcome_source": outcome_source,
            "recovered": recovered,
            "note": note,
            "raw": (order or {}).get("raw"),
        }


def _parse(ts):
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)
