"""Create the local Saleor runtime App identity without exposing its token.

This script is only used by the isolated commerce sandbox bootstrap container.
The administrator credential is discarded with that one-shot container. The
long-running fact provider receives only the generated App token through a
Docker volume.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


APP_IDENTIFIER = "product-insight-commerce-fact-reader"
LOGIN_MUTATION = """
mutation RuntimeBootstrapLogin($email: String!, $password: String!) {
  tokenCreate(email: $email, password: $password) {
    token
    errors { field message code }
  }
}
"""
LIST_APPS_QUERY = """
query RuntimeBootstrapApps {
  apps(first: 100) {
    edges { node { id identifier } }
  }
}
"""
DELETE_APP_MUTATION = """
mutation RuntimeBootstrapDelete($id: ID!) {
  appDelete(id: $id) { errors { field message code } }
}
"""
CREATE_APP_MUTATION = """
mutation RuntimeBootstrapCreate($input: AppInput!) {
  appCreate(input: $input) {
    app { id identifier }
    authToken
    errors { field message code }
  }
}
"""


class BootstrapError(RuntimeError):
    pass


def _graphql(
    url: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    payload = json.dumps(
        {"query": query, "variables": variables or {}},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise BootstrapError(f"Saleor returned HTTP {response.status}")
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errors"):
        raise BootstrapError("Saleor GraphQL request failed")
    return result.get("data") or {}


def _wait_for_saleor(url: str, attempts: int = 60) -> None:
    for attempt in range(1, attempts + 1):
        try:
            _graphql(url, "query RuntimeBootstrapReady { shop { name } }")
            return
        except (BootstrapError, OSError, urllib.error.URLError, ValueError):
            if attempt == attempts:
                break
            time.sleep(2)
    raise BootstrapError("Saleor did not become ready before the timeout")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise BootstrapError(f"Required environment variable is missing: {name}")
    return value


def main() -> int:
    url = _require("SALEOR_GRAPHQL_URL")
    email = _require("SALEOR_SEED_ADMIN_EMAIL")
    password = _require("SALEOR_SEED_ADMIN_PASSWORD")
    token_path = Path(
        os.getenv(
            "SALEOR_APP_TOKEN_FILE",
            "/run/commerce-credentials/app-token",
        )
    )

    try:
        _wait_for_saleor(url)
        login = _graphql(
            url,
            LOGIN_MUTATION,
            {"email": email, "password": password},
        ).get("tokenCreate") or {}
        admin_token = login.get("token")
        if not admin_token:
            raise BootstrapError("Saleor bootstrap administrator login failed")

        apps = _graphql(url, LIST_APPS_QUERY, token=admin_token)
        for edge in ((apps.get("apps") or {}).get("edges") or []):
            app = edge.get("node") or {}
            if app.get("identifier") == APP_IDENTIFIER and app.get("id"):
                deleted = _graphql(
                    url,
                    DELETE_APP_MUTATION,
                    {"id": app["id"]},
                    token=admin_token,
                ).get("appDelete") or {}
                if deleted.get("errors"):
                    raise BootstrapError("Existing runtime App could not be replaced")

        created = _graphql(
            url,
            CREATE_APP_MUTATION,
            {
                "input": {
                    "name": "Product Insight Commerce Fact Reader",
                    "identifier": APP_IDENTIFIER,
                    "permissions": ["MANAGE_ORDERS"],
                }
            },
            token=admin_token,
        ).get("appCreate") or {}
        if created.get("errors") or not created.get("authToken"):
            raise BootstrapError("Saleor runtime App creation failed")

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(created["authToken"], encoding="utf-8")
        token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print("Saleor runtime identity created successfully")
        return 0
    except (BootstrapError, OSError, urllib.error.URLError, ValueError) as exc:
        print(f"Saleor runtime identity bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
