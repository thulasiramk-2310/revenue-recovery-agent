"""Guards for the execution layer and the replay.

Two things are being protected here.

The first is that the executor cannot quietly do something dangerous: run
against a live key, leak the API secret into the trail, claim a payment it did
not make, or actually send a customer message while the send is stubbed.

The second is that the audit trail stays sufficient. `replay.py` rebuilds a
run from the log alone; if a field stops being logged, the replay stops being
able to answer and these tests fail. That is the point -- the trail is only an
audit trail for as long as it can reproduce the run.

None of these tests touch the network.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import audit as audit_mod
from src.execute import (
    Executor, LiveKeyRefused, RateLimiter, SecretLeak, mask,
)
from src.policy import Decision
from src.replay import rebuild, story

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "results", "run.log")
SUMMARY = os.path.join(ROOT, "results", "run_summary.json")

TXN = {
    "transaction_id": "pay_TEST01", "amount_paise": 149900,
    "timestamp": "2026-08-10T14:00:00+05:30", "issuer_bank": "HDFC",
    "failure_code": "NETWORK_TIMEOUT", "customer_id": "cust_1",
    "attempt_number": 1, "is_subscription": False,
}


def decision(action="silent_retry", **kw):
    base = dict(
        transaction_id="pay_TEST01", action=action, reason="test",
        policy_rule_applied="retry_windows.NETWORK_TIMEOUT.delays_minutes[0]",
        audit_decision="retry_scheduled",
        scheduled_time="2026-08-10T14:02:00+05:30",
    )
    base.update(kw)
    return Decision(**base)


class TestSafetyRails(unittest.TestCase):

    def test_a_live_key_is_a_hard_abort(self):
        os.environ["RAZORPAY_KEY_ID"] = "rzp_live_ABCDEFGHIJKL"
        os.environ["RAZORPAY_KEY_SECRET"] = "whatever"
        try:
            with self.assertRaises(LiveKeyRefused):
                Executor(live=True)
        finally:
            os.environ.pop("RAZORPAY_KEY_ID", None)
            os.environ.pop("RAZORPAY_KEY_SECRET", None)

    def test_an_unrecognised_key_prefix_is_refused_rather_than_guessed(self):
        os.environ["RAZORPAY_KEY_ID"] = "acct_something_else"
        os.environ["RAZORPAY_KEY_SECRET"] = "whatever"
        try:
            with self.assertRaises(LiveKeyRefused):
                Executor(live=True)
        finally:
            os.environ.pop("RAZORPAY_KEY_ID", None)
            os.environ.pop("RAZORPAY_KEY_SECRET", None)

    def test_dry_run_is_the_default(self):
        ex = Executor()
        self.assertTrue(ex.dry_run)
        self.assertFalse(ex.live)

    def test_dry_run_makes_no_gateway_calls(self):
        ex = Executor(ground_truth={})
        ex.attempt(TXN, decision())
        self.assertEqual(ex.stats["gateway_calls"], 0)

    def test_the_secret_never_reaches_a_log_payload(self):
        ex = Executor()
        ex._secret = "s3cr3t-value-not-for-logs"
        with self.assertRaises(SecretLeak):
            ex._log("test_event", note="leaking " + ex._secret)

    def test_masking_keeps_enough_to_identify_and_not_enough_to_use(self):
        m = mask("rzp_test_ABCDEFGHIJKL")
        self.assertIn("rzp_test_", m)
        self.assertNotIn("ABCDEFGHIJKL", m)
        self.assertEqual(mask(""), "<unset>")


class TestOutcomeHonesty(unittest.TestCase):

    def gt(self, **kw):
        base = {
            "is_recoverable": True,
            "would_recover_if_retried_at": "2026-08-11T00:00:00+05:30",
            "recovery_window_closes_at": "2026-08-15T00:00:00+05:30",
            "recovery_reason": "test window",
        }
        base.update(kw)
        return {"pay_TEST01": base}

    def test_a_retry_before_the_window_opens_does_not_recover(self):
        ex = Executor(ground_truth=self.gt())
        ok, why, _ = ex._resolve_outcome(TXN, "2026-08-10T14:00:00+05:30")
        self.assertFalse(ok)
        self.assertIn("before", why)

    def test_a_retry_after_the_window_closes_does_not_recover(self):
        ex = Executor(ground_truth=self.gt())
        ok, why, _ = ex._resolve_outcome(TXN, "2026-08-20T00:00:00+05:30")
        self.assertFalse(ok)
        self.assertIn("after", why)

    def test_a_retry_inside_the_window_recovers(self):
        ex = Executor(ground_truth=self.gt())
        ok, _, _ = ex._resolve_outcome(TXN, "2026-08-12T00:00:00+05:30")
        self.assertTrue(ok)

    def test_an_unrecoverable_payment_never_recovers_however_well_timed(self):
        ex = Executor(ground_truth=self.gt(is_recoverable=False))
        ok, _, _ = ex._resolve_outcome(TXN, "2026-08-12T00:00:00+05:30")
        self.assertFalse(ok)

    def test_dry_run_outcomes_are_labelled_as_simulated(self):
        # The label is what stops a reader mistaking a simulated recovery for
        # a real capture. It must never say "gateway" when nothing was called.
        ex = Executor(ground_truth=self.gt())
        r = ex.attempt(TXN, decision())
        self.assertEqual(r["outcome_source"], "ground_truth_simulation")
        self.assertEqual(r["gateway_call"], "none")


class TestContactIsStubbed(unittest.TestCase):

    def test_contact_is_prepared_but_never_delivered(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with audit_mod.AuditLog(path, dry_run=True) as log:
                ex = Executor(audit=log)
                r = ex.attempt(TXN, decision(action="notify_customer",
                                             channel="email",
                                             customer_visible=True))
            self.assertEqual(r["status"], "contact_stubbed")
            self.assertFalse(r["delivered"])
            entries = [e for e in audit_mod.read_entries(path)
                       if e.get("event") == "contact_stubbed"]
            self.assertEqual(len(entries), 1)
            e = entries[0]
            self.assertTrue(e["stubbed"])
            self.assertFalse(e["delivered"])
            # The payload has to be complete, or "we logged what we'd send"
            # is not a meaningful claim.
            for key in ("channel", "template", "subject", "body", "to"):
                self.assertIn(key, e["intended_payload"])
        finally:
            os.unlink(path)


class TestRateLimiter(unittest.TestCase):

    def test_backoff_grows_with_each_attempt(self):
        rl = RateLimiter(base_backoff=1.0, jitter=0.0, max_backoff=100)
        delays = [rl.backoff_for(i) for i in range(4)]
        self.assertEqual(delays, sorted(delays))
        self.assertGreater(delays[-1], delays[0])

    def test_backoff_is_capped(self):
        rl = RateLimiter(base_backoff=1.0, jitter=0.0, max_backoff=5.0)
        self.assertLessEqual(rl.backoff_for(20), 5.0)

    def test_retry_after_header_is_honoured_over_our_own_schedule(self):
        rl = RateLimiter(base_backoff=1.0, jitter=0.0, max_backoff=100)
        self.assertEqual(rl.backoff_for(0, retry_after="7"), 7.0)

    def test_a_junk_retry_after_falls_back_rather_than_crashing(self):
        rl = RateLimiter(base_backoff=1.0, jitter=0.0)
        self.assertGreater(rl.backoff_for(0, retry_after="soon"), 0)


@unittest.skipUnless(os.path.exists(LOG), "run `python -m src.run_batch` first")
class TestReplayReconstructsTheRun(unittest.TestCase):
    """The trail's actual claim, tested rather than asserted."""

    @classmethod
    def setUpClass(cls):
        cls.rec = rebuild(LOG)

    def test_the_chain_verifies(self):
        self.assertTrue(self.rec["chain"]["ok"], self.rec["chain"]["errors"][:3])

    def test_the_replay_finds_no_gaps(self):
        self.assertEqual(self.rec["gaps"], [])

    def test_the_rebuilt_summary_matches_what_the_run_claimed(self):
        with open(SUMMARY, encoding="utf-8") as fh:
            claimed = json.load(fh)
        rebuilt = self.rec["rebuilt_summary"]
        for key in ("batch_size", "total_value_paise", "recovered_count",
                    "recovered_paise", "actions", "final_status",
                    "contacts_stubbed", "detections"):
            self.assertEqual(rebuilt[key], claimed[key],
                             "%s does not survive the round trip" % key)

    def test_every_decision_can_be_explained_from_the_log_alone(self):
        for tid, d in self.rec["decisions"].items():
            self.assertTrue(d.get("reason"), tid)
            self.assertTrue(d.get("policy_rule_applied"), tid)

    def test_any_transaction_story_is_reconstructable(self):
        tid = next(iter(self.rec["decisions"]))
        lines = story(self.rec, tid)
        self.assertTrue(lines)
        self.assertTrue(any(l.startswith("DIAGNOSED") for l in lines))
        self.assertTrue(any(l.startswith("DECIDED") for l in lines))

    def test_a_diagnosis_is_always_logged_before_the_decision_it_informs(self):
        for tid in self.rec["decisions"]:
            self.assertIn(tid, self.rec["diagnoses"],
                          "%s was decided with no diagnosis in the trail" % tid)

    def test_no_execution_is_ambiguous_about_where_its_outcome_came_from(self):
        for tid, entries in self.rec["executions"].items():
            for e in entries:
                self.assertIn(e.get("outcome_source"),
                              ("gateway", "ground_truth_simulation", "stubbed",
                               "policy"), tid)

    def test_tampering_with_the_log_is_detected(self):
        # If an edited log still verified, the trail would be decoration.
        with open(LOG, encoding="utf-8") as fh:
            lines = fh.readlines()
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            mid = len(lines) // 2
            e = json.loads(lines[mid])
            if "reason" in e:
                e["reason"] = "quietly rewritten after the fact"
            lines[mid] = json.dumps(e) + "\n"
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            self.assertFalse(audit_mod.verify_chain(path)["ok"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
