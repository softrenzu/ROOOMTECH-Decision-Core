from __future__ import annotations

import asyncio
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

from app.atomic_catalog import (
    AtomicCatalogControlStore,
    AtomicCatalogRuntime,
    ImmutableCatalogBuilder,
)
from app.candidate_catalog import CandidateCatalogSearchRequest
from app.engine import DecisionEngine
from app.operations import OperationalDecisionEngine
from app.shared_object_store import LocalObjectStore


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


def main() -> None:
    rows = int(os.getenv("RTDC_BENCHMARK_ROWS", "1000000"))
    batch_size = int(os.getenv("RTDC_BENCHMARK_BATCH_SIZE", "10000"))
    query_count = int(os.getenv("RTDC_BENCHMARK_QUERIES", "100"))
    output = Path(os.getenv("RTDC_BENCHMARK_OUTPUT", "benchmark-million.json"))
    if rows < 1:
        raise SystemExit("RTDC_BENCHMARK_ROWS must be positive")

    with tempfile.TemporaryDirectory(prefix="rtdc-million-") as temp:
        root = Path(temp)
        source = root / "million.jsonl"
        source_started = time.perf_counter()
        with source.open("w", encoding="utf-8", buffering=1024 * 1024) as handle:
            for i in range(rows):
                # Deliberately short but unique synthetic state. Every row still
                # receives a real sparse character-ngram index.
                text = f"i{i:07d} g{i % 100:02d} jp"
                handle.write(
                    json.dumps(
                        {"id": f"i{i:07d}", "text": text, "metadata": {"group": i % 100, "region": "jp"}},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        source_seconds = time.perf_counter() - source_started

        storage = LocalObjectStore(root / "objects")
        source_ref = storage.put_file(source, "sources/million.jsonl")
        control = AtomicCatalogControlStore(str(root / "control.sqlite3"))
        builder = ImmutableCatalogBuilder(root / "build")
        operations = OperationalDecisionEngine(DecisionEngine())
        runtime = AtomicCatalogRuntime(control, storage, operations)
        generation = control.create_generation(
            project_id="benchmark",
            catalog_id="million",
            source_uri=source_ref.uri,
            source_sha256=source_ref.sha256,
            source_format="jsonl",
            batch_size=batch_size,
            max_rows=rows,
            feature_dim=8192,
            ngram_min=3,
            ngram_max=3,
            auto_activate=False,
        )

        build = builder.build(
            source_path=source,
            source_format="jsonl",
            generation_id=generation.generation_id,
            batch_size=batch_size,
            max_rows=rows,
            feature_dim=8192,
            ngram_min=3,
            ngram_max=3,
        )
        if build.item_count != rows:
            raise RuntimeError(f"expected {rows} indexed items, got {build.item_count}")
        artifact = storage.put_file(build.database_path, f"indexes/{generation.generation_id}.sqlite3")
        ready = control.mark_ready(
            generation,
            artifact_uri=artifact.uri,
            artifact_sha256=artifact.sha256,
            item_count=build.item_count,
            feature_rows=build.feature_rows,
            database_bytes=build.database_bytes,
            build_seconds=build.build_seconds,
        )
        runtime.prewarm(ready)
        activation = control.activate("benchmark", "million", ready.generation_id, reason="benchmark")

        async def queries() -> tuple[list[float], int]:
            latencies: list[float] = []
            correct = 0
            stride = max(1, rows // query_count)
            for n in range(query_count):
                i = min(rows - 1, n * stride)
                started = time.perf_counter()
                result = await runtime.search(
                    "benchmark",
                    "million",
                    CandidateCatalogSearchRequest(
                        query=f"i{i:07d} g{i % 100:02d} jp",
                        metadata_equals={"region": "jp"},
                        top_k=1,
                        min_selection_score=0.0,
                        min_selection_margin=0.0,
                    ),
                )
                latencies.append((time.perf_counter() - started) * 1000.0)
                correct += int(result.selected_id == f"i{i:07d}")
            return latencies, correct

        latencies, correct = asyncio.run(queries())
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            max_rss_bytes = int(max_rss * 1024)
        else:
            max_rss_bytes = int(max_rss)
        report = {
            "benchmark": "rtdc_atomic_catalog_million_v1",
            "synthetic": True,
            "rows": rows,
            "source_bytes": source.stat().st_size,
            "source_generation_seconds": round(source_seconds, 3),
            "batch_size": batch_size,
            "feature_dim": 8192,
            "ngram_min": 3,
            "ngram_max": 3,
            "feature_rows": build.feature_rows,
            "database_bytes": build.database_bytes,
            "build_seconds": build.build_seconds,
            "items_per_second": round(rows / build.build_seconds, 3),
            "features_per_second": round(build.feature_rows / build.build_seconds, 3),
            "atomic_swap_ms": activation.swap_latency_ms,
            "query_count": query_count,
            "query_correct": correct,
            "query_accuracy": round(correct / query_count, 6) if query_count else None,
            "query_mean_ms": round(statistics.mean(latencies), 3) if latencies else None,
            "query_p50_ms": round(percentile(latencies, 0.50), 3),
            "query_p95_ms": round(percentile(latencies, 0.95), 3),
            "query_p99_ms": round(percentile(latencies, 0.99), 3),
            "max_rss_bytes": max_rss_bytes,
            "cpu_count": os.cpu_count(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "notes": [
                "Measures an actual immutable sparse-index build over the reported row count.",
                "Synthetic short candidate strings are used so results are reproducible; production corpora can be slower or larger.",
                "Atomic swap measures only the control-plane pointer transaction after artifact prewarming.",
                "S3 network transfer is not included in this local-runner benchmark.",
            ],
        }
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True), flush=True)
        runtime.close()
        control.close()
        Path(build.database_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
