from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

import websockets


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


async def run(args):
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(args.requests):
        queue.put_nowait(index)

    latencies: list[float] = []
    errors = 0
    server_target_hits = 0
    headers = None
    if args.api_key:
        headers = {"x-rtdc-api-key": args.api_key}

    async def worker(worker_id: int):
        nonlocal errors, server_target_hits
        async with websockets.connect(
            args.ws_url,
            additional_headers=headers,
            max_size=2 * 1024 * 1024,
            open_timeout=args.timeout,
            close_timeout=args.timeout,
        ) as ws:
            while True:
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                started = time.perf_counter()
                try:
                    payload = {
                        "request_id": f"ws-{worker_id}-{index}",
                        "kind": "fast",
                        "request": {
                            "profile_id": args.profile_id,
                            "input": args.input,
                        },
                    }
                    await ws.send(json.dumps(payload, ensure_ascii=False))
                    response = json.loads(await ws.recv())
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    latencies.append(elapsed_ms)
                    if not response.get("ok"):
                        errors += 1
                    else:
                        nested = response.get("data") or {}
                        if nested.get("within_target"):
                            server_target_hits += 1
                except Exception:
                    latencies.append((time.perf_counter() - started) * 1000.0)
                    errors += 1
                finally:
                    queue.task_done()

    wall_started = time.perf_counter()
    workers = [asyncio.create_task(worker(i)) for i in range(min(args.connections, args.requests))]
    await asyncio.gather(*workers)
    wall_seconds = max(time.perf_counter() - wall_started, 1e-9)

    result = {
        "ws_url": args.ws_url,
        "profile_id": args.profile_id,
        "requests": args.requests,
        "connections": args.connections,
        "errors": errors,
        "wall_seconds": round(wall_seconds, 6),
        "requests_per_second": round(args.requests / wall_seconds, 3),
        "mean_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "p50_ms": round(percentile(latencies, 0.50), 3),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "p99_ms": round(percentile(latencies, 0.99), 3),
        "max_ms": round(max(latencies), 3) if latencies else 0.0,
        "server_target_hit_rate": round(server_target_hits / args.requests, 6) if args.requests else 0.0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="WebSocket round-trip load test for ROOOMTECH Decision Core fast profiles")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8000/v1/realtime/ws")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--requests", type=int, default=5000)
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests must be >= 1")
    if args.connections < 1:
        parser.error("--connections must be >= 1")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
