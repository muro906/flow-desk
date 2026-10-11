"""Fire N concurrent webhooks and report latency.

Usage: python scripts/fire.py [N] [--repeat]
  --repeat  send the same external_id every time (exercises idempotency)
"""

import asyncio
import os
import statistics
import sys
import time
import uuid

import httpx

URL = os.environ.get("FLOWDESK_URL", "http://localhost:8000")
SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")


async def send(client: httpx.AsyncClient, external_id: str) -> tuple[int, float]:
    started = time.perf_counter()
    response = await client.post(
        "/webhooks/ticket",
        headers={"X-Webhook-Secret": SECRET},
        json={
            "external_id": external_id,
            "subject": "Charged twice this month",
            "body": "My card was billed twice for the March invoice. Please refund one.",
            "source": "fire.py",
        },
    )
    return response.status_code, time.perf_counter() - started


async def main(n: int, repeat: bool) -> None:
    shared_id = f"fire-{uuid.uuid4()}"
    async with httpx.AsyncClient(base_url=URL, timeout=120) as client:
        results = await asyncio.gather(
            *(send(client, shared_id if repeat else f"fire-{uuid.uuid4()}") for _ in range(n))
        )
    latencies = sorted(seconds for _, seconds in results)
    codes: dict[int, int] = {}
    for code, _ in results:
        codes[code] = codes.get(code, 0) + 1
    print(f"status codes: {codes}")
    print(
        f"latency  p50={statistics.median(latencies):.3f}s  "
        f"p95={latencies[int(0.95 * (n - 1))]:.3f}s  max={latencies[-1]:.3f}s"
    )


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(int(args[0]) if args else 20, "--repeat" in sys.argv))
