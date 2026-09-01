"""Stopping rules: the part that has to be defensible.

A recovery agent is judged as much on what it refuses to do as on what it
recovers. These tests pin the refusals:

  * terminal failure codes are never retried
  * an opt-out outranks every other consideration, including money
  * attempt caps are ceilings that no path may exceed
  * cooldown DEFERS rather than STOPS -- the distinction matters, because
    stopping a recoverable payment loses it permanently
  * a degraded issuer HOLDS the attempt instead of spending it
  * the enforcement order is exactly as specified, and each rule outranks
    every rule below it

Plus the structural guards: policy.py must contain no literal failure codes,
the diagnosis taxonomy must not drift from policy.yaml, and every decision
must reach the audit trail.

stdlib unittest, deliberately: adding pytest to run six tests would break the
minimal-dependency rule for no benefit.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import audit as audit_mod
from src.diagnose import HARD_CODES, SOFT_CODES, diagnose, diagnose_batch
from src.detect import detect_issuer_degradation
from src.generate_data import load_batch
from src.policy import (
    DEFER, HOLD, STOP, Policy, TransactionState, decide_and_log, load_policy,
)

IST = timezone(timedelta(hours=5, minutes=30))
POLICY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policy.yaml"
)

BASE_TS = "2026-08-10T14:00:00+05:30"


def txn(**over):
    t = {
        "transaction_id": "pay_TEST01",
        "amount_paise": 149900,
        "timestamp": BASE_TS,
        "issuer_bank": "HDFC",
        "failure_code": "INSUFFICIENT_FUNDS",
        "customer_id": "cust_00001",
        "attempt_number": 1,
        "is_subscription": False,
    }
    t.update(over)
    return t


def later(hours):
    return (datetime.fromisoformat(BASE_TS) + timedelta(hours=hours)).isoformat()


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY_PATH)
        # Well past any cooldown, so nothing is accidentally blocked by
        # timing when the test is about something else.
        self.now = later(48)

    def decide(self, transaction, state=None, signals=None, now=None):
        d = diagnose(transaction, signals or [])
        return self.policy.decide(
            transaction, d, state or TransactionState(),
            signals or [], now or self.now,
        )


# -- terminal failure codes ----------------------------------------------

class TestTerminalCodes(PolicyCase):
    """A hard decline forecloses the GATEWAY, not the transaction.

    These tests previously asserted `action == STOP`. That was a proxy: while
    a terminal code ended the transaction outright, "stopped" and "was not
    retried" were the same observation. They are no longer the same, because a
    hard decline now escalates to the customer -- which is the one move that
    can actually fix an expired card.

    So each test here asserts the rule that has to hold, which is that no
    attempt is spent, and asserts it through `consumes` and
    `retry_foreclosed_by` rather than through the shape of the outcome. That
    is strictly stronger: it survives any future change to the ladder, and it
    would still fail if a terminal code were quietly re-authorised.
    """

    def test_every_terminal_code_forecloses_retries(self):
        for code in self.policy.stop_immediately_on:
            with self.subTest(code=code):
                d = self.decide(txn(failure_code=code))
                self.assertNotEqual(d.consumes, "attempt",
                                    code + " must never spend an attempt")
                self.assertEqual(d.retry_foreclosed_by,
                                 "stop_immediately_on[%s]" % code)

    def test_terminal_code_cites_the_rule_that_foreclosed_it(self):
        # The trail must name the rule even though the decision that comes
        # back is now an escalation. A reviewer greps retry_foreclosed_by;
        # without it the refusal would only be visible as an absence.
        for code in self.policy.stop_immediately_on:
            with self.subTest(code=code):
                d = self.decide(txn(failure_code=code))
                self.assertEqual(d.retry_foreclosed_by,
                                 "stop_immediately_on[%s]" % code)

    def test_terminal_code_is_not_retried_with_everything_favourable(self):
        # Attempt 1, cooldown long elapsed, consent on file, healthy issuer,
        # an alternate instrument on hand. Nothing in the context may rescue a
        # hard decline onto a retry rung -- including rung 2, which would
        # otherwise be tempted by the alternate instrument.
        d = self.decide(
            txn(failure_code="CARD_EXPIRED", attempt_number=1),
            TransactionState(consent_on_file=True,
                             alternate_instrument_available=True),
        )
        self.assertNotEqual(d.consumes, "attempt")
        self.assertIsNotNone(d.retry_foreclosed_by)

    def test_terminal_code_is_not_retried_even_on_a_large_amount(self):
        # The forgone-money case. A blocked card that would clear on retry is
        # still not retried; size must not buy an exception.
        d = self.decide(txn(failure_code="CARD_BLOCKED", amount_paise=5_000_000))
        self.assertNotEqual(d.consumes, "attempt")
        self.assertEqual(d.retry_foreclosed_by, "stop_immediately_on[CARD_BLOCKED]")

    def test_issuer_outage_cannot_launder_a_terminal_code(self):
        # A degradation window must not turn CARD_EXPIRED into "transient".
        signal = [{
            "signal": "ISSUER_DEGRADED", "issuer_bank": "HDFC",
            "window_start": "2026-08-10T13:00:00+05:30",
            "window_end": "2026-08-10T16:00:00+05:30",
            "observed_window_start": "2026-08-10T13:00:00+05:30",
            "observed_window_end": "2026-08-10T16:00:00+05:30",
            "confidence": 0.99, "z_mix": 5.0, "z_volume": 5.0,
            "evidence_transaction_ids": ["a", "b", "c"],
        }]
        for code in self.policy.stop_immediately_on:
            with self.subTest(code=code):
                d = self.decide(txn(failure_code=code), signals=signal)
                self.assertNotEqual(d.consumes, "attempt")
                self.assertEqual(d.retry_foreclosed_by,
                                 "stop_immediately_on[%s]" % code)

    # -- the behaviour the old design was missing entirely ----------------

    def test_terminal_code_still_reaches_the_customer(self):
        # The point of the change. An expired card is the case where the
        # customer is the ONLY party who can resolve the failure, so refusing
        # to retry must not also mean refusing to tell them.
        d = self.decide(txn(failure_code="CARD_EXPIRED"))
        self.assertEqual(d.consumes, "contact")
        self.assertTrue(d.customer_visible)
        self.assertIsNotNone(d.channel)

    def test_terminal_code_escalation_is_still_bounded_by_the_contact_quota(self):
        # Escalating instead of stopping must not become an unbounded licence
        # to message someone. The contact budget is a real ceiling.
        quota = int(self.policy.limits["max_customer_contacts_per_transaction"])
        d = self.decide(
            txn(failure_code="CARD_EXPIRED"),
            TransactionState(contacts_used=quota),
        )
        self.assertNotEqual(d.consumes, "contact")
        self.assertFalse(d.customer_visible)

    def test_terminal_code_without_consent_hands_off_rather_than_contacting(self):
        # Consent gates the contact rungs, so the ladder should fall through
        # to handoff rather than inventing a contact it is not permitted to
        # make. Failing closed, not failing silent.
        d = self.decide(
            txn(failure_code="ACCOUNT_CLOSED"),
            TransactionState(consent_on_file=False),
        )
        self.assertFalse(d.customer_visible)
        self.assertNotEqual(d.consumes, "attempt")
        self.assertTrue(d.terminal)

    def test_a_ladder_that_drops_retry_permitted_is_refused_not_obeyed(self):
        # The choke point. policy.yaml is editable, so the predicate guarding
        # the retry rungs is not by itself a guarantee. Simulate the dangerous
        # edit -- someone removes retry_permitted from rung 1 -- and assert the
        # code refuses rather than quietly retrying a blocked card.
        import copy
        from src.policy import Policy, PolicyViolation
        doc = copy.deepcopy(self.policy.doc)
        for rung in doc["escalation_ladder"]:
            rung["requires"] = [r for r in (rung.get("requires") or [])
                                if r not in ("retry_permitted", "retryable_failure")]
        weakened = Policy(doc)
        t = txn(failure_code="CARD_BLOCKED")
        with self.assertRaises(PolicyViolation):
            weakened.decide(t, diagnose(t, []), TransactionState(), [], self.now)


# -- opt-out --------------------------------------------------------------

class TestOptOut(PolicyCase):

    def test_opt_out_stops_a_perfectly_retryable_failure(self):
        d = self.decide(
            txn(failure_code="NETWORK_TIMEOUT"),
            TransactionState(opted_out=True),
        )
        self.assertEqual(d.action, STOP)
        self.assertIn("opt_out", d.policy_rule_applied)

    def test_opt_out_outranks_a_terminal_code(self):
        # Rule 1 runs before rule 2, so the trail must attribute the refusal
        # to the opt-out. Both stop; only one is the reason.
        d = self.decide(
            txn(failure_code="ACCOUNT_CLOSED"),
            TransactionState(opted_out=True),
        )
        self.assertEqual(d.action, STOP)
        self.assertIn("opt_out", d.policy_rule_applied)

    def test_opt_out_is_not_negotiable_against_value(self):
        # No amount makes contacting an opted-out customer acceptable.
        for amount in (100, 500_000, 100_000_000):
            with self.subTest(amount=amount):
                d = self.decide(
                    txn(amount_paise=amount),
                    TransactionState(opted_out=True),
                )
                self.assertEqual(d.action, STOP)

    def test_opt_out_never_produces_a_customer_contact(self):
        d = self.decide(txn(), TransactionState(opted_out=True, escalation_step=2))
        self.assertFalse(d.customer_visible)
        self.assertIsNone(d.channel)


# -- attempt caps ---------------------------------------------------------

class TestAttemptCaps(PolicyCase):

    def test_at_the_cap_spends_no_further_attempt(self):
        # The cap bounds the ATTEMPT budget, so that is what is asserted. It
        # no longer ends the transaction: escalation to the customer spends a
        # different budget and stays available. See TestTerminalCodes for why
        # the assertion moved off `action == STOP`.
        cap = self.policy.limits["max_attempts"]
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=cap))
        self.assertNotEqual(d.consumes, "attempt")
        self.assertIsNotNone(d.retry_foreclosed_by)

    def test_above_the_cap_spends_no_further_attempt(self):
        cap = self.policy.limits["max_attempts"]
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=cap + 5))
        self.assertNotEqual(d.consumes, "attempt")
        self.assertIsNotNone(d.retry_foreclosed_by)

    def test_exhausting_attempts_does_not_exhaust_contacts(self):
        # The whole point of separating the budgets. A customer past the retry
        # cap is still reachable, and previously was not: this is the case
        # that made rungs 3-5 dead code and put a false "0 contacts" in the
        # cost table.
        cap = self.policy.limits["max_attempts"]
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=cap))
        self.assertEqual(d.consumes, "contact")
        self.assertTrue(d.customer_visible)

    def test_exhausting_contacts_does_not_exhaust_attempts(self):
        # And the converse, which is the same rule read the other way round.
        quota = int(self.policy.limits["max_customer_contacts_per_transaction"])
        d = self.decide(
            txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=1),
            TransactionState(contacts_used=quota),
        )
        self.assertEqual(d.consumes, "attempt")
        self.assertIsNone(d.retry_foreclosed_by)

    def test_below_the_cap_does_not_stop_for_that_reason(self):
        d = self.decide(txn(failure_code="NETWORK_TIMEOUT", attempt_number=1))
        self.assertNotEqual(d.audit_decision, "abandoned")

    def test_override_can_only_restrict_never_raise(self):
        # A per-code override must never authorise more attempts than the
        # global ceiling. This is the invariant that keeps limits.max_attempts
        # meaningful as a ceiling rather than a suggestion.
        ceiling = self.policy.limits["max_attempts"]
        codes = list(self.policy.retry_windows.keys())
        for code in codes:
            for is_sub in (False, True):
                with self.subTest(code=code, subscription=is_sub):
                    eff, _ = self.policy.effective_max_attempts(code, is_sub)
                    self.assertLessEqual(eff, ceiling)

    def test_do_not_honor_is_capped_tighter_than_the_global_limit(self):
        eff, rule = self.policy.effective_max_attempts("DO_NOT_HONOR", False)
        self.assertLess(eff, self.policy.limits["max_attempts"])
        self.assertIn("DO_NOT_HONOR", rule)

    def test_subscriptions_are_capped_at_or_below_one_off_payments(self):
        # RBI e-mandate norms are stricter than card retry norms, so a
        # subscription may never get MORE attempts than a one-off.
        for code in self.policy.retry_windows:
            with self.subTest(code=code):
                one_off, _ = self.policy.effective_max_attempts(code, False)
                sub, _ = self.policy.effective_max_attempts(code, True)
                self.assertLessEqual(sub, one_off)

    def test_cap_is_never_exceeded_anywhere_in_the_real_batch(self):
        # Sweep every transaction at every attempt number and assert no path
        # schedules a retry past its cap.
        _, _, txns = load_batch()
        signals = detect_issuer_degradation(txns)
        for t in txns[:120]:
            eff, _ = self.policy.effective_max_attempts(
                t["failure_code"], t["is_subscription"]
            )
            for n in range(1, eff + 4):
                probe = dict(t, attempt_number=n)
                d = self.decide(probe, signals=signals,
                                now=later(720))
                if n >= eff:
                    self.assertNotEqual(
                        d.consumes, "attempt",
                        "%s attempt %d/%d produced %s"
                        % (t["transaction_id"], n, eff, d.action),
                    )


# -- cooldown -------------------------------------------------------------

class TestCooldown(PolicyCase):
    """Cooldown is per code: a global default that a retry_window may shorten
    for itself with a stated reason. These tests read the applicable gap from
    the document rather than assuming one number, so they keep working when
    the document changes -- which is the whole point of the design."""

    def test_inside_cooldown_defers_rather_than_stops(self):
        # The distinction is load-bearing: STOP throws away a recoverable
        # payment, DEFER only postpones it.
        for code in ("INSUFFICIENT_FUNDS", "NETWORK_TIMEOUT", "DO_NOT_HONOR"):
            with self.subTest(code=code):
                cd, rule = self.policy.cooldown_for(code)
                inside = (datetime.fromisoformat(BASE_TS)
                          + cd * 0.5).isoformat()
                d = self.decide(
                    txn(failure_code=code),
                    TransactionState(last_attempt_at=BASE_TS),
                    now=inside,
                )
                self.assertEqual(d.action, DEFER, code)
                self.assertNotEqual(d.action, STOP, code)
                self.assertEqual(d.policy_rule_applied, rule, code)

    def test_defer_schedules_exactly_at_the_cooldown_boundary(self):
        for code in ("INSUFFICIENT_FUNDS", "NETWORK_TIMEOUT"):
            with self.subTest(code=code):
                cd, _ = self.policy.cooldown_for(code)
                inside = (datetime.fromisoformat(BASE_TS) + cd * 0.5).isoformat()
                d = self.decide(
                    txn(failure_code=code),
                    TransactionState(last_attempt_at=BASE_TS),
                    now=inside,
                )
                self.assertEqual(
                    datetime.fromisoformat(d.scheduled_time),
                    datetime.fromisoformat(BASE_TS) + cd,
                )

    def test_after_cooldown_the_rule_no_longer_blocks(self):
        for code in ("INSUFFICIENT_FUNDS", "NETWORK_TIMEOUT"):
            with self.subTest(code=code):
                cd, rule = self.policy.cooldown_for(code)
                after = (datetime.fromisoformat(BASE_TS)
                         + cd + timedelta(minutes=1)).isoformat()
                d = self.decide(
                    txn(failure_code=code),
                    TransactionState(last_attempt_at=BASE_TS),
                    now=after,
                )
                self.assertNotEqual(d.policy_rule_applied, rule)

    def test_a_scheduled_retry_never_lands_inside_the_cooldown(self):
        for code in ("NETWORK_TIMEOUT", "INSUFFICIENT_FUNDS", "DO_NOT_HONOR"):
            with self.subTest(code=code):
                cd, _ = self.policy.cooldown_for(code)
                state = TransactionState(last_attempt_at=BASE_TS)
                after = (datetime.fromisoformat(BASE_TS)
                         + cd + timedelta(minutes=1)).isoformat()
                d = self.decide(txn(failure_code=code), state, now=after)
                if d.scheduled_time:
                    self.assertGreaterEqual(
                        datetime.fromisoformat(d.scheduled_time),
                        datetime.fromisoformat(BASE_TS) + cd,
                    )

    def test_every_configured_delay_is_actually_reachable(self):
        # The bug this test exists to prevent: a global cooldown longer than a
        # per-code delay silently overrides that delay, so the schedule the
        # document describes is not the schedule that runs. NETWORK_TIMEOUT's
        # [2, 15, 90] fast backoff was dead under a 6h global cooldown until
        # cooldown_override_minutes was added -- 40 retries in the real batch
        # were being re-timed without anything saying so.
        for code, window in self.policy.retry_windows.items():
            delays = window.get("delays_minutes") or []
            if not delays:
                continue
            cd, rule = self.policy.cooldown_for(code)
            floor = cd.total_seconds() / 60.0
            self.assertLessEqual(
                floor, min(delays),
                "retry_windows.%s delays %s are unreachable: %s forces a "
                "%.0f min floor, so the configured schedule never runs. "
                "Either shorten the cooldown for this code with a stated "
                "reason, or lengthen the delays to match."
                % (code, delays, rule, floor),
            )

    def test_a_cooldown_override_must_state_its_reason(self):
        # Loosening a safety limit silently is how safety limits rot. If a
        # code shortens its cooldown, the document has to say why.
        for code, window in self.policy.retry_windows.items():
            if "cooldown_override_minutes" in window:
                with self.subTest(code=code):
                    self.assertTrue(
                        window.get("cooldown_override_rationale", "").strip(),
                        "retry_windows.%s shortens the cooldown without a "
                        "cooldown_override_rationale" % code,
                    )

    def test_an_override_may_shorten_the_gap_but_never_the_attempt_cap(self):
        # The gap is negotiable with a reason; the number of attempts is not.
        ceiling = self.policy.limits["max_attempts"]
        for code in self.policy.retry_windows:
            with self.subTest(code=code):
                eff, _ = self.policy.effective_max_attempts(code, False)
                self.assertLessEqual(eff, ceiling)


# -- issuer degradation ---------------------------------------------------

class TestIssuerDegradation(PolicyCase):

    def rested(self):
        """State with the cooldown long elapsed.

        Rule 4 (cooldown) is enforced before rule 5 (degradation), so without
        this every one of these tests would be measuring the cooldown instead
        of the thing it claims to measure.
        """
        return TransactionState(last_attempt_at=later(-24))

    def signal(self, end="2026-08-10T18:00:00+05:30"):
        return [{
            "signal": "ISSUER_DEGRADED", "issuer_bank": "HDFC",
            "window_start": "2026-08-10T13:00:00+05:30",
            "window_end": end,
            "observed_window_start": "2026-08-10T13:00:00+05:30",
            "observed_window_end": end,
            "confidence": 0.99, "z_mix": 5.0, "z_volume": 4.0,
            "evidence_transaction_ids": ["a", "b", "c"],
        }]

    def test_transaction_inside_a_degraded_window_holds(self):
        d = self.decide(
            txn(failure_code="ISSUER_DOWN", issuer_bank="HDFC"),
            self.rested(), signals=self.signal(), now=later(1),
        )
        self.assertEqual(d.action, HOLD)

    def test_hold_lasts_at_least_until_the_window_ends(self):
        end = "2026-08-10T18:00:00+05:30"
        d = self.decide(
            txn(failure_code="ISSUER_DOWN", issuer_bank="HDFC"),
            self.rested(), signals=self.signal(end), now=later(1),
        )
        self.assertGreaterEqual(
            datetime.fromisoformat(d.scheduled_time),
            datetime.fromisoformat(end),
        )

    def test_hold_is_capped_by_max_wait_hours(self):
        # An outage that never visibly ends must not park a transaction
        # forever; the policy's ceiling has to bite.
        window = self.policy.retry_windows["ISSUER_DOWN"]
        max_wait = window["max_wait_hours"]
        now = later(1)
        d = self.decide(
            txn(failure_code="ISSUER_DOWN", issuer_bank="HDFC"),
            self.rested(), signals=self.signal("2026-09-30T00:00:00+05:30"),
            now=now,
        )
        self.assertEqual(d.action, HOLD)
        self.assertLessEqual(
            datetime.fromisoformat(d.scheduled_time),
            datetime.fromisoformat(now) + timedelta(hours=max_wait),
        )

    def test_a_different_bank_in_the_same_window_is_unaffected(self):
        d = self.decide(
            txn(failure_code="NETWORK_TIMEOUT", issuer_bank="SBI"),
            self.rested(), signals=self.signal(), now=later(1),
        )
        self.assertNotEqual(d.action, HOLD)
        self.assertFalse(d.issuer_degraded)

    def test_hold_does_not_consume_an_attempt(self):
        # attempt_number must stay below this code's cap, or we would be
        # measuring the cap instead of the hold. ISSUER_DOWN allows only one
        # retry (delays_minutes has a single entry), so its cap is 2.
        eff, _ = self.policy.effective_max_attempts("ISSUER_DOWN", False)
        self.assertGreater(eff, 1)
        d = self.decide(
            txn(failure_code="ISSUER_DOWN", issuer_bank="HDFC",
                attempt_number=eff - 1),
            self.rested(), signals=self.signal(), now=later(1),
        )
        self.assertEqual(d.action, HOLD)
        # Suppressed, not abandoned: the attempt is preserved for after the
        # outage rather than spent on it.
        self.assertEqual(d.audit_decision, "retry_suppressed")
        self.assertNotEqual(d.audit_decision, "abandoned")


# -- enforcement ordering -------------------------------------------------

class TestEnforcementOrder(PolicyCase):
    """Each rule must outrank every rule below it, not merely fire."""

    def test_opt_out_beats_terminal_code(self):
        d = self.decide(
            txn(failure_code="CARD_EXPIRED"), TransactionState(opted_out=True)
        )
        self.assertIn("opt_out", d.policy_rule_applied)

    def test_terminal_code_beats_attempt_cap(self):
        # Both foreclose retries; only one is the reason, and the trail must
        # attribute it to the rule that fired first.
        cap = self.policy.limits["max_attempts"]
        d = self.decide(txn(failure_code="CARD_EXPIRED", attempt_number=cap + 2))
        self.assertTrue(d.retry_foreclosed_by.startswith("stop_immediately_on"))

    def test_attempt_cap_beats_cooldown(self):
        # Rule 3 forecloses before rule 4 can defer. With no attempt left to
        # protect, the cooldown is not merely outranked, it is skipped -- so
        # the decision must not be attributed to it.
        cap = self.policy.limits["max_attempts"]
        d = self.decide(
            txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=cap),
            TransactionState(last_attempt_at=BASE_TS),
            now=later(1),
        )
        self.assertNotEqual(d.consumes, "attempt")
        self.assertNotEqual(d.policy_rule_applied, "limits.cooldown_hours")
        self.assertNotIn("cooldown", (d.retry_foreclosed_by or ""))

    def test_cooldown_beats_issuer_degradation(self):
        signals = [{
            "signal": "ISSUER_DEGRADED", "issuer_bank": "HDFC",
            "window_start": "2026-08-10T13:00:00+05:30",
            "window_end": "2026-08-10T20:00:00+05:30",
            "observed_window_start": "2026-08-10T13:00:00+05:30",
            "observed_window_end": "2026-08-10T20:00:00+05:30",
            "confidence": 0.99, "z_mix": 5.0, "z_volume": 4.0,
            "evidence_transaction_ids": ["a", "b"],
        }]
        cd, rule = self.policy.cooldown_for("ISSUER_DOWN")
        inside = (datetime.fromisoformat(BASE_TS) + cd * 0.5).isoformat()
        d = self.decide(
            txn(failure_code="ISSUER_DOWN", issuer_bank="HDFC"),
            TransactionState(last_attempt_at=BASE_TS),
            signals=signals, now=inside,
        )
        self.assertEqual(d.action, DEFER)
        self.assertEqual(d.policy_rule_applied, rule)


# -- escalation ladder ----------------------------------------------------

class TestEscalationLadder(PolicyCase):

    def test_ladder_never_skips_an_eligible_rung(self):
        # The chosen rung must be the LOWEST eligible one at or above the
        # current step. Jumping to handoff while a cheaper rung was still
        # available would waste a customer relationship.
        #
        # This assertion used to be assertGreater, which encoded the wrong
        # rule -- that a rung must always ADVANCE -- and so let through a bug
        # where every transaction got exactly one retry instead of its full
        # allowance. A rung that still qualifies may fire again.
        for current in range(0, len(self.policy.escalation_ladder)):
            with self.subTest(current_step=current):
                state = TransactionState(
                    escalation_step=current,
                    alternate_instrument_available=True,
                    consent_on_file=True,
                )
                d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state,
                                now=later(24))
                if d.escalation_step is not None:
                    self.assertGreaterEqual(d.escalation_step, max(current, 1))
                    for skipped in d.rungs_passed_over:
                        self.assertTrue(
                            skipped["unmet"],
                            "rung %d was passed over with no unmet predicate"
                            % skipped["step"],
                        )

    def test_a_still_eligible_rung_may_fire_again(self):
        # The bug this test exists to prevent: rung 1 requires
        # attempts_remaining precisely so it can repeat until the allowance is
        # spent. Forcing the ladder to advance after every attempt made that
        # predicate dead, gave each transaction a single retry, and cost
        # Rs 28,923 of recoverable money on the standard batch.
        state = TransactionState(escalation_step=1,
                                 last_attempt_at=later(-24))
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS",
                            attempt_number=1), state, now=later(24))
        self.assertEqual(d.escalation_step, 1,
                         "rung 1 still has attempts remaining and must be "
                         "allowed to fire again rather than escalating")

    def test_the_ladder_advances_once_a_rung_stops_qualifying(self):
        # The other half: at the attempt cap, rung 1 no longer qualifies and
        # the ladder must move on rather than stalling.
        eff, _ = self.policy.effective_max_attempts("INSUFFICIENT_FUNDS", False)
        state = TransactionState(escalation_step=1, contacts_used=0,
                                 consent_on_file=True,
                                 last_attempt_at=later(-24))
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS",
                            attempt_number=eff - 1), state, now=later(24))
        if d.escalation_step is not None:
            self.assertGreaterEqual(d.escalation_step, 1)

    def test_a_rung_is_only_passed_over_with_a_stated_reason(self):
        state = TransactionState(escalation_step=0)
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state, now=later(24))
        for rung in d.rungs_passed_over:
            self.assertTrue(rung["unmet"])
            self.assertIsInstance(rung["unmet"][0], str)

    def test_ladder_never_moves_backwards(self):
        for current in range(0, len(self.policy.escalation_ladder) + 1):
            with self.subTest(current_step=current):
                state = TransactionState(escalation_step=current)
                d = self.decide(txn(failure_code="DO_NOT_HONOR"), state, now=later(24))
                if d.escalation_step is not None:
                    self.assertGreaterEqual(d.escalation_step, current)


# -- escalation reaches the customer --------------------------------------

class TestEscalationActuallyRuns(PolicyCase):
    """Regression tests for rules that were advertised but never fired.

    Three separate limits in policy.yaml had never once taken effect in a
    real run: rungs 3-5 of the ladder (unreachable behind a terminal STOP),
    rung 4 specifically (rung 3 repeated and ate the contact budget), and
    limits.max_attempts_per_customer_per_day (TransactionState carried the
    field and no caller ever populated it).

    All three failed the same way -- silently, looking like restraint. A
    policy document that promises a rule the run does not apply is worse than
    one that never promised it, so each is pinned here.
    """

    def test_a_hard_decline_reaches_the_instrument_update_rung(self):
        # The end-to-end path that matters: expired card, no retry possible,
        # customer asked for a new instrument. Walk the ladder by hand.
        seen = []
        state = TransactionState(consent_on_file=True)
        now = "2026-08-11T10:00:00+05:30"
        for _ in range(6):
            d = self.decide(txn(failure_code="CARD_EXPIRED"), state, now=now)
            if d.action == DEFER:
                now = d.scheduled_time
                continue
            seen.append(d.action)
            if d.terminal:
                break
            if d.consumes == "contact":
                state.contacts_used += 1
                state.last_contact_at = now
            if d.escalation_step:
                state.escalation_step = max(state.escalation_step,
                                            d.escalation_step)
        self.assertIn("notify_customer", seen)
        self.assertIn("request_instrument_update", seen)
        self.assertEqual(seen[-1], "hand_off_to_human")

    def test_rung_three_does_not_repeat_and_starve_rung_four(self):
        # The bug: with rung 3 repeatable, two identical notices spent the
        # whole contact budget and the instrument-update request never sent.
        state = TransactionState(escalation_step=3, contacts_used=1,
                                 consent_on_file=True,
                                 last_contact_at="2026-08-10T10:00:00+05:30")
        d = self.decide(txn(failure_code="CARD_EXPIRED"), state,
                        now="2026-08-11T11:00:00+05:30")
        self.assertEqual(d.action, "request_instrument_update")
        self.assertEqual(d.escalation_step, 4)

    def test_retry_rungs_still_repeat(self):
        # The converse. Rung 1 must keep repeating while attempts remain --
        # this is the ladder fix that a naive "never repeat" rule would undo.
        rung = next(r for r in self.policy.escalation_ladder if r["step"] == 1)
        self.assertTrue(rung.get("repeatable"),
                        "rung 1 must repeat or the attempt allowance is unusable")
        contact = next(r for r in self.policy.escalation_ladder
                       if r.get("consumes") == "contact")
        self.assertFalse(contact.get("repeatable"))

    def test_every_ladder_rung_declares_which_budget_it_spends(self):
        for rung in self.policy.escalation_ladder:
            self.assertIn(rung.get("consumes"), ("attempt", "contact", "none"),
                          "rung %s must declare consumes" % rung["step"])


# -- per-customer limits --------------------------------------------------

class TestPerCustomerLimits(PolicyCase):

    def test_customer_contact_cap_is_enforced_across_transactions(self):
        # A cap that only counts within one transaction is not a cap on what
        # a person receives.
        cap = int(self.policy.limits["max_contacts_per_customer_per_24h"])
        state = TransactionState(
            consent_on_file=True,
            contacts_recent_for_customer=cap,
            customer_contact_quota_resets_at="2026-08-12T10:00:00+05:30",
        )
        d = self.decide(txn(failure_code="CARD_EXPIRED"), state,
                        now="2026-08-11T11:00:00+05:30")
        self.assertEqual(d.action, DEFER)
        self.assertIn("limits.max_contacts_per_customer_per_24h", d.bounded_by)

    def test_customer_contact_cap_defers_rather_than_abandons(self):
        # It is a rolling window, so the right response is to wait for a slot,
        # not to drop the message.
        cap = int(self.policy.limits["max_contacts_per_customer_per_24h"])
        resets = "2026-08-12T10:00:00+05:30"
        state = TransactionState(
            consent_on_file=True,
            contacts_recent_for_customer=cap,
            customer_contact_quota_resets_at=resets,
        )
        d = self.decide(txn(failure_code="CARD_EXPIRED"), state,
                        now="2026-08-11T11:00:00+05:30")
        self.assertIsNotNone(d.scheduled_time)
        self.assertGreaterEqual(datetime.fromisoformat(d.scheduled_time),
                                datetime.fromisoformat(resets))

    def test_under_the_customer_cap_the_contact_proceeds(self):
        state = TransactionState(consent_on_file=True,
                                 contacts_recent_for_customer=0)
        d = self.decide(txn(failure_code="CARD_EXPIRED"), state,
                        now="2026-08-11T11:00:00+05:30")
        self.assertTrue(d.customer_visible)

    def test_the_orchestrator_actually_populates_per_customer_state(self):
        # The failure mode this guards is not a wrong value, it is a field
        # nobody ever writes to -- which is how max_attempts_per_customer_per_day
        # sat in policy.yaml for the whole project without ever firing.
        from src.run_batch import CustomerLedger
        led = CustomerLedger()
        t = {"customer_id": "cust_1", "transaction_id": "t1"}
        when = "2026-08-11T11:00:00+05:30"
        st = TransactionState()
        led.charge(t, when, "contact")
        led.charge(t, when, "attempt")
        led.load(st, t, when, cap=2)
        self.assertEqual(st.contacts_recent_for_customer, 1)
        self.assertEqual(st.attempts_today_for_customer, 1)

    def test_the_customer_window_is_rolling_not_calendar(self):
        # Measured by calendar day, two messages 2h apart across midnight both
        # count as "first of the day". The window has to be rolling or the cap
        # has a documented way around it.
        from src.run_batch import CustomerLedger
        led = CustomerLedger()
        t = {"customer_id": "cust_1", "transaction_id": "t1"}
        led.charge(t, "2026-08-11T23:00:00+05:30", "contact")
        led.charge(t, "2026-08-12T01:00:00+05:30", "contact")
        st = TransactionState()
        led.load(st, t, "2026-08-12T02:00:00+05:30", cap=2)
        self.assertEqual(st.contacts_recent_for_customer, 2,
                         "both messages are inside the rolling 24h window")
        self.assertIsNotNone(st.customer_contact_quota_resets_at)


# -- compliance -----------------------------------------------------------

class TestComplianceRails(PolicyCase):

    def test_no_customer_contact_outside_permitted_hours(self):
        # 03:00 IST. A customer-visible rung must defer, never fire.
        night = "2026-08-11T03:00:00+05:30"
        state = TransactionState(escalation_step=2, consent_on_file=True)
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state, now=night)
        if d.customer_visible:
            self.assertEqual(d.action, DEFER)
            # The contact-hours deferral is no longer a special case in the
            # ladder; it is one instance of the general "blocked only by a
            # clock" rule, so policy_rule_applied names the rung and the
            # predicate that held it. The compliance path is still cited --
            # asserted here, because a rail that stops appearing in the trail
            # is indistinguishable from a rail that stopped being enforced.
            self.assertIn("within_contact_hours", d.policy_rule_applied)
            self.assertIn("compliance.contact_hours_ist", d.bounded_by)

    def test_two_contacts_on_one_transaction_are_spaced_apart(self):
        # The contact budget allows a second message; the spacing rule says
        # not yet. That is a deferral, not a refusal -- and specifically not a
        # reason to fall through to handoff and strand rung 4.
        gap = float(self.policy.limits["min_hours_between_contacts"])
        just_sent = "2026-08-11T10:00:00+05:30"
        state = TransactionState(escalation_step=3, contacts_used=1,
                                 last_contact_at=just_sent, consent_on_file=True)
        d = self.decide(txn(failure_code="CARD_EXPIRED"), state,
                        now="2026-08-11T11:00:00+05:30")
        self.assertEqual(d.action, DEFER)
        self.assertIn("limits.min_hours_between_contacts", d.bounded_by)
        self.assertGreaterEqual(
            datetime.fromisoformat(d.scheduled_time),
            datetime.fromisoformat(just_sent) + timedelta(hours=gap),
        )

    def test_a_spacing_deferral_still_lands_inside_contact_hours(self):
        # 24h after a 22:00 send is 22:00, which is outside the window. The
        # two rules compose: the later of the two clocks wins, then the
        # contact window is applied on top.
        state = TransactionState(escalation_step=3, contacts_used=1,
                                 last_contact_at="2026-08-11T20:30:00+05:30",
                                 consent_on_file=True)
        d = self.decide(txn(failure_code="CARD_EXPIRED"), state,
                        now="2026-08-11T21:30:00+05:30")
        self.assertEqual(d.action, DEFER)
        self.assertTrue(self.policy.within_contact_hours(
            datetime.fromisoformat(d.scheduled_time)))

    def test_deferred_contact_is_rescheduled_into_permitted_hours(self):
        night = "2026-08-11T03:00:00+05:30"
        state = TransactionState(escalation_step=2, consent_on_file=True)
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state, now=night)
        if d.scheduled_time and d.customer_visible:
            self.assertTrue(
                self.policy.within_contact_hours(
                    datetime.fromisoformat(d.scheduled_time)
                )
            )

    def test_contact_hours_deferral_holds_its_place_on_the_ladder(self):
        # Being deferred for the hour must not cost the rung.
        night = "2026-08-11T03:00:00+05:30"
        state = TransactionState(escalation_step=2, consent_on_file=True)
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state, now=night)
        if d.customer_visible and d.action == DEFER:
            self.assertEqual(d.escalation_step, 3)

    def test_no_contact_without_consent(self):
        state = TransactionState(escalation_step=2, consent_on_file=False)
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state, now=later(24))
        self.assertFalse(d.customer_visible and d.action not in (DEFER, STOP))

    def test_contact_quota_is_respected(self):
        cap = self.policy.limits["max_customer_contacts_per_transaction"]
        state = TransactionState(escalation_step=2, contacts_used=cap,
                                 consent_on_file=True)
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state, now=later(24))
        self.assertNotEqual(d.audit_decision, "escalated")

    def test_subscription_retry_respects_pre_debit_notification(self):
        sub = self.policy.compliance["subscription_rules"]
        hrs = sub["pre_debit_notification_hours"]
        now = later(24)
        d = self.decide(
            txn(failure_code="INSUFFICIENT_FUNDS", is_subscription=True),
            now=now,
        )
        if d.scheduled_time and not d.customer_visible:
            self.assertGreaterEqual(
                datetime.fromisoformat(d.scheduled_time),
                datetime.fromisoformat(now) + timedelta(hours=hrs),
            )


# -- structural guards ----------------------------------------------------

class TestStructuralGuards(unittest.TestCase):
    """Guards against the design decaying quietly."""

    def setUp(self):
        self.policy = load_policy(POLICY_PATH)
        self.src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "policy.py",
        )

    def test_policy_module_hardcodes_no_failure_codes(self):
        # The rule that makes the policy auditable: every code lives in the
        # YAML. A literal here means the document is no longer the source of
        # truth, and the trail's rule paths stop being reproducible.
        with open(self.src, encoding="utf-8") as fh:
            body = fh.read()
        # Strip the docstrings and comments -- prose may name codes.
        body = re.sub(r'"""[\s\S]*?"""', "", body)
        body = re.sub(r"#.*", "", body)
        codes = set(self.policy.retry_windows) | set(self.policy.stop_immediately_on)
        found = [c for c in codes if re.search(r"\b%s\b" % re.escape(c), body)]
        self.assertEqual(
            found, [],
            "policy.py hardcodes failure codes %s; they belong in policy.yaml"
            % found,
        )

    def test_limits_come_from_the_document_not_the_code(self):
        # Stronger than grepping for the numbers: change the document and the
        # behaviour must follow. A grep can be fooled by a constant that
        # merely happens not to collide; this cannot. If max_attempts were
        # hardcoded, the mutated policy would still stop at the old value.
        import copy
        doc = copy.deepcopy(self.policy.doc)
        doc["limits"]["max_attempts"] = 9
        doc["retry_windows"]["INSUFFICIENT_FUNDS"]["delays_minutes"] = [60] * 9
        loosened = Policy(doc)
        eff, _ = loosened.effective_max_attempts("INSUFFICIENT_FUNDS", False)
        self.assertEqual(eff, 9)

        doc["limits"]["max_attempts"] = 1
        tightened = Policy(doc)
        eff, rule = tightened.effective_max_attempts("INSUFFICIENT_FUNDS", False)
        self.assertEqual(eff, 1)
        self.assertEqual(rule, "limits.max_attempts")

    def test_cooldown_comes_from_the_document_not_the_code(self):
        import copy
        doc = copy.deepcopy(self.policy.doc)
        doc["limits"]["cooldown_hours"] = 100
        p = Policy(doc)
        # A code with no cooldown_override_minutes, so the global default is
        # what governs it.
        self.assertNotIn(
            "cooldown_override_minutes", doc["retry_windows"]["INSUFFICIENT_FUNDS"]
        )
        d = p.decide(
            txn(failure_code="INSUFFICIENT_FUNDS"),
            diagnose(txn(failure_code="INSUFFICIENT_FUNDS")),
            TransactionState(last_attempt_at=BASE_TS),
            [], later(50),
        )
        # 50h elapsed against a 100h cooldown: still inside it.
        self.assertEqual(d.action, DEFER)
        self.assertEqual(d.policy_rule_applied, "limits.cooldown_hours")
        self.assertEqual(
            datetime.fromisoformat(d.scheduled_time),
            datetime.fromisoformat(BASE_TS) + timedelta(hours=100),
        )

    def test_terminal_codes_come_from_the_document_not_the_code(self):
        # Promote a soft code to terminal in the document only; the policy
        # must start refusing it with no code change.
        import copy
        doc = copy.deepcopy(self.policy.doc)
        doc["stop_immediately_on"] = list(doc["stop_immediately_on"]) + ["NETWORK_TIMEOUT"]
        p = Policy(doc)
        t = txn(failure_code="NETWORK_TIMEOUT")
        d = p.decide(t, diagnose(t), TransactionState(), [], later(48))
        self.assertNotEqual(d.consumes, "attempt")
        self.assertEqual(d.retry_foreclosed_by,
                         "stop_immediately_on[NETWORK_TIMEOUT]")

    def test_diagnosis_hard_set_matches_policy_terminal_codes(self):
        # Drift guard. If someone adds a code to stop_immediately_on but not
        # to the taxonomy, diagnose would call it SOFT while policy refuses
        # to retry it -- the two layers would disagree about the same
        # transaction, and the audit trail would read as incoherent.
        self.assertEqual(
            HARD_CODES, frozenset(self.policy.stop_immediately_on),
            "diagnose.HARD_CODES and policy.yaml stop_immediately_on disagree",
        )

    def test_soft_and_hard_partition_the_taxonomy(self):
        self.assertEqual(SOFT_CODES & HARD_CODES, frozenset())

    def test_invalid_cvv_is_hard_for_retry_but_soft_for_customer_action(self):
        d = diagnose(txn(failure_code="INVALID_CVV"))
        self.assertFalse(d["retryable"])
        self.assertTrue(d["customer_actionable"])

    def test_every_ladder_predicate_is_implemented(self):
        from src.policy import PREDICATES
        for rung in self.policy.escalation_ladder:
            for name in rung.get("requires") or []:
                self.assertIn(
                    name, PREDICATES,
                    "escalation_ladder step %d requires unimplemented "
                    "predicate %r; it would fail closed and the rung could "
                    "never fire" % (rung["step"], name),
                )

    def test_every_taxonomy_code_has_a_retry_window(self):
        from src.diagnose import FAILURE_TAXONOMY
        for code in FAILURE_TAXONOMY:
            self.assertIn(code, self.policy.retry_windows)


# -- the audit trail ------------------------------------------------------

class TestAuditCoverage(unittest.TestCase):

    def test_every_decision_writes_exactly_one_line(self):
        policy = load_policy(POLICY_PATH)
        _, _, txns = load_batch()
        sample = txns[:60]

        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with audit_mod.AuditLog(path, policy_id=policy.policy_id,
                                    policy_version=policy.version,
                                    dry_run=True) as log:
                diagnoses, signals = diagnose_batch(sample, audit=log)
                before = sum(log.counts.values())
                for t in sample:
                    decide_and_log(
                        policy, log, t, diagnoses[t["transaction_id"]],
                        TransactionState(), signals, later(720),
                    )
                after = sum(log.counts.values())
            self.assertEqual(after - before, len(sample))
            self.assertEqual(log.write_failures, 0)
        finally:
            os.unlink(path)

    def test_chain_verifies_after_a_full_decision_run(self):
        policy = load_policy(POLICY_PATH)
        _, _, txns = load_batch()

        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with audit_mod.AuditLog(path, policy_id=policy.policy_id,
                                    policy_version=policy.version,
                                    dry_run=True) as log:
                diagnoses, signals = diagnose_batch(txns, audit=log)
                for t in txns:
                    decide_and_log(
                        policy, log, t, diagnoses[t["transaction_id"]],
                        TransactionState(), signals, later(720),
                    )
            result = audit_mod.verify_chain(path)
            self.assertTrue(result["ok"], result["errors"][:3])
        finally:
            os.unlink(path)

    def test_refusals_are_logged_not_silently_dropped(self):
        # policy.yaml sets audit.log_skipped_decisions for a reason: a
        # decision NOT to act is the decision most likely to be questioned.
        policy = load_policy(POLICY_PATH)
        terminal = [txn(transaction_id="t%d" % i, failure_code=c)
                    for i, c in enumerate(policy.stop_immediately_on)]

        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with audit_mod.AuditLog(path, dry_run=True) as log:
                for t in terminal:
                    decide_and_log(policy, log, t, diagnose(t),
                                   TransactionState(), [], later(48))
            # The refusal is what must be in the trail, not any particular
            # decision verb. A hard decline now logs an escalation, so keying
            # on "no_action_terminal" would silently pass by matching nothing
            # -- which is how this test failed when the semantics changed, and
            # is exactly the failure mode it exists to catch. Key on the
            # refusal itself.
            entries = [e for e in audit_mod.read_entries(path)
                       if e.get("retry_foreclosed_by")]
            self.assertEqual(len(entries), len(terminal))
            for e in entries:
                self.assertTrue(e["reason"])
                self.assertTrue(e["policy_rule_applied"])
                self.assertTrue(e["retry_foreclosed_by"].startswith(
                    "stop_immediately_on"))
        finally:
            os.unlink(path)


# -- whole-batch invariants ----------------------------------------------

class TestBatchInvariants(unittest.TestCase):
    """Properties that must hold across all 240 transactions at once."""

    @classmethod
    def setUpClass(cls):
        cls.policy = load_policy(POLICY_PATH)
        _, _, cls.txns = load_batch()
        cls.diagnoses, cls.signals = diagnose_batch(cls.txns)
        cls.decisions = {
            t["transaction_id"]: cls.policy.decide(
                t, cls.diagnoses[t["transaction_id"]], TransactionState(),
                cls.signals, later(720),
            )
            for t in cls.txns
        }

    def test_no_terminal_code_is_ever_scheduled_for_retry(self):
        # Asserts the GUARANTEE -- no attempt is spent - rather than the shape
        # the refusal happens to take. These used to assert action == STOP,
        # which was a valid proxy only while a hard decline ended the
        # transaction. It now escalates to the customer instead, so STOP would
        # test the old design rather than the rule that matters.
        terminal = set(self.policy.stop_immediately_on)
        for t in self.txns:
            d = self.decisions[t["transaction_id"]]
            if t["failure_code"] in terminal:
                self.assertNotEqual(d.consumes, "attempt", t["transaction_id"])
                self.assertEqual(
                    d.retry_foreclosed_by,
                    "stop_immediately_on[%s]" % t["failure_code"],
                    t["transaction_id"],
                )

    def test_no_decision_exceeds_its_attempt_cap(self):
        for t in self.txns:
            d = self.decisions[t["transaction_id"]]
            eff, _ = self.policy.effective_max_attempts(
                t["failure_code"], t["is_subscription"]
            )
            if t["attempt_number"] >= eff:
                self.assertNotEqual(d.consumes, "attempt", t["transaction_id"])
                self.assertIsNotNone(d.retry_foreclosed_by, t["transaction_id"])

    def test_every_decision_cites_a_resolvable_rule_path(self):
        for t in self.txns:
            d = self.decisions[t["transaction_id"]]
            self.assertTrue(d.policy_rule_applied, t["transaction_id"])
            root = re.split(r"[.\[]", d.policy_rule_applied)[0]
            self.assertIn(
                root,
                set(self.policy.doc) | {"escalation_ladder", "compliance"},
                "%s cites unresolvable path %r"
                % (t["transaction_id"], d.policy_rule_applied),
            )

    def test_every_decision_has_a_human_readable_reason(self):
        for t in self.txns:
            d = self.decisions[t["transaction_id"]]
            self.assertGreater(len(d.reason), 20, t["transaction_id"])

    def test_no_scheduled_time_is_in_the_past(self):
        now = datetime.fromisoformat(later(720))
        for t in self.txns:
            d = self.decisions[t["transaction_id"]]
            if d.scheduled_time:
                self.assertGreaterEqual(
                    datetime.fromisoformat(d.scheduled_time), now,
                    t["transaction_id"],
                )

    def test_decisions_are_deterministic(self):
        again = {
            t["transaction_id"]: self.policy.decide(
                t, self.diagnoses[t["transaction_id"]], TransactionState(),
                self.signals, later(720),
            )
            for t in self.txns
        }
        for tid, d in self.decisions.items():
            self.assertEqual(d.action, again[tid].action, tid)
            self.assertEqual(d.policy_rule_applied,
                             again[tid].policy_rule_applied, tid)
            self.assertEqual(d.scheduled_time, again[tid].scheduled_time, tid)

    def test_no_ground_truth_reaches_the_decision_layer(self):
        for t in self.txns:
            self.assertNotIn("_ground_truth", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
