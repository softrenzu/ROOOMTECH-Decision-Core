from __future__ import annotations

import hmac
import json
import os
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.calibration import CalibrationEngine
from app.review_store import ReviewStore
from app.studio_models import (
    CalibrationRequest,
    CalibrationResponse,
    GovernedDecisionRequest,
    GovernedDecisionResponse,
    ReviewCreate,
    ReviewItem,
    ReviewResolve,
)


class StudioServices:
    def __init__(self, engine):
        self.engine = engine
        self.calibration = CalibrationEngine()
        self.reviews = ReviewStore()

    async def governed_decide(self, request: GovernedDecisionRequest) -> GovernedDecisionResponse:
        response = await self.engine.decide(request.decision)
        review_ids: list[str] = []
        for result in response.results:
            needs_review = (
                (request.policy.review_if_requires_review and result.requires_review)
                or (request.policy.review_if_abstained and result.abstained)
                or result.confidence < request.policy.review_below_confidence
            )
            if not needs_review:
                continue
            review = self.reviews.create(
                ReviewCreate(
                    source="governed_decision",
                    decision_id=result.id,
                    input_text=request.decision.input,
                    payload={"metadata": request.decision.metadata},
                    model_output=result.model_dump(mode="json"),
                    suggested_label=result.selected,
                    confidence=result.confidence,
                    external_ref=request.external_ref,
                    store_input=request.policy.store_input,
                    retention_days=request.policy.retention_days,
                )
            )
            review_ids.append(review.id)
        return GovernedDecisionResponse(
            decision=response,
            routed_to_review=bool(review_ids),
            review_ids=review_ids,
        )


