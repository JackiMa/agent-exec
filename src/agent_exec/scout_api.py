"""Parent-scoped API: issue read-only scout jobs without owner credentials."""
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from .server import _call
from . import __version__


def scout_router(service: Any) -> APIRouter:
    router = APIRouter(prefix="/v1/scouts")

    async def parent(authorization: str | None = Header(default=None)) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256:
            raise HTTPException(401, "Invalid scout delegation credential")
        return await _call(service.scout_parent, token)

    async def scoped(method: str, parent_id: str, run_id: str, **kwargs: Any) -> Any:
        await _call(service.scout_access, parent_id, run_id)
        return await _call(getattr(service, method), run_id, **kwargs)

    @router.get("/health")
    async def health(parent_id: str = Depends(parent)) -> Any:
        return {"status": "ok", "version": __version__, "parent_run_id": parent_id}

    @router.get("/capabilities")
    async def capabilities(parent_id: str = Depends(parent)) -> Any:
        return await _call(service.scout_capabilities, parent_id)

    @router.post("/plan")
    async def plan(request: Request, parent_id: str = Depends(parent)) -> Any:
        return await _call(service.plan_scout, parent_id, await request.json())

    @router.post("/runs", status_code=202)
    async def submit(request: Request, parent_id: str = Depends(parent), idempotency_key: str | None = Header(default=None)) -> Any:
        return await _call(service.submit_scout, parent_id, await request.json(), idempotency_key=idempotency_key)

    @router.get("/runs")
    async def runs(parent_id: str = Depends(parent), limit: int = Query(default=100)) -> Any:
        return await _call(service.list_scouts, parent_id, limit=limit)

    @router.get("/runs/{run_id}")
    async def status(run_id: str, parent_id: str = Depends(parent)) -> Any:
        return await scoped("get", parent_id, run_id)

    @router.get("/runs/{run_id}/events")
    async def events(run_id: str, parent_id: str = Depends(parent), after: int = Query(default=0), limit: int = Query(default=200)) -> Any:
        return await scoped("events", parent_id, run_id, after=after, limit=limit)

    @router.get("/runs/{run_id}/logs")
    async def logs(run_id: str, parent_id: str = Depends(parent), stream: str = Query(default="stdout"), offset: int = Query(default=0), limit: int = Query(default=65536)) -> Any:
        return await scoped("logs", parent_id, run_id, stream=stream, offset=offset, limit=limit)

    @router.get("/runs/{run_id}/result")
    async def result(run_id: str, parent_id: str = Depends(parent)) -> Any:
        return await scoped("result", parent_id, run_id)

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str, parent_id: str = Depends(parent)) -> Any:
        return await scoped("cancel", parent_id, run_id)

    return router
