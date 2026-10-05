import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import Settings
from storage.base import CapacityError, StorageError
from tiering.manager import TierManager


class ObjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class PutResult(BaseModel):
    key: str
    tier: str
    size_bytes: int


class GetResult(BaseModel):
    key: str
    value: str
    tier: str
    latency_ms: float


Key = Annotated[str, Path(min_length=1, max_length=512)]


def create_app(settings: Settings | None = None, manager: TierManager | None = None) -> FastAPI:
    configuration = settings or (manager.settings if manager else Settings.from_env())

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active = manager or TierManager(configuration)
        application.state.manager = active
        await active.start()
        try:
            yield
        finally:
            await active.close()

    application = FastAPI(title="AtlasTier", version="0.1.0", lifespan=lifespan)
    # Tests using ASGITransport can inject a manager without implicit background work.
    if manager is not None:
        application.state.manager = manager

    @application.middleware("http")
    async def instrument(request: Request, call_next):
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            label = route.path if route is not None else "unmatched"
            active = getattr(request.app.state, "manager", None)
            if active is not None:
                metrics = active.metrics
                metrics.requests.labels(method=request.method, route=label, status=str(status)).inc()
                metrics.latency.labels(method=request.method, route=label).observe(time.perf_counter() - started)

    @application.exception_handler(KeyError)
    async def missing_object(_request, _exc):
        return JSONResponse(status_code=404, content={"detail": "object not found"})

    @application.exception_handler(StorageError)
    async def storage_error(_request, exc):
        status = 507 if isinstance(exc, CapacityError) else 503
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @application.put("/objects/{key}", response_model=PutResult)
    async def put_object(key: Key, body: ObjectInput, request: Request):
        value = body.value.encode("utf-8")
        if len(value) > configuration.max_object_bytes:
            raise HTTPException(status_code=413, detail="object exceeds configured max_object_bytes")
        return await request.app.state.manager.put(key, value)

    @application.get("/objects/{key}", response_model=GetResult)
    async def get_object(key: Key, request: Request):
        return await request.app.state.manager.get(key)

    @application.delete("/objects/{key}", status_code=204)
    async def delete_object(key: Key, request: Request):
        await request.app.state.manager.delete(key)
        return Response(status_code=204)

    @application.get("/objects/{key}/metadata")
    async def metadata(key: Key, request: Request):
        return await request.app.state.manager.get_metadata(key)

    @application.get("/tiers")
    async def tiers(request: Request):
        return await request.app.state.manager.stats()

    @application.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        return Response(content=generate_latest(request.app.state.manager.metrics.registry),
                        headers={"Content-Type": CONTENT_TYPE_LATEST})

    @application.get("/healthz")
    async def health(request: Request):
        return {"status": "ok", "tiering_enabled": request.app.state.manager.settings.tiering_enabled}

    return application


app = create_app()
