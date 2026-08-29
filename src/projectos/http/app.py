"""FastAPI application factory for the ProjectOS control plane."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from projectos.auth import Actor, AuthPolicy, required_capability
from projectos.errors import AuthorizationError
from projectos.http.errors import json_error, register_exception_handlers
from projectos.http.routes.delivery import router as delivery_router
from projectos.http.routes.control import router as control_router
from projectos.http.routes.integrations import router as integrations_router
from projectos.http.routes.health import router as health_router
from projectos.http.routes.projection import router as projection_router
from projectos.http.routes.projects import router as projects_router
from projectos.http.routes.project_ops import router as project_ops_router
from projectos.http.routes.portfolio import router as portfolio_router
from projectos.http.routes.settings import router as settings_router
from projectos.paths import (
    DEFAULT_DB_PATH,
    DEFAULT_REGISTRY_PATH,
    DASHBOARD_DIST,
    dashboard_index,
    dashboard_is_built,
)
from projectos.services import ServiceContext


def create_app(
    *,
    registry_path: Path | str | None = None,
    db_path: Path | str | None = None,
    projectctl_runner=None,
    cursor_runner=None,
    skip_identity_validation: bool = False,
    auth_required: bool = False,
    actors: dict[str, str] | None = None,
) -> FastAPI:
    """Build a local API. Filesystem roots come from factory config, not request bodies."""
    app = FastAPI(
        title="ProjectOS",
        version="0.1.0",
        description="Local versioned control-plane API. Thin adapter over application services.",
    )
    app.state.service_context = ServiceContext(
        db_path=Path(db_path) if db_path is not None else DEFAULT_DB_PATH,
        registry_path=Path(registry_path)
        if registry_path is not None
        else DEFAULT_REGISTRY_PATH,
    )
    app.state.projectctl_runner = projectctl_runner
    app.state.cursor_runner = cursor_runner
    app.state.skip_identity_validation = skip_identity_validation
    app.state.auth_policy = AuthPolicy(required=auth_required, actors=dict(actors or {}))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8787",
            "http://localhost:8787",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["ETag", "X-Correlation-ID", "X-Request-ID"],
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        incoming = (
            request.headers.get("x-correlation-id")
            or request.headers.get("x-request-id")
            or ""
        ).strip()
        cid = incoming or str(uuid.uuid4())
        request.state.correlation_id = cid
        policy: AuthPolicy = request.app.state.auth_policy
        needed = required_capability(request.method, request.url.path)
        try:
            actor = policy.resolve(request.headers.get("x-projectos-actor"))
        except AuthorizationError as exc:
            if needed is None:
                actor = Actor("anonymous", "local")
            else:
                return json_error(403, "forbidden", str(exc), cid)
        request.state.actor = actor
        if needed and not actor.allows(needed):
            return json_error(
                403,
                "forbidden",
                f"{actor.actor_id} cannot {needed}",
                cid,
            )
        try:
            response = await call_next(request)
        except Exception:
            # exception handlers still see request.state.correlation_id
            raise
        response.headers["X-Correlation-ID"] = cid
        response.headers["X-Request-ID"] = cid
        return response

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(projects_router)
    app.include_router(portfolio_router)
    app.include_router(project_ops_router)
    app.include_router(projection_router)
    app.include_router(control_router)
    app.include_router(integrations_router)
    app.include_router(settings_router)
    app.include_router(delivery_router)
    _mount_dashboard(app)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built dashboard from the same loopback process as the API."""
    index = dashboard_index()
    if not dashboard_is_built():
        return
    assets = DASHBOARD_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="dashboard-assets")

    @app.get("/")
    def dashboard_root() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.get("/{full_path:path}")
    def dashboard_spa(full_path: str) -> FileResponse:
        target = DASHBOARD_DIST / full_path
        if target.is_file():
            if full_path.endswith(".html"):
                return FileResponse(target, headers={"Cache-Control": "no-cache"})
            return FileResponse(target)
        if full_path.startswith("projects") or full_path.startswith("settings"):
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        raise HTTPException(status_code=404, detail="not found")

