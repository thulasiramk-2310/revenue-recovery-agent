"""One-minute demo of where the AI is used.

The normal batch can run without ever needing a model if all gateway failure
codes are already in the taxonomy. That is good for production, but it makes
the AI contribution easy to miss in a buildathon demo.

This script creates the case a real merchant eventually hits: a gateway sends
an unfamiliar code. The LLM may map that code onto one existing diagnosis, but
it cannot choose the recovery action, change a retry limit, or bypass policy.

Default mode is deterministic and offline: a fake transport returns the same
JSON shape the model would return, so the demo is repeatable and needs no API
key. Use --live-llm only when you deliberately want to call the configured
model.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .audit import AuditLog, read_entries, verify_chain
from .diagnose import FAILURE_TAXONOMY, diagnose
from .llm_diagnose import resolve_unmapped
from .policy import TransactionState, decide_and_log, load_policy

DEFAULT_LOG = "results/ai_demo.log"
UNKNOWN_CODE = "ISSUER_UNAVAILABLE_TRY_LATER"
DEMO_NOW = "2026-08-11T16:15:00+05:30"

DEMO_TRANSACTION = {
    "transaction_id": "pay_AI_DEMO_001",
    "amount_paise": 249900,
    "timestamp": "2026-08-11T10:15:00+05:30",
    "issuer_bank": "HDFC",
    "failure_code": UNKNOWN_CODE,
    "gateway_message": "Issuer unavailable, please try later",
    "customer_id": "cust_ai_demo",
    "attempt_number": 1,
    "is_subscription": False,
}

DEMO_SIGNAL = {
    "signal": "ISSUER_DEGRADED",
    "issuer_bank": "HDFC",
    "window_start": "2026-08-11T10:00:00+05:30",
    "window_end": "2026-08-11T18:00:00+05:30",
    "observed_window_start": "2026-08-11T10:00:00+05:30",
    "observed_window_end": "2026-08-11T10:40:00+05:30",
    "confidence": 0.99,
    "z_mix": 4.8,
    "z_volume": 2.1,
    "evidence_transaction_ids": ["pay_prior_001", "pay_prior_002"],
}


def _offline_transport(prompt, key, model, timeout, max_tokens):
    """Deterministic stand-in for the LLM API."""
    return json.dumps({
        "code": "ISSUER_DOWN",
        "confidence": 0.92,
        "reasoning": "The gateway message says the issuer is unavailable.",
    }), None


def _log_diagnosis(log, diagnosis):
    log.event(
        "diagnosed",
        transaction_id=diagnosis["transaction_id"],
        failure_code=diagnosis["failure_code"],
        severity=diagnosis["severity"],
        retryable=diagnosis["retryable"],
        customer_actionable=diagnosis["customer_actionable"],
        cause=diagnosis["cause"],
        is_transient=diagnosis["is_transient"],
        recoverability_estimate=diagnosis["recoverability_estimate"],
        recommended_wait_hours=diagnosis["recommended_wait_hours"],
        issuer_degraded=diagnosis["issuer_degraded"],
        confidence=diagnosis["confidence"],
        evidence=diagnosis["evidence"],
        llm_proposed_code=diagnosis.get("llm_proposed_code"),
        llm_prompt_version=diagnosis.get("llm_prompt_version"),
    )


def run_demo(log_path=DEFAULT_LOG, policy_path="policy.yaml", live_llm=False,
             quiet=False):
    """Run the AI demo and return the key facts for tests or scripts."""
    policy = load_policy(policy_path)
    cfg = dict(policy.doc.get("llm_diagnosis") or {})
    cfg["enabled"] = True

    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    if os.path.exists(log_path):
        os.remove(log_path)

    transport = None if live_llm else _offline_transport
    api_key = None if live_llm else "offline-demo-key"

    with AuditLog(
        log_path,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        dry_run=True,
        extra_run_context={
            "demo": "ai_unmapped_failure_code",
            "mode": "live_llm" if live_llm else "offline_deterministic",
        },
    ) as log:
        log.event(
            "issuer_degradation_detected",
            issuer_bank=DEMO_SIGNAL["issuer_bank"],
            window_start=DEMO_SIGNAL["window_start"],
            window_end=DEMO_SIGNAL["window_end"],
            confidence=DEMO_SIGNAL["confidence"],
            z_mix=DEMO_SIGNAL["z_mix"],
            z_volume=DEMO_SIGNAL["z_volume"],
            evidence_count=len(DEMO_SIGNAL["evidence_transaction_ids"]),
            evidence_transaction_ids=DEMO_SIGNAL["evidence_transaction_ids"],
            method="demo fixture: prior failures established issuer degradation",
        )
        proposals = resolve_unmapped(
            [DEMO_TRANSACTION],
            set(FAILURE_TAXONOMY),
            cfg,
            audit=log,
            api_key=api_key,
            _transport=transport,
        )
        diagnosis = diagnose(
            DEMO_TRANSACTION, [DEMO_SIGNAL], resolved=proposals)
        _log_diagnosis(log, diagnosis)
        decision = decide_and_log(
            policy, log, DEMO_TRANSACTION, diagnosis,
            TransactionState(), [DEMO_SIGNAL], DEMO_NOW)

    chain = verify_chain(log_path)
    entries = list(read_entries(log_path))
    proposal = proposals[UNKNOWN_CODE]
    result = {
        "unknown_code": UNKNOWN_CODE,
        "provider": proposal.provider,
        "model": proposal.model,
        "ai_verdict": proposal.verdict,
        "ai_proposed_code": proposal.code,
        "ai_confidence": round(proposal.confidence, 4),
        "diagnosis_cause": diagnosis["cause"],
        "diagnosis_confidence": diagnosis["confidence"],
        "policy_action": decision.action,
        "policy_rule_applied": decision.policy_rule_applied,
        "scheduled_time": decision.scheduled_time,
        "llm_chose_action": False,
        "chain_ok": chain["ok"],
        "log_path": log_path,
        "audit_events": len(entries),
    }

    if not quiet:
        print_report(result, live_llm=live_llm)
    return result


def print_report(result, live_llm=False):
    print("\n" + "=" * 72)
    print("  AI DIAGNOSIS DEMO")
    print("=" * 72)
    print("  input gateway code      %s" % result["unknown_code"])
    print("  model mode              %s"
          % ("live API" if live_llm else "offline deterministic fixture"))
    print("  provider/model          %s / %s"
          % (result["provider"], result["model"]))
    print("  AI verdict              %s" % result["ai_verdict"])
    print("  AI proposed diagnosis   %s  confidence %.2f"
          % (result["ai_proposed_code"], result["ai_confidence"]))
    print("  diagnosis after context %s  confidence %.2f"
          % (result["diagnosis_cause"], result["diagnosis_confidence"]))
    print("  policy action           %s" % result["policy_action"])
    print("  policy rule             %s" % result["policy_rule_applied"])
    print("  scheduled for           %s" % result["scheduled_time"])
    print("  guardrail               LLM chose diagnosis only; policy chose action")
    print("  audit chain             %s over %d entries"
          % ("VERIFIED" if result["chain_ok"] else "BROKEN",
             result["audit_events"]))
    print("  audit log               %s" % result["log_path"])
    print("=" * 72)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Show the bounded AI diagnosis path for an unmapped code.")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--policy", default="policy.yaml")
    ap.add_argument("--live-llm", action="store_true",
                    help="call the configured LLM instead of the offline fixture")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    result = run_demo(
        log_path=args.log,
        policy_path=args.policy,
        live_llm=args.live_llm,
        quiet=args.quiet,
    )
    return 0 if result["chain_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