def _require_studio(x_rtdc_studio_key: str | None = Header(default=None)) -> bool:
    expected = os.getenv("RTDC_STUDIO_API_KEY", "").strip() or os.getenv("RTDC_ADMIN_API_KEY", "").strip()
    if expected and (x_rtdc_studio_key is None or not hmac.compare_digest(x_rtdc_studio_key, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-RTDC-Studio-Key")
    return True


def _studio_html() -> str:
    return r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROOOMTECH Decision Studio</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#171717;background:#f5f5f5}
body{margin:0}.shell{max-width:1180px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:end;gap:20px}.muted{color:#666}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.card{background:white;border:1px solid #ddd;border-radius:14px;padding:18px;margin-top:18px}label{display:block;font-size:13px;font-weight:650;margin:12px 0 5px}input,textarea,select,button{font:inherit}input,textarea,select{width:100%;box-sizing:border-box;border:1px solid #bbb;border-radius:8px;padding:10px;background:#fff}textarea{min-height:95px;resize:vertical}button{border:0;border-radius:8px;padding:10px 14px;background:#171717;color:white;cursor:pointer;margin-top:12px}.secondary{background:#e9e9e9;color:#171717}.result{white-space:pre-wrap;background:#111;color:#eaeaea;padding:12px;border-radius:8px;min-height:80px;overflow:auto;font:12px ui-monospace,monospace}.row{display:flex;gap:10px}.row>*{flex:1}.review{border-top:1px solid #eee;padding:12px 0}.pill{display:inline-block;padding:2px 7px;border-radius:999px;background:#eee;font-size:12px}@media(max-width:820px){.grid{grid-template-columns:1fr}.top{display:block}}
</style>
</head>
<body><div class="shell">
<div class="top"><div><h1>ROOOMTECH Decision Studio</h1><div class="muted">Design, evaluate and review decisions. Independent ROOOMTECH interface.</div></div><div><label>Studio API key</label><input id="key" type="password" placeholder="optional on local deployments"></div></div>
<div class="grid">
<section class="card"><h2>Decision Builder</h2>
<label>Input</label><textarea id="input">ログインできなくてパスワードも忘れました</textarea>
<div class="row"><div><label>Decision ID</label><input id="decisionId" value="support_route"></div><div><label>Provider</label><select id="provider"><option>rules</option><option>local_classifier</option><option>auto</option></select></div></div>
<label>Question</label><input id="question" value="Which support route should handle this request?">
<label>Choices (one per line)</label><textarea id="choices">account
billing
other</textarea>
<label>Keyword rules JSON</label><textarea id="keywords">{"account":["ログイン","パスワード"],"billing":["請求","支払い"]}</textarea>
<div class="row"><div><label>Review below confidence</label><input id="reviewThreshold" type="number" min="0" max="1" step="0.01" value="0.80"></div><div><label>Store raw input for review</label><select id="storeInput"><option value="false">No (privacy default)</option><option value="true">Yes</option></select></div></div>
<button onclick="runDecision()">Run governed decision</button><pre class="result" id="decisionResult"></pre></section>
<section class="card"><h2>Calibration Studio</h2><div class="muted">Paste held-out outcomes you own. Each row is confidence + whether the decision was correct.</div>
<label>Samples JSON</label><textarea id="samples">[{"confidence":0.99,"correct":true},{"confidence":0.98,"correct":true},{"confidence":0.97,"correct":true},{"confidence":0.96,"correct":true},{"confidence":0.95,"correct":true},{"confidence":0.94,"correct":true},{"confidence":0.93,"correct":true},{"confidence":0.92,"correct":true},{"confidence":0.91,"correct":true},{"confidence":0.90,"correct":true},{"confidence":0.89,"correct":true},{"confidence":0.88,"correct":true},{"confidence":0.87,"correct":true},{"confidence":0.86,"correct":true},{"confidence":0.85,"correct":true},{"confidence":0.84,"correct":true},{"confidence":0.83,"correct":false},{"confidence":0.82,"correct":true},{"confidence":0.75,"correct":false},{"confidence":0.60,"correct":false}]</textarea>
<div class="row"><div><label>Target max error rate</label><input id="targetError" type="number" step="0.001" value="0.01"></div><div><label>Min accepted samples</label><input id="minAccepted" type="number" value="10"></div></div>
<button onclick="calibrate()">Calculate threshold</button><pre class="result" id="calibrationResult"></pre></section>
</div>
<section class="card"><div class="top"><div><h2>Human Review Queue</h2><div class="muted">Only inputs explicitly stored by the operator are visible. Otherwise the queue keeps a SHA-256 reference only.</div></div><button class="secondary" onclick="loadReviews()">Refresh</button></div><div id="reviews"></div></section>
</div>
<script>
const headers=()=>{const h={'Content-Type':'application/json'};const k=document.getElementById('key').value;if(k)h['X-RTDC-Studio-Key']=k;return h};
async function api(url,opts={}){opts.headers={...(opts.headers||{}),...headers()};const r=await fetch(url,opts);const text=await r.text();let data;try{data=JSON.parse(text)}catch{data=text}if(!r.ok)throw new Error(typeof data==='string'?data:JSON.stringify(data));return data}
async function runDecision(){const out=document.getElementById('decisionResult');try{const choices=document.getElementById('choices').value.split('\n').map(x=>x.trim()).filter(Boolean);const body={decision:{input:document.getElementById('input').value,provider:document.getElementById('provider').value,decisions:[{id:document.getElementById('decisionId').value,question:document.getElementById('question').value,choices,keywords:JSON.parse(document.getElementById('keywords').value)}]},policy:{review_below_confidence:Number(document.getElementById('reviewThreshold').value),store_input:document.getElementById('storeInput').value==='true'}};out.textContent=JSON.stringify(await api('/v1/decide/governed',{method:'POST',body:JSON.stringify(body)}),null,2);loadReviews()}catch(e){out.textContent=e.message}}
async function calibrate(){const out=document.getElementById('calibrationResult');try{const body={samples:JSON.parse(document.getElementById('samples').value),target_max_error_rate:Number(document.getElementById('targetError').value),min_accepted_samples:Number(document.getElementById('minAccepted').value)};out.textContent=JSON.stringify(await api('/v1/evals/calibrate',{method:'POST',body:JSON.stringify(body)}),null,2)}catch(e){out.textContent=e.message}}
async function loadReviews(){const box=document.getElementById('reviews');try{const rows=await api('/v1/reviews?status=pending&limit=100');box.innerHTML=rows.length?'':'<div class="muted">No pending reviews.</div>';for(const x of rows){const d=document.createElement('div');d.className='review';d.innerHTML=`<span class="pill">${x.decision_id}</span> confidence ${x.confidence??'-'}<div><b>${x.input_text??'[raw input not stored]'}</b></div><div class="muted">suggested: ${x.suggested_label??'-'} · ${x.id}</div><div class="row"><input id="label-${x.id}" placeholder="resolved label"><input id="reviewer-${x.id}" placeholder="reviewer"></div><button onclick="resolveReview('${x.id}')">Resolve</button>`;box.appendChild(d)}}catch(e){box.textContent=e.message}}
async function resolveReview(id){try{await api('/v1/reviews/'+id+'/resolve',{method:'POST',body:JSON.stringify({status:'resolved',resolved_label:document.getElementById('label-'+id).value,reviewer:document.getElementById('reviewer-'+id).value})});loadReviews()}catch(e){alert(e.message)}}
loadReviews();
</script></body></html>'''


def install_studio(app, engine, fast_path) -> StudioServices:
    services = StudioServices(engine)
    router = APIRouter()

    @router.get("/studio", response_class=HTMLResponse, include_in_schema=False)
    async def studio_page():
        return HTMLResponse(_studio_html())

    @router.post("/v1/evals/calibrate", response_model=CalibrationResponse, dependencies=[Depends(_require_studio)])
    async def calibrate(request: CalibrationRequest):
        return services.calibration.evaluate(request)

    @router.post("/v1/decide/governed", response_model=GovernedDecisionResponse, dependencies=[Depends(_require_studio)])
    async def governed_decide(request: GovernedDecisionRequest):
        try:
            return await services.governed_decide(request)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"governed decision error: {exc}") from exc

    @router.post("/v1/reviews", response_model=ReviewItem, dependencies=[Depends(_require_studio)])
    async def create_review(request: ReviewCreate):
        return services.reviews.create(request)

    @router.get("/v1/reviews", response_model=list[ReviewItem], dependencies=[Depends(_require_studio)])
    async def list_reviews(
        status: Literal["pending", "resolved", "dismissed", "all"] = Query(default="pending"),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return services.reviews.list(status=status, limit=limit)

    @router.get("/v1/reviews/{review_id}", response_model=ReviewItem, dependencies=[Depends(_require_studio)])
    async def get_review(review_id: str):
        try:
            return services.reviews.get(review_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/v1/reviews/{review_id}/resolve", response_model=ReviewItem, dependencies=[Depends(_require_studio)])
    async def resolve_review(review_id: str, request: ReviewResolve):
        try:
            return services.reviews.resolve(review_id, request)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/v1/reviews/purge-expired", dependencies=[Depends(_require_studio)])
    async def purge_expired_reviews():
        return {"deleted": services.reviews.purge_expired()}

    @router.get("/v1/reviews/export/training-examples", dependencies=[Depends(_require_studio)])
    async def export_training_examples(limit: int = Query(default=5000, ge=1, le=50_000)):
        return {
            "examples": services.reviews.export_training_examples(limit=limit),
            "note": "Only resolved items whose raw input was explicitly retained are exported.",
        }

    app.include_router(router)

    @app.on_event("startup")
    async def _studio_startup():
        services.reviews.purge_expired()
        await fast_path.start()

    @app.on_event("shutdown")
    async def _studio_shutdown():
        await fast_path.stop()
        services.reviews.close()

    return services
