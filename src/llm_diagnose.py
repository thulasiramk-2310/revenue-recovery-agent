"""LLM classification of failure codes the taxonomy does not know.

WHAT THIS IS FOR
----------------
src/diagnose.py maps a gateway failure code to a severity, a retryability and
a recoverability prior. That mapping is a hand-written dictionary, so it only
knows the eight codes this project planted. A real merchant stream carries
codes nobody wrote a branch for, and today every one of them collapses to
UNKNOWN: one conservative retry, then a human.

This module asks a model to place an unfamiliar code into the EXISTING
taxonomy, so an unmapped `ISSUER_UNAVAILABLE_TRY_LATER` can be recognised as
the ISSUER_DOWN-shaped thing it obviously is instead of being treated as a
total mystery.

WHAT IT IS NOT FOR, AND THE CONTAINMENT THAT ENFORCES THAT
-----------------------------------------------------------
The model proposes a DIAGNOSIS. It never chooses an action, never sets a
delay, never touches a limit. policy.py remains the only thing that decides
what happens, and policy.yaml remains the only place bounds live. Four
structural properties keep that true, and each has a test:

1. CLOSED OUTPUT SET. The only thing accepted back is one code that already
   exists in FAILURE_TAXONOMY. A code that is not in the taxonomy is
   rejected. The model cannot invent a severity, a retry count or a policy
   entry, because it cannot return anything the policy did not already
   define.

2. BLAST RADIUS IS EXACTLY THE UNKNOWN. Only codes MISSING from the taxonomy
   are ever sent. A model proposal can never override, soften or relabel a
   code the project already understands, so the worst case is that it does a
   poor job on a case whose current handling is an admitted guess.

3. FAIL CLOSED, ALWAYS. Every failure mode -- unmapped proposal, malformed
   output, HTTP error, timeout, missing credentials -- returns None, and None
   means UNKNOWN_DIAGNOSIS: the conservative path that policy.yaml already
   defined for its own reasons, before any of this existed. An LLM timeout
   must never become permission.

4. PROVENANCE ON EVERY OUTCOME. Accepted or rejected, a line goes to the
   audit trail carrying the model id, the prompt version, the raw response
   and the reason for the verdict. replay.py can therefore still rebuild the
   run, and a reviewer six months from now can tell a model that changed from
   a prompt that changed -- which have completely different fixes.

The HTTP client is stdlib urllib, matching src/execute.py, so retries,
timeouts and latency stay visible here rather than inside a vendor library.
No new dependency.
"""

from __future__ import annotations

import inspect
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Verdicts. Every call produces exactly one of these in the trail.
ACCEPTED = "accepted"
REJECTED_UNKNOWN_CODE = "rejected_code_not_in_taxonomy"
REJECTED_LOW_CONFIDENCE = "rejected_below_confidence_floor"
REJECTED_MALFORMED = "rejected_malformed_response"
FAILED_NO_CREDENTIALS = "failed_no_credentials"
FAILED_TRANSPORT = "failed_transport_error"
FAILED_TIMEOUT = "failed_timeout"
DISABLED = "disabled_by_policy"

# Verdicts that mean "we learned nothing". All of them route to UNKNOWN.
NON_ACCEPTING = frozenset({
    REJECTED_UNKNOWN_CODE, REJECTED_LOW_CONFIDENCE, REJECTED_MALFORMED,
    FAILED_NO_CREDENTIALS, FAILED_TRANSPORT, FAILED_TIMEOUT, DISABLED,
})

PROMPT = """You classify payment failure codes for an Indian payment gateway.

A transaction failed with a code that is not in our taxonomy. Map it to the \
single closest code from this CLOSED list, or answer UNSURE.

Allowed codes:
{codes}

Unmapped code: {code}
Gateway message: {message}
Issuer bank: {bank}

Reply with ONLY a JSON object, no prose and no code fence:
{{"code": "<one allowed code, or UNSURE>", "confidence": <0.0-1.0>, \
"reasoning": "<one short sentence>"}}

Answer UNSURE when the code could plausibly map to several of the allowed \
codes, or to none of them. UNSURE is the correct answer when you are \
guessing; a wrong confident mapping is worse than no mapping, because a \
wrong mapping can authorise retries against an account that will never \
settle."""


