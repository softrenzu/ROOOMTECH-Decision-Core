from __future__ import annotations

import asyncio
import io
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.engine import DecisionEngine
from app.models import DecisionRequest, DecisionSpec, MultimodalDecisionResponse


IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/gif"}
PDF_MIMES = {"application/pdf"}
AUDIO_MIMES = {
    "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/x-m4a",
    "audio/ogg", "audio/webm", "audio/flac", "audio/aac",
}


@dataclass
class MediaFile:
    filename: str
    content_type: str
    data: bytes


def detect_media_kind(filename: str, content_type: str) -> str:
    mime = (content_type or "").lower().split(";", 1)[0].strip()
    suffix = Path(filename or "").suffix.lower()
    if mime in IMAGE_MIMES or suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
        return "image"
    if mime in PDF_MIMES or suffix == ".pdf":
        return "pdf"
    if mime in AUDIO_MIMES or suffix in {".wav", ".mp3", ".m4a", ".ogg", ".webm", ".flac", ".aac"}:
        return "audio"
    raise ValueError(f"unsupported media type: {content_type or suffix or 'unknown'}")


def average_scores(rows: list[dict[str, float]], choices: list[str]) -> dict[str, float]:
    if not rows:
        return {choice: 1.0 / len(choices) for choice in choices}
    merged = {choice: 0.0 for choice in choices}
    for row in rows:
        total = sum(max(0.0, float(row.get(choice, 0.0))) for choice in choices)
        if total <= 0:
            continue
        for choice in choices:
            merged[choice] += max(0.0, float(row.get(choice, 0.0))) / total
    denom = sum(merged.values())
    if denom <= 0:
        return {choice: 1.0 / len(choices) for choice in choices}
    return {choice: merged[choice] / denom for choice in choices}


class PDFExtractor:
    def extract(self, data: bytes, render_if_sparse: bool = False) -> tuple[str, int, list[bytes]]:
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PDF support is not installed; install with: pip install -e '.[multimodal]'") from exc

        max_pages = int(os.getenv("RTDC_MAX_PDF_PAGES", "50"))
        max_vision_pages = int(os.getenv("RTDC_MAX_PDF_VISION_PAGES", "6"))
        min_text_chars = int(os.getenv("RTDC_PDF_MIN_TEXT_CHARS", "100"))
        document = fitz.open(stream=data, filetype="pdf")
        pages = min(len(document), max_pages)
        texts: list[str] = []
        for index in range(pages):
            text = document[index].get_text("text")
            if text:
                texts.append(text)
        combined = "\n".join(texts).strip()

        rendered: list[bytes] = []
        if render_if_sparse and len(combined) < min_text_chars:
            for index in range(min(pages, max_vision_pages)):
                page = document[index]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
                rendered.append(pixmap.tobytes("png"))
        document.close()
        return combined, pages, rendered


class AudioTranscriber:
    def __init__(self):
        self.model_name = os.getenv("RTDC_AUDIO_MODEL", "tiny").strip() or "tiny"
        self.device = os.getenv("RTDC_AUDIO_DEVICE", "cpu").strip() or "cpu"
        self.compute_type = os.getenv("RTDC_AUDIO_COMPUTE_TYPE", "int8").strip() or "int8"
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("audio support is not installed; install with: pip install -e '.[multimodal]'") from exc
        self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, media: MediaFile) -> tuple[str, float]:
        model = self._load()
        suffix = Path(media.filename or "audio.bin").suffix or ".bin"
        path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(media.data)
                path = handle.name
            segments, info = model.transcribe(path, vad_filter=True)
            text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
            duration = float(getattr(info, "duration", 0.0) or 0.0)
            return text, duration
        finally:
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass


class VisionScorer:
    def __init__(self):
        self.model_name = os.getenv("RTDC_VISION_MODEL", "").strip()
        self.requested_device = os.getenv("RTDC_VISION_DEVICE", "auto").strip().lower() or "auto"
        self._model = None
        self._processor = None
        self._device = None

    @property
    def configured(self) -> bool:
        return bool(self.model_name)

    def _load(self):
        if not self.configured:
            raise RuntimeError("image decision requires RTDC_VISION_MODEL to be set")
        if self._model is not None:
            return self._model, self._processor, self._device
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
        except ImportError as exc:
            raise RuntimeError("vision support is not installed; install with: pip install -e '.[multimodal]'") from exc
        if self.requested_device == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("RTDC_VISION_DEVICE=cuda but CUDA is unavailable")
            device = "cuda"
        elif self.requested_device == "cpu":
            device = "cpu"
        else:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        processor = CLIPProcessor.from_pretrained(self.model_name)
        model = CLIPModel.from_pretrained(self.model_name).to(device)
        model.eval()
        self._model, self._processor, self._device = model, processor, device
        return model, processor, device

    def score(self, image_bytes: bytes, spec: DecisionSpec) -> dict[str, float]:
        try:
            import torch
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("vision support is not installed; install with: pip install -e '.[multimodal]'") from exc
        model, processor, device = self._load()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        prompts = [f"{spec.question}. Choice: {choice}" for choice in spec.choices]
        inputs = processor(text=prompts, images=image, return_tensors="pt", padding=True)
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.inference_mode():
            logits = model(**inputs).logits_per_image[0]
            probabilities = torch.softmax(logits, dim=0).detach().cpu().tolist()
        return {choice: float(probabilities[index]) for index, choice in enumerate(spec.choices)}


