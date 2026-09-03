"""Diagnosis: turn raw failures into causes the policy can act on.

Two levels, per the design:

  1. Per transaction -- classify the failure_code SOFT (retryable) or HARD
     (never retryable) and attach a recovery hint.
  2. Per batch -- issuer degradation, delegated to detect.py, which is where
     the pinned contract for it lives and where CLAUDE.md's architecture
     diagram puts it. `diagnose_batch` below runs both levels together so
     callers get one entry point.

The distinction that carries the whole project: a failure CODE is what the
gateway said; a DIAGNOSIS is why it happened and whether waiting changes
anything. INSUFFICIENT_FUNDS is not "retry later", it is "this customer has no
balance until money arrives, and the arrival time is predictable". Same code,
different diagnosis depending on batch context from detect.py.

INVALID_CVV sits deliberately across the two buckets: HARD for retry (the
stored credential is wrong and will stay wrong; re-sending it is pure cost)
but SOFT for a customer nudge (a human re-entering the CVV fixes it in
seconds). `retryable` and `customer_actionable` are therefore separate fields
rather than one severity flag, because collapsing them would either throw the
recoverable money away or authorise a pointless retry.

Rules
-----
* MUST NOT read `_ground_truth`. Use load_batch(), which strips it.
* MUST NOT decide the action -- that is policy.py's job, bounded by
  policy.yaml. This module only characterises the failure.
* Every diagnosis is logged before a decision is derived from it, so the trail
  shows what the agent believed at decision time, not just what it did.

Contract
--------
diagnose(transaction, batch_signals) -> dict
    {cause, is_transient, recoverability_estimate, recommended_wait_hours,
     evidence, confidence, severity, retryable, customer_actionable,
     recovery_hint}
"""

from __future__ import annotations

from datetime import datetime

from .detect import detect_issuer_degradation, signal_for_transaction

SOFT = "SOFT"
HARD = "HARD"

# Failure taxonomy. This is characterisation, not policy: it says what a code
# MEANS, never what to do about it. policy.yaml decides the action.
#
# recoverability_estimate is a DOMAIN PRIOR -- a payments practitioner's rough
# expectation for how often each code clears on a well-timed retry. It is not
# fitted to this batch and never touches _ground_truth; the round numbers are
# a deliberate signal that these are judgement, not measurement. They inform
# ordering and reporting only. policy.yaml, not this table, decides what runs.
FAILURE_TAXONOMY = {
    "INSUFFICIENT_FUNDS": {
        "severity": SOFT,
        "retryable": True,
        "customer_actionable": True,
        "cause": "customer_balance_insufficient",
        "is_transient": True,
        "recoverability_estimate": 0.50,
        "recommended_wait_hours": 24,
        "recovery_hint": (
            "Balance-driven, not instrument-driven. Retry aligned to the "
            "salary-credit window rather than an hour later."
        ),
    },
    "NETWORK_TIMEOUT": {
        "severity": SOFT,
        "retryable": True,
        "customer_actionable": False,
        "cause": "transport_or_gateway_timeout",
        "is_transient": True,
        "recoverability_estimate": 0.85,
        "recommended_wait_hours": 1,
        "recovery_hint": (
            "Outcome is genuinely unknown -- the authorisation may have "
            "succeeded at the issuer. Reconcile before any retry."
        ),
    },
    "ISSUER_DOWN": {
        "severity": SOFT,
        "retryable": True,
        "customer_actionable": False,
        "cause": "issuer_unavailable",
        "is_transient": True,
        "recoverability_estimate": 0.90,
        "recommended_wait_hours": 2,
        "recovery_hint": (
            "Nothing wrong with this customer or instrument. Wait for the "
            "issuer to recover, then spend a single attempt."
        ),
    },
    "DO_NOT_HONOR": {
        "severity": SOFT,
        "retryable": True,
        "customer_actionable": True,
        "cause": "issuer_generic_refusal",
        "is_transient": True,
        "recoverability_estimate": 0.25,
        "recommended_wait_hours": 12,
        "recovery_hint": (
            "Opaque catch-all hiding several causes. Soft velocity and risk "
            "blocks clear; most others never will. Retry sparingly."
        ),
    },
    "CARD_EXPIRED": {
        "severity": HARD,
        "retryable": False,
        "customer_actionable": True,
        "cause": "instrument_expired",
        "is_transient": False,
        "recoverability_estimate": 0.0,
        "recommended_wait_hours": None,
        "recovery_hint": (
            "No amount of waiting revives an expired card. Only a new "
            "instrument from the customer recovers this."
        ),
    },
    "CARD_BLOCKED": {
        "severity": HARD,
        "retryable": False,
        "customer_actionable": True,
        "cause": "instrument_blocked_by_issuer",
        "is_transient": False,
        "recoverability_estimate": 0.0,
        "recommended_wait_hours": None,
        "recovery_hint": (
            "Issuer-side block. The customer must clear it with their bank; "
            "retrying counts against the merchant's decline ratio."
        ),
    },
    "ACCOUNT_CLOSED": {
        "severity": HARD,
        "retryable": False,
        "customer_actionable": True,
        "cause": "account_no_longer_exists",
        "is_transient": False,
        "recoverability_estimate": 0.0,
        "recommended_wait_hours": None,
        "recovery_hint": "The account is gone. Nothing to retry against.",
    },
    "INVALID_CVV": {
        # The deliberate split: hard for retry, soft for a customer nudge.
        "severity": HARD,
        "retryable": False,
        "customer_actionable": True,
        "cause": "stored_credential_incorrect",
        "is_transient": False,
        "recoverability_estimate": 0.0,
        "recommended_wait_hours": None,
        "recovery_hint": (
            "HARD for retry -- the stored CVV is wrong and re-sending it "
            "will fail identically. SOFT for customer action: one re-entry "
            "fixes it. Escalate to the customer, never to the gateway."
        ),
    },
}

