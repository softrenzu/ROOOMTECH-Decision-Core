from __future__ import annotations

import math
import time
from app.models import ChoiceScore, DecisionRequest, DecisionResponse, DecisionResult, DecisionSpec
from app.ml import LocalClassifierProvider
from app.providers import RulesProvider, OpenAICompatibleProvider


class DecisionEngine:
    def __init__(self):
        self.rules = RulesProvider()
        self.local = LocalClassifierProvider()
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
    def _blend(spec: DecisionSpec, baseline: dict[str, float], model: dict[str, float], model_weight: float = 0.8) -> dict[str, float]:
        a = DecisionEngine._normalize(spec, baseline)
        b = DecisionEngine._normalize(spec, model)
        mixed = {choice: (1 - model_weight) * a[choice] + model_weight * b[choice] for choice in spec.choices}
        return DecisionEngine._normalize(spec, mixed)

    def _result(self, spec: DecisionSpec, data: dict, provider: str) -> DecisionResult:
        scores = self._normalize(spec, data.get("scores", {}))
        selected, confidence, margin, entropy = self._metrics(scores)
        requires_review = confidence < spec.min_confidence or margin < spec.min_margin or entropy > spec.max_entropy
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
        local_data: dict[str, dict] = {}
        local_errors: dict[str, str] = {}
        model_data: dict[str, dict] = {}
        model_called = False

        if request.provider in {"auto", "local_classifier"}:
            local_specs = [spec for spec in request.decisions if spec.model_id]
            if request.provider == "local_classifier":
                missing = [spec.id for spec in request.decisions if not spec.model_id]
                if missing:
                    raise ValueError(f"decision {missing[0]} requires model_id for local_classifier provider")

            async def evaluate_local(spec: DecisionSpec):
                try:
                    return spec.id, await self.local.evaluate_one_async(spec.model_id, request.input, spec.choices), None
                except Exception as exc:
                    return spec.id, None, exc

            if local_specs:
                import asyncio
                local_rows = await asyncio.gather(*(evaluate_local(spec) for spec in local_specs))
                for spec_id, data, error in local_rows:
                    if error is None:
                        local_data[spec_id] = data
                    elif request.provider == "local_classifier":
                        raise error
                    else:
                        local_errors[spec_id] = type(error).__name__

        if request.provider == "openai_compatible":
            model_data = await self.model.evaluate(request.input, request.decisions)
            model_called = True
        elif request.provider == "auto" and self.model.configured:
            unresolved: list[DecisionSpec] = []
            for spec in request.decisions:
                baseline = local_data.get(spec.id, rules_data[spec.id])
                provider = "local_classifier" if spec.id in local_data else "rules"
                if self._result(spec, baseline, provider).requires_review:
                    unresolved.append(spec)
            if unresolved:
                model_data = await self.model.evaluate(request.input, unresolved)
                model_called = True

        results: list[DecisionResult] = []
        for spec in request.decisions:
            if request.provider == "rules":
                results.append(self._result(spec, rules_data[spec.id], "rules"))
                continue
            if request.provider == "local_classifier":
                results.append(self._result(spec, local_data[spec.id], "local_classifier"))
                continue
            if request.provider == "openai_compatible":
                data = model_data.get(spec.id, {"scores": {}})
                results.append(self._result(spec, data, "openai_compatible"))
                continue

            baseline = local_data.get(spec.id, rules_data[spec.id])
            baseline_provider = "local_classifier" if spec.id in local_data else "rules"
            if spec.id in model_data:
                merged = {
                    "scores": self._blend(spec, baseline.get("scores", {}), model_data[spec.id].get("scores", {})),
                    "evidence": list(dict.fromkeys(model_data[spec.id].get("evidence", []) + baseline.get("evidence", [])))[:3],
                    "reason_codes": ["LLM_FALLBACK", f"BASELINE_{baseline_provider.upper()}"] + model_data[spec.id].get("reason_codes", []),
                }
                results.append(self._result(spec, merged, f"hybrid_{baseline_provider}_llm"))
            else:
                data = dict(baseline)
                if spec.id in local_errors:
                    data["reason_codes"] = data.get("reason_codes", []) + [f"LOCAL_FALLBACK_{local_errors[spec.id]}"]
                results.append(self._result(spec, data, baseline_provider))

        latency_ms = (time.perf_counter() - started) * 1000
        return DecisionResponse(results=results, latency_ms=round(latency_ms, 3), model_called=model_called)