class MultimodalDecisionEngine:
    def __init__(self, decision_engine: DecisionEngine):
        self.engine = decision_engine
        self.pdf = PDFExtractor()
        self.audio = AudioTranscriber()
        self.vision = VisionScorer()

    async def decide(
        self,
        text: str,
        decisions: list[DecisionSpec],
        provider: str,
        files: list[MediaFile],
        image_weight: float = 0.55,
    ) -> MultimodalDecisionResponse:
        started = time.perf_counter()
        text_parts: list[str] = []
        modalities: set[str] = set()
        images: list[bytes] = []
        pdf_pages = 0
        pdf_text_chars = 0
        transcript_chars = 0
        audio_seconds = 0.0

        if text.strip():
            text_parts.append(text.strip())
            modalities.add("text")

        for media in files:
            kind = detect_media_kind(media.filename, media.content_type)
            modalities.add(kind)
            if kind == "image":
                images.append(media.data)
            elif kind == "pdf":
                pdf_text, pages, rendered = await asyncio.to_thread(
                    self.pdf.extract, media.data, self.vision.configured
                )
                pdf_pages += pages
                pdf_text_chars += len(pdf_text)
                if pdf_text:
                    text_parts.append(f"[PDF:{media.filename}]\n{pdf_text}")
                images.extend(rendered)
            elif kind == "audio":
                transcript, duration = await asyncio.to_thread(self.audio.transcribe, media)
                audio_seconds += duration
                transcript_chars += len(transcript)
                if transcript:
                    text_parts.append(f"[AUDIO:{media.filename}]\n{transcript}")

        fused_text = "\n\n".join(text_parts).strip()
        if not fused_text and not images:
            raise ValueError("no usable text, image, PDF text, or audio transcript was produced")

        base_response = None
        if fused_text:
            base_response = await self.engine.decide(
                DecisionRequest(input=fused_text[:100_000], decisions=decisions, provider=provider)
            )
        base_by_id = {item.id: item for item in (base_response.results if base_response else [])}

        image_by_id: dict[str, dict[str, float]] = {}
        vision_called = False
        if images:
            if not self.vision.configured:
                raise RuntimeError("image input was supplied but RTDC_VISION_MODEL is not configured")
            for spec in decisions:
                rows = [
                    await asyncio.to_thread(self.vision.score, image_bytes, spec)
                    for image_bytes in images
                ]
                image_by_id[spec.id] = average_scores(rows, spec.choices)
            vision_called = True

        results = []
        for spec in decisions:
            base = base_by_id.get(spec.id)
            image_scores = image_by_id.get(spec.id)
            if base is not None and image_scores is not None:
                base_scores = {item.choice: item.probability for item in base.scores}
                mixed = self.engine._blend(spec, base_scores, image_scores, model_weight=image_weight)
                data = {
                    "scores": mixed,
                    "evidence": base.evidence,
                    "reason_codes": list(dict.fromkeys(base.reason_codes + ["MULTIMODAL_IMAGE_TEXT_FUSION"])),
                }
                results.append(self.engine._result(spec, data, f"multimodal_{base.provider}_vision"))
            elif image_scores is not None:
                results.append(self.engine._result(
                    spec,
                    {"scores": image_scores, "reason_codes": ["VISION_ZERO_SHOT"]},
                    "vision_classifier",
                ))
            elif base is not None:
                results.append(base)
            else:
                raise RuntimeError(f"decision {spec.id} has no usable modality")

        latency_ms = (time.perf_counter() - started) * 1000
        return MultimodalDecisionResponse(
            results=results,
            latency_ms=round(latency_ms, 3),
            model_called=bool(base_response.model_called) if base_response else False,
            vision_model_called=vision_called,
            modalities=sorted(modalities),
            files_processed=len(files),
            image_count=len(images),
            pdf_pages=pdf_pages,
            pdf_text_chars=pdf_text_chars,
            audio_seconds=round(audio_seconds, 3),
            transcript_chars=transcript_chars,
            fused_text_chars=len(fused_text),
            raw_files_persisted=False,
        )
