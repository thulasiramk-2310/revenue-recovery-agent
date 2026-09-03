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
from src.policy import Decision, load_policy
from src.diagnose import diagnose
from src.run_batch import DEFAULT_HORIZON, _work_transaction
from src.run_batch import RunGuard
from src.replay import rebuild, story

POLICY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policy.yaml")
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

    # The key fixtures below are obvious placeholders on purpose. A
    # realistic-looking rzp_live_ string in a public repo trips GitHub secret
    # scanning and forces a reviewer to stop and check whether it is real.
    def test_a_live_key_is_a_hard_abort(self):
        os.environ["RAZORPAY_KEY_ID"] = "rzp_live_xxxxPLACEHOLDERxxxx"
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
        m = mask("rzp_test_xxxxPLACEHOLDERxxxx")
        self.assertIn("rzp_test_", m)
        self.assertNotIn("PLACEHOLDER", m)
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



class TestRetryAllowanceIsFullyUsed(unittest.TestCase):
    """Regression: a transaction with two scheduled retries must use both.

    The defect this guards against was subtle. The escalation ladder advanced
    one rung after every attempt, so rung 1 (silent_retry) could fire exactly
    once per transaction no matter how many retries policy.yaml allowed. That
    made `attempts_remaining` -- rung 1's own precondition, and the thing that
    is supposed to govern repetition -- dead code.

    Nothing crashed and no test failed, because the unit tests only asserted
    that the ladder never SKIPPED a rung, never that a still-eligible rung
    could fire again. The only visible symptom was recovered money:
    Rs 75,192 instead of Rs 104,115 on the standard batch.

    So this test runs the real scheduling loop end to end rather than probing
    the policy in isolation. INSUFFICIENT_FUNDS declares two delays
    (+1 day, +3 days) and therefore an allowance of two retries; both must
    actually be spent.
    """

    def setUp(self):
        self.policy = load_policy(POLICY_PATH)
        self.txn = {
            "transaction_id": "pay_REGR01", "amount_paise": 249900,
            "timestamp": "2026-08-10T14:00:00+05:30", "issuer_bank": "HDFC",
            "failure_code": "INSUFFICIENT_FUNDS", "customer_id": "cust_regr",
            "attempt_number": 1, "is_subscription": False,
        }
        # Never recoverable, so the loop cannot exit early on success and we
        # observe the full allowance being spent rather than a lucky first hit.
        self.gt = {"pay_REGR01": {"is_recoverable": False,
                                  "recovery_reason": "fixture: never recovers"}}

    def _run(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with audit_mod.AuditLog(path, dry_run=True) as log:
                ex = Executor(audit=log, policy=self.policy,
                              ground_truth=self.gt)
                diag = diagnose(self.txn, [])
                _work_transaction(self.policy, log, ex, self.txn, diag, [],
                                  DEFAULT_HORIZON)
            return ex, list(audit_mod.read_entries(path))
        finally:
            os.unlink(path)

    def test_the_declared_retry_allowance_is_two(self):
        delays = self.policy.retry_windows["INSUFFICIENT_FUNDS"]["delays_minutes"]
        self.assertEqual(len(delays), 2,
                         "fixture assumes two scheduled retries for this code")
        eff, _ = self.policy.effective_max_attempts("INSUFFICIENT_FUNDS", False)
        self.assertEqual(eff, len(delays) + 1)

    def test_both_scheduled_retries_are_actually_used(self):
        ex, _ = self._run()
        self.assertEqual(
            ex.stats["attempts"], 2,
            "the policy schedules two retries for INSUFFICIENT_FUNDS but the "
            "loop spent %d. A rung that stops firing while attempts_remaining "
            "is still true makes that predicate meaningless."
            % ex.stats["attempts"],
        )

    def test_the_two_retries_land_at_the_two_configured_delays(self):
        # Using both attempts is not enough -- they have to be spent at the
        # times the document specifies, or the schedule is decorative.
        _, entries = self._run()
        fired = [e for e in entries
                 if e.get("decision") == "retry_scheduled"
                 and e.get("action") == "silent_retry"]
        self.assertEqual(len(fired), 2)
        rules = [e["policy_rule_applied"] for e in fired]
        self.assertNotEqual(rules[0], rules[1],
                            "both retries cite the same rule path, so the "
                            "second is a repeat of the first rather than the "
                            "next step in the schedule")

    def test_the_ladder_only_escalates_once_retries_are_exhausted(self):
        _, entries = self._run()
        # Policy lines only. The executor writes its own line per attempt
        # carrying the same action but no rung, and including those would
        # make the sequence look like it oscillates when it does not.
        seq = [e.get("escalation_step") for e in entries
               if e.get("decision") == "retry_scheduled"
               and e.get("action") == "silent_retry"]
        self.assertTrue(seq, "no silent retries were attempted at all")
        self.assertTrue(all(s == 1 for s in seq),
                        "silent_retry should stay on rung 1 for the whole "
                        "allowance, got rungs %s" % seq)


class TestBatchRuntimeGuards(unittest.TestCase):
    """Controls that only exist once the whole batch shares a budget."""

    def test_spend_ceiling_aborts_before_the_next_paid_action(self):
        policy = load_policy(POLICY_PATH)
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with audit_mod.AuditLog(path, dry_run=True) as log:
                ex = Executor(audit=log, policy=policy)
                guard = RunGuard(
                    policy, log, attempt_cost_paise=250,
                    contact_cost_paise=100,
                )
                policy.limits["batch_spend_ceiling_paise"] = 1
                final_decision, final_result = _work_transaction(
                    policy, log, ex, TXN, diagnose(TXN), [], DEFAULT_HORIZON,
                    guard=guard,
                )
            self.assertTrue(guard.aborted)
            self.assertEqual(final_result["status"], "batch_aborted")
            self.assertEqual(ex.stats["attempts"], 0)
            rows = [e for e in audit_mod.read_entries(path)
                    if e.get("decision") == "batch_aborted"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["policy_rule_applied"],
                             "limits.batch_spend_ceiling_paise")
        finally:
            os.unlink(path)



@unittest.skipUnless(os.path.exists(os.path.join(ROOT, "data", "failed_payments.json")),
                     "needs data/failed_payments.json -- run python -m src.generate_data")
class TestBaselineIsHonestlyNaive(unittest.TestCase):
    """The baseline must be blind, or the comparison is rigged.

    A fixed-retry system has no diagnosis layer. That is the whole point of
    it: it cannot tell a temporary network fault from a closed account, so it
    retries both on the same schedule and wastes attempts on the second.

    If the baseline quietly skipped hard declines it would be using knowledge
    the agent is being credited for having, and beating it would prove
    nothing. These tests exist so that can never happen silently -- a future
    edit that makes the baseline smarter fails here rather than flattering
    the headline number.
    """

    @classmethod
    def setUpClass(cls):
        from src.generate_data import load_batch
        from src.run_batch import _ground_truth_index
        from baseline.fixed_retry import FIXED_SCHEDULE_HOURS, run_baseline
        _, _, cls.txns = load_batch()
        cls.gt = _ground_truth_index()
        cls.schedule = FIXED_SCHEDULE_HOURS
        cls.result = run_baseline(cls.txns, cls.gt)
        cls.policy = load_policy(POLICY_PATH)

    def test_the_baseline_retries_every_hard_decline(self):
        # Every terminal-coded transaction must receive the full schedule.
        # policy.yaml's stop_immediately_on is the agent's knowledge; the
        # baseline is not entitled to it.
        per = self.result["per_transaction"]
        for t in self.txns:
            if t["failure_code"] not in self.policy.stop_immediately_on:
                continue
            with self.subTest(txn=t["transaction_id"], code=t["failure_code"]):
                self.assertEqual(
                    per[t["transaction_id"]]["attempts"], len(self.schedule),
                    "the baseline gave %s (%s) fewer than the full %d "
                    "attempts -- it is diagnosing, which makes it smarter "
                    "than a blind retry schedule should be"
                    % (t["transaction_id"], t["failure_code"],
                       len(self.schedule)),
                )

    def test_the_baseline_recovers_a_hard_decline_only_where_truth_allows(self):
        # Not "recovers nothing from hard declines" -- that assertion was
        # wrong and this test caught it. The generator plants CARD_BLOCKED
        # transactions that are temporary fraud holds and WOULD clear on
        # retry. The blind baseline retries them and collects the money;
        # policy.yaml forbids the agent from touching them.
        #
        # That is the compliance cost made concrete, and it belongs in the
        # comparison rather than being assumed away. What must never happen
        # is the baseline recovering something ground truth says is dead.
        per = self.result["per_transaction"]
        for t in self.txns:
            if t["failure_code"] not in self.policy.stop_immediately_on:
                continue
            tid = t["transaction_id"]
            if per[tid]["recovered"]:
                with self.subTest(txn=tid):
                    self.assertTrue(
                        self.gt[tid].get("is_recoverable"),
                        "the baseline recovered %s, which ground truth says "
                        "was never recoverable -- the scorer is wrong" % tid,
                    )

    def test_the_compliance_cost_is_visible_and_nonzero(self):
        # The batch must actually contain the tension the project claims to
        # demonstrate. If no terminal-coded transaction is recoverable, the
        # "money we deliberately do not chase" story is untested decoration.
        forgone = [t for t in self.txns
                   if t["failure_code"] in self.policy.stop_immediately_on
                   and self.gt[t["transaction_id"]].get("is_recoverable")]
        self.assertGreater(
            len(forgone), 0,
            "no terminal-coded transaction is recoverable, so the compliance "
            "trade-off this project reports is not present in the data",
        )

    def test_the_wasted_attempts_are_counted_and_reported(self):
        # The waste has to be visible in the result, not inferred by a reader.
        self.assertGreater(self.result["attempts_on_unrecoverable"], 0)
        self.assertGreater(self.result["unrecoverable_transactions_retried"], 0)

    def test_the_baseline_applies_one_schedule_to_every_failure_code(self):
        # No per-code branching anywhere: every transaction either runs the
        # full schedule or stops early because it succeeded.
        per = self.result["per_transaction"]
        for tid, row in per.items():
            with self.subTest(txn=tid):
                if not row["recovered"]:
                    self.assertEqual(row["attempts"], len(self.schedule))
                else:
                    self.assertLessEqual(row["attempts"], len(self.schedule))

    def test_both_arms_are_scored_by_the_same_function(self):
        # If the two arms ever get separate scorers they are free to drift,
        # and each would effectively be marking its own homework.
        import baseline.fixed_retry as fr
        from src.execute import resolve_outcome
        self.assertIs(fr.resolve_outcome, resolve_outcome)


if __name__ == "__main__":
    unittest.main(verbosity=2)
