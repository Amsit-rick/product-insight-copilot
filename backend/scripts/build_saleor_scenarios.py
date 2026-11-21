"""Build the deterministic local Saleor scenario manifest."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import httpx

from sandbox_services.scenarios import load_scenario_manifest, write_scenario_manifest


SALEOR_SCENARIO_ORDERS_QUERY = """query SandboxScenarioOrders {
  orders(first: 100) {
    edges {
      node {
        number
        status
        total { gross { amount } }
        totalCharged { amount }
        totalRefunded { amount }
        fulfillments { id }
      }
    }
  }
}"""


def _amount(node: dict[str, Any], *path: str) -> str:
    current: Any = node
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise RuntimeError("Saleor scenario response is missing amount fields")
        current = current[key]
    return str(current)


def _summaries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("errors"):
        raise RuntimeError("Saleor scenario query returned errors")
    data = payload.get("data")
    orders = data.get("orders") if isinstance(data, dict) else None
    edges = orders.get("edges") if isinstance(orders, dict) else None
    if not isinstance(edges, list):
        raise RuntimeError("Saleor scenario response is missing orders")
    summaries: list[dict[str, Any]] = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            raise RuntimeError("Saleor scenario response contains an invalid order")
        summaries.append(
            {
                "number": str(node.get("number") or ""),
                "status": str(node.get("status") or ""),
                "total_amount": _amount(node, "total", "gross", "amount"),
                "charged_amount": _amount(node, "totalCharged", "amount"),
                "refunded_amount": _amount(node, "totalRefunded", "amount"),
                "has_fulfillment": bool(node.get("fulfillments")),
            }
        )
    return summaries


def build_manifest(
    *,
    graphql_url: str,
    app_token_file: Path,
    output_file: Path,
    timeout_seconds: float,
) -> int:
    if output_file.exists():
        manifest = load_scenario_manifest(output_file)
        print(f"Scenario manifest already exists: {len(manifest['scenarios'])} scenarios")
        return 0
    try:
        app_token = app_token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Saleor runtime credential is unavailable") from exc
    if not app_token:
        raise RuntimeError("Saleor runtime credential is unavailable")

    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(
            graphql_url,
            headers={"Authorization": f"Bearer {app_token}"},
            json={"query": SALEOR_SCENARIO_ORDERS_QUERY, "variables": {}},
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Saleor scenario query failed with status {response.status_code}"
        )
    if len(response.content) > 1_000_000:
        raise RuntimeError("Saleor scenario response exceeded the size limit")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError("Saleor scenario response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Saleor scenario response must be an object")

    manifest = write_scenario_manifest(output_file, _summaries(payload))
    print(f"Scenario manifest created: {len(manifest['scenarios'])} scenarios")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--graphql-url",
        default=os.getenv(
            "SALEOR_GRAPHQL_URL", "http://commerce-api:8000/graphql/"
        ),
    )
    parser.add_argument(
        "--app-token-file",
        type=Path,
        default=Path(
            os.getenv(
                "SALEOR_APP_TOKEN_FILE", "/run/commerce-credentials/app-token"
            )
        ),
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=Path(
            os.getenv(
                "SANDBOX_SCENARIO_MANIFEST_FILE",
                "/run/commerce-scenarios/manifest.json",
            )
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()
    return build_manifest(
        graphql_url=args.graphql_url,
        app_token_file=args.app_token_file,
        output_file=args.output_file,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
