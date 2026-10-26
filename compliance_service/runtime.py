"""Opt-in production adapter wiring; loading configuration never connects.

The deployment supplies an already-installed MySQL-compatible DB-API driver
and environment credentials. This module does not install a driver, infer an
OceanBase tenant mode, or log any resolved secret.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
import tomllib
from typing import Mapping

from .contracts import InvalidRequest, required_text


def load_production_settings(path) -> dict:
    try:
        with Path(path).open("rb") as stream:
            settings = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InvalidRequest("production configuration could not be read") from exc
    if set(settings) - {"service", "oceanbase", "callback", "attachments", "principals", "status_map"}:
        raise InvalidRequest("unknown production configuration sections")
    if not isinstance(settings.get("service"), dict):
        raise InvalidRequest("production configuration requires [service]")
    local_debug = settings["service"].get("local_debug", False) is True
    required_sections = ("service", "oceanbase", "attachments") + (() if local_debug else ("callback",))
    for section in required_sections:
        if not isinstance(settings.get(section), dict):
            raise InvalidRequest(f"production configuration requires [{section}]")
    if local_debug and settings.get("callback") is not None and not isinstance(settings.get("callback"), dict):
        raise InvalidRequest("callback must be an object when provided")
    return settings


def _secret(environment: Mapping[str, str], name: str, purpose: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise InvalidRequest(f"invalid environment variable name for {purpose}")
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"required environment variable is absent for {purpose}")
    return value


def connection_factory(settings: dict, *, env: Mapping[str, str] | None = None):
    """Return a lazy DB connection callable; driver/credentials are worker-only."""
    environment = os.environ if env is None else env
    if settings.get("compatibility_mode") != "mysql":
        raise InvalidRequest("oceanbase.compatibility_mode must explicitly be mysql")
    module = settings.get("driver_module")
    if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
        raise InvalidRequest("oceanbase.driver_module must identify an installed DB-API module")
    timeout = settings.get("connect_timeout_seconds", 10)
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise InvalidRequest("OceanBase connect timeout must be in (0, 60]")
    names = {key: settings.get(f"{key}_env", f"COMPLIANCE_OB_{key.upper()}")
             for key in ("host", "port", "database", "user", "password")}

    def connect():
        values = {key: _secret(environment, variable, f"OceanBase {key}") for key, variable in names.items()}
        try:
            port = int(values.pop("port"))
            if not 1 <= port <= 65535:
                raise ValueError()
        except ValueError as exc:
            raise InvalidRequest("OceanBase port must be between 1 and 65535") from exc
        try:
            driver = importlib.import_module(module)
        except ImportError:
            raise InvalidRequest("configured OceanBase DB-API driver is not installed") from None
        connector = getattr(driver, "connect", None)
        if not callable(connector):
            raise InvalidRequest("configured OceanBase driver has no connect function")
        try:
            # The adapter SQL uses MySQL %s placeholders. The deployment must
            # validate this keyword contract against its selected driver.
            return connector(**values, port=port, connect_timeout=timeout)
        except Exception:
            # Drivers sometimes embed DSNs or credentials in error messages.
            raise RuntimeError("OceanBase connection failed; inspect deployment connectivity securely") from None

    return connect


def _policy(settings: dict):
    from .callbacks import URLPolicy
    for name in ("allowed_hosts", "allowed_origins"):
        values = settings.get(name, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise InvalidRequest(f"network {name} must be a list of non-empty strings")
    for flag in ("allow_http", "allow_private", "allow_loopback"):
        if flag in settings and type(settings[flag]) is not bool:
            raise InvalidRequest(f"network {flag} must be boolean")
    try:
        return URLPolicy(
            allowed_hosts=frozenset(settings.get("allowed_hosts", [])),
            allowed_origins=frozenset(settings.get("allowed_origins", [])),
            allow_http=settings.get("allow_http", False),
            allow_private=settings.get("allow_private", False),
            allow_loopback=settings.get("allow_loopback", False),
        )
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


def build_service(db, production_config=None, *, env: Mapping[str, str] | None = None):
    """Build the local service, or explicitly configured upstream adapters."""
    from .ledger import Ledger
    from .service import InspectionService
    if production_config is None:
        return InspectionService(Ledger(Path(db)))
    settings = load_production_settings(production_config)
    environment = os.environ if env is None else env
    service = settings["service"]
    source_system = required_text(service.get("source_system"), "service.source_system")
    profile = required_text(service.get("environment"), "service.environment")
    if profile not in {"local", "test", "production"}:
        raise InvalidRequest("service.environment must be local, test or production")
    if type(service.get("local_debug", False)) is not bool:
        raise InvalidRequest("service.local_debug must be boolean")
    local_debug = service.get("local_debug", False)
    if local_debug and profile == "production":
        raise InvalidRequest("service.local_debug cannot be enabled in production")
    check_url = service.get("check_url", "/inspection/tasks")
    if (not isinstance(check_url, str) or not check_url.startswith("/") or check_url.startswith("//")
            or any(char in check_url for char in "?#{}") or any(char.isspace() for char in check_url)):
        raise InvalidRequest("service.check_url must be a static route path")
    reserved_routes = ("/v1", "/health", "/debug", "/docs", "/redoc", "/openapi.json")
    if any(check_url == route or check_url.startswith(route + "/") for route in reserved_routes):
        raise InvalidRequest("service.check_url collides with a reserved service route")
    principals = settings.get("principals", [])
    if not isinstance(principals, list) or len(principals) != 1:
        raise InvalidRequest("production configuration currently requires exactly one principal")
    task_principals, all_mines, tokens = [], set(), set()
    for principal in principals:
        if not isinstance(principal, dict) or set(principal) - {"token_env", "allowed_mines"}:
            raise InvalidRequest("invalid task principal configuration")
        mines = principal.get("allowed_mines")
        if not isinstance(mines, list) or not mines or any(not isinstance(mine, str) or not mine.strip() for mine in mines):
            raise InvalidRequest("principal allowed_mines must be a non-empty string list")
        if local_debug:
            token = None
        else:
            token = _secret(environment, principal.get("token_env"), "task bearer token")
        if token is not None and (len(token) < 16 or any(char.isspace() for char in token)):
            raise InvalidRequest("task bearer token must contain at least 16 non-whitespace characters")
        if token is not None and token in tokens:
            raise InvalidRequest("duplicate task principal token")
        if token is not None:
            tokens.add(token)
        task_principals.append({"token": token, "allowed_mines": set(mines)})
        all_mines.update(mines)
    status_map = settings.get("status_map", {})
    if not isinstance(status_map, dict) or any(not isinstance(key, str) or value not in {"running", "pause", "resume"}
                                               for key, value in status_map.items()):
        raise InvalidRequest("status_map must map negotiated strings to running/pause/resume")
    from .acquisition import AttachmentAcquisition
    from .callbacks import HttpCallbackClient
    from .inspection_config import InspectionConfigRegistry
    from .oceanbase import OceanBaseRepository
    inspection_config_path = service.get("inspection_config_path") or environment.get("COMPLIANCE_INSPECTION_CONFIG") or (Path(db).parent / "inspection-config.json")
    registry = InspectionConfigRegistry(inspection_config_path)
    registry.read()  # Validate the selected configuration before starting.
    db_settings = settings["oceanbase"]
    repository = OceanBaseRepository(
        connection_factory(db_settings, env=environment),
        compatibility_mode=db_settings["compatibility_mode"],
        table=db_settings.get("table", "mine_remote_disaster_file"),
        source_system=source_system, environment=profile,
        config_path=inspection_config_path,
        attachment_base_url=settings["attachments"].get("base_url"),
    )
    callback = None
    if not local_debug:
        callback_timeout = settings["callback"].get("timeout_seconds", 10)
        if type(callback_timeout) not in (int, float) or not 0 < callback_timeout < 30:
            raise InvalidRequest("callback timeout must be positive and shorter than the 30 second delivery lease")
        header_envs = settings["callback"].get("header_envs", {})
        if not isinstance(header_envs, dict) or any(not isinstance(name, str) for name in header_envs):
            raise InvalidRequest("callback.header_envs must map header names to environment variable names")
        headers = {name: _secret(environment, variable, "callback authentication header") for name, variable in header_envs.items()}
        callback = HttpCallbackClient(_policy(settings["callback"]), timeout=callback_timeout, extra_headers=headers)
    acquisition = AttachmentAcquisition(
        url_policy=_policy(settings["attachments"]),
        source_system=source_system, environment=profile,
        max_bytes=settings["attachments"].get("max_bytes", 50 * 1024 * 1024),
        tls_certificate_sha256=settings["attachments"].get("tls_certificate_sha256"),
    )
    result = InspectionService(
        Ledger(Path(db)), repository=repository, acquisition=acquisition,
        callback_client=callback, status_map=status_map,
        source_system=source_system, environment=profile, allowed_mines=all_mines,
        inspection_registry=registry, inspection_config_path=inspection_config_path,
    )
    result.task_principals = task_principals
    result.task_token = task_principals[0]["token"] if task_principals and task_principals[0]["token"] else None
    result.local_debug = local_debug
    result.check_url = check_url
    return result
