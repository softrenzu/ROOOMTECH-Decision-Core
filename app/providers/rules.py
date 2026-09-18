from __future__ import annotations

from collections import defaultdict
from app.models import DecisionSpec


class RulesProvider:
    name = "rules"

    def evaluate(self, text: str, decisions: list[DecisionSpec]) -> dict[str, dict]:
        text_lower = text.lower()
        output: dict[str, dict] = {}
        for spec in decisions:
            raw = defaultdict(float)
            evidence: list[str] = []
            for choice in spec.choices:
                for keyword in spec.keywords.get(choice, []):
                    if keyword.lower() in text_lower:
                        raw[choice] += 1.0
                        if keyword not in evidence and len(evidence) < 5:
                            evidence.append(keyword)
            total = sum(raw.values())
            if total == 0:
                scores = {choice: 1.0 / len(spec.choices) for choice in spec.choices}
                reason_codes = ["NO_RULE_MATCH"]
            else:
                # Add small smoothing so probabilities remain defined for all choices.
                smoothed = {choice: raw[choice] + 0.05 for choice in spec.choices}
                denom = sum(smoothed.values())
                scores = {choice: value / denom for choice, value in smoothed.items()}
                reason_codes = ["RULE_MATCH"]
            output[spec.id] = {
                "scores": scores,
                "evidence": evidence,
                "reason_codes": reason_codes,
            }
        return output
