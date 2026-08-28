"""Thin read-only HTTP client for a remote FastDB server.

The server's REST API lives under the ``/fastdb/api`` prefix. This module
reimplements just the read operations the MCP tools need, using ``httpx``
directly so the package has no dependency on the (heavier) ``fastdb`` runtime.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import DatabaseConfig, ServerConfig

_PREFIX = "/fastdb/api"


class FastDBError(RuntimeError):
    """Raised when the remote server returns a non-200 response."""


class RemoteFastDB:
    """Read-only HTTP client for a remote FastDB server."""

    def __init__(
        self,
        base_url: str,
        db_name: str,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._db_name = db_name
        self._timeout = timeout
        self._base_url = self._normalize_url(base_url)
        headers: dict[str, str] = {}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.rstrip("/")
        if url.endswith(_PREFIX):
            url = url[: -len(_PREFIX)]
        return f"{url}{_PREFIX}"

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._client.request(method, url, **kwargs)
        if response.status_code != 200:
            body = response.text
            try:
                err = response.json().get("error") or {}
                body = err.get("message", body)
            except Exception:
                pass
            raise FastDBError(f"HTTP {response.status_code}: {body}")
        return response.json()

    def search(
        self,
        query: str,
        topk: int = 10,
        use_rrf: bool | None = None,
        rrf_k: int | None = None,
        alpha: float | None = None,
        threshold: float | None = None,
        filters: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "query": query,
            "topk": topk,
        }
        if use_rrf is not None:
            body["use_rrf"] = use_rrf
        if rrf_k is not None:
            body["rrf_k"] = rrf_k
        if alpha is not None:
            body["alpha"] = alpha
        if threshold is not None:
            body["threshold"] = threshold
        if filters is not None:
            body["filter"] = filters
        return self._request("POST", f"/databases/{self._db_name}/search", json=body)["results"]

    def query(self, filters: str, limit: int = 1000) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"filter": filters, "limit": limit}
        return self._request("POST", f"/databases/{self._db_name}/query", json=body)["results"]

    def grep(
        self,
        text_pattern: str,
        path_pattern: str | None = None,
        topk: int = 10,
        filters: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"text_pattern": text_pattern, "topk": topk}
        if path_pattern is not None:
            body["path_pattern"] = path_pattern
        if filters is not None:
            body["filter"] = filters
        return self._request("POST", f"/databases/{self._db_name}/grep", json=body)["results"]

    def list_files(
        self,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None or offset is not None:
            actual_limit = limit if limit is not None else 100
            actual_offset = offset if offset is not None else 0
            response = self._request(
                "GET",
                f"/databases/{self._db_name}/files/",
                params={"limit": actual_limit, "offset": actual_offset},
            )
            return response.get("items", [])

        all_items: list[dict[str, Any]] = []
        page_offset = 0
        page_limit = 100
        while True:
            response = self._request(
                "GET",
                f"/databases/{self._db_name}/files/",
                params={"limit": page_limit, "offset": page_offset},
            )
            items = response.get("items", [])
            all_items.extend(items)
            total = response.get("total", 0)
            if len(all_items) >= total or not items:
                break
            page_offset += page_limit
        return all_items

    def get_stats(self) -> dict[str, int]:
        return self._request("GET", f"/databases/{self._db_name}/stats")

    def toc(self, file_path: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/databases/{self._db_name}/toc",
            params={"file_path": file_path},
        )


class ClientPool:
    """Lazily creates and reuses one ``RemoteFastDB`` client per configured database."""

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._clients: dict[str, RemoteFastDB] = {}

    def get(self, name: str | None = None) -> RemoteFastDB:
        db = self._config.get(name)
        if db.name not in self._clients:
            self._clients[db.name] = RemoteFastDB(
                base_url=db.base_url,
                db_name=db.db_name,
                api_key=db.api_key,
                timeout=db.timeout,
            )
        return self._clients[db.name]

    def database(self, name: str | None = None) -> DatabaseConfig:
        return self._config.get(name)

    def names(self) -> list[str]:
        return self._config.names()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()