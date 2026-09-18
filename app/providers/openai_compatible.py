from __future__ import annotations

import json
import os
import httpx
from app.models import DecisionSpec


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self):
        self.base_url = os.getenv("RTDC_MODEL_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("RTDC_MODEL_API_KEY", "")
        self.model = os.getenv("RTDC_MODEL_NAME", "")
        self.timeout = float(os.getenv("RTDC_MODEL_TIMEOUT_SECONDS", "20"))
        self.json_mode = os.getenv("RTDC_JSON_MODE", "true").lower() == "true"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    async def evaluate(self, text: str, decisions: list[DecisionSpec]) -> dict[str, dict]:
        if not self.configured:
            raise RuntimeError("OpenAI-compatible provider is not configured")

        schema = [
            {"id": d.id, "question": d.question, "choices": d.choices}
            for d in decisions
        ]
        system = (
            "You are a structured classifier. Return JSON only. "
            "For each decision id, provide a probability for every allowed choice. "
            "Probabilities must be non-negative and should sum to 1. "
            "Also return up to 3 short verbatim evidence snippets from the input and short reason_codes. "
            "Do not provide chain-of-thought, hidden reasoning, explanations, or prose outside JSON."
        )
        user = json.dumps({"input": text, "decisions": schema}, ensure_ascii=False)
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        parsed = json.loads(content)
        rows = parsed.get("decisions", parsed.get("results", []))
        if isinstance(rows, dict):
            rows = [{"id": key, **value} for key, value in rows.items()]
        return {
            row["id"]: {
                "scores": row.get("scores", {}),
                "evidence": row.get("evidence", [])[:3],
                "reason_codes": row.get("reason_codes", ["MODEL_RESULT"]),
            }
            for row in rows
            if isinstance(row, dict) and row.get("id")
        }