@dataclass
class Proposal:
    """One classification attempt. Always produced, even when nothing worked.

    `code` is None unless the verdict is ACCEPTED, so a caller cannot
    accidentally use a rejected proposal by reading the field without
    checking the verdict.
    """
    verdict: str
    code: Optional[str] = None
    confidence: float = 0.0
    reasoning: str = ""
    original_code: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    raw_response: Optional[str] = None
    latency_ms: int = 0
    error: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.verdict == ACCEPTED and self.code is not None

    def audit_fields(self) -> dict:
        return {
            "verdict": self.verdict,
            "proposed_code": self.code,
            "original_code": self.original_code,
            "confidence": round(float(self.confidence), 4),
            "reasoning": self.reasoning,
            "provider": self.provider,
            "model": self.model,
            # The prompt version is the field that makes this replayable.
            # Without it a trail records WHICH code was proposed but not the
            # reasoning that produced it, and a model change and a prompt
            # change become indistinguishable after the fact.
            "prompt_version": self.prompt_version,
            "raw_response": self.raw_response,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def load_api_key(env_path=".env", provider="anthropic"):
    """Read the provider API key. Returns None when absent -- never raises.

    Absence is an ordinary state, not an error: the whole system runs without
    a key and simply routes unmapped codes to UNKNOWN, which is what it did
    before this module existed.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:
        p = Path(env_path)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(
                        k.strip(), v.strip().strip('"').strip("'"))
    env_name = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    return (os.environ.get(env_name) or "").strip() or None


def _accepts_provider(fn):
    """Whether a transport takes the six-argument (provider-aware) form.

    Asked of the SIGNATURE rather than discovered by calling and catching
    TypeError. Catching it cannot tell "this callable has five parameters"
    apart from "this callable has a bug that raised TypeError", and the
    retry-on-TypeError version of this did the second one silently: a real
    error inside a six-argument transport was swallowed, replaced with a
    misleading arity message, and re-raised out of propose -- which would
    crash a batch instead of degrading to UNKNOWN, in the one module whose
    entire purpose is failing closed.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True          # builtins and C callables: assume current form
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values()):
        return True          # *args accepts either
    return len([p for p in params.values()
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD)]) >= 6


