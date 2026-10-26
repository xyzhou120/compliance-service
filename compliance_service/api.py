"""Injected HTTP adapter; importing it never starts a worker or external call."""

from pathlib import Path
import hmac
import json
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .contracts import Conflict, DEFINITIONS, DOCUMENT_DEFINITION_VERSION, InvalidRequest, NotFound

DEBUG_WEB_ROOT = Path(__file__).with_name("web")


def create_app(service: Any) -> FastAPI:
    app = FastAPI(title="Compliance inspection service", version=__version__)

    def inspection_registry():
        return service.inspection_registry

    def authorized(request: Request, mine_code: str | None = None):
        expected = getattr(service, "task_token", None)
        supplied = request.headers.get("X-Inspection-Token", "")
        if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise NotFound("task principal is not authorized")
        mines = getattr(service, "allowed_mines", set())
        if mine_code is not None and mine_code not in mines:
            raise NotFound("mine not authorized")
        return mines

    @app.middleware("http")
    async def restrict_production_routes(request: Request, call_next):
        if getattr(service, "task_token", None):
            path = request.url.path
            allowed = path in {getattr(service, "check_url", "/inspection/tasks"), "/health", "/v1/definitions"} or (
                (path.startswith("/v1/runs/") or path.startswith(getattr(service, "check_url", "/inspection/tasks") + "/")) and request.method == "GET")
            if not allowed:
                return JSONResponse(status_code=404, content={"detail": "route not available"})
        return await call_next(request)

    @app.get("/debug", include_in_schema=False)
    @app.get("/debug/", include_in_schema=False)
    def debug_page():
        frontend_index = DEBUG_WEB_ROOT / "index.html"
        if frontend_index.is_file():
            return FileResponse(frontend_index, media_type="text/html; charset=utf-8")
        return FileResponse(Path(__file__).with_name("debug.html"), media_type="text/html; charset=utf-8")

    @app.get("/debug/legacy", include_in_schema=False)
    def legacy_debug_page():
        return FileResponse(Path(__file__).with_name("debug.html"), media_type="text/html; charset=utf-8")

    frontend_assets = DEBUG_WEB_ROOT / "assets"
    if frontend_assets.is_dir():
        app.mount("/debug/assets", StaticFiles(directory=frontend_assets), name="debug-assets")

    @app.exception_handler(InvalidRequest)
    async def invalid_request(_request: Request, exc: InvalidRequest):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NotFound)
    async def not_found(_request: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        configured = bool(getattr(service, "task_token", None))
        profile = getattr(service, "environment", "local") if configured else "local-fixture"
        response = {"status": "ok", "profile": profile, "real_connectivity": False}
        if configured:
            response["configured_adapters"] = getattr(service, "repository", None) is not None
        return response

    @app.get("/v1/definitions")
    def definitions():
        from .rulebook import load_rulebook
        baseline = load_rulebook()
        configured = inspection_registry().read()
        items = configured["config"]["items"]
        return {
            "profile": getattr(service, "environment", "local") if getattr(service, "task_token", None) else "local-fixture",
            "available_document_rulebook": {"version": baseline["version"], "revision": baseline["revision"],
                                            "status": baseline["status"], "source": baseline["source"]},
            "inspection_config_revision": configured["revision"],
            "items": [{**item, "engineering_mock": not bool(getattr(service, "task_token", None)),
                        "document_definition_version": DOCUMENT_DEFINITION_VERSION,
                        "document_mode": "evidence-bound-deterministic"} for item in items],
        }

    @app.post("/v1/manual/runs", status_code=202)
    @app.post("/v1/upstream/runs", status_code=202)
    def submit(body: dict):
        return service.submit(body)

    @app.post("/v1/debug/upstream/tasks", status_code=202)
    def debug_upstream_task(body: dict, background_tasks: BackgroundTasks):
        if getattr(service, "task_token", None):
            raise NotFound("local debug task route is disabled for configured production adapters")
        accepted = service.submit_debug_task(body)
        if not getattr(service, "external_worker", False):
            background_tasks.add_task(service.execute_debug_items, accepted["items"])
        return accepted

    @app.get("/v1/debug/task-options")
    def debug_task_options():
        configured = inspection_registry().read()
        return {"oceanbase_available": service.repository is not None and service.acquisition is not None,
                "sourceSystem": service.source_system, "environment": service.environment,
                "allowed_mines": sorted(service.allowed_mines), "callbackMode": "none",
                "inspection_config": configured["config"], "inspection_config_revision": configured["revision"]}

    @app.get("/v1/debug/inspection-config")
    def get_inspection_config():
        if getattr(service, "task_token", None) or service.environment == "production":
            raise NotFound("inspection configuration management is unavailable in production mode")
        return inspection_registry().read()

    @app.post("/v1/debug/inspection-config")
    def save_inspection_config(body: dict):
        if getattr(service, "task_token", None) or service.environment == "production":
            raise NotFound("inspection configuration management is unavailable in production mode")
        if (not isinstance(body, dict) or set(body) != {"config", "expectedRevision"}
                or not isinstance(body["expectedRevision"], str) or not body["expectedRevision"].strip()):
            raise InvalidRequest("configuration save requires config and expectedRevision; reload before saving")
        return inspection_registry().save(body["config"], expected_revision=body.get("expectedRevision"))

    @app.get("/v1/debug/executions")
    def execution_history(mine_code: str | None = None, check_cycle: str | None = None,
                          offset: int = 0, limit: int = 20):
        if getattr(service, "task_token", None) or service.environment == "production":
            raise NotFound("local execution history is unavailable in production mode")
        return service.ledger.list_executions(mine_code=mine_code, check_cycle=check_cycle,
                                              offset=offset, limit=limit)

    @app.post(getattr(service, "check_url", "/inspection/tasks"))
    async def inspection_task(request: Request):
        # The production gateway supplies the authenticated mine allowlist. A
        # local process must opt in explicitly rather than trusting mineId.
        mines = authorized(request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidRequest("task must be valid JSON") from exc
        accepted = await run_in_threadpool(service.receive_task, body, allowed_mines=mines)
        from .upstream import control_response
        return JSONResponse(content=control_response(), headers={"X-Inspection-Task": accepted["task_key"]})

    @app.get(getattr(service, "check_url", "/inspection/tasks") + "/{task_key}")
    def task_status(request: Request, task_key: str, mine_id: str):
        authorized(request, mine_id)
        return service.ledger.get_task(task_key, service.source_system, mine_id, service.environment)

    @app.get("/v1/debug/material-groups")
    def material_groups():
        from .debug_uploads import MATERIAL_GROUPS
        items = inspection_registry().read()["config"]["items"]
        return {"groups": [{**group, "handlers": group["items"], "items": [item["itemCode"] for item in items
                                                if item["enabled"] and item["handler"] in group["items"]]}
                           for group in MATERIAL_GROUPS]}

    @app.post("/v1/debug/bundles", status_code=202)
    async def submit_bundle(request: Request):
        from .debug_uploads import read_bundle_body, submit_bundle
        body = await read_bundle_body(request)
        return await run_in_threadpool(submit_bundle, service, body)

    @app.get("/v1/runs/{run_id}")
    def get_run(request: Request, run_id: str, source_system: str, mine_code: str, environment: str):
        if environment == "production" or getattr(service, "task_token", None):
            authorized(request, mine_code)
            if (source_system, environment) != (service.source_system, service.environment):
                raise NotFound("run not found")
        return service.ledger.get(run_id, source_system, mine_code, environment)

    @app.get("/v1/runs/{run_id}/progress")
    def get_progress(request: Request, run_id: str, source_system: str, mine_code: str, environment: str,
                     after: int = 0, limit: int = 100):
        if environment == "production" or getattr(service, "task_token", None):
            authorized(request, mine_code)
            if (source_system, environment) != (service.source_system, service.environment):
                raise NotFound("run not found")
        return service.ledger.get_progress(run_id, source_system, mine_code, environment,
                                           after=after, limit=limit)

    from .debug_views import register_debug_views
    register_debug_views(app, service)
    return app
