from __future__ import annotations

import asyncio

from .local_classifier import LocalClassifierProvider as _BaseLocalClassifierProvider


class AdaptiveLocalClassifierProvider(_BaseLocalClassifierProvider):
    """Choose the low-latency scheduling strategy for the active accelerator.

    CUDA requests use the base provider's micro-batching and bounded GPU queue.
    CPU requests skip the batching collection window and run through a bounded
    thread gate, which avoids adding batch-wait latency to this lightweight model.
    """

    def __init__(self):
        super().__init__()
        self._cpu_direct_calls = 0
        self._cpu_direct_errors = 0

    def runtime_info(self) -> dict:
        info = super().runtime_info()
        scheduling = info.setdefault("inference_batching", {})
        scheduling["strategy"] = "cuda_microbatch_cpu_direct"
        scheduling["cpu_direct_calls"] = self._cpu_direct_calls
        scheduling["cpu_direct_errors"] = self._cpu_direct_errors
        return info

    async def evaluate_one_async(self, model_id: str, text: str, allowed_choices: list[str]) -> dict:
        device = self.resolve_device()
        if device == "cuda":
            return await super().evaluate_one_async(model_id, text, allowed_choices)

        try:
            async with self._cpu_gate:
                _, predictions = await asyncio.to_thread(
                    self.predict_many,
                    model_id,
                    [text],
                    device,
                )
            self._cpu_direct_calls += 1
            return self._prediction_for_choices(predictions[0], allowed_choices)
        except Exception:
            self._cpu_direct_errors += 1
            raise
