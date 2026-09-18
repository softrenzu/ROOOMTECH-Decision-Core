# Multimodal decisions

ROOOMTECH Decision Core v0.4 accepts text, images, PDFs and audio through one decision endpoint.

## Endpoint

`POST /v1/multimodal/decide` uses `multipart/form-data`.

Fields:

- `text`: optional plain text.
- `decisions_json`: required JSON array using the normal `DecisionSpec` schema.
- `provider`: `auto`, `rules`, `local_classifier`, or `openai_compatible`.
- `image_weight`: weight assigned to vision scores when image and text-derived scores are fused. Default `0.55`.
- `files`: zero or more image, PDF or audio files.

Example:

```bash
curl -X POST http://localhost:8000/v1/multimodal/decide \
  -F 'text=The customer says the device stopped working.' \
  -F 'decisions_json=[{"id":"status","question":"What is the visible and reported condition?","choices":["normal","needs_attention","damaged"]}]' \
  -F 'files=@photo.jpg'
```

## Image + text

Images are evaluated locally with a configurable CLIP-compatible model through Transformers. No vision weights are bundled with this repository. Set `RTDC_VISION_MODEL` to a model you are independently authorized to use. Text and image probability distributions are then fused before confidence, margin and entropy gates are applied.

## PDF

PDFs are processed locally with PyMuPDF. Embedded text is extracted and routed through the normal decision engine. When a PDF contains little extractable text and a vision model is configured, up to `RTDC_MAX_PDF_VISION_PAGES` pages are rendered to images and evaluated through the vision path. This supports scanned/image-heavy PDFs without adding an OCR dependency.

## Audio

Audio is transcribed locally with faster-whisper, then the transcript is routed through the same text decision engine. The default model name is `tiny`; set `RTDC_AUDIO_MODEL` to another compatible model when appropriate. The temporary audio file is deleted immediately after transcription.

## Install

```bash
pip install -e '.[multimodal]'
```

Set a vision model before sending images:

```env
RTDC_VISION_MODEL=<authorized CLIP-compatible model id>
RTDC_VISION_DEVICE=auto
```

## Privacy and security

Raw uploaded files are held in memory except for a temporary audio file required by the transcription library. That temporary file is removed immediately after transcription. The API does not persist raw uploads by default.

Default upload limits are 20 MB per file and 50 MB total per request. Change them with `RTDC_MAX_FILE_MB` and `RTDC_MAX_TOTAL_UPLOAD_MB`.

Do not expose the service directly to untrusted networks without authentication, TLS, request-rate limits and malware/content scanning at the gateway.

## Independence / licensing

The multimodal layer does not use any third-party proprietary decision service, output or private API. It is a modality-normalization and score-fusion implementation built around general-purpose open interfaces. No third-party model weights are redistributed by this repository. Operators are responsible for verifying the license and acceptable-use terms for any external or downloaded model they configure.
