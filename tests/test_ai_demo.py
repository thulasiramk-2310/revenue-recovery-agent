from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import audit as audit_mod
from src.run_ai_demo import UNKNOWN_CODE, run_demo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_PATH = os.path.join(ROOT, "policy.yaml")


class TestAIDemo(unittest.TestCase):

    def test_demo_makes_ai_diagnosis_visible_without_network(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            result = run_demo(
                log_path=path,
                policy_path=POLICY_PATH,
                live_llm=False,
                quiet=True,
            )
            self.assertEqual(result["unknown_code"], UNKNOWN_CODE)
            self.assertEqual(result["ai_verdict"], "accepted")
            self.assertEqual(result["ai_proposed_code"], "ISSUER_DOWN")
            self.assertEqual(result["diagnosis_cause"], "issuer_degradation")
            self.assertEqual(result["policy_action"], "HOLD")
            self.assertFalse(result["llm_chose_action"])
            self.assertTrue(result["chain_ok"])
        finally:
            os.unlink(path)

    def test_demo_audit_contains_model_proposal_and_policy_rule(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            run_demo(
                log_path=path,
                policy_path=POLICY_PATH,
                live_llm=False,
                quiet=True,
            )
            rows = list(audit_mod.read_entries(path))
            proposals = [
                r for r in rows
                if r.get("event") == "llm_diagnosis_proposed"
            ]
            decisions = [r for r in rows if r.get("decision")]
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["original_code"], UNKNOWN_CODE)
            self.assertEqual(proposals[0]["proposed_code"], "ISSUER_DOWN")
            self.assertTrue(decisions)
            self.assertTrue(decisions[-1]["policy_rule_applied"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
