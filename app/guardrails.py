from __future__ import annotations

import json
import re
import time
from typing import Iterable

from app.guardrail_models import (
    GuardrailFinding,
    GuardrailRequest,
    GuardrailResponse,
    PolicyRule,
    RagClaimFinding,
    RagVerificationRequest,
    RagVerificationResponse,
    ToolGateRequest,
)
from app.ml.local_classifier import normalize_text
from app.operation_models import DetectionRequest


_PROMPT_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "reveal the system prompt",
    "show the system prompt",
    "developer message",
    "system message",
    "jailbreak",
    "以前の指示を無視",
    "これまでの指示を無視",
    "システムプロンプトを表示",
    "システムプロンプトを教えて",
]

_RISKY_TOOL_PATTERNS = [
    "delete",
    "remove",
    "drop",
    "transfer",
    "wire",
    "payment",
    "purchase",
    "refund",
    "send_email",
    "send_message",
    "shell",
    "exec",
    "command",
    "chmod",
    "credential",
    "password",
    "削除",
    "送金",
    "振込",
    "決済",
    "購入",
    "返金",
    "メール送信",
    "実行",
]

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?81[- ]?)?(?:0\d{1,4}[- ]?\d{1,4}[- ]?\d{3,4})(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SECRET_RES = [
    re.compile(r"(?i)\b(?:bearer\s+)[A-Za-z0-9._~+/-]{16,}={0,2}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password)\s*[:=]\s*[^\s,;]{8,}"),
]

_ACTION_ORDER = {"allow": 0, "review": 1, "block": 2}


def _truncate(value: str, limit: int = 160) -> str:
    return value[:limit] + ("…" if len(value) > limit else "")


def _keyword_matches(text: str, keywords: Iterable[str]) -> list[str]:
    normalized = normalize_text(text)
    found: list[str] = []
    for keyword in keywords:
        key = normalize_text(keyword)
        if key and key in normalized:
            found.append(keyword)
    return found[:10]


def _injection_matches(text: str) -> list[str]:
    return _keyword_matches(text, _PROMPT_INJECTION_PATTERNS)


def _risky_matches(text: str) -> list[str]:
    return _keyword_matches(text, _RISKY_TOOL_PATTERNS)


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    normalized = normalize_text(text)
    if not normalized:
        return set()
    if len(normalized) < n:
        return {normalized}
    return {normalized[i : i + n] for i in range(len(normalized) - n + 1)}


def _lexical_support(claim: str, context: str) -> float:
    claim_grams = _char_ngrams(claim)
    if not claim_grams:
        return 0.0
    context_grams = _char_ngrams(context)
    return min(1.0, len(claim_grams & context_grams) / len(claim_grams))


class GuardrailEngine:
    """Independent policy enforcement layer for AI inputs, outputs and tool calls.

    Built-in pattern checks are deterministic screening signals. Custom semantic rules can
    optionally use RTDC's existing decision providers. Results are intentionally expressed
    as allow/review/block rather than as a security guarantee.
    """

    def __init__(self, operations_engine):
        self.operations = operations_engine

    @staticmethod
    def _artifact(request: GuardrailRequest) -> str:
        parts: list[str] = []
        if request.input_text:
            parts.append("INPUT:\n" + request.input_text)
        if request.output_text:
            parts.append("OUTPUT:\n" + request.output_text)
        if request.tool_call:
            parts.append(
                "TOOL_CALL:\n"
                + request.tool_call.name
                + "\n"
                + json.dumps(request.tool_call.arguments, ensure_ascii=False, sort_keys=True)
            )
        return "\n\n".join(parts)

    @staticmethod
    def _builtin_finding(
        finding_id: str,
        matched: bool,
        severity: str,
        action: str,
        evidence: list[str],
        method: str = "deterministic_heuristic",
    ) -> GuardrailFinding:
        return GuardrailFinding(
            id=finding_id,
            matched=matched,
            probability=1.0 if matched else 0.0,
            severity=severity,
            action=action if matched else "allow",
            method=method,
            evidence=evidence[:10],
        )

    async def _custom_policy(self, artifact: str, policy: PolicyRule, provider: str) -> GuardrailFinding:
        matches = _keyword_matches(artifact, policy.keywords)
        if matches:
            return GuardrailFinding(
                id=policy.id,
                matched=True,
                probability=1.0,
                severity=policy.severity,
                action=policy.action,
                method="keyword_policy",
                evidence=[f"keyword:{_truncate(item)}" for item in matches],
            )
        if policy.semantic_property:
            try:
                response = await self.operations.detect(
                    DetectionRequest(
                        input=artifact,
                        property=policy.semantic_property,
                        provider=provider,
                        model_id=policy.model_id,
                        threshold=policy.threshold,
                        review_below=min(policy.threshold, 0.65),
                        keywords=policy.keywords,
                    )
                )
                matched = response.probability >= policy.threshold
                action = policy.action if matched else ("review" if response.requires_review else "allow")
                return GuardrailFinding(
                    id=policy.id,
                    matched=matched,
                    probability=response.probability,
                    severity=policy.severity,
                    action=action,
                    method=f"semantic:{response.result.provider}",
                    evidence=response.result.evidence,
                )
            except Exception as exc:
                return GuardrailFinding(
                    id=policy.id,
                    matched=False,
                    probability=0.0,
                    severity=policy.severity,
                    action="review",
                    method="semantic_error",
                    evidence=[f"{type(exc).__name__}: {_truncate(str(exc))}"],
                )
        return GuardrailFinding(
            id=policy.id,
            matched=False,
            probability=0.0,
            severity=policy.severity,
            action="allow",
            method="keyword_policy",
            evidence=[],
        )

    async def evaluate(self, request: GuardrailRequest) -> GuardrailResponse:
        started = time.perf_counter()
        artifact = self._artifact(request)
        findings: list[GuardrailFinding] = []

        if request.builtins.prompt_injection:
            hits = _injection_matches(artifact)
            findings.append(self._builtin_finding("prompt_injection", bool(hits), "high", "review", [f"pattern:{_truncate(x)}" for x in hits]))

        if request.builtins.sensitive_data_patterns:
            evidence: list[str] = []
            if _EMAIL_RE.search(artifact):
                evidence.append("email-like pattern")
            if _PHONE_RE.search(artifact):
                evidence.append("phone-like pattern")
            if _CARD_RE.search(artifact):
                evidence.append("long numeric/payment-card-like pattern")
            findings.append(self._builtin_finding("sensitive_data_pattern", bool(evidence), "medium", "review", evidence))

        if request.builtins.credential_patterns:
            evidence = ["credential-like pattern" for regex in _SECRET_RES if regex.search(artifact)]
            findings.append(self._builtin_finding("credential_exposure", bool(evidence), "critical", "block", evidence))

        if request.builtins.risky_tool_actions and request.tool_call:
            tool_text = request.tool_call.name + " " + json.dumps(request.tool_call.arguments, ensure_ascii=False)
            hits = _risky_matches(tool_text)
            findings.append(self._builtin_finding("risky_tool_action", bool(hits), "high", "review", [f"pattern:{_truncate(x)}" for x in hits]))

        for policy in request.policies:
            findings.append(await self._custom_policy(artifact, policy, request.semantic_provider))

        action = "allow"
        for finding in findings:
            if _ACTION_ORDER[finding.action] > _ACTION_ORDER[action]:
                action = finding.action
        elapsed = (time.perf_counter() - started) * 1000.0
        return GuardrailResponse(
            action=action,
            allowed=action == "allow",
            requires_review=action == "review",
            findings=findings,
            latency_ms=round(elapsed, 3),
        )

    async def gate_tool(self, request: ToolGateRequest) -> GuardrailResponse:
        policies = list(request.policies)
        tool = request.tool_call
        extra_findings: list[GuardrailFinding] = []
        if request.allowed_tools and tool.name not in request.allowed_tools:
            extra_findings.append(self._builtin_finding("tool_not_allowlisted", True, "high", "block", [tool.name], "tool_policy"))
        if tool.name in request.blocked_tools:
            extra_findings.append(self._builtin_finding("tool_blocklisted", True, "critical", "block", [tool.name], "tool_policy"))

        tool_text = tool.name + " " + json.dumps(tool.arguments, ensure_ascii=False)
        risky = _risky_matches(tool_text)
        if request.require_explicit_authorization_for_risky_actions and risky and tool.user_authorized is not True:
            extra_findings.append(
                self._builtin_finding(
                    "risky_action_not_explicitly_authorized",
                    True,
                    "critical",
                    "block",
                    [f"pattern:{_truncate(x)}" for x in risky],
                    "authorization_policy",
                )
            )

        base = await self.evaluate(
            GuardrailRequest(
                input_text=request.purpose,
                tool_call=tool,
                policies=policies,
            )
        )
        findings = extra_findings + base.findings
        action = "allow"
        for finding in findings:
            if _ACTION_ORDER[finding.action] > _ACTION_ORDER[action]:
                action = finding.action
        return GuardrailResponse(
            action=action,
            allowed=action == "allow",
            requires_review=action == "review",
            findings=findings,
            latency_ms=base.latency_ms,
        )

    def verify_rag(self, request: RagVerificationRequest) -> RagVerificationResponse:
        started = time.perf_counter()
        passages = {item.id: item.text for item in request.passages}
        context_text = "\n".join(passages.values())
        injection_hits = _injection_matches(context_text)
        findings: list[RagClaimFinding] = []
        for claim in request.claims:
            cited = "\n".join(passages[cid] for cid in claim.citation_ids)
            score = _lexical_support(claim.claim, cited)
            if score >= request.pass_threshold:
                status = "pass"
            elif score >= request.review_threshold:
                status = "review"
            else:
                status = "fail"
            evidence = [f"lexical_support={score:.3f}"]
            findings.append(
                RagClaimFinding(
                    claim_id=claim.id,
                    status=status,
                    lexical_support=round(score, 6),
                    citation_ids=claim.citation_ids,
                    evidence=evidence,
                )
            )
        elapsed = (time.perf_counter() - started) * 1000.0
        return RagVerificationResponse(
            passed=all(item.status == "pass" for item in findings) and not injection_hits,
            requires_review=bool(injection_hits) or any(item.status != "pass" for item in findings),
            claim_findings=findings,
            context_injection_detected=bool(injection_hits),
            injection_evidence=[f"pattern:{_truncate(item)}" for item in injection_hits],
            latency_ms=round(elapsed, 3),
        )
