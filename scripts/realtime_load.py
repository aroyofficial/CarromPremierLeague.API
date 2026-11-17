"""Open many public realtime streams, trigger writes, and report fan-out latency.

Local (starts its own server with the real realtime routes and a fake write route):
    python scripts/realtime_load.py --local --clients 200 --writes 5

Against a deployed API (needs an admin token allowed to save settings; the write is a no-op PUT of current settings):
    set CPL_LOAD_TOKEN=...   (never commit it)
    python scripts/realtime_load.py --base https://example.onrender.com/ --clients 50 --writes 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _start_local() -> tuple[str, object]:
    import uvicorn
    from fastapi import FastAPI

    os.environ["CPL_REALTIME_ENABLED"] = "true"
    from core.realtime import PublicChangeMiddleware
    from routes.realtime_routes import router

    app = FastAPI()
    app.add_middleware(PublicChangeMiddleware)
    app.include_router(router)

    @app.post("/matches")
    def write():
        return {"ok": True}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/", server


async def _listen(client, url, ready, results, stop):
    try:
        async with client.stream("GET", url, timeout=None) as response:
            if response.status_code != 200:
                results["refused"].append(response.status_code)
                ready.release()
                return
            ready.release()
            async for line in response.aiter_lines():
                if stop.is_set():
                    return
                if line.startswith("event: league.invalidated"):
                    results["arrivals"].append(time.perf_counter())
    except (httpx.HTTPError, asyncio.CancelledError):
        return


async def run(base: str, clients: int, writes: int, write_request) -> dict:
    results = {"refused": [], "arrivals": []}
    stop = asyncio.Event()
    ready = asyncio.Semaphore(0)
    limits = httpx.Limits(max_connections=clients + 10, max_keepalive_connections=clients + 10)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [asyncio.create_task(_listen(client, f"{base}realtime/public", ready, results, stop)) for _ in range(clients)]
        for _ in range(clients):
            await asyncio.wait_for(ready.acquire(), timeout=60)
        connected = clients - len(results["refused"])
        latencies = []
        for _ in range(writes):
            results["arrivals"].clear()
            sent = time.perf_counter()
            await write_request(client)
            deadline = time.perf_counter() + 10
            while len(results["arrivals"]) < connected and time.perf_counter() < deadline:
                await asyncio.sleep(0.01)
            latencies.extend((arrival - sent) * 1000 for arrival in results["arrivals"])
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    delivered = len(latencies)
    ordered = sorted(latencies) or [0.0]
    return {
        "clients": clients,
        "connected": connected,
        "refused": len(results["refused"]),
        "writes": writes,
        "expected": connected * writes,
        "delivered": delivered,
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max_ms": round(ordered[-1], 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--writes", type=int, default=3)
    args = parser.parse_args()

    if args.local:
        base, _server = _start_local()

        async def write_request(client):
            (await client.post(f"{base}matches")).raise_for_status()
    else:
        if not args.base:
            parser.error("--base or --local is required")
        base = args.base.rstrip("/") + "/"
        token = os.environ.get("CPL_LOAD_TOKEN")
        if not token:
            parser.error("set CPL_LOAD_TOKEN to an admin token that may update settings")
        headers = {"Authorization": f"Bearer {token}"}

        async def write_request(client):
            current = (await client.get(f"{base}settings", headers=headers)).json().get("data")
            (await client.put(f"{base}settings", json=current, headers=headers)).raise_for_status()

    print(asyncio.run(run(base, args.clients, args.writes, write_request)))


if __name__ == "__main__":
    main()
