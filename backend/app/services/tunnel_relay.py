"""Tunnel Relay — raw HTTP passthrough to user containers.

The simplest possible proxy: take the raw request bytes from the browser,
forward them verbatim to opencode inside the container, return the raw response
bytes. No JSON parsing, no reserialization, no body inspection — just bytes in,
bytes out.

This is deliberately dumb because opencode's session runner is sensitive to
subtle differences in the request body (field ordering, null vs missing keys).
A transparent byte-level proxy guarantees the container sees exactly what the
browser sent.
"""
import logging

import httpx

from ..config import settings
from .container_manager import container_manager

logger = logging.getLogger(__name__)


class TunnelRelay:
    """Forwards HTTP requests to user containers via the Docker network.

    A single long-lived httpx.AsyncClient is shared across all requests for
    connection pooling. The client is created lazily on first use.
    """

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        return self._client

    async def http_request(
        self,
        user_id: str,
        method: str,
        path: str,
        raw_body: bytes | None = None,
        params: dict | None = None,
        password: str = "",
        timeout: float = 60,
        headers: dict | None = None,
    ) -> dict:
        """Forward an HTTP request as raw bytes.

        Args:
            user_id: The user whose container to target.
            method: HTTP method (GET, POST, DELETE, PATCH, PUT).
            path: Path on the container (e.g. /api/session).
            raw_body: Raw request body bytes — forwarded verbatim.
            params: Query parameters.
            password: BasicAuth password for the container.
            timeout: Request timeout in seconds.
            headers: Extra headers to forward (e.g. Content-Type from original request).

        Returns:
            dict with keys: status, body (parsed if JSON, else raw text), headers, raw (bytes).
        """
        base_url = container_manager.get_container_url(user_id)
        url = f"{base_url}{path}"

        if params:
            from urllib.parse import urlencode
            qs = urlencode(params)
            url = f"{url}?{qs}" if "?" not in url else f"{url}&{qs}"

        auth = ("opencode", password) if password else None

        # Build forwarded headers — only pass through content-type, nothing else
        fwd_headers = {}
        if headers:
            ct = headers.get("content-type") or headers.get("Content-Type")
            if ct:
                fwd_headers["content-type"] = ct

        try:
            client = await self._get_client()

            # Use content= for raw bytes, NOT json= — this is the key fix.
            # json= would re-serialize and potentially alter the body.
            if method == "GET":
                r = await client.get(url, auth=auth)
            elif method == "POST":
                r = await client.post(url, content=raw_body, headers=fwd_headers, auth=auth)
            elif method == "DELETE":
                r = await client.delete(url, auth=auth)
            elif method == "PATCH":
                r = await client.patch(url, content=raw_body, headers=fwd_headers, auth=auth)
            elif method == "PUT":
                r = await client.put(url, content=raw_body, headers=fwd_headers, auth=auth)
            else:
                return {"status": 405, "body": {"error": f"Method {method} not allowed"}, "headers": {}, "raw": b""}

            # Parse response body for JSON endpoints, but also keep raw bytes
            resp_raw = r.content
            try:
                resp_body = r.json()
            except Exception:
                resp_body = r.text

            return {
                "status": r.status_code,
                "body": resp_body,
                "headers": dict(r.headers),
                "raw": resp_raw,
            }
        except httpx.ConnectError:
            logger.warning("Cannot connect to container for user %s at %s", user_id, url)
            return {
                "status": 503,
                "body": {"error": "Agent container not reachable. It may be starting up."},
                "headers": {},
                "raw": b"",
            }
        except httpx.TimeoutException:
            logger.warning("Request to container for user %s timed out", user_id)
            return {
                "status": 504,
                "body": {"error": "Agent container request timed out"},
                "headers": {},
                "raw": b"",
            }
        except Exception as e:
            logger.error("Tunnel relay error for user %s: %s", user_id, e)
            return {
                "status": 500,
                "body": {"error": f"Tunnel relay error: {str(e)}"},
                "headers": {},
                "raw": b"",
            }

    async def close(self):
        """Clean up the shared HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# Global singleton
tunnel_relay = TunnelRelay()
