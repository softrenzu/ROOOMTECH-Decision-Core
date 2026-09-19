from __future__ import annotations

import hmac
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.decision_graph import (
    DecisionGraphRuntime,
    DecisionGraphStore,
    GraphDefinition,
    GraphRunRecord,
    GraphRunRequest,
    GraphRunResponse,
    GraphSummary,
)
from app.engine import DecisionEngine
from app.operations import OperationalDecisionEngine
from app.review_store import ReviewStore
from app.schema_extraction import SchemaExtractor
from app.tenant_context import reset_tenant_context, set_tenant_context


def _enterprise_enforced() -> bool:
    return os.getenv("RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def install_decision_graph_api(app, enterprise_services):
    engine = DecisionEngine()
    # Share the already-instantiated local model provider with the main enterprise
    # services so graph runs see the same model directory/cache and ownership checks.
    engine.local = enterprise_services.local
    operations = OperationalDecisionEngine(engine)
    schema_extractor = SchemaExtractor(engine.model)
    reviews = ReviewStore()
    store = DecisionGraphStore()
    runtime = DecisionGraphRuntime(
        engine,
        operations,
        schema_extractor,
        reviews=reviews,
        resource_registry=getattr(enterprise_services, "resource_registry", None),
    )
    router = APIRouter(prefix="/v1/project/graphs")

    def _auth(*, consume_quota: bool = False):
        def dependency(
            x_rtdc_project_key: str | None = Header(default=None),
            x_rtdc_graph_key: str | None = Header(default=None),
            x_rtdc_admin_key: str | None = Header(default=None),
        ):
            if _enterprise_enforced():
                try:
                    return enterprise_services.store.authenticate(
                        x_rtdc_project_key or "",
                        required_scope="graphs",
                        consume_quota=consume_quota,
                    )
                except PermissionError as exc:
                    raise HTTPException(status_code=401, detail=str(exc)) from exc
                except OverflowError as exc:
                    raise HTTPException(
                        status_code=429,
                        detail=str(exc),
                        headers={"Retry-After": "86400"},
                    ) from exc

            expected = os.getenv("RTDC_GRAPH_API_KEY", "").strip() or os.getenv("RTDC_ADMIN_API_KEY", "").strip()
            supplied = x_rtdc_graph_key or x_rtdc_admin_key
            if expected and (supplied is None or not hmac.compare_digest(supplied, expected)):
                raise HTTPException(status_code=401, detail="invalid or missing graph/admin key")
            return True

        return dependency

    graph_auth = _auth(consume_quota=False)
    run_auth = _auth(consume_quota=True)

    def project_id(auth) -> str:
        return auth.project_id if auth is not True else "local"

    def audit_meta(auth, method: str, path: str, status_code: int, started: float, request: Request) -> None:
        if not _enterprise_enforced() or auth is True:
            return
        enterprise_services.store.audit(
            project_id=auth.project_id,
            key_id=auth.key_id,
            method=method,
            path=path,
            status_code=status_code,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            request_id=request.headers.get("x-request-id") or "graph_" + uuid.uuid4().hex[:20],
        )

    @router.post("/validate")
    async def validate_graph(payload: GraphDefinition, auth=Depends(graph_auth)):
        return {
            "valid": True,
            "graph_id": payload.graph_id,
            "node_count": len(payload.nodes),
            "edge_count": len(payload.edges),
            "topological_order": payload.topological_order(),
            "project_id": project_id(auth),
        }

    @router.post("", response_model=GraphSummary)
    async def create_graph(payload: GraphDefinition, auth=Depends(graph_auth)):
        return store.put_graph(project_id(auth), payload)

    @router.get("", response_model=list[GraphSummary])
    async def list_graphs(
        limit: int = Query(default=100, ge=1, le=1000),
        auth=Depends(graph_auth),
    ):
        return store.list_graphs(project_id(auth), limit=limit)

    @router.get("/{graph_id}")
    async def get_graph(
        graph_id: str,
        version: int | None = Query(default=None, ge=1),
        auth=Depends(graph_auth),
    ):
        try:
            definition, summary = store.get_graph(project_id(auth), graph_id, version)
            return {"summary": summary.model_dump(mode="json"), "definition": definition.model_dump(mode="json")}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/{graph_id}/run", response_model=GraphRunResponse)
    async def run_graph(
        graph_id: str,
        payload: GraphRunRequest,
        request: Request,
        auth=Depends(run_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        tenant_token = None
        try:
            definition, summary = store.get_graph(project_id(auth), graph_id, payload.version)
            registry = getattr(enterprise_services, "resource_registry", None)
            runtime.resource_registry = registry
            if registry is not None and auth is not True:
                tenant_token = set_tenant_context(auth.project_id, registry)
            started_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            result = await runtime.run(definition, summary.version, payload, project_id(auth))
            completed_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            store.save_run(project_id(auth), definition, summary.version, payload, result, started_at, completed_at)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            status_code = 400 if isinstance(exc, ValueError) else 403
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        finally:
            if tenant_token is not None:
                reset_tenant_context(tenant_token)
            audit_meta(auth, "POST", f"/v1/project/graphs/{graph_id}/run", status_code, started, request)

    @router.get("/runs/{run_id}", response_model=GraphRunRecord)
    async def get_run(run_id: str, auth=Depends(graph_auth)):
        try:
            return store.get_run(project_id(auth), run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/runs/{run_id}/replay", response_model=GraphRunResponse)
    async def replay_run(
        run_id: str,
        request: Request,
        dry_run: bool = Query(default=True),
        auth=Depends(run_auth),
    ):
        started = time.perf_counter()
        status_code = 500
        tenant_token = None
        try:
            raw_input, context, graph_id, version = store.replay_input(project_id(auth), run_id)
            definition, summary = store.get_graph(project_id(auth), graph_id, version)
            payload = GraphRunRequest(
                input=raw_input,
                context=context,
                version=version,
                dry_run=dry_run,
                retain_input=False,
            )
            registry = getattr(enterprise_services, "resource_registry", None)
            runtime.resource_registry = registry
            if registry is not None and auth is not True:
                tenant_token = set_tenant_context(auth.project_id, registry)
            started_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            result = await runtime.run(definition, summary.version, payload, project_id(auth))
            completed_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            store.save_run(project_id(auth), definition, summary.version, payload, result, started_at, completed_at)
            status_code = 200
            return result
        except FileNotFoundError as exc:
            status_code = 404
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            status_code = 400 if isinstance(exc, ValueError) else 403
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        finally:
            if tenant_token is not None:
                reset_tenant_context(tenant_token)
            audit_meta(auth, "POST", f"/v1/project/graphs/runs/{run_id}/replay", status_code, started, request)

    app.include_router(router)

    @app.on_event("shutdown")
    async def _decision_graph_close():
        store.close()
        reviews.close()

    app.state.decision_graph_store = store
    app.state.decision_graph_runtime = runtime
    return runtime
