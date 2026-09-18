from __future__ import annotations

import math
import time
from app.models import ChoiceScore, DecisionRequest, DecisionResponse, DecisionResult, DecisionSpec
from app.providers import RulesProvider, OpenAICompatibleProvider


class DecisionEngine:
    def __init__(self):
        self.rules = RulesProvider()
        self.model = OpenAICompatibleProvider()

    @staticmethod
    def _normalize(spec: DecisionSpec, raw: dict[str, float]) -> dict[str, float]:
        cleaned = {choice: max(0.0, float(raw.get(choice, 0.0))) for choice in spec.choices}
        total = sum(cleaned.values())
        if total <= 0:
            return {choice: 1.0 / len(spec.choices) for choice in spec.choices}
        return {choice: value / total for choice, value in cleaned.items()}

    @staticmethod
    def _metrics(scores: dict[str, float]) -> tuple[str, float, float, float]:
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        selected, confidence = ordered[0]
        second = ordered[1][1] if len(ordered) > 1 else 0.0
        margin = confidence - second
        n = len(scores)
        entropy = -sum(p * math.log(p) for p in scores.values() if p > 0)
        normalized_entropy = entropy / math.log(n) if n > 1 else 0.0
        return selected, confidence, margin, normalized_entropy

    @staticmethod
    def _blend(spec: DecisionSpec, rules: dict[str, float], model: dict[str, float], model_weight: float = 0.8) -> dict[str, float]:
        r = DecisionEngine._normalize(spec, rules)
        m = DecisionEngine._normalize(spec, model)
        mixed = {choice: (1 - model_weight) * r[choice] + model_weight * m[choice] for choice in spec.choices}
        return DecisionEngine._normalize(spec, mixed)

    def _result(self, spec: DecisionSpec, data: dict, provider: str) -> DecisionResult:
        scores = self._normalize(spec, data.get("scores", {}))
        selected, confidence, margin, entropy = self._metrics(scores)
        requires_review = (
            confidence < spec.min_confidence
            or margin < spec.min_margin
            or entropy > spec.max_entropy
        )
        abstained = bool(spec.allow_abstain and requires_review)
        return DecisionResult(
            id=spec.id,
            selected=None if abstained else selected,
            scores=[ChoiceScore(choice=c, probability=round(scores[c], 6)) for c in spec.choices],
            confidence=round(confidence, 6),
            margin=round(margin, 6),
            entropy=round(entropy, 6),
            requires_review=requires_review,
            abstained=abstained,
            provider=provider,
            evidence=[str(x)[:240] for x in data.get("evidence", [])[:3]],
            reason_codes=[str(x)[:80] for x in data.get("reason_codes", [])[:10]],
        )

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        started = time.perf_counter()
        rules_data = self.rules.evaluate(request.input, request.decisions)
        model_called = False
        model_data: dict[str, dict] = {}

        if request.provider == "openai_compatible":
            model_data = await self.model.evaluate(request.input, request.decisions)
            model_called = True
        elif request.provider == "auto" and self.model.configured:
            weak_specs = []
            for spec in request.decisions:
                probe = self._result(spec, rules_data[spec.id], "rules")
                if probe.requires_review:
                    weak_specs.append(spec)
            if weak_specs:
                model_data = await self.model.evaluate(request.input, weak_specs)
                model_called = True

        results: list[DecisionResult] = []
        for spec in request.decisions:
            if request.provider == "rules":
                results.append(self._result(spec, rules_data[spec.id], "rules"))
                continue

            if spec.id in model_data:
                if request.provider == "auto":
                    merged = {
                        "scores": self._blend(spec, rules_data[spec.id]["scores"], model_data[spec.id].get("scores", {})),
                        "evidence": list(dict.fromkeys(model_data[spec.id].get("evidence", []) + rules_data[spec.id].get("evidence", [])))[:3],
                        "reason_codes": ["HYBRID_RULE_MODEL"] + model_data[spec.id].get("reason_codes", []),
                    }
                    results.append(self._result(spec, merged, "hybrid"))
                else:
                    results.append(self._result(spec, model_data[spec.id], "openai_compatible"))
            else:
                results.append(self._result(spec, rules_data[spec.id], "rules"))

        latency_ms = (time.perf_counter() - started) * 1000
        return DecisionResponse(results=results, latency_ms=round(latency_ms, 3), model_called=model_called)
