"""Two tiny local HTTP clients can be started with different --client names."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class LocalHttpClient:
    def __init__(self, client_id: str, base_url: str = "http://127.0.0.1:8931"):
        self.client_id = client_id
        self.base_url = base_url.rstrip("/")

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Client-Id": self.client_id,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        request = urllib.request.Request(
            f"{self.base_url}/rpc/{method}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
                payload["http_status"] = response.status
                return payload
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"ok": False, "error": {"message": raw}}
            payload["http_status"] = exc.code
            return payload
        except OSError as exc:
            return {
                "ok": False,
                "http_status": 0,
                "error": {"code": "connection_error", "message": str(exc)},
            }

    def require(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        response = self.call(method, params, idempotency_key=idempotency_key)
        if not response.get("ok"):
            raise RuntimeError(f"{method} failed via {self.client_id}: {response}")
        return response["result"]
