"""The LLM may propose a diagnosis. It may never widen a bound.

Every test here is about containment rather than classification quality. The
model's accuracy is the vendor's problem; what this project has to guarantee
is that a model which is wrong, slow, offline, or actively malformed can
never cause a retry the policy would otherwise have refused.

The five failure modes each get their own test, because a failure path nobody
tested is a failure path that fails open:

    unmapped proposal / malformed output / API error / deadline / no key

All five must land on the same conservative default, and that default has to
be the one policy.yaml already defined for its own reasons.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import audit as audit_mod
from src.diagnose import (
    FAILURE_TAXONOMY, UNKNOWN_DIAGNOSIS, diagnose, diagnose_batch,
)
from src.llm_diagnose import (
    ACCEPTED, DISABLED, FAILED_NO_CREDENTIALS, FAILED_TIMEOUT,
    FAILED_TRANSPORT, NON_ACCEPTING, REJECTED_LOW_CONFIDENCE,
    REJECTED_MALFORMED, REJECTED_UNKNOWN_CODE, Proposal, _call_api,
    propose, resolve_unmapped,
)
from src.policy import load_policy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_PATH = os.path.join(ROOT, "policy.yaml")

CONFIG = {
    "enabled": True,
    "provider": "groq",
    "model": "llama-3.3-70b-versatile",
    "prompt_version": "diag-v1",
    "min_confidence": 0.70,
    "timeout_seconds": 8,
    "max_output_tokens": 300,
}

CODES = set(FAILURE_TAXONOMY)
MYSTERY = "ISSUER_UNAVAILABLE_TRY_LATER"


def transport(text=None, error=None):
    """A stand-in for the HTTP call, so failure paths run for real."""
    def _t(prompt, key, model, timeout, max_tokens):
        return text, error
    return _t


def provider_transport(text=None, error=None, seen=None):
    def _t(prompt, key, model, timeout, max_tokens, provider):
        if seen is not None:
            seen.append(provider)
        return text, error
    return _t


def reply(code, confidence=0.95, reasoning="looks like an issuer outage"):
    return ('{"code": "%s", "confidence": %s, "reasoning": "%s"}'
            % (code, confidence, reasoning))


def txn(code=MYSTERY, tid="pay_x1"):
    return {
        "transaction_id": tid, "customer_id": "cust_1", "failure_code": code,
        "amount_paise": 50000, "timestamp": "2026-08-11T10:00:00+05:30",
        "is_subscription": False, "issuer_bank": "HDFC", "attempt_number": 1,
        "gateway_message": "Issuer unavailable, please try later",
    }


# -- the five failure modes ------------------------------------------------

class TestFailsClosed(unittest.TestCase):
    """Each of these must produce a non-accepting verdict. No exceptions."""

    def test_1_a_proposal_outside_the_taxonomy_is_rejected(self):
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=transport(reply("BANK_IS_SAD")))
        self.assertEqual(p.verdict, REJECTED_UNKNOWN_CODE)
        self.assertFalse(p.accepted)
        self.assertIsNone(p.code)

    def test_2_malformed_output_is_rejected(self):
        for body in ("not json at all",
                     "{ this is not valid json",
                     '{"confidence": 0.9}',            # no code field
                     '["a", "list", "not", "object"]',
                     "",
                     None):
            with self.subTest(body=repr(body)[:30]):
                p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                            _transport=transport(body))
                self.assertEqual(p.verdict, REJECTED_MALFORMED)
                self.assertFalse(p.accepted)

    def test_3_an_api_error_is_not_an_answer(self):
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=transport(error="http_500 upstream exploded"))
        self.assertEqual(p.verdict, FAILED_TRANSPORT)
        self.assertFalse(p.accepted)

    def test_4_a_deadline_overrun_is_not_permission(self):
        # The one that matters most. A slow model must never be indistinguish-
        # able from a model that approved something.
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=transport(error="timeout"))
        self.assertEqual(p.verdict, FAILED_TIMEOUT)
        self.assertFalse(p.accepted)
        self.assertIsNone(p.code)

    def test_5_missing_credentials_degrade_quietly(self):
        # No key is an ordinary state, not a crash: the whole system ran
        # without one before this module existed.
        p = propose(MYSTERY, CODES, CONFIG, api_key="",
                    env_path="/nonexistent/.env")
        self.assertEqual(p.verdict, FAILED_NO_CREDENTIALS)
        self.assertFalse(p.accepted)

    def test_every_non_accepting_verdict_yields_no_code(self):
        # Belt and braces across the whole verdict vocabulary: nothing that
        # is not ACCEPTED may carry a usable code.
        for v in NON_ACCEPTING:
            with self.subTest(verdict=v):
                self.assertNotEqual(v, ACCEPTED)
                self.assertFalse(Proposal(verdict=v).accepted)
                # Even if a code is somehow attached to a non-accepting
                # verdict, .accepted must still be False -- callers gate on
                # the verdict, never on the presence of a code.
                self.assertFalse(
                    Proposal(verdict=v, code="NETWORK_TIMEOUT").accepted)

    def test_a_transport_that_raises_is_caught_not_propagated(self):
        # Regression. propose() promises to ALWAYS return a Proposal; a
        # transport that blows up is one more way of learning nothing, and
        # learning nothing means UNKNOWN. Letting it escape would take down a
        # whole batch run over a classification that was never load-bearing --
        # fail-open behaviour in the one module built to fail closed.
        def boom(prompt, key, model, timeout, max_tokens, provider):
            raise RuntimeError("provider client exploded")

        p = propose(MYSTERY, CODES, CONFIG, api_key="k", _transport=boom)
        self.assertEqual(p.verdict, FAILED_TRANSPORT)
        self.assertFalse(p.accepted)
        self.assertIn("RuntimeError", p.error)

    def test_a_typeerror_inside_a_transport_is_not_mistaken_for_wrong_arity(self):
        # The specific bug this replaced. Arity used to be discovered by
        # calling with six arguments and catching TypeError, which cannot tell
        # "this takes five parameters" from "this has a bug". A real TypeError
        # was swallowed, retried with the wrong arity, and re-raised with a
        # misleading "missing 1 required positional argument" message that
        # named the wrong problem entirely.
        def buggy(prompt, key, model, timeout, max_tokens, provider):
            raise TypeError("a real bug inside the transport")

        p = propose(MYSTERY, CODES, CONFIG, api_key="k", _transport=buggy)
        self.assertEqual(p.verdict, FAILED_TRANSPORT)
        self.assertIn("a real bug inside the transport", p.error,
                      "the original error must survive, not be replaced by an "
                      "arity message about a different problem")

    def test_both_transport_arities_are_dispatched_correctly(self):
        # Five-argument transports predate provider routing and are still used
        # across this file; six-argument ones get the provider. Both must
        # work, and the choice is made from the signature, never by trying one
        # and catching the failure.
        five = propose(MYSTERY, CODES, CONFIG, api_key="k",
                       _transport=transport(reply("ISSUER_DOWN")))
        self.assertTrue(five.accepted)

        seen = []
        six = propose(MYSTERY, CODES, CONFIG, api_key="k",
                      _transport=provider_transport(reply("ISSUER_DOWN"),
                                                    seen=seen))
        self.assertTrue(six.accepted)
        self.assertEqual(seen, [CONFIG["provider"]])

    def test_disabled_in_policy_means_no_call_is_made(self):
        calls = []

        def spy(*a, **k):
            calls.append(a)
            return reply("ISSUER_DOWN"), None

        cfg = dict(CONFIG, enabled=False)
        p = propose(MYSTERY, CODES, cfg, api_key="k", _transport=spy)
        self.assertEqual(p.verdict, DISABLED)
        self.assertEqual(calls, [], "a disabled feature must not call out")

    def test_provider_is_passed_to_the_transport(self):
        seen = []
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=provider_transport(
                        reply("ISSUER_DOWN"), seen=seen))
        self.assertTrue(p.accepted)
        self.assertEqual(seen, ["groq"])
        self.assertEqual(p.provider, "groq")

    def test_missing_groq_key_names_the_right_env_var(self):
        p = propose(MYSTERY, CODES, CONFIG, api_key="",
                    env_path="/nonexistent/.env")
        self.assertEqual(p.verdict, FAILED_NO_CREDENTIALS)
        self.assertIn("GROQ_API_KEY", p.error)

    def test_groq_chat_completion_envelope_is_parsed(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'{"choices":[{"message":{"content":'
                    b'"\\"{\\\\\\"code\\\\\\": \\\\\\"ISSUER_DOWN\\\\\\", '
                    b'\\\\\\"confidence\\\\\\": 0.91}\\""}}]}'
                )

        with mock.patch("urllib.request.urlopen", return_value=Response()):
            text, error = _call_api("prompt", "key", CONFIG["model"],
                                    8, 300, "groq")
        self.assertIsNone(error)
        self.assertIn("ISSUER_DOWN", text)

    def test_anthropic_messages_envelope_is_still_supported(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'{"content":[{"text":"{\\"code\\": \\"ISSUER_DOWN\\", '
                    b'\\"confidence\\": 0.91}"}]}'
                )

        with mock.patch("urllib.request.urlopen", return_value=Response()):
            text, error = _call_api("prompt", "key", "claude-sonnet-5",
                                    8, 300, "anthropic")
        self.assertIsNone(error)
        self.assertIn("ISSUER_DOWN", text)


# -- every failure lands on the conservative default -----------------------

class TestFailureRoutesToUnknown(unittest.TestCase):

    def test_a_rejected_proposal_leaves_the_diagnosis_unknown(self):
        for maker in (transport(reply("BANK_IS_SAD")),
                      transport("garbage"),
                      transport(error="timeout"),
                      transport(error="http_429 rate limited")):
            with self.subTest():
                p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                            _transport=maker)
                d = diagnose(txn(), [], resolved={MYSTERY: p})
                self.assertEqual(d["cause"], UNKNOWN_DIAGNOSIS["cause"])
                self.assertEqual(d["recoverability_estimate"],
                                 UNKNOWN_DIAGNOSIS["recoverability_estimate"])
                self.assertNotIn("llm_proposed_code", d)

    def test_the_unknown_fallback_is_the_one_policy_already_defined(self):
        # The conservative path is not invented for the LLM. It is the same
        # unmapped_code_fallback that has been in policy.yaml since phase 2,
        # argued for before any model was planned.
        policy = load_policy(POLICY_PATH)
        self.assertEqual(policy.doc["unmapped_code_fallback"], "UNKNOWN")
        window, _ = policy._window_for(MYSTERY)
        self.assertEqual(window, policy.retry_windows["UNKNOWN"])

    def test_below_the_confidence_floor_is_rejected(self):
        floor = CONFIG["min_confidence"]
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=transport(reply("ISSUER_DOWN",
                                               confidence=floor - 0.01)))
        self.assertEqual(p.verdict, REJECTED_LOW_CONFIDENCE)
        d = diagnose(txn(), [], resolved={MYSTERY: p})
        self.assertEqual(d["cause"], UNKNOWN_DIAGNOSIS["cause"])

    def test_unsure_is_an_available_answer_and_is_honoured(self):
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=transport(reply("UNSURE")))
        self.assertEqual(p.verdict, REJECTED_UNKNOWN_CODE)
        self.assertFalse(p.accepted)


# -- containment: the model cannot widen anything --------------------------

class TestTheModelCannotWidenABound(unittest.TestCase):

    def test_a_known_code_is_never_sent_to_the_model(self):
        # Blast radius. The model only ever sees codes the taxonomy does not
        # have, so it cannot relabel something the project understands.
        asked = []

        def spy(prompt, key, model, timeout, max_tokens):
            asked.append(prompt)
            return reply("NETWORK_TIMEOUT"), None

        txns = [txn(code=c, tid="t%d" % i)
                for i, c in enumerate(sorted(CODES))]
        resolve_unmapped(txns, CODES, CONFIG, api_key="k", _transport=spy)
        self.assertEqual(asked, [],
                         "no known code may be sent for classification")

    def test_a_proposal_cannot_override_a_code_the_taxonomy_knows(self):
        # Even if a proposal for a known code is somehow fabricated and handed
        # in, diagnose must ignore it. The guard is in the consumer, not only
        # in the caller that builds the map.
        forged = Proposal(verdict=ACCEPTED, code="NETWORK_TIMEOUT",
                          confidence=0.99, original_code="CARD_EXPIRED")
        d = diagnose(txn(code="CARD_EXPIRED"), [],
                     resolved={"CARD_EXPIRED": forged})
        self.assertEqual(d["severity"], FAILURE_TAXONOMY["CARD_EXPIRED"]["severity"])
        self.assertFalse(d["retryable"])
        self.assertNotIn("llm_proposed_code", d)

    def test_an_accepted_proposal_only_reaches_the_existing_taxonomy(self):
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=transport(reply("ISSUER_DOWN")))
        self.assertTrue(p.accepted)
        d = diagnose(txn(), [], resolved={MYSTERY: p})
        # It inherits ISSUER_DOWN's entry wholesale -- no new severity, no new
        # retry allowance, nothing the policy did not already define.
        self.assertEqual(d["severity"], FAILURE_TAXONOMY["ISSUER_DOWN"]["severity"])
        self.assertEqual(d["cause"], FAILURE_TAXONOMY["ISSUER_DOWN"]["cause"])
        self.assertEqual(d["llm_proposed_code"], "ISSUER_DOWN")

    def test_an_accepted_proposal_is_discounted_not_trusted_outright(self):
        # A model's opinion about an unfamiliar code must not read as
        # confidently as a code the taxonomy actually knows.
        p = propose(MYSTERY, CODES, CONFIG, api_key="k",
                    _transport=transport(reply("ISSUER_DOWN", confidence=0.9)))
        d = diagnose(txn(), [], resolved={MYSTERY: p})
        known = diagnose(txn(code="ISSUER_DOWN"), [])
        self.assertLess(d["confidence"], known["confidence"])

    def test_a_hard_code_proposal_stays_hard(self):
        # Mapping onto a terminal code must not become a way to make it
        # retryable. It inherits HARD, and the policy still forecloses.
        p = propose("CARD_IS_DEAD_FOREVER", CODES, CONFIG, api_key="k",
                    _transport=transport(reply("ACCOUNT_CLOSED")))
        d = diagnose(txn(code="CARD_IS_DEAD_FOREVER"), [],
                     resolved={"CARD_IS_DEAD_FOREVER": p})
        self.assertEqual(d["severity"], "HARD")
        self.assertFalse(d["retryable"])


# -- provenance ------------------------------------------------------------

class TestProvenanceIsLogged(unittest.TestCase):

    def _log_and_read(self, transport_fn, code=MYSTERY):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with audit_mod.AuditLog(path, dry_run=True) as log:
                diagnose_batch([txn(code=code)], audit=log, llm_config=CONFIG,
                               llm_api_key="k", _llm_transport=transport_fn)
            return list(audit_mod.read_entries(path))
        finally:
            os.unlink(path)

    def test_an_accepted_proposal_is_logged_with_its_prompt_version(self):
        rows = self._log_and_read(transport(reply("ISSUER_DOWN")))
        prop = [r for r in rows if r.get("event") == "llm_diagnosis_proposed"]
        self.assertEqual(len(prop), 1)
        e = prop[0]
        self.assertEqual(e["verdict"], ACCEPTED)
        self.assertEqual(e["proposed_code"], "ISSUER_DOWN")
        self.assertEqual(e["original_code"], MYSTERY)
        self.assertEqual(e["model"], CONFIG["model"])
        # Without this a replay reconstructs WHICH code was proposed but not
        # the reasoning that produced it, and a model change becomes
        # indistinguishable from a prompt change.
        self.assertEqual(e["prompt_version"], CONFIG["prompt_version"])
        self.assertTrue(e["raw_response"])

    def test_a_rejection_is_logged_too(self):
        # The more interesting audit line of the two: it records that the
        # agent was offered an answer and declined it.
        rows = self._log_and_read(transport(reply("BANK_IS_SAD")))
        prop = [r for r in rows if r.get("event") == "llm_diagnosis_proposed"]
        self.assertEqual(len(prop), 1)
        self.assertEqual(prop[0]["verdict"], REJECTED_UNKNOWN_CODE)
        self.assertIsNone(prop[0]["proposed_code"])
        self.assertTrue(prop[0]["error"])

    def test_a_transport_failure_is_logged_rather_than_swallowed(self):
        rows = self._log_and_read(transport(error="timeout"))
        prop = [r for r in rows if r.get("event") == "llm_diagnosis_proposed"]
        self.assertEqual(len(prop), 1)
        self.assertEqual(prop[0]["verdict"], FAILED_TIMEOUT)

    def test_the_transaction_line_carries_the_proposal_too(self):
        rows = self._log_and_read(transport(reply("ISSUER_DOWN")))
        diag = [r for r in rows if r.get("event") == "diagnosed"]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0]["llm_proposed_code"], "ISSUER_DOWN")
        self.assertEqual(diag[0]["llm_prompt_version"], "diag-v1")

    def test_one_question_per_code_not_per_transaction(self):
        # Forty payments carrying the same unfamiliar code is one question.
        asked = []

        def spy(prompt, key, model, timeout, max_tokens):
            asked.append(prompt)
            return reply("ISSUER_DOWN"), None

        txns = [txn(tid="t%d" % i) for i in range(40)]
        resolve_unmapped(txns, CODES, CONFIG, api_key="k", _transport=spy)
        self.assertEqual(len(asked), 1)


# -- the policy document stays the authority -------------------------------

class TestPolicyOwnsTheConfiguration(unittest.TestCase):

    def test_the_llm_block_lives_in_policy_yaml(self):
        policy = load_policy(POLICY_PATH)
        cfg = policy.doc.get("llm_diagnosis")
        self.assertIsNotNone(cfg, "llm_diagnosis must be configured in policy.yaml")
        for key in ("enabled", "model", "prompt_version", "min_confidence",
                    "timeout_seconds"):
            self.assertIn(key, cfg)

    def test_the_confidence_floor_is_read_from_the_document(self):
        # Change the document, behaviour follows -- the same guarantee the
        # rest of the policy has.
        strict = dict(CONFIG, min_confidence=0.99)
        p = propose(MYSTERY, CODES, strict, api_key="k",
                    _transport=transport(reply("ISSUER_DOWN", confidence=0.95)))
        self.assertEqual(p.verdict, REJECTED_LOW_CONFIDENCE)

        loose = dict(CONFIG, min_confidence=0.10)
        p2 = propose(MYSTERY, CODES, loose, api_key="k",
                     _transport=transport(reply("ISSUER_DOWN", confidence=0.95)))
        self.assertTrue(p2.accepted)

    def test_no_failure_code_is_hardcoded_in_the_llm_module(self):
        # Same architectural rule the rest of the project follows: the
        # allowed set is passed in from the taxonomy, never written here.
        with open(os.path.join(ROOT, "src", "llm_diagnose.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        body = src.split('"""', 2)[-1]        # skip the module docstring
        for code in CODES:
            self.assertNotIn(
                '"%s"' % code, body,
                "%s is hardcoded in llm_diagnose.py; it must come from the "
                "taxonomy" % code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
