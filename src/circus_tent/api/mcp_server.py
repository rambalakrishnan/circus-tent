"""FastMCP server over Streamable HTTP. See spec.

Caller identity: MCP tools resolve the caller from the transport request's
Authorization header (request_context.request.headers on Streamable HTTP in
mcp 1.28); when unavailable the tool raises an unauthenticated tool error.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from circus_tent.api.service import Service


def _authorization_from(ctx: Context[Any, Any, Any]) -> str | None:
    try:
        req = getattr(ctx.request_context, "request", None)
        headers = getattr(req, "headers", None)
        if headers is not None:
            value = headers.get("authorization") or headers.get("Authorization")
            return str(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None
    return None


def build_mcp_server(service: Service) -> FastMCP:
    mcp = FastMCP("circus-tent")

    def _caller(ctx: Context[Any, Any, Any]) -> Any:
        auth_header = _authorization_from(ctx)
        if not auth_header:
            raise RuntimeError("unauthenticated: missing Authorization header on MCP request")
        return service.auth.authenticate(auth_header)

    @mcp.tool()
    async def run(
        ctx: Context[Any, Any, Any],
        domain: str,
        steps: list[dict[str, Any]],
        idempotency_key: str,
        account: str | None = None,
        session_id: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """Execute a step sequence against a target domain (idempotency_key required)."""
        caller = _caller(ctx)
        result = await service.run(
            caller,
            {
                "domain": domain,
                "steps": steps,
                "idempotency_key": idempotency_key,
                "account": account,
                "session_id": session_id,
                "callback_url": callback_url,
            },
        )
        return {
            "run_id": result.run_id,
            "status": result.status,
            "resumed_from": result.resumed_from,
            "steps": [
                {"id": s.id, "status": s.status, "tier": s.tier, "error": s.error}
                for s in result.steps
            ],
            "extracted": result.extracted,
            "session": result.session,
        }

    @mcp.tool()
    async def extract(
        ctx: Context[Any, Any, Any],
        domain: str,
        url: str,
        query: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Extract markdown/structured content from a URL via the domain's shard."""
        caller = _caller(ctx)
        result = await service.extract(
            caller, {"domain": domain, "url": url, "query": query, "schema": schema}
        )
        return {
            "markdown": result.markdown,
            "structured": result.structured,
            "schema_incomplete": result.schema_incomplete,
        }

    @mcp.tool()
    async def health() -> dict[str, Any]:
        """Shard health, tab utilization, budget and breaker state."""
        return service.health()

    @mcp.tool()
    async def list_shards() -> dict[str, Any]:
        """Shard topology and fingerprint status."""
        return service.shards()

    return mcp
