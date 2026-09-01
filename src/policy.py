"""Pure interpreter of policy.yaml.

Given a diagnosed transaction plus any batch signals, returns the single
bounded action to take.

HARD ARCHITECTURAL RULE
-----------------------
This module contains NO failure codes, NO delays, NO thresholds. Every such
value is read from policy.yaml. If a reviewer asks "why did it retry that at
11am on the 3rd?", the answer must be a path into the YAML, not a branch in
this file. That is what makes the policy auditable and what lets the bounds
change without touching code. tests/test_stopping_rules.py enforces this by
scanning the source for literal failure codes.

The one thing this file does hardcode is the VOCABULARY policy.yaml is written
in -- the names of the `requires` predicates, and the ladder action names. A
YAML document has to mean something to somebody; PREDICATES below is where
those names acquire meaning. Values, limits and ordering all still come from
the document.

ENFORCEMENT ORDER
-----------------
Strictly ordered. A later rule can only ever be more restrictive; none can
re-authorise something an earlier one refused.

    1. customer opt-out                    -> STOP, universally
    2. failure_code in stop_immediately_on -> retries foreclosed
    3. attempt_number >= max_attempts      -> retries foreclosed
       (then, ONLY while a retry is still on the table:)
    3b. customer at the daily attempt cap  -> DEFER
    4. inside cooldown window              -> DEFER
    5. inside an ISSUER_DEGRADED window    -> HOLD until it clears
    6. always                              -> retry_windows + escalation_ladder

Opt-out leads because it is the one refusal that no amount of recoverable
money may override: compliance.never_do lists retry_after_customer_opt_out,
and a rule that can be outvoted by a large enough amount is not a rule. It is
also the only rule that stops the transaction outright.

TWO BUDGETS, AND WHY RULES 2 AND 3 STOPPED BEING TERMINAL
---------------------------------------------------------
Rules 2 and 3 used to return STOP and end the transaction. That conflated two
different resources. Gateway attempts and customer contacts have different
costs, different caps and different reasons to run out, and exhausting one is
not a reason to forfeit the other -- a customer whose card has expired needs a
message precisely BECAUSE retrying is pointless.

The practical effect of the old shape was that rungs 3-5 of the ladder were
unreachable: every transaction either recovered or hit a STOP first, the run
made zero customer contacts, and three of five rungs were dead code while
policy.yaml advertised them. "Customers contacted: 0" then appeared in the
cost table looking like restraint when it was a bug.

So rules 2 and 3 now FORECLOSE RETRIES rather than stopping the transaction,
and rules 3b, 4 and 5 -- which exist solely to protect the attempt budget --
are skipped once retries are foreclosed, since there is no longer an attempt
to protect. Control always reaches the ladder, which disables the rungs
marked `consumes: attempt` and keeps descending to the contact rungs.

The stopping guarantees are unchanged and are enforced in two places, not
one: the `retry_permitted` and `attempts_remaining` predicates keep the retry
rungs ineligible, and _build_rung_decision raises PolicyViolation if a rung
that consumes an attempt is ever selected while retries are foreclosed. The
second check exists because the first lives in policy.yaml, and a YAML edit
that dropped a predicate would otherwise silently re-authorise retrying a
blocked card.

PURITY AND THE AUDIT TRAIL
--------------------------
`Policy.decide` is a pure function: same inputs, same Decision, no I/O, no
mutation. That is what makes the stopping rules testable in isolation, which
matters because they are the part that must be defensible.

Every decision must still reach the audit log. The two are reconciled by
having `decide` return a Decision that carries its own audit fields, and by
routing all production use through `decide_and_log` / `decide_batch`, which
require an AuditLog and write exactly one line per decision. Purity is in the
computation; the trail is in the caller.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

IST = timezone(timedelta(hours=5, minutes=30))


class PolicyViolation(RuntimeError):
    """A decision was reached that the stopping rules forbid.

    Raised, never logged and swallowed. If the ladder ever selects a rung that
    spends a budget which is already foreclosed, the run is not trustworthy
    and finishing it would produce a recovery figure that cannot be defended.
    """

# Outcome verbs. Not policy values -- these are the shapes a decision can take.
STOP = "STOP"
DEFER = "DEFER"
HOLD = "HOLD"

# Which audit vocabulary each outcome maps to. audit.VALID_DECISIONS is a
# closed set, so this mapping is what keeps the trail's vocabulary stable.
_TERMINAL_CODE = "no_action_terminal"
_SUPPRESSED = "retry_suppressed"
_ABANDONED = "abandoned"
_SCHEDULED = "retry_scheduled"
_ESCALATED = "escalated"
_HANDOFF = "handed_off_to_human"


def _parse(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def _hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


@dataclass
class TransactionState:
    """Mutable per-transaction context the policy reads but never writes.

    Kept outside the transaction record because the batch file is an input
    artefact, while this is where the run accumulates. The caller owns
    updating it; `decide` only reads.
    """
    escalation_step: int = 0            # highest ladder rung already executed
    contacts_used: int = 0
    last_attempt_at: Optional[str] = None   # defaults to the failure timestamp
    last_contact_at: Optional[str] = None   # None = never contacted. Tracked
                                            # apart from last_attempt_at: a
                                            # message does not reset the
                                            # instrument cooldown, and a retry
                                            # does not reset contact spacing.
    opted_out: bool = False
    consent_on_file: bool = True
    alternate_instrument_available: bool = False
    attempts_today_for_customer: int = 0
    retries_this_mandate_cycle: int = 0


@dataclass
class Decision:
    """One bounded decision. Carries everything the audit line needs."""
    transaction_id: str
    action: str
    reason: str
    policy_rule_applied: str
    audit_decision: str
    scheduled_time: Optional[str] = None
    channel: Optional[str] = None
    escalation_step: Optional[int] = None
    bounded_by: list = field(default_factory=list)
    rungs_passed_over: list = field(default_factory=list)
    issuer_degraded: bool = False
    requires_reconcile: bool = False
    customer_visible: bool = False
    terminal: bool = False
    consumes: Optional[str] = None          # 'attempt' | 'contact' | 'none'
    retry_foreclosed_by: Optional[str] = None
    # Dotted rule path that took retries off the table, or None while they are
    # still live. Present on EVERY decision made after foreclosure, so the
    # trail records both the refusal and what was done instead -- a reviewer
    # greps this rather than inferring it from an absence.

    # The stub's pinned contract names this field retry_at; the phase-2 spec
    # names it scheduled_time. Same value, both exposed, no second source of
    # truth to drift.
    @property
    def retry_at(self):
        return self.scheduled_time

    def audit_fields(self) -> dict:
        return {
            "action": self.action,
            "scheduled_time": self.scheduled_time,
            "channel": self.channel,
            "escalation_step": self.escalation_step,
            "bounded_by": self.bounded_by,
            "rungs_passed_over": self.rungs_passed_over,
            "issuer_degraded": self.issuer_degraded,
            "requires_reconcile": self.requires_reconcile,
            "customer_visible": self.customer_visible,
            "terminal": self.terminal,
            "consumes": self.consumes,
            "retry_foreclosed_by": self.retry_foreclosed_by,
        }


# -- ladder predicates ----------------------------------------------------
# The vocabulary policy.yaml's `requires` lists are written in. Each returns
# (met: bool, detail: str). Anything not in this table is treated as UNMET --
# failing closed, so a typo in the YAML can never silently authorise a
# customer contact or an extra attempt.

def _p_retry_permitted(ctx):
    """False once any rule has foreclosed the gateway for this transaction.

    Distinct from retryable_failure, which asks the diagnosis whether a retry
    could work. This asks the POLICY whether one is allowed at all, and is the
    predicate that keeps hard declines and spent allowances off the retry
    rungs now that neither ends the transaction outright.
    """
    foreclosed = ctx["retry_foreclosed_by"]
    return (foreclosed is None), (foreclosed or "retries permitted")


def _p_retryable_failure(ctx):
    return bool(ctx["diagnosis"].get("retryable")), "diagnosis.retryable"


def _p_attempts_remaining(ctx):
    return ctx["attempt_number"] < ctx["effective_max_attempts"], (
        "attempt %d of %d" % (ctx["attempt_number"], ctx["effective_max_attempts"])
    )


def _p_cooldown_elapsed(ctx):
    return ctx["cooldown_elapsed"], "cooldown_hours"


def _p_alternate_instrument_available(ctx):
    return bool(ctx["state"].alternate_instrument_available), "state.alternate_instrument"


def _p_contact_quota_remaining(ctx):
    return ctx["state"].contacts_used < ctx["max_contacts"], (
        "contacts %d of %d" % (ctx["state"].contacts_used, ctx["max_contacts"])
    )


def _p_contact_spacing_elapsed(ctx):
    return ctx["contact_spacing_elapsed"], "limits.min_hours_between_contacts"


def _p_within_contact_hours(ctx):
    return ctx["within_contact_hours"], "compliance.contact_hours_ist"


def _p_consent_on_file(ctx):
    return bool(ctx["state"].consent_on_file), "state.consent_on_file"


# Predicates that a clock will satisfy on its own. An unmet predicate in this
# set means "not yet", and a rung blocked only by these is deferred to the
# moment they clear -- never passed over. Passing over would let a scheduling
# constraint quietly demote a rung, which is the same class of bug as letting
# a spent attempt budget terminate a transaction: a temporary "no" read as a
# permanent one. Everything not listed here is a real ineligibility.
DEFERRABLE_PREDICATES = frozenset({
    "within_contact_hours", "contact_spacing_elapsed",
})

PREDICATES = {
    "retry_permitted": _p_retry_permitted,
    "retryable_failure": _p_retryable_failure,
    "attempts_remaining": _p_attempts_remaining,
    "cooldown_elapsed": _p_cooldown_elapsed,
    "alternate_instrument_available": _p_alternate_instrument_available,
    "contact_quota_remaining": _p_contact_quota_remaining,
    "contact_spacing_elapsed": _p_contact_spacing_elapsed,
    "within_contact_hours": _p_within_contact_hours,
    "consent_on_file": _p_consent_on_file,
}


class Policy:
    def __init__(self, doc: dict, source_path: str = "policy.yaml"):
        self.doc = doc
        self.source_path = source_path
        self.policy_id = doc.get("policy_id")
        self.version = doc.get("version")
        self.limits = doc.get("limits", {})
        self.stop_immediately_on = list(doc.get("stop_immediately_on", []))
        self.retry_windows = doc.get("retry_windows", {})
        self.fallback_code = doc.get("unmapped_code_fallback")
        self.escalation_ladder = sorted(
            doc.get("escalation_ladder", []), key=lambda r: r["step"]
        )
        self.compliance = doc.get("compliance", {})
        self.salary_window = doc.get("salary_credit_window", {})
        self.audit_cfg = doc.get("audit", {})

    # -- helpers ---------------------------------------------------------

    def _window_for(self, code):
        """The retry_windows entry for a code, or the document's fallback.

        Which entry catches an unmapped code is itself a policy question, so
        the answer lives in policy.yaml (`unmapped_code_fallback`) rather than
        as a literal here. That keeps this module free of failure codes, which
        tests/test_stopping_rules.py enforces by scanning the source.
        """
        if code in self.retry_windows:
            return self.retry_windows[code], "retry_windows.%s" % code
        if self.fallback_code and self.fallback_code in self.retry_windows:
            return (self.retry_windows[self.fallback_code],
                    "retry_windows.%s" % self.fallback_code)
        return {}, "retry_windows.<no fallback configured>"

    def effective_max_attempts(self, code, is_subscription):
        """Tightest applicable attempt cap, and which rule set it.

        Every candidate is a ceiling; the smallest wins. Nothing may raise the
        cap above limits.max_attempts -- an override in retry_windows can only
        restrict.
        """
        candidates = [(int(self.limits.get("max_attempts", 1)), "limits.max_attempts")]

        window, wpath = self._window_for(code)
        if "max_attempts_override" in window:
            candidates.append(
                (int(window["max_attempts_override"]), wpath + ".max_attempts_override")
            )
        delays = window.get("delays_minutes") or []
        if delays:
            # The delay list length caps retries: no delay defined, no retry.
            candidates.append((len(delays) + 1, wpath + ".delays_minutes"))

        if is_subscription:
            sub = self.compliance.get("subscription_rules", {})
            if "max_retries_per_mandate_cycle" in sub:
                candidates.append((
                    int(sub["max_retries_per_mandate_cycle"]) + 1,
                    "compliance.subscription_rules.max_retries_per_mandate_cycle",
                ))

        value, rule = min(candidates, key=lambda c: c[0])
        return value, rule

    def cooldown_for(self, code):
        """Minimum gap between attempts for this code, and the rule that set it.

        The global default protects a customer's instrument from repeated
        dunning. A retry_window may shorten it for itself where the document
        gives a reason -- a reconcile-and-settle after an ambiguous timeout is
        not a dunning attempt and should not be paced like one. The override
        can only touch the gap: max_attempts stays absolute.
        """
        window, wpath = self._window_for(code)
        if "cooldown_override_minutes" in window:
            return (timedelta(minutes=float(window["cooldown_override_minutes"])),
                    wpath + ".cooldown_override_minutes")
        return (timedelta(hours=float(self.limits.get("cooldown_hours", 0))),
                "limits.cooldown_hours")

    def _contact_window(self, dt):
        ch = self.compliance.get("contact_hours_ist", {})
        if not ch:
            return None, None
        local = dt.astimezone(IST)
        start = datetime.combine(local.date(), _hhmm(ch["start"]), tzinfo=IST)
        end = datetime.combine(local.date(), _hhmm(ch["end"]), tzinfo=IST)
        return start, end

    def within_contact_hours(self, dt) -> bool:
        start, end = self._contact_window(dt)
        if start is None:
            return True
        return start <= dt.astimezone(IST) <= end

    def next_contact_opening(self, dt) -> datetime:
        """Earliest moment at or after dt that is inside contact hours."""
        start, end = self._contact_window(dt)
        if start is None:
            return dt
        local = dt.astimezone(IST)
        if local < start:
            return start
        if local > end:
            nxt = local.date() + timedelta(days=1)
            ch = self.compliance["contact_hours_ist"]
            return datetime.combine(nxt, _hhmm(ch["start"]), tzinfo=IST)
        return local

    def align_to_salary_window(self, dt):
        """Nudge a scheduled retry into the next salary-credit window.

        Returns (moved_datetime, applied: bool). Only moves when a window
        opens within lookahead_hours -- otherwise waiting costs more than the
        improved odds are worth, and the original time stands.
        """
        cfg = self.salary_window
        if not cfg:
            return dt, False
        days = set(cfg.get("days_of_month", []))
        if not days:
            return dt, False
        hour = int(cfg.get("preferred_hour_ist", 0))
        lookahead = timedelta(hours=float(cfg.get("lookahead_hours", 0)))

        local = dt.astimezone(IST)
        limit = local + lookahead
        probe = local
        for _ in range(int(lookahead.total_seconds() // 3600) + 48):
            if probe.day in days:
                candidate = datetime.combine(
                    probe.date(), time(hour, 0), tzinfo=IST
                )
                if candidate >= local and candidate <= limit:
                    return candidate, True
            probe = datetime.combine(
                probe.date() + timedelta(days=1), time(0, 0), tzinfo=IST
            )
            if probe > limit:
                break
        return dt, False

    # -- the decision ----------------------------------------------------

    def decide(self, transaction, diagnosis, state=None, batch_signals=None,
               now=None) -> Decision:
        """Return the one bounded action for this transaction. Pure."""
        state = state or TransactionState()
        txn_id = transaction.get("transaction_id")
        code = transaction.get("failure_code")
        failed_at = _parse(transaction["timestamp"])
        now = _parse(now) if now is not None else failed_at
        is_sub = bool(transaction.get("is_subscription"))
        attempt_number = int(transaction.get("attempt_number", 1))

        degraded_signal = (diagnosis or {}).get("batch_signal")
        is_degraded = bool((diagnosis or {}).get("issuer_degraded"))

        # --- 1. opt-out ------------------------------------------------
        # First, and unconditional. compliance.never_do makes this the one
        # refusal that recoverable value may not outweigh.
        if state.opted_out:
            return Decision(
                transaction_id=txn_id,
                action=STOP,
                reason=(
                    "Customer has opted out of recovery contact. No retry and "
                    "no message, regardless of recoverable value."
                ),
                policy_rule_applied="compliance.never_do[retry_after_customer_opt_out]",
                audit_decision=_SUPPRESSED,
                bounded_by=["customer_opt_out"],
                issuer_degraded=is_degraded,
                terminal=True,
            )

        # --- 2. terminal failure code -> retries foreclosed -------------
        # The gateway is closed for this code. The customer is not. A hard
        # decline is precisely the case where asking the customer to act is
        # the only thing that can work, so this forecloses the retry rungs
        # and lets the ladder carry on to the contact rungs rather than
        # ending the transaction.
        max_attempts, cap_rule = self.effective_max_attempts(code, is_sub)
        retry_foreclosed_by = None
        if code in self.stop_immediately_on:
            retry_foreclosed_by = "stop_immediately_on[%s]" % code

        # --- 3. attempt allowance -> retries foreclosed -----------------
        elif attempt_number >= max_attempts:
            retry_foreclosed_by = cap_rule

        cooldown, cooldown_rule = self.cooldown_for(code)
        last_attempt = (
            _parse(state.last_attempt_at) if state.last_attempt_at else failed_at
        )
        cooldown_ends = last_attempt + cooldown
        cooldown_elapsed = now >= cooldown_ends

        # Rules 3b, 4 and 5 each exist to protect the attempt budget: defer a
        # retry, space a retry, hold a retry through an outage. With retries
        # foreclosed there is no attempt left to protect, and applying them
        # would only postpone a message the customer should already have had.
        # Skip to the ladder.
        if retry_foreclosed_by is None:
            per_day = self.limits.get("max_attempts_per_customer_per_day")
            if per_day is not None and state.attempts_today_for_customer >= int(per_day):
                return Decision(
                    transaction_id=txn_id,
                    action=DEFER,
                    reason=(
                        "Customer already at %d attempts today, the daily ceiling. "
                        "Deferring rather than stopping: the cap resets."
                        % state.attempts_today_for_customer
                    ),
                    policy_rule_applied="limits.max_attempts_per_customer_per_day",
                    audit_decision=_SUPPRESSED,
                    scheduled_time=datetime.combine(
                        now.astimezone(IST).date() + timedelta(days=1),
                        time(0, 0), tzinfo=IST,
                    ).isoformat(),
                    bounded_by=["limits.max_attempts_per_customer_per_day"],
                    issuer_degraded=is_degraded,
                )

            # --- 4. cooldown -----------------------------------------------
            if not cooldown_elapsed:
                return Decision(
                    transaction_id=txn_id,
                    action=DEFER,
                    reason=(
                        "Only %.1f min since the last attempt; this failure "
                        "requires %.1f min between attempts."
                        % ((now - last_attempt).total_seconds() / 60.0,
                           cooldown.total_seconds() / 60.0)
                    ),
                    policy_rule_applied=cooldown_rule,
                    audit_decision=_SUPPRESSED,
                    scheduled_time=cooldown_ends.isoformat(),
                    bounded_by=[cooldown_rule],
                    issuer_degraded=is_degraded,
                )

            # --- 5. issuer degradation -------------------------------------
            # Retrying into a bank that is currently falling over spends an
            # attempt against limits.max_attempts for a reason that has nothing
            # to do with this customer. Hold; the attempt keeps its value.
            if is_degraded and degraded_signal:
                window, wpath = self._window_for(code)
                window_end = _parse(degraded_signal["window_end"])
                resume_at = window_end
                bounded = ["issuer_degradation"]

                delays = window.get("delays_minutes") or []
                rule = wpath
                if window.get("strategy") == "await_signal" and delays:
                    resume_at = window_end + timedelta(minutes=float(delays[0]))
                    rule = wpath + ".delays_minutes[0]"

                max_wait = window.get("max_wait_hours")
                capped = False
                if max_wait is not None:
                    ceiling = now + timedelta(hours=float(max_wait))
                    if resume_at > ceiling:
                        resume_at = ceiling
                        capped = True
                        bounded.append(wpath + ".max_wait_hours")

                if resume_at > now:
                    return Decision(
                        transaction_id=txn_id,
                        action=HOLD,
                        reason=(
                            "%s is in a detected degradation window until %s "
                            "(confidence %.2f). Holding%s rather than burning an "
                            "attempt on an issuer-side fault."
                            % (degraded_signal["issuer_bank"],
                               degraded_signal["window_end"],
                               degraded_signal.get("confidence", 0.0),
                               " to the max_wait ceiling" if capped else "")
                        ),
                        policy_rule_applied=rule,
                        audit_decision=_SUPPRESSED,
                        scheduled_time=resume_at.isoformat(),
                        bounded_by=bounded,
                        issuer_degraded=True,
                        requires_reconcile=bool(
                            window.get("require_reconcile_before_retry")
                        ),
                    )

        # --- 6. retry window + escalation ladder ------------------------
        # Always reached. The ladder decides what is still permitted given
        # which budgets survive.
        return self._climb_ladder(
            transaction, diagnosis, state, now, failed_at,
            code, attempt_number, max_attempts, cap_rule, is_degraded,
            retry_foreclosed_by, cooldown_elapsed,
        )

    def _predicate_rule(self, name):
        """The policy path a deferrable predicate answers to."""
        return {
            "within_contact_hours": "compliance.contact_hours_ist",
            "contact_spacing_elapsed": "limits.min_hours_between_contacts",
        }.get(name, name)

    def _deferrable_until(self, ctx, unmet_names, now):
        """Earliest instant at which every named predicate is satisfied.

        The max, not the min: waiting out one clock does not satisfy another,
        so a rung blocked by both spacing and contact hours resumes only when
        the later of the two has passed.
        """
        state = ctx["state"]
        whens = []
        for name in unmet_names:
            if name == "contact_spacing_elapsed" and state.last_contact_at:
                whens.append(_parse(state.last_contact_at) + timedelta(
                    hours=float(self.limits.get("min_hours_between_contacts", 0))))
            elif name == "within_contact_hours":
                whens.append(self.next_contact_opening(now))
        if not whens:
            return None
        until = max(whens)
        # A spacing wait can land at 03:00; the contact window still applies.
        if not self.within_contact_hours(until):
            until = self.next_contact_opening(until)
        return until

    def _climb_ladder(self, transaction, diagnosis, state, now, failed_at,
                      code, attempt_number, max_attempts, cap_rule,
                      is_degraded, retry_foreclosed_by=None,
                      cooldown_elapsed=True) -> Decision:
        txn_id = transaction.get("transaction_id")
        is_sub = bool(transaction.get("is_subscription"))
        window, wpath = self._window_for(code)

        spacing_hours = float(self.limits.get("min_hours_between_contacts", 0))
        if state.last_contact_at:
            spacing_ends = _parse(state.last_contact_at) + timedelta(
                hours=spacing_hours)
            contact_spacing_elapsed = now >= spacing_ends
        else:
            contact_spacing_elapsed = True   # never contacted; nothing to space

        ctx = {
            "diagnosis": diagnosis or {},
            "state": state,
            "attempt_number": attempt_number,
            "effective_max_attempts": max_attempts,
            "retry_foreclosed_by": retry_foreclosed_by,
            # True whenever rule 4 cleared. It can be False here only when
            # retries are foreclosed, which skips rule 4 -- and in that case
            # retry_permitted has already made the rungs that read this
            # ineligible, so the honest value is passed through rather than
            # a convenient one.
            "cooldown_elapsed": cooldown_elapsed,
            "max_contacts": int(
                self.limits.get("max_customer_contacts_per_transaction", 0)
            ),
            "contact_spacing_elapsed": contact_spacing_elapsed,
            "within_contact_hours": self.within_contact_hours(now),
        }

        passed_over = []
        for rung in self.escalation_ladder:
            step = int(rung["step"])
            # Never go backwards, and never skip: rungs are considered in
            # order and the FIRST eligible one is taken. A rung is only
            # passed over when its own requires are unmet.
            #
            # Whether a rung may fire again is a property of the rung, and
            # policy.yaml declares it. Rung 1 repeats: each firing is a
            # distinct retry and attempts_remaining bounds it, which is why
            # forcing strict advance once gave every transaction exactly one
            # retry. The contact rungs do NOT repeat: firing step 3 twice
            # sends the same notice twice and consumes the budget that step 4
            # needs, so the customer never receives the message that actually
            # asks them to fix the instrument.
            floor = (state.escalation_step if rung.get("repeatable")
                     else state.escalation_step + 1)
            if step < floor:
                continue

            unmet, unmet_names = [], []
            for name in rung.get("requires", []) or []:
                fn = PREDICATES.get(name)
                if fn is None:
                    # Fail closed on an unrecognised predicate.
                    sys.stderr.write(
                        "policy: unknown predicate %r in escalation_ladder "
                        "step %d; treating as unmet\n" % (name, step)
                    )
                    unmet.append(name + "=<unknown predicate>")
                    unmet_names.append(name)
                    continue
                met, detail = fn(ctx)
                if not met:
                    unmet.append("%s (%s)" % (name, detail))
                    unmet_names.append(name)

            if unmet:
                # Blocked only by the clock? Then this rung is not ineligible,
                # it is early. Wait for it and keep our place on the ladder.
                if set(unmet_names) <= DEFERRABLE_PREDICATES:
                    until = self._deferrable_until(ctx, unmet_names, now)
                    if until is not None and until > now:
                        return Decision(
                            transaction_id=txn_id,
                            action=DEFER,
                            reason=(
                                "Rung %d (%s) is eligible apart from timing: "
                                "%s. Deferring to %s; the rung is held, not "
                                "skipped."
                                % (step, rung["action"], "; ".join(unmet),
                                   until.astimezone(IST).strftime(
                                       "%Y-%m-%d %H:%M"))
                            ),
                            policy_rule_applied=(
                                "escalation_ladder[step=%d].requires[%s]"
                                % (step, ",".join(sorted(unmet_names)))
                            ),
                            audit_decision=_SUPPRESSED,
                            scheduled_time=until.isoformat(),
                            escalation_step=step,
                            bounded_by=sorted(
                                self._predicate_rule(x) for x in unmet_names),
                            rungs_passed_over=passed_over,
                            issuer_degraded=is_degraded,
                            customer_visible=bool(rung.get("customer_visible")),
                            consumes=rung.get("consumes"),
                            retry_foreclosed_by=retry_foreclosed_by,
                        )
                passed_over.append({
                    "step": step,
                    "action": rung["action"],
                    "unmet": unmet,
                })
                continue

            return self._build_rung_decision(
                rung, transaction, diagnosis, state, now, failed_at, code,
                attempt_number, max_attempts, cap_rule, is_degraded,
                window, wpath, passed_over, is_sub, retry_foreclosed_by,
            )

        # Ladder exhausted. Nothing further is authorised.
        return Decision(
            transaction_id=txn_id,
            action=STOP,
            reason=(
                "Every escalation rung above step %d is ineligible. The agent "
                "withdraws rather than inventing an action outside the ladder."
                % state.escalation_step
            ),
            policy_rule_applied="escalation_ladder[exhausted]",
            audit_decision=(_TERMINAL_CODE if retry_foreclosed_by
                            and retry_foreclosed_by.startswith("stop_immediately_on")
                            else _ABANDONED),
            escalation_step=state.escalation_step,
            bounded_by=["escalation_ladder"],
            rungs_passed_over=passed_over,
            issuer_degraded=is_degraded,
            terminal=True,
            retry_foreclosed_by=retry_foreclosed_by,
        )

    def _build_rung_decision(self, rung, transaction, diagnosis, state, now,
                             failed_at, code, attempt_number, max_attempts,
                             cap_rule, is_degraded, window, wpath,
                             passed_over, is_sub,
                             retry_foreclosed_by=None) -> Decision:
        txn_id = transaction.get("transaction_id")
        step = int(rung["step"])
        action = rung["action"]
        consumes = rung.get("consumes")

        # The choke point. Every retry in the system is built here, so this is
        # the one place that can guarantee a foreclosed gateway stays closed.
        # The `retry_permitted` predicate in policy.yaml should already have
        # made this rung ineligible; that predicate lives in a document a
        # reviewer can edit, and dropping it would otherwise silently
        # re-authorise retrying a blocked card. Two independent checks, and
        # this one is not editable without touching code.
        if consumes == "attempt" and retry_foreclosed_by is not None:
            raise PolicyViolation(
                "escalation_ladder step %d (%s) consumes an attempt, but "
                "retries for %s are foreclosed by %s. Refusing to build the "
                "decision: check that the rung still lists retry_permitted "
                "in its `requires`."
                % (step, action, txn_id, retry_foreclosed_by)
            )

        # Cite the budget this rung actually spends. A contact rung bounded by
        # the attempt cap reads as though the two shared a ceiling, which is
        # the confusion this whole change exists to remove.
        if consumes == "attempt":
            bounded = [cap_rule]
        elif consumes == "contact":
            bounded = ["limits.min_hours_between_contacts"]
        else:
            bounded = []
        channel = None
        scheduled = None
        rule = "escalation_ladder[step=%d].%s" % (step, action)
        reason_bits = []

        if rung.get("terminal"):
            return Decision(
                transaction_id=txn_id,
                action=action,
                reason=(
                    "Automated options are exhausted for this transaction. "
                    "Queued for merchant ops; the agent takes no further "
                    "action."
                ),
                policy_rule_applied=rule,
                audit_decision=_HANDOFF,
                escalation_step=step,
                bounded_by=bounded,
                rungs_passed_over=passed_over,
                issuer_degraded=is_degraded,
                terminal=True,
                consumes=consumes,
                retry_foreclosed_by=retry_foreclosed_by,
            )

        if rung.get("customer_visible"):
            prefs = rung.get("channel_preference") or []
            channel = prefs[0] if prefs else "email"
            scheduled = now
            audit_decision = _ESCALATED
            reason_bits.append(
                "Rung %d (%s) via %s." % (step, action, channel)
            )
            if retry_foreclosed_by:
                reason_bits.append(
                    "Retries are foreclosed by %s, which is the reason to "
                    "contact rather than a reason to stop: the customer is "
                    "the only party who can still resolve this."
                    % retry_foreclosed_by
                )
            bounded.append("limits.max_customer_contacts_per_transaction")
            if self.compliance.get("transactional_only"):
                bounded.append("compliance.transactional_only")
        else:
            # A silent retry. Timing comes from retry_windows, then gets
            # pushed later by any rule that applies -- never earlier.
            audit_decision = _SCHEDULED
            delays = window.get("delays_minutes") or []
            idx = max(attempt_number - 1, 0)
            if idx < len(delays):
                offset = float(delays[idx])
                scheduled = failed_at + timedelta(minutes=offset)
                rule = "%s.delays_minutes[%d]" % (wpath, idx)
                reason_bits.append(
                    "%s: attempt %d at +%.0f min from the original failure."
                    % (window.get("strategy", "scheduled"), attempt_number + 1, offset)
                )
            else:
                scheduled = now
                reason_bits.append(
                    "No further delay defined for attempt %d; retrying now."
                    % (attempt_number + 1)
                )

            if window.get("align_to") == "salary_credit_window":
                moved, applied = self.align_to_salary_window(scheduled)
                if applied:
                    scheduled = moved
                    rule = "%s.align_to -> salary_credit_window" % wpath
                    bounded.append("salary_credit_window")
                    reason_bits.append(
                        "Aligned to the salary-credit window: balances reload "
                        "on the 1st-7th, so that is where the recoverable "
                        "money actually is."
                    )

            # Never schedule before the cooldown has run out.
            last_attempt = (
                _parse(state.last_attempt_at) if state.last_attempt_at else failed_at
            )
            cooldown, cooldown_rule = self.cooldown_for(code)
            cooldown_end = last_attempt + cooldown
            if scheduled < cooldown_end:
                scheduled = cooldown_end
                bounded.append(cooldown_rule)
                reason_bits.append("Pushed out to respect the cooldown.")

            if is_sub:
                sub = self.compliance.get("subscription_rules", {})
                if sub.get("require_pre_debit_notification"):
                    hrs = float(sub.get("pre_debit_notification_hours", 0))
                    earliest = now + timedelta(hours=hrs)
                    if scheduled < earliest:
                        scheduled = earliest
                        reason_bits.append(
                            "Recurring mandate: held to the %.0fh pre-debit "
                            "notification period required for e-mandates." % hrs
                        )
                    bounded.append(
                        "compliance.subscription_rules.require_pre_debit_notification"
                    )

        if scheduled is not None and scheduled < now:
            scheduled = now

        return Decision(
            transaction_id=txn_id,
            action=action,
            reason=" ".join(reason_bits) or ("Rung %d: %s." % (step, action)),
            policy_rule_applied=rule,
            audit_decision=audit_decision,
            scheduled_time=scheduled.isoformat() if scheduled else None,
            channel=channel,
            escalation_step=step,
            bounded_by=bounded,
            rungs_passed_over=passed_over,
            issuer_degraded=is_degraded,
            requires_reconcile=bool(window.get("require_reconcile_before_retry")),
            customer_visible=bool(rung.get("customer_visible")),
            consumes=consumes,
            retry_foreclosed_by=retry_foreclosed_by,
        )

    # -- batch-level circuit breaker -------------------------------------

    def should_abort_batch(self, decline_rate):
        """Circuit breaker. True when live declines say we are making it worse."""
        threshold = self.limits.get("abort_batch_if_decline_rate_above")
        if threshold is None:
            return False, None
        if decline_rate > float(threshold):
            return True, "limits.abort_batch_if_decline_rate_above"
        return False, None


def load_policy(path="policy.yaml") -> Policy:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Policy(doc, source_path=str(path))


# -- audited entry points -------------------------------------------------

def decide_and_log(policy, audit, transaction, diagnosis, state=None,
                   batch_signals=None, now=None) -> Decision:
    """Decide, then write exactly one audit line. The production path.

    A decision NOT to act is still a decision and still gets a line --
    policy.yaml sets audit.log_skipped_decisions for exactly this reason.
    """
    d = policy.decide(transaction, diagnosis, state, batch_signals, now)
    audit.decision(
        transaction_id=d.transaction_id,
        decision=d.audit_decision,
        reason=d.reason,
        policy_rule_applied=d.policy_rule_applied,
        amount_paise=transaction.get("amount_paise"),
        failure_code=transaction.get("failure_code"),
        issuer_bank=transaction.get("issuer_bank"),
        attempt_number=transaction.get("attempt_number"),
        is_subscription=bool(transaction.get("is_subscription")),
        **d.audit_fields()
    )
    return d


def decide_batch(policy, audit, transactions, diagnoses, states=None,
                 batch_signals=None, now=None):
    """Decide for a whole batch. `audit` is required, not optional."""
    if audit is None:
        raise ValueError(
            "decide_batch requires an AuditLog: every decision goes through "
            "the trail, without exception."
        )
    states = states or {}
    out = {}
    for t in transactions:
        tid = t["transaction_id"]
        out[tid] = decide_and_log(
            policy, audit, t, diagnoses.get(tid), states.get(tid),
            batch_signals, now,
        )
    return out