# Anything the gateway sends that we have not mapped. Treated conservatively:
# retryable, but with a low prior and an explicit flag so it surfaces for
# human review rather than silently inheriting a confident classification.
UNKNOWN_DIAGNOSIS = {
    "severity": SOFT,
    "retryable": True,
    "customer_actionable": False,
    "cause": "unmapped_failure_code",
    "is_transient": True,
    "recoverability_estimate": 0.15,
    "recommended_wait_hours": 4,
    "recovery_hint": (
        "Code not in the taxonomy. One conservative attempt, then a human "
        "should look at it and the taxonomy should be extended."
    ),
}

HARD_CODES = frozenset(
    c for c, v in FAILURE_TAXONOMY.items() if v["severity"] == HARD
)
SOFT_CODES = frozenset(
    c for c, v in FAILURE_TAXONOMY.items() if v["severity"] == SOFT
)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def diagnose(transaction, batch_signals=None, resolved=None):
    """Characterise one failure. Pure: no I/O, no mutation of the input.

    `resolved` is an optional {failure_code: Proposal} map from
    src/llm_diagnose. It is passed IN rather than fetched here on purpose:
    this function is pure and must stay that way, because the stopping rules
    are tested against it in isolation. The network call happens in
    diagnose_batch, which already owns the audit log.

    `batch_signals` is the list returned by detect_issuer_degradation. When a
    transaction falls inside a degradation window the diagnosis is REWRITTEN,
    not merely annotated: a DO_NOT_HONOR from a bank that is currently falling
    over is not a customer problem, and treating it as one wastes the
    escalation ladder on somebody whose card is fine.
    """
    code = transaction.get("failure_code")
    base = FAILURE_TAXONOMY.get(code, UNKNOWN_DIAGNOSIS)

    # An accepted LLM proposal maps an unmapped code onto a taxonomy entry.
    # It can only ever apply where `base` is UNKNOWN_DIAGNOSIS: a code the
    # taxonomy knows is never sent to a model, so a proposal cannot override
    # or soften something the project already understands.
    proposal = (resolved or {}).get(code)
    llm_applied = False
    if (proposal is not None and proposal.accepted
            and code not in FAILURE_TAXONOMY
            and proposal.code in FAILURE_TAXONOMY):
        base = FAILURE_TAXONOMY[proposal.code]
        llm_applied = True

    d = dict(base)
    d["failure_code"] = code
    d["transaction_id"] = transaction.get("transaction_id")
    d["is_subscription"] = bool(transaction.get("is_subscription"))
    d["known_code"] = code in FAILURE_TAXONOMY
    d["confidence"] = 0.9 if d["known_code"] else 0.3
    d["evidence"] = ["failure_code=" + str(code)]

    if llm_applied:
        # Never silently inherit the mapped code's confidence. The diagnosis
        # is now a model's opinion about an unfamiliar code, and the trail has
        # to say so on the transaction, not only on the proposal line.
        d["llm_proposed_code"] = proposal.code
        d["llm_confidence"] = round(float(proposal.confidence), 4)
        d["llm_model"] = proposal.model
        d["llm_prompt_version"] = proposal.prompt_version
        d["confidence"] = round(0.9 * float(proposal.confidence), 4)
        d["evidence"].append(
            "llm_diagnosis:%s -> %s (confidence %.2f, %s/%s)"
            % (code, proposal.code, proposal.confidence,
               proposal.model, proposal.prompt_version))
    d["batch_signal"] = None
    d["issuer_degraded"] = False

    signal = signal_for_transaction(transaction, batch_signals or [])

    if signal is not None:
        d["issuer_degraded"] = True
        d["batch_signal"] = {
            "signal": signal["signal"],
            "issuer_bank": signal["issuer_bank"],
            "window_start": signal["window_start"],
            "window_end": signal["window_end"],
            "confidence": signal["confidence"],
        }
        d["evidence"].append(
            "issuer_degradation:%s %s..%s (z_mix=%.2f, n_evidence=%d)" % (
                signal["issuer_bank"], signal["window_start"],
                signal["window_end"], signal["z_mix"],
                len(signal["evidence_transaction_ids"]),
            )
        )

        # A HARD code stays HARD. An expired card is expired whatever the
        # issuer's uptime is, and letting an outage launder a terminal code
        # into a retryable one would quietly defeat the stopping rules.
        if d["severity"] == SOFT:
            d["cause"] = "issuer_degradation"
            d["is_transient"] = True
            d["recoverability_estimate"] = max(
                d["recoverability_estimate"], 0.80
            )
            d["confidence"] = min(0.95, d["confidence"] + 0.05)
            d["recovery_hint"] = (
                "Failed inside a detected degradation window for "
                + str(signal["issuer_bank"])
                + ". Attributable to the issuer, not this customer. Hold "
                "until the window clears rather than spending an attempt."
            )
            window_end = _parse(signal["window_end"])
            failed_at = _parse(transaction["timestamp"])
            hours = (window_end - failed_at).total_seconds() / 3600.0
            d["recommended_wait_hours"] = round(max(hours, 0.0), 2)

    return d


