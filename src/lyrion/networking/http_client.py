"""
Async HTTP client built on httpx.AsyncClient.

Features:
  - Connection pooling (managed by httpx)
  - Streaming downloads with progress callbacks
  - Retry logic with exponential backoff
  - Configurable timeouts
  - Graceful error handling

This replaces the Perl Slim::Networking::SimpleHTTP / SimpleSyncHTTP modules.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Sequence

import httpx

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RETRIES = 3
INITIAL_BACKOFF = 0.5  # seconds


# ---------------------------------------------------------------------------
# Progress callback type
# ---------------------------------------------------------------------------
ProgressCallback = Callable[[int, int | None], None | Coroutine[Any, Any, None]]
"""Called with (bytes_downloaded, total_bytes). total_bytes may be None if unknown."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class HTTPDownloadResult:
    """Result of a successful HTTP download."""

    content: bytes
    status_code: int
    headers: httpx.Headers
    url: str

    @classmethod
    def from_response(cls, response: httpx.Response) -> HTTPDownloadResult:
        return cls(
            content=response.content,
            status_code=response.status_code,
            headers=response.headers,
            url=str(response.url),
        )


# ---------------------------------------------------------------------------
# HTTPClient
# ---------------------------------------------------------------------------


class HTTPClient:
    """Async HTTP client with connection pooling, retries, and streaming.

    Wraps httpx.AsyncClient to add:
      - Exponential-backoff retry for transient failures
      - Streaming download with progress callbacks
      - Per-request timeout overrides
      - Common LMS headers (User-Agent, Accept)
    """

    def __init__(
        self,
        base_url: str = "",
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        headers: dict[str, str] | None = None,
        limits: httpx.Limits | None = None,
    ):
        self._base_url = base_url
        self._timeout = (
            httpx.Timeout(timeout) if isinstance(timeout, float) else timeout
        )
        self._max_retries = max_retries
        self._default_headers = {
            "User-Agent": "Lyrion/9.2.0",
            "Accept": "*/*",
        }
        if headers:
            self._default_headers.update(headers)

        self._limits = limits or httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
        )
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> HTTPClient:
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the underlying httpx client."""
        async with self._lock:
            if self._client is not None:
                return
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._default_headers,
                limits=self._limits,
                follow_redirects=True,
            )

    async def stop(self) -> None:
        """Close the underlying client."""
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HTTPClient not started — call start() first")
        return self._client

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform an HTTP request with exponential-backoff retry."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await self.client.request(method, url, **kwargs)
                # Retry on 5xx and specific 4xx
                if response.status_code >= 500 and attempt < self._max_retries - 1:
                    backoff = INITIAL_BACKOFF * (2**attempt)
                    logger.warning(
                        "HTTP %s %s → %d, retrying in %.1fs (attempt %d/%d)",
                        method,
                        url,
                        response.status_code,
                        backoff,
                        attempt + 1,
                        self._max_retries,
                    )
                    await asyncio.sleep(backoff)
                    continue
                return response
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    backoff = INITIAL_BACKOFF * (2**attempt)
                    logger.warning(
                        "HTTP %s %s → %s, retrying in %.1fs (attempt %d/%d)",
                        method,
                        url,
                        exc,
                        backoff,
                        attempt + 1,
                        self._max_retries,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "HTTP %s %s failed after %d attempts: %s",
                        method,
                        url,
                        self._max_retries,
                        exc,
                    )
        if last_exc:
            raise last_exc
        raise httpx.HTTPError("Request failed after retries")

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def get(
        self,
        url: str,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a GET request with retry."""
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        if headers:
            kwargs["headers"] = headers
        return await self._request_with_retry("GET", url, **kwargs)

    async def download(
        self,
        url: str,
        dest: BinaryIO | None = None,
        progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> HTTPDownloadResult:
        """Download a URL, optionally streaming to a file with progress callbacks.

        If dest is None, the full content is accumulated in memory and returned
        in HTTPDownloadResult.content.

        progress callback receives (bytes_read, total_bytes) where total_bytes
        may be None if the server doesn't send Content-Length.
        """
        kwargs: dict[str, Any] = {"stream": True}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        response = await self._request_with_retry("GET", url, **kwargs)
        response.raise_for_status()

        total = response.headers.get("content-length")
        total_bytes: int | None = int(total) if total is not None else None
        downloaded = 0

        async def run_progress(n: int) -> None:
            nonlocal downloaded
            downloaded += n
            if progress:
                result = progress(downloaded, total_bytes)
                if asyncio.iscoroutine(result):
                    await result

        if dest is None:
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                content.extend(chunk)
                await run_progress(len(chunk))
            return HTTPDownloadResult(
                content=bytes(content),
                status_code=response.status_code,
                headers=response.headers,
                url=str(response.url),
            )
        else:
            async for chunk in response.aiter_bytes(chunk_size=65536):
                dest.write(chunk)
                await run_progress(len(chunk))
            return HTTPDownloadResult(
                content=b"",
                status_code=response.status_code,
                headers=response.headers,
                url=str(response.url),
            )

    async def post(
        self,
        url: str,
        data: dict[str, Any] | bytes | None = None,
        json: Any = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a POST request with retry."""
        kwargs: dict[str, Any] = {}
        if data is not None:
            kwargs["content"] = data
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        if headers:
            kwargs["headers"] = headers
        return await self._request_with_retry("POST", url, **kwargs)

    async def head(
        self,
        url: str,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Perform a HEAD request."""
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        return await self._request_with_retry("HEAD", url, **kwargs)

    async def get_json(
        self,
        url: str,
        timeout: float | None = None,
    ) -> Any:
        """GET a URL and parse the response as JSON."""
        response = await self.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
