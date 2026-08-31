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

    def test_every_terminal_code_stops(self):
        for code in self.policy.stop_immediately_on:
            with self.subTest(code=code):
                d = self.decide(txn(failure_code=code))
                self.assertEqual(d.action, STOP, code + " must stop")
                self.assertIsNone(d.scheduled_time, code + " must not be scheduled")
                self.assertTrue(d.terminal)

    def test_terminal_code_cites_the_rule_that_stopped_it(self):
        for code in self.policy.stop_immediately_on:
            with self.subTest(code=code):
                d = self.decide(txn(failure_code=code))
                self.assertEqual(d.policy_rule_applied, "stop_immediately_on[%s]" % code)

    def test_terminal_code_stops_on_first_attempt_with_everything_favourable(self):
        # Attempt 1, cooldown long elapsed, consent on file, healthy issuer.
        # Nothing about the context should rescue a hard decline.
        d = self.decide(
            txn(failure_code="CARD_EXPIRED", attempt_number=1),
            TransactionState(consent_on_file=True,
                             alternate_instrument_available=True),
        )
        self.assertEqual(d.action, STOP)

    def test_terminal_code_stops_even_on_a_large_amount(self):
        # The forgone-money case. A blocked card that would clear on retry is
        # still not retried; size must not buy an exception.
        d = self.decide(txn(failure_code="CARD_BLOCKED", amount_paise=5_000_000))
        self.assertEqual(d.action, STOP)
        self.assertEqual(d.audit_decision, "no_action_terminal")

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
                self.assertEqual(d.action, STOP)
                self.assertEqual(
                    d.policy_rule_applied, "stop_immediately_on[%s]" % code
                )


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

    def test_at_the_cap_stops(self):
        cap = self.policy.limits["max_attempts"]
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=cap))
        self.assertEqual(d.action, STOP)
        self.assertEqual(d.audit_decision, "abandoned")

    def test_above_the_cap_stops(self):
        cap = self.policy.limits["max_attempts"]
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=cap + 5))
        self.assertEqual(d.action, STOP)

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
                    self.assertIn(
                        d.action, (STOP, DEFER, HOLD),
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
        cap = self.policy.limits["max_attempts"]
        d = self.decide(txn(failure_code="CARD_EXPIRED", attempt_number=cap + 2))
        self.assertTrue(d.policy_rule_applied.startswith("stop_immediately_on"))

    def test_attempt_cap_beats_cooldown(self):
        cap = self.policy.limits["max_attempts"]
        d = self.decide(
            txn(failure_code="INSUFFICIENT_FUNDS", attempt_number=cap),
            TransactionState(last_attempt_at=BASE_TS),
            now=later(1),
        )
        self.assertEqual(d.action, STOP)
        self.assertNotEqual(d.policy_rule_applied, "limits.cooldown_hours")

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
        # The chosen rung must be the LOWEST eligible one above the current
        # step. Jumping to handoff while a cheaper rung was available would
        # waste a customer relationship.
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
                    self.assertGreater(d.escalation_step, current)
                    for skipped in d.rungs_passed_over:
                        self.assertTrue(
                            skipped["unmet"],
                            "rung %d was passed over with no unmet predicate"
                            % skipped["step"],
                        )

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


# -- compliance -----------------------------------------------------------

class TestComplianceRails(PolicyCase):

    def test_no_customer_contact_outside_permitted_hours(self):
        # 03:00 IST. A customer-visible rung must defer, never fire.
        night = "2026-08-11T03:00:00+05:30"
        state = TransactionState(escalation_step=2, consent_on_file=True)
        d = self.decide(txn(failure_code="INSUFFICIENT_FUNDS"), state, now=night)
        if d.customer_visible:
            self.assertEqual(d.action, DEFER)
            self.assertEqual(d.policy_rule_applied, "compliance.contact_hours_ist")

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
        self.assertEqual(d.action, STOP)
        self.assertEqual(d.policy_rule_applied, "stop_immediately_on[NETWORK_TIMEOUT]")

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
            entries = [e for e in audit_mod.read_entries(path)
                       if e.get("decision") == "no_action_terminal"]
            self.assertEqual(len(entries), len(terminal))
            for e in entries:
                self.assertTrue(e["reason"])
                self.assertTrue(e["policy_rule_applied"])
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
        terminal = set(self.policy.stop_immediately_on)
        for t in self.txns:
            d = self.decisions[t["transaction_id"]]
            if t["failure_code"] in terminal:
                self.assertEqual(d.action, STOP, t["transaction_id"])
                self.assertIsNone(d.scheduled_time, t["transaction_id"])

    def test_no_decision_exceeds_its_attempt_cap(self):
        for t in self.txns:
            d = self.decisions[t["transaction_id"]]
            eff, _ = self.policy.effective_max_attempts(
                t["failure_code"], t["is_subscription"]
            )
            if t["attempt_number"] >= eff:
                self.assertIn(d.action, (STOP, DEFER, HOLD), t["transaction_id"])

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