def _extract_json(text):
    """Pull the first JSON object out of a response. None if there isn't one.

    Deliberately tolerant of a code fence or a stray sentence, because those
    are formatting noise rather than a wrong answer. Not tolerant of anything
    that fails to parse or is not an object -- that is malformed, and
    malformed fails closed.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _call_anthropic(prompt, api_key, model, timeout, max_tokens):
    """POST to Anthropic Messages. Returns (text, error). Never raises."""
    body = json.dumps({
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(ANTHROPIC_API_URL, data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("anthropic-version", ANTHROPIC_API_VERSION)
    req.add_header("x-api-key", api_key)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Read the body for the message but never echo a header: the API key
        # travels in one.
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            detail = ""
        return None, "http_%s %s" % (e.code, detail)
    except TimeoutError:
        return None, "timeout"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return None, "timeout"
        return None, "transport: %s" % reason
    except ValueError as e:
        return None, "unparseable api envelope: %s" % e

    try:
        parts = payload.get("content") or []
        return "".join(p.get("text", "") for p in parts), None
    except (AttributeError, TypeError) as e:
        return None, "unexpected api envelope: %s" % e


def _call_groq(prompt, api_key, model, timeout, max_tokens):
    """POST to Groq's OpenAI-compatible chat endpoint. Never raises."""
    body = json.dumps({
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(GROQ_API_URL, data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", "Bearer " + api_key)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            detail = ""
        return None, "http_%s %s" % (e.code, detail)
    except TimeoutError:
        return None, "timeout"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return None, "timeout"
        return None, "transport: %s" % reason
    except ValueError as e:
        return None, "unparseable api envelope: %s" % e

    try:
        choices = payload.get("choices") or []
        return choices[0]["message"].get("content", ""), None
    except (IndexError, KeyError, AttributeError, TypeError) as e:
        return None, "unexpected api envelope: %s" % e


def _call_api(prompt, api_key, model, timeout, max_tokens, provider="anthropic"):
    """Provider router. Returns (text, error). Never raises."""
    if provider == "groq":
        return _call_groq(prompt, api_key, model, timeout, max_tokens)
    return _call_anthropic(prompt, api_key, model, timeout, max_tokens)


def propose(failure_code, allowed_codes, config, gateway_message="",
            issuer_bank="", api_key=None, env_path=".env", _transport=None):
    """Propose a taxonomy code for an unmapped failure code.

    Returns a Proposal, always. Check `.accepted` before using `.code`; a
    non-accepted proposal means the caller should use UNKNOWN_DIAGNOSIS.

    `_transport` is a seam for tests: a callable with the same contract as
    _call_api. Tests drive every failure path through it rather than mocking
    the network, so the fail-closed behaviour is exercised for real.
    """
    cfg = config or {}
    provider = str(cfg.get("provider", "anthropic")).lower()
    model = cfg.get("model")
    prompt_version = cfg.get("prompt_version")

    def result(verdict, **kw):
        return Proposal(verdict=verdict, original_code=failure_code,
                        provider=provider, model=model,
                        prompt_version=prompt_version, **kw)

    if not cfg.get("enabled"):
        return result(DISABLED)

    key = api_key if api_key is not None else load_api_key(
        env_path, provider=provider)
    if not key:
        env_name = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
        return result(FAILED_NO_CREDENTIALS,
                      error="%s not set" % env_name)

    allowed = sorted(allowed_codes)
    prompt = PROMPT.format(codes="\n".join("  - " + c for c in allowed),
                           code=failure_code,
                           message=gateway_message or "(none supplied)",
                           bank=issuer_bank or "(unknown)")

    call = _transport or _call_api
    timeout = float(cfg.get("timeout_seconds", 8))
    max_tokens = int(cfg.get("max_output_tokens", 300))
    args = ((prompt, key, model, timeout, max_tokens, provider)
            if _accepts_provider(call)
            else (prompt, key, model, timeout, max_tokens))

    started = time.time()
    try:
        text, error = call(*args)
    except Exception as e:
        # The contract is that propose ALWAYS returns a Proposal. A transport
        # that raises -- for any reason, including a bug of its own -- is one
        # more way of learning nothing, and learning nothing means UNKNOWN.
        # Letting it propagate would take down a whole batch run over a
        # classification that was never load-bearing.
        return result(FAILED_TRANSPORT,
                      error="transport raised %s: %s" % (type(e).__name__, e),
                      latency_ms=int((time.time() - started) * 1000))
    latency = int((time.time() - started) * 1000)

    if error:
        verdict = FAILED_TIMEOUT if "timeout" in error else FAILED_TRANSPORT
        return result(verdict, error=error, latency_ms=latency,
                      raw_response=text)

    obj = _extract_json(text)
    if obj is None or "code" not in obj:
        return result(REJECTED_MALFORMED, raw_response=text,
                      latency_ms=latency,
                      error="no JSON object with a 'code' field")

    proposed = obj.get("code")
    reasoning = str(obj.get("reasoning", ""))[:300]
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        return result(REJECTED_MALFORMED, raw_response=text,
                      latency_ms=latency,
                      error="confidence was not a number")

    # CLOSED OUTPUT SET. UNSURE lands here too, which is the intended way for
    # the model to decline rather than guess.
    if proposed not in allowed_codes:
        return result(REJECTED_UNKNOWN_CODE, raw_response=text,
                      confidence=confidence, reasoning=reasoning,
                      latency_ms=latency,
                      error="%r is not in the taxonomy" % (proposed,))

    floor = float(cfg.get("min_confidence", 0.0))
    if confidence < floor:
        return result(REJECTED_LOW_CONFIDENCE, raw_response=text,
                      confidence=confidence, reasoning=reasoning,
                      latency_ms=latency,
                      error="confidence %.2f below floor %.2f"
                            % (confidence, floor))

    return result(ACCEPTED, code=proposed, confidence=confidence,
                  reasoning=reasoning, raw_response=text, latency_ms=latency)


def resolve_unmapped(transactions, taxonomy_codes, config, audit=None,
                     env_path=".env", api_key=None, _transport=None):
    """Classify every unmapped code in a batch. Returns {code: Proposal}.

    Keyed by CODE, not by transaction: the same unfamiliar code appearing on
    forty payments is one question, asked once. That keeps cost and latency
    proportional to the number of unknown codes rather than the batch size,
    and it guarantees the batch cannot diagnose two identical codes
    differently.

    Every outcome is logged, accepted or not. A rejection is the more
    interesting audit line of the two.
    """
    known = set(taxonomy_codes)
    unmapped = []
    for t in transactions:
        code = t.get("failure_code")
        if code not in known and code not in [u[0] for u in unmapped]:
            unmapped.append((code, t.get("gateway_message", ""),
                             t.get("issuer_bank", "")))

    proposals = {}
    for code, message, bank in unmapped:
        p = propose(code, known, config, gateway_message=message,
                    issuer_bank=bank, api_key=api_key, env_path=env_path,
                    _transport=_transport)
        proposals[code] = p
        if audit is not None:
            audit.event("llm_diagnosis_proposed", **p.audit_fields())
    return proposals
