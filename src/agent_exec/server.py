"""Authenticated HTTP transport for the agent execution service."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__


MAX_REQUEST_BODY = 256 * 1024
MIN_REMOTE_TOKEN_LENGTH = 32


class _BodyLimitMiddleware:
    """Buffer and bound request bodies before they reach application handlers."""

    def __init__(self, app: Callable[..., Awaitable[None]], limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await JSONResponse({"detail": "invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
            if content_length < 0:
                await JSONResponse({"detail": "invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
            if content_length > self.limit:
                await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
                return

        messages: list[dict[str, Any]] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            size += len(message.get("body", b""))
            if size > self.limit:
                await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> dict[str, Any]:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


def _read_token(token_file: str | Path | None) -> str | None:
    if token_file is None:
        return None
    try:
        token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return token or None


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_bind_auth(host: str, token: str | None) -> None:
    if not _is_loopback(host) and (token is None or len(token) < MIN_REMOTE_TOKEN_LENGTH):
        raise RuntimeError(
            f"refusing non-loopback bind without a token of at least {MIN_REMOTE_TOKEN_LENGTH} characters"
        )


def _service_http_error(exc: Exception) -> HTTPException:
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    if isinstance(status, int) and 400 <= status <= 599 and isinstance(detail, str):
        return HTTPException(status_code=status, detail=detail)
    return HTTPException(status_code=500, detail="internal service error")


async def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await run_in_threadpool(method, *args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        raise _service_http_error(exc) from exc


def create_app(
    service: Any,
    *,
    token: str | None = None,
    token_file: str | Path | None = None,
    max_body_bytes: int = MAX_REQUEST_BODY,
) -> FastAPI:
    """Create an HTTP app around an already-owned service instance."""

    configured_token = token if token is not None else _read_token(token_file)
    app = FastAPI(
        title="agent-exec",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(_BodyLimitMiddleware, limit=max_body_bytes)

    @app.exception_handler(json.JSONDecodeError)
    async def invalid_json(_request: Request, _exc: json.JSONDecodeError) -> JSONResponse:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=422)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        scheme, _, candidate = (authorization or "").partition(" ")
        expected = configured_token or "\0" * 32
        matches = hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
        valid = scheme.lower() == "bearer" and bool(candidate) and matches
        if configured_token is None:
            raise HTTPException(status_code=503, detail="service authentication is not configured")
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz")
    async def public_health() -> Any:
        try:
            health = await _call(service.health)
        except HTTPException:
            return JSONResponse({"status": "error", "version": __version__}, status_code=503)
        return {"status": health.get("status", "ok"), "version": health.get("version", __version__)}

    router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])

    @router.get("/health")
    async def health() -> Any:
        return await _call(service.health)

    @router.get("/capabilities")
    async def capabilities() -> Any:
        return await _call(service.capabilities)

    @router.post("/runs", status_code=202)
    async def submit(request: Request, idempotency_key: str | None = Header(default=None)) -> Any:
        payload = await request.json()
        return await _call(service.submit, payload, idempotency_key=idempotency_key)

    @router.post("/plan")
    async def plan(request: Request) -> Any:
        return await _call(service.plan, await request.json())

    @router.get("/runs")
    async def list_runs(limit: int = Query(default=100)) -> Any:
        return await _call(service.list_runs, limit=limit)

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> Any:
        return await _call(service.get, run_id)

    @router.get("/runs/{run_id}/events")
    async def events(run_id: str, after: int = Query(default=0), limit: int = Query(default=200)) -> Any:
        return await _call(service.events, run_id, after=after, limit=limit)

    @router.get("/runs/{run_id}/logs")
    async def logs(
        run_id: str,
        stream: str = Query(default="stdout"),
        offset: int = Query(default=0),
        limit: int = Query(default=65536),
    ) -> Any:
        return await _call(service.logs, run_id, stream=stream, offset=offset, limit=limit)

    @router.get("/runs/{run_id}/result")
    async def result(run_id: str) -> Any:
        return await _call(service.result, run_id)

    @router.get("/runs/{run_id}/diff")
    async def diff(run_id: str) -> Any:
        return await _call(service.diff, run_id)

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> Any:
        return await _call(service.cancel, run_id)

    @router.post("/runs/{run_id}/retry")
    async def retry(run_id: str) -> Any:
        return await _call(service.retry, run_id)

    @router.post("/runs/{run_id}/verdict")
    async def verdict(run_id: str, request: Request) -> Any:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="verdict body must be an object")
        if set(payload) != {"accepted", "reason", "evidence"}:
            raise HTTPException(status_code=422, detail="verdict requires only accepted, reason and evidence")
        try:
            accepted = payload["accepted"]
            reason = payload["reason"]
            evidence = payload["evidence"]
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"missing verdict field: {exc.args[0]}") from None
        return await _call(service.verdict, run_id, accepted=accepted, reason=reason, evidence=evidence)

    app.include_router(router)
    from .scout_api import scout_router
    app.include_router(scout_router(service))
    return app


def serve(config_path: str | Path | None = None) -> None:
    """Load, own, and serve a Service until the HTTP server exits."""

    import uvicorn

    from .config import load_settings
    from .core import Service

    settings = load_settings(config_path)
    token = _read_token(settings.token_file)
    _check_bind_auth(settings.host, token)
    service = Service(settings)
    service.start()
    try:
        app = create_app(service, token=token)
        uvicorn.run(app, host=settings.host, port=settings.port)
    finally:
        service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-execd")
    parser.add_argument("--config", help="service configuration file")
    args = parser.parse_args(argv)
    serve(args.config)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