def diagnose_batch(transactions, audit=None, llm_config=None,
                   env_path=".env", llm_api_key=None, _llm_transport=None,
                   **detect_kwargs):
    """Run both levels: batch degradation first, then every transaction.

    Order matters. Batch signals must exist before any per-transaction
    diagnosis, because a transaction inside an outage window gets a materially
    different diagnosis from an identical one outside it.

    This is also where the LLM call lives, if one happens at all. `diagnose`
    itself stays pure; the I/O is hoisted here, where the audit log already
    is. Codes are resolved once each rather than once per transaction, so a
    batch asks a question per unknown CODE, not per payment.

    Returns (diagnoses_by_transaction_id, batch_signals).
    """
    signals = detect_issuer_degradation(transactions, audit=audit, **detect_kwargs)

    resolved = {}
    if llm_config and llm_config.get("enabled"):
        from .llm_diagnose import resolve_unmapped
        resolved = resolve_unmapped(
            transactions, set(FAILURE_TAXONOMY), llm_config, audit=audit,
            env_path=env_path, api_key=llm_api_key, _transport=_llm_transport,
        )

    diagnoses = {}
    for t in transactions:
        d = diagnose(t, signals, resolved=resolved)
        diagnoses[t["transaction_id"]] = d
        if audit is not None:
            # Logged BEFORE any decision consumes it, so the trail records
            # what the agent believed at decision time, not just what it did.
            audit.event(
                "diagnosed",
                transaction_id=t["transaction_id"],
                failure_code=d["failure_code"],
                severity=d["severity"],
                retryable=d["retryable"],
                customer_actionable=d["customer_actionable"],
                cause=d["cause"],
                is_transient=d["is_transient"],
                recoverability_estimate=d["recoverability_estimate"],
                recommended_wait_hours=d["recommended_wait_hours"],
                issuer_degraded=d["issuer_degraded"],
                confidence=d["confidence"],
                evidence=d["evidence"],
                llm_proposed_code=d.get("llm_proposed_code"),
                llm_prompt_version=d.get("llm_prompt_version"),
            )

    return diagnoses, signals


def summarise_diagnoses(diagnoses):
    """Counts by severity, cause, and degradation flag. Reporting only."""
    out = {
        "total": len(diagnoses),
        "by_severity": {},
        "by_cause": {},
        "retryable": 0,
        "customer_actionable_only": 0,
        "issuer_degraded": 0,
        "unknown_code": 0,
    }
    for d in diagnoses.values():
        out["by_severity"][d["severity"]] = out["by_severity"].get(d["severity"], 0) + 1
        out["by_cause"][d["cause"]] = out["by_cause"].get(d["cause"], 0) + 1
        if d["retryable"]:
            out["retryable"] += 1
        if not d["retryable"] and d["customer_actionable"]:
            out["customer_actionable_only"] += 1
        if d["issuer_degraded"]:
            out["issuer_degraded"] += 1
        if not d["known_code"]:
            out["unknown_code"] += 1
    return out
