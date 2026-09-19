from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, model_validator

from app.advanced_models import SchemaExtractionRequest
from app.engine import DecisionEngine
from app.models import DecisionRequest
from app.operation_models import (
    DetectionRequest,
    FeatureExtractionRequest,
    RouteRequest,
    ScoreRequest,
    VerificationRequest,
)
from app.operations import OperationalDecisionEngine
from app.review_store import ReviewStore
from app.schema_extraction import SchemaExtractor
from app.studio_models import ReviewCreate


ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
NodeKind = Literal[
    "decide", "detect", "route", "score", "verify", "features", "extract",
    "gate", "review", "action",
]
NodeStatus = Literal["completed", "skipped", "failed"]
GraphStatus = Literal["completed", "review", "failed"]
ConditionOperator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "truthy", "falsy", "exists",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _path_parts(path: str) -> list[str]:
    value = path.strip()
    if value.startswith("$"):
        value = value[1:]
    if value.startswith("."):
        value = value[1:]
    return [part for part in value.split(".") if part]


def resolve_path(state: dict[str, Any], path: str, default: Any = None) -> Any:
    if path in {"", "$"}:
        return state
    current: Any = state
    for part in _path_parts(path):
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def _resolve_template(value: Any, state: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return resolve_path(state, value)
    if isinstance(value, dict):
        return {str(k): _resolve_template(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_template(v, state) for v in value]
    return value


class GraphCondition(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    op: ConditionOperator = "eq"
    value: Any = None

    def evaluate(self, state: dict[str, Any]) -> bool:
        sentinel = object()
        actual = resolve_path(state, self.path, sentinel)
        if self.op == "exists":
            return actual is not sentinel
        if actual is sentinel:
            return False
        if self.op == "truthy":
            return bool(actual)
        if self.op == "falsy":
            return not bool(actual)
        if self.op == "eq":
            return actual == self.value
        if self.op == "ne":
            return actual != self.value
        if self.op == "in":
            try:
                return actual in self.value
            except TypeError:
                return False
        if self.op == "contains":
            try:
                return self.value in actual
            except TypeError:
                return False
        try:
            left = float(actual)
            right = float(self.value)
        except (TypeError, ValueError):
            return False
        if self.op == "gt":
            return left > right
        if self.op == "gte":
            return left >= right
        if self.op == "lt":
            return left < right
        if self.op == "lte":
            return left <= right
        return False


class GraphEdge(BaseModel):
    source: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    target: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    condition: GraphCondition | None = None


class GraphNode(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    kind: NodeKind
    config: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int | None = Field(default=None, ge=10, le=120_000)
    retries: int = Field(default=0, ge=0, le=3)
    on_error: Literal["fail", "skip", "review"] = "fail"


class GraphDefinition(BaseModel):
    graph_id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    nodes: list[GraphNode] = Field(min_length=1, max_length=100)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=500)
    max_parallel: int = Field(default=8, ge=1, le=64)
    default_timeout_ms: int = Field(default=10_000, ge=10, le=120_000)
    max_runtime_ms: int = Field(default=120_000, ge=100, le=900_000)
    persist_output_data: bool = False

    @model_validator(mode="after")
    def validate_graph(self):
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("node ids must be unique")
        known = set(ids)
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in ids}
        indegree = {node_id: 0 for node_id in ids}
        seen_edges: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise ValueError("every edge source/target must reference an existing node")
            if edge.source == edge.target:
                raise ValueError("self edges are not allowed")
            key = (edge.source, edge.target)
            if key in seen_edges:
                raise ValueError("duplicate source/target edges are not allowed")
            seen_edges.add(key)
            adjacency[edge.source].append(edge.target)
            indegree[edge.target] += 1
        queue = [node_id for node_id in ids if indegree[node_id] == 0]
        visited = 0
        while queue:
            current = queue.pop(0)
            visited += 1
            for target in adjacency[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(ids):
            raise ValueError("decision graph must be acyclic")
        for node in self.nodes:
            if node.kind == "action":
                action = str(node.config.get("action", "emit"))
                if action not in {"emit", "webhook"}:
                    raise ValueError(f"unsupported action type for node {node.id}: {action}")
                if action == "webhook" and not node.config.get("url"):
                    raise ValueError(f"webhook action node {node.id} requires config.url")
        return self

    def topological_order(self) -> list[str]:
        ids = [node.id for node in self.nodes]
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in ids}
        indegree = {node_id: 0 for node_id in ids}
        for edge in self.edges:
            adjacency[edge.source].append(edge.target)
            indegree[edge.target] += 1
        ready = [node_id for node_id in ids if indegree[node_id] == 0]
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for target in adjacency[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        return order


class GraphRunRequest(BaseModel):
    input: str = Field(min_length=1, max_length=200_000)
    context: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)
    retain_input: bool = False


class GraphNodeTrace(BaseModel):
    node_id: str
    kind: str
    status: NodeStatus
    attempts: int
    latency_ms: float
    output: Any = None
    error: str | None = None


class GraphRunResponse(BaseModel):
    run_id: str
    graph_id: str
    graph_version: int
    status: GraphStatus
    dry_run: bool
    review_required: bool
    review_ids: list[str] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    trace: list[GraphNodeTrace] = Field(default_factory=list)
    latency_ms: float


class GraphSummary(BaseModel):
    project_id: str
    graph_id: str
    name: str
    version: int
    node_count: int
    edge_count: int
    created_at: str


class GraphRunRecord(BaseModel):
    run_id: str
    project_id: str
    graph_id: str
    graph_version: int
    status: str
    dry_run: bool
    input_sha256: str
    input_retained: bool
    review_ids: list[str]
    actions: list[dict[str, Any]]
    outputs: dict[str, Any]
    trace: list[GraphNodeTrace]
    started_at: str
    completed_at: str


class DecisionGraphStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_GRAPH_DB", "data/decision_graphs.sqlite3")
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS decision_graphs (
                    project_id TEXT NOT NULL,
                    graph_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, graph_id, version)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS decision_graph_runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    graph_id TEXT NOT NULL,
                    graph_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    retained_input TEXT,
                    context_json TEXT NOT NULL,
                    review_ids_json TEXT NOT NULL,
                    actions_json TEXT NOT NULL,
                    outputs_json TEXT NOT NULL,
                    trace_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                )"""
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_graph_latest ON decision_graphs(project_id, graph_id, version DESC)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_graph_runs_project ON decision_graph_runs(project_id, started_at DESC)")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def put_graph(self, project_id: str, definition: GraphDefinition) -> GraphSummary:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM decision_graphs WHERE project_id=? AND graph_id=?",
                (project_id, definition.graph_id),
            ).fetchone()
            version = int(row["v"]) + 1
            now = _utc_now()
            self._db.execute(
                "INSERT INTO decision_graphs VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, definition.graph_id, version, definition.name,
                 definition.model_dump_json(), now),
            )
            self._db.commit()
        return GraphSummary(
            project_id=project_id, graph_id=definition.graph_id, name=definition.name,
            version=version, node_count=len(definition.nodes), edge_count=len(definition.edges), created_at=now,
        )

    def get_graph(self, project_id: str, graph_id: str, version: int | None = None) -> tuple[GraphDefinition, GraphSummary]:
        with self._lock:
            if version is None:
                row = self._db.execute(
                    "SELECT * FROM decision_graphs WHERE project_id=? AND graph_id=? ORDER BY version DESC LIMIT 1",
                    (project_id, graph_id),
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT * FROM decision_graphs WHERE project_id=? AND graph_id=? AND version=?",
                    (project_id, graph_id, version),
                ).fetchone()
        if row is None:
            raise FileNotFoundError("decision graph not found")
        definition = GraphDefinition.model_validate_json(row["definition_json"])
        return definition, GraphSummary(
            project_id=row["project_id"], graph_id=row["graph_id"], name=row["name"], version=row["version"],
            node_count=len(definition.nodes), edge_count=len(definition.edges), created_at=row["created_at"],
        )

    def list_graphs(self, project_id: str, limit: int = 100) -> list[GraphSummary]:
        with self._lock:
            rows = self._db.execute(
                """SELECT g.* FROM decision_graphs g JOIN (
                    SELECT graph_id, MAX(version) AS v FROM decision_graphs WHERE project_id=? GROUP BY graph_id
                ) latest ON latest.graph_id=g.graph_id AND latest.v=g.version
                WHERE g.project_id=? ORDER BY g.created_at DESC LIMIT ?""",
                (project_id, project_id, limit),
            ).fetchall()
        result: list[GraphSummary] = []
        for row in rows:
            definition = GraphDefinition.model_validate_json(row["definition_json"])
            result.append(GraphSummary(
                project_id=row["project_id"], graph_id=row["graph_id"], name=row["name"], version=row["version"],
                node_count=len(definition.nodes), edge_count=len(definition.edges), created_at=row["created_at"],
            ))
        return result

    def save_run(
        self,
        project_id: str,
        definition: GraphDefinition,
        version: int,
        request: GraphRunRequest,
        response: GraphRunResponse,
        started_at: str,
        completed_at: str,
    ) -> None:
        trace = response.trace if definition.persist_output_data else [
            item.model_copy(update={"output": None}) for item in response.trace
        ]
        outputs = response.outputs if definition.persist_output_data else {}
        with self._lock:
            self._db.execute(
                "INSERT INTO decision_graph_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    response.run_id, project_id, definition.graph_id, version, response.status, int(response.dry_run),
                    hashlib.sha256(request.input.encode("utf-8")).hexdigest(),
                    request.input if request.retain_input else None,
                    json.dumps(request.context, ensure_ascii=False, sort_keys=True),
                    json.dumps(response.review_ids, ensure_ascii=False),
                    json.dumps(response.actions, ensure_ascii=False),
                    json.dumps(outputs, ensure_ascii=False),
                    json.dumps([item.model_dump(mode="json") for item in trace], ensure_ascii=False),
                    started_at, completed_at,
                ),
            )
            self._db.commit()

    def get_run(self, project_id: str, run_id: str) -> GraphRunRecord:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM decision_graph_runs WHERE run_id=? AND project_id=?", (run_id, project_id)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("decision graph run not found")
        return GraphRunRecord(
            run_id=row["run_id"], project_id=row["project_id"], graph_id=row["graph_id"], graph_version=row["graph_version"],
            status=row["status"], dry_run=bool(row["dry_run"]), input_sha256=row["input_sha256"],
            input_retained=row["retained_input"] is not None,
            review_ids=json.loads(row["review_ids_json"]), actions=json.loads(row["actions_json"]),
            outputs=json.loads(row["outputs_json"]),
            trace=[GraphNodeTrace.model_validate(item) for item in json.loads(row["trace_json"])],
            started_at=row["started_at"], completed_at=row["completed_at"],
        )

    def replay_input(self, project_id: str, run_id: str) -> tuple[str, dict[str, Any], str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT retained_input, context_json, graph_id, graph_version FROM decision_graph_runs WHERE run_id=? AND project_id=?",
                (run_id, project_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("decision graph run not found")
        if row["retained_input"] is None:
            raise ValueError("run input was not retained; replay is unavailable")
        return row["retained_input"], json.loads(row["context_json"]), row["graph_id"], int(row["graph_version"])


async def _assert_safe_webhook_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError("graph webhooks require https")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid graph webhook URL")
    port = parts.port or 443
    if port != 443:
        raise ValueError("graph webhooks only allow port 443")
    host = parts.hostname.lower()
    allowed = {item.strip().lower() for item in os.getenv("RTDC_GRAPH_WEBHOOK_HOSTS", "").split(",") if item.strip()}
    if host not in allowed:
        raise ValueError("webhook host is not in RTDC_GRAPH_WEBHOOK_HOSTS")

    def resolve() -> list[str]:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return sorted({row[4][0] for row in rows})

    try:
        addresses = await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        raise ValueError("webhook hostname could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("webhook hostname must resolve only to public addresses")
    return host


class DecisionGraphRuntime:
    def __init__(
        self,
        engine: DecisionEngine,
        operations: OperationalDecisionEngine,
        schema_extractor: SchemaExtractor,
        reviews: ReviewStore | None = None,
        resource_registry=None,
    ):
        self.engine = engine
        self.operations = operations
        self.schema_extractor = schema_extractor
        self.reviews = reviews
        self.resource_registry = resource_registry

    @staticmethod
    def _text_input(state: dict[str, Any], config: dict[str, Any]) -> str:
        path = str(config.pop("input_path", "input"))
        value = resolve_path(state, path)
        if value is None:
            raise ValueError(f"input_path not found: {path}")
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    async def _semantic_node(self, kind: str, config: dict[str, Any], state: dict[str, Any]) -> Any:
        cfg = dict(config)
        text = self._text_input(state, cfg)
        if kind == "decide":
            return await self.engine.decide(DecisionRequest.model_validate({"input": text, **cfg}))
        if kind == "detect":
            return await self.operations.detect(DetectionRequest.model_validate({"input": text, **cfg}))
        if kind == "route":
            return await self.operations.route(RouteRequest.model_validate({"input": text, **cfg}))
        if kind == "score":
            return await self.operations.score(ScoreRequest.model_validate({"input": text, **cfg}))
        if kind == "verify":
            return await self.operations.verify(VerificationRequest.model_validate({"artifact": text, **cfg}))
        if kind == "features":
            return await self.operations.extract_features(FeatureExtractionRequest.model_validate({"input": text, **cfg}))
        if kind == "extract":
            return await self.schema_extractor.extract(SchemaExtractionRequest.model_validate({"input": text, **cfg}))
        raise ValueError(f"unsupported semantic node kind: {kind}")

    def _gate_node(self, config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        conditions = [GraphCondition.model_validate(item) for item in config.get("conditions", [])]
        if not conditions:
            raise ValueError("gate node requires config.conditions")
        mode = str(config.get("mode", "all"))
        values = [item.evaluate(state) for item in conditions]
        if mode == "all":
            passed = all(values)
        elif mode == "any":
            passed = any(values)
        else:
            raise ValueError("gate mode must be all or any")
        return {"passed": passed, "mode": mode, "conditions": values}

    def _review_node(self, node: GraphNode, config: dict[str, Any], state: dict[str, Any], project_id: str) -> dict[str, Any]:
        if self.reviews is None:
            raise RuntimeError("human review store is unavailable")
        decision_id = str(config.get("decision_id") or node.id)
        suggested = None
        confidence = None
        if config.get("suggested_label_path"):
            value = resolve_path(state, str(config["suggested_label_path"]))
            suggested = None if value is None else str(value)[:500]
        if config.get("confidence_path"):
            value = resolve_path(state, str(config["confidence_path"]))
            if value is not None:
                confidence = max(0.0, min(1.0, float(value)))
        payload: dict[str, Any] = {}
        for path in config.get("include_paths", []):
            payload[str(path)] = _jsonable(resolve_path(state, str(path)))
        item = self.reviews.create(ReviewCreate(
            source="decision_graph",
            decision_id=decision_id,
            input_text=str(state["input"]),
            payload=payload,
            model_output={},
            suggested_label=suggested,
            confidence=confidence,
            external_ref=str(config.get("external_ref"))[:500] if config.get("external_ref") else None,
            store_input=bool(config.get("store_input", False)),
            retention_days=int(config.get("retention_days", 30)),
        ))
        if self.resource_registry is not None and project_id != "local":
            self.resource_registry.register_resource(project_id, "review", item.id)
        return {"review_id": item.id, "queued": True, "decision_id": decision_id}

    async def _action_node(
        self,
        node: GraphNode,
        config: dict[str, Any],
        state: dict[str, Any],
        dry_run: bool,
        run_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        action = str(config.get("action", "emit"))
        payload = _resolve_template(config.get("payload", {}), state)
        if action == "emit":
            return {"action": "emit", "executed": not dry_run, "payload": payload, "status": "simulated" if dry_run else "emitted"}
        if action != "webhook":
            raise ValueError(f"unsupported action: {action}")
        url = str(config.get("url", ""))
        await _assert_safe_webhook_url(url)
        body = _resolve_template(config.get("body", payload), state)
        result = {"action": "webhook", "url": url, "executed": False, "status": "simulated", "body": body}
        if dry_run:
            return result
        enabled = os.getenv("RTDC_GRAPH_ACTIONS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            raise PermissionError("external graph actions are disabled; set RTDC_GRAPH_ACTIONS_ENABLED=true")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key or f"{run_id}:{node.id}",
            "X-RTDC-Graph-Run": run_id,
        }
        auth_env = config.get("auth_env")
        if auth_env:
            token = os.getenv(str(auth_env), "")
            if not token:
                raise RuntimeError(f"webhook auth environment variable is missing: {auth_env}")
            headers["Authorization"] = f"Bearer {token}"
        timeout = min(10.0, max(1.0, float(config.get("request_timeout_seconds", 5.0))))
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            response = await client.post(url, json=body, headers=headers, timeout=timeout)
        if 300 <= response.status_code < 400:
            raise RuntimeError("webhook redirects are not followed")
        response.raise_for_status()
        return {**result, "executed": True, "status": "sent", "http_status": response.status_code}

    async def _execute_once(
        self,
        node: GraphNode,
        state: dict[str, Any],
        dry_run: bool,
        project_id: str,
        run_id: str,
        idempotency_key: str | None,
    ) -> Any:
        if node.kind in {"decide", "detect", "route", "score", "verify", "features", "extract"}:
            return await self._semantic_node(node.kind, node.config, state)
        if node.kind == "gate":
            return self._gate_node(node.config, state)
        if node.kind == "review":
            return self._review_node(node, node.config, state, project_id)
        if node.kind == "action":
            return await self._action_node(node, node.config, state, dry_run, run_id, idempotency_key)
        raise ValueError(f"unsupported node kind: {node.kind}")

    async def _execute_node(
        self,
        node: GraphNode,
        state: dict[str, Any],
        dry_run: bool,
        project_id: str,
        run_id: str,
        idempotency_key: str | None,
        default_timeout_ms: int,
    ) -> GraphNodeTrace:
        started = time.perf_counter()
        error: Exception | None = None
        for attempt in range(1, node.retries + 2):
            try:
                output = await asyncio.wait_for(
                    self._execute_once(node, state, dry_run, project_id, run_id, idempotency_key),
                    timeout=(node.timeout_ms or default_timeout_ms) / 1000.0,
                )
                return GraphNodeTrace(
                    node_id=node.id, kind=node.kind, status="completed", attempts=attempt,
                    latency_ms=round((time.perf_counter() - started) * 1000.0, 3), output=_jsonable(output),
                )
            except Exception as exc:
                error = exc
                if attempt <= node.retries:
                    await asyncio.sleep(min(0.25, 0.025 * (2 ** (attempt - 1))))
        return GraphNodeTrace(
            node_id=node.id, kind=node.kind, status="failed", attempts=node.retries + 1,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            error=f"{type(error).__name__}: {error}",
        )

    async def run(
        self,
        definition: GraphDefinition,
        version: int,
        request: GraphRunRequest,
        project_id: str = "local",
    ) -> GraphRunResponse:
        run_id = "dgr_" + uuid.uuid4().hex[:20]
        started = time.perf_counter()
        state: dict[str, Any] = {"input": request.input, "context": request.context, "nodes": {}}
        nodes = {node.id: node for node in definition.nodes}
        incoming: dict[str, list[GraphEdge]] = {node.id: [] for node in definition.nodes}
        for edge in definition.edges:
            incoming[edge.target].append(edge)
        traces: dict[str, GraphNodeTrace] = {}
        pending = set(nodes)
        review_required = False
        review_ids: list[str] = []
        actions: list[dict[str, Any]] = []
        failed = False
        deadline = started + definition.max_runtime_ms / 1000.0

        while pending:
            if time.perf_counter() >= deadline:
                failed = True
                for node_id in sorted(pending):
                    traces[node_id] = GraphNodeTrace(
                        node_id=node_id, kind=nodes[node_id].kind, status="failed", attempts=0,
                        latency_ms=0.0, error="GraphTimeout: graph max_runtime_ms exceeded",
                    )
                break
            ready: list[str] = []
            for node_id in definition.topological_order():
                if node_id not in pending:
                    continue
                parents = incoming[node_id]
                if all(edge.source in traces for edge in parents):
                    ready.append(node_id)
            if not ready:
                failed = True
                break

            runnable: list[str] = []
            for node_id in ready:
                parents = incoming[node_id]
                if not parents:
                    runnable.append(node_id)
                    continue
                active = False
                for edge in parents:
                    parent_trace = traces[edge.source]
                    if parent_trace.status != "completed":
                        continue
                    if edge.condition is None or edge.condition.evaluate(state):
                        active = True
                        break
                if active:
                    runnable.append(node_id)
                else:
                    traces[node_id] = GraphNodeTrace(
                        node_id=node_id, kind=nodes[node_id].kind, status="skipped", attempts=0, latency_ms=0.0,
                    )
                    pending.remove(node_id)

            for offset in range(0, len(runnable), definition.max_parallel):
                batch_ids = runnable[offset: offset + definition.max_parallel]
                snapshot = {"input": state["input"], "context": state["context"], "nodes": dict(state["nodes"])}
                batch = await asyncio.gather(*(
                    self._execute_node(
                        nodes[node_id], snapshot, request.dry_run, project_id, run_id, request.idempotency_key,
                        definition.default_timeout_ms,
                    )
                    for node_id in batch_ids
                ))
                for trace in batch:
                    traces[trace.node_id] = trace
                    pending.discard(trace.node_id)
                    node = nodes[trace.node_id]
                    if trace.status == "completed":
                        state["nodes"][trace.node_id] = trace.output
                        if node.kind == "review" and isinstance(trace.output, dict):
                            review_required = True
                            if trace.output.get("review_id"):
                                review_ids.append(str(trace.output["review_id"]))
                        if node.kind == "action" and isinstance(trace.output, dict):
                            actions.append(trace.output)
                        if isinstance(trace.output, dict) and trace.output.get("requires_review") is True:
                            review_required = True
                    else:
                        if node.on_error == "review":
                            review_required = True
                        elif node.on_error == "fail":
                            failed = True
                if failed:
                    break
            if failed:
                for node_id in sorted(pending):
                    traces[node_id] = GraphNodeTrace(
                        node_id=node_id, kind=nodes[node_id].kind, status="skipped", attempts=0, latency_ms=0.0,
                        error="skipped after upstream graph failure",
                    )
                pending.clear()
                break

        ordered_trace = [traces[node_id] for node_id in definition.topological_order() if node_id in traces]
        status: GraphStatus = "failed" if failed else ("review" if review_required else "completed")
        return GraphRunResponse(
            run_id=run_id, graph_id=definition.graph_id, graph_version=version, status=status,
            dry_run=request.dry_run, review_required=review_required, review_ids=review_ids,
            actions=actions, outputs=state["nodes"], trace=ordered_trace,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
