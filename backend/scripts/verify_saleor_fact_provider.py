#!/usr/bin/env python
"""Verify the Saleor provider without printing tokens or fact values."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sandbox_services.providers import (  # noqa: E402
    ProviderError,
    build_fact_provider,
)


async def _verify(order_number: str) -> dict:
    provider = build_fact_provider()
    if provider.mode != "saleor":
        raise RuntimeError("SANDBOX_FACT_PROVIDER must be saleor")
    try:
        health = await provider.health()
        sources = {}
        for source in ("order", "payment", "logistics", "aftersale"):
            fact = await provider.fetch(source, order_number)
            sources[source] = {
                "field_names": sorted(fact.data),
                "source_version": fact.source_version,
                "observed_at_present": bool(fact.observed_at),
            }
        return {
            "schema_version": "saleor-fact-provider-check-v1",
            "status": "verified_local",
            "health": health,
            "sources": sources,
            "environment": "local_high_fidelity_sandbox",
            "enterprise_endpoint_verified": False,
            "real_payment_verified": False,
            "real_carrier_verified": False,
            "production_ready": False,
        }
    finally:
        await provider.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-number", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(_verify(args.order_number))
    except (ProviderError, RuntimeError, ValueError) as exc:
        print(f"Saleor provider verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
