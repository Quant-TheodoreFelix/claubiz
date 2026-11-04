import ssl

import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from .config import Settings
from .secrets import SecretValue

FINGERPRINT_HEADERS = ("x-stainless-os", "x-stainless-arch", "x-stainless-runtime", "x-stainless-runtime-version")


class EgressBlocked(httpx2.TransportError):
    pass


class EgressGuardTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, allowed_hosts: frozenset[str]) -> None:
        self._inner = inner
        self._allowed = allowed_hosts

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        if url.scheme != "https" or url.host not in self._allowed or url.port not in (None, 443):
            raise EgressBlocked(f"허용되지 않은 외부 통신 차단: {url.scheme}://{url.host}", request=request)
        for name in FINGERPRINT_HEADERS:
            if name in request.headers:
                del request.headers[name]
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_ssl_context(ca_bundle: str) -> ssl.SSLContext:
    if ca_bundle:
        ctx = ssl.create_default_context(cafile=ca_bundle)
    else:
        try:
            import truststore

            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def build_client(settings: Settings, api_key: SecretValue) -> AsyncAnthropic:
    api = settings.api
    inner = httpx2.AsyncHTTPTransport(verify=build_ssl_context(api.ca_bundle), proxy=api.proxy or None)
    guard = EgressGuardTransport(inner, frozenset(api.egress_allowlist))
    http_client = DefaultAsyncHttpxClient(transport=guard, follow_redirects=False, trust_env=False)
    headers = {"anthropic-workspace-id": api.workspace_id} if api.workspace_id else None
    return AsyncAnthropic(
        api_key=api_key.reveal(),
        base_url=api.base_url,
        default_headers=headers,
        http_client=http_client,
        max_retries=api.max_retries,
        timeout=api.timeout_seconds,
    )
