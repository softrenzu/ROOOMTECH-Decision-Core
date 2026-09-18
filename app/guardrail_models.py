from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
Action = Literal["allow", "review", "block"]
Severity = Literal["low", "medium", "high", "critical"]


class ToolCallInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_authorized: bool | None = None
    external_ref: str | None = Field(default=None, max_length=500)


class PolicyRule(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    description: str = Field(min_length=1, max_length=1000)
    severity: Severity = "medium"
    action: Action = "review"
    keywords: list[str] = Field(default_factory=list, max_length=100)
    semantic_property: str | None = Field(default=None, max_length=800)
    threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def require_signal(self):
        if not self.keywords and not self.semantic_property:
            raise ValueError("policy requires keywords or semantic_property")
        return self


class BuiltInGuardrails(BaseModel):
    prompt_injection: bool = True
    sensitive_data_patterns: bool = True
    credential_patterns: bool = True
    risky_tool_actions: bool = True


class GuardrailRequest(BaseModel):
    input_text: str | None = Field(default=None, max_length=100_000)
    output_text: str | None = Field(default=None, max_length=100_000)
    tool_call: ToolCallInput | None = None
    policies: list[PolicyRule] = Field(default_factory=list, max_length=50)
    builtins: BuiltInGuardrails = Field(default_factory=BuiltInGuardrails)
    semantic_provider: Literal["rules", "local_classifier", "openai_compatible", "auto"] = "rules"

    @model_validator(mode="after")
    def require_artifact(self):
        if not self.input_text and not self.output_text and self.tool_call is None:
            raise ValueError("guardrail request requires input_text, output_text, or tool_call")
        return self


class GuardrailFinding(BaseModel):
    id: str
    matched: bool
    probability: float = Field(ge=0.0, le=1.0)
    severity: Severity
    action: Action
    method: str
    evidence: list[str] = Field(default_factory=list)


class GuardrailResponse(BaseModel):
    action: Action
    allowed: bool
    requires_review: bool
    findings: list[GuardrailFinding]
    latency_ms: float
    note: str = "Guardrail findings are decision-support signals, not a security or compliance guarantee."


class ToolGateRequest(BaseModel):
    tool_call: ToolCallInput
    purpose: str | None = Field(default=None, max_length=2000)
    policies: list[PolicyRule] = Field(default_factory=list, max_length=50)
    allowed_tools: list[str] = Field(default_factory=list, max_length=500)
    blocked_tools: list[str] = Field(default_factory=list, max_length=500)
    require_explicit_authorization_for_risky_actions: bool = True


class RagPassage(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=50_000)


class RagClaim(BaseModel):
    id: str = Field(min_length=1, max_length=120, pattern=ID_PATTERN)
    claim: str = Field(min_length=1, max_length=10_000)
    citation_ids: list[str] = Field(min_length=1, max_length=50)


class RagVerificationRequest(BaseModel):
    query: str = Field(default="", max_length=10_000)
    answer: str = Field(min_length=1, max_length=100_000)
    passages: list[RagPassage] = Field(min_length=1, max_length=500)
    claims: list[RagClaim] = Field(min_length=1, max_length=200)
    pass_threshold: float = Field(default=0.24, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.10, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_thresholds_and_citations(self):
        if self.review_threshold > self.pass_threshold:
            raise ValueError("review_threshold must be <= pass_threshold")
        known = {item.id for item in self.passages}
        unknown = sorted({cid for claim in self.claims for cid in claim.citation_ids if cid not in known})
        if unknown:
            raise ValueError(f"unknown citation ids: {unknown[:10]}")
        return self


class RagClaimFinding(BaseModel):
    claim_id: str
    status: Literal["pass", "review", "fail"]
    lexical_support: float = Field(ge=0.0, le=1.0)
    citation_ids: list[str]
    evidence: list[str] = Field(default_factory=list)


class RagVerificationResponse(BaseModel):
    passed: bool
    requires_review: bool
    claim_findings: list[RagClaimFinding]
    context_injection_detected: bool
    injection_evidence: list[str] = Field(default_factory=list)
    latency_ms: float
    method: str = "local lexical support + deterministic context safety heuristics"
    note: str = "Lexical support is a fast screening signal and is not proof of entailment or factual correctness."
