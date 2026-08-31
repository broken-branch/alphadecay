import hmac
import json
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from backend.app.api.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    OwnerSessionManager,
    SchedulerAuthenticator,
    SchedulerAuthError,
    SessionAuthError,
)
from backend.app.competition_archive import (
    CompetitionRecord,
    SQLAlchemyCompetitionArchiveRepository,
    UnavailableCompetitionArchiveReader,
)
from backend.app.config import Settings
from backend.app.contracts.v1 import (
    AccountRole,
    AutonomyStatusResponse,
    CompetitionPerformanceProofResponse,
    CompetitionRecordItem,
    CompetitionRecordResponse,
    HealthResponse,
    ProviderSettingsResponse,
    ProviderSettingsUpdateRequest,
    ReplayResponse,
    ReplayScenario,
    SchedulerTickResponse,
    SessionCreateRequest,
    SessionResponse,
)
from backend.app.contracts.v1.openapi_copy import (
    ANONYMOUS_TAG,
    AUTONOMY_DISABLE_DESCRIPTION,
    AUTONOMY_DISABLE_SUMMARY,
    AUTONOMY_ENABLE_DESCRIPTION,
    AUTONOMY_ENABLE_SUMMARY,
    AUTONOMY_STATUS_DESCRIPTION,
    AUTONOMY_STATUS_SUMMARY,
    COMPETITION_RECORD_DESCRIPTION,
    COMPETITION_RECORD_SUMMARY,
    CSRF_COOKIE_DESCRIPTION,
    CSRF_COOKIE_TITLE,
    HEALTH_DESCRIPTION,
    HEALTH_SUMMARY,
    INTERNAL_TAG,
    OPENAPI_TAGS,
    OWNER_RUN_DESCRIPTION,
    OWNER_RUN_SUMMARY,
    OWNER_SESSION_COOKIE_DESCRIPTION,
    OWNER_SESSION_COOKIE_TITLE,
    OWNER_TAG,
    PROOF_DESCRIPTION,
    PROOF_PUBLICATION_DESCRIPTION,
    PROOF_PUBLICATION_SUMMARY,
    PROOF_SUMMARY,
    PROVIDER_SETTINGS_CLEAR_DESCRIPTION,
    PROVIDER_SETTINGS_CLEAR_SUMMARY,
    PROVIDER_SETTINGS_REPLACE_DESCRIPTION,
    PROVIDER_SETTINGS_REPLACE_SUMMARY,
    PROVIDER_SETTINGS_STATUS_DESCRIPTION,
    PROVIDER_SETTINGS_STATUS_SUMMARY,
    REPLAY_DESCRIPTION,
    REPLAY_NOT_FOUND_DESCRIPTION,
    REPLAY_SUMMARY,
    SCHEDULER_TICK_DESCRIPTION,
    SCHEDULER_TICK_SUMMARY,
    SESSION_CREATE_DESCRIPTION,
    SESSION_CREATE_SUMMARY,
    SESSION_DELETE_DESCRIPTION,
    SESSION_DELETE_SUMMARY,
)
from backend.app.execution import Actor, ExecutionBlocked
from backend.app.performance import (
    NoEligiblePerformanceSnapshot,
    PerformanceProofIntegrityError,
    UnavailablePerformanceProofReader,
)
from backend.app.provider_settings import (
    CredentialCodecError,
    ProviderSettingsValidationError,
)
from backend.app.replay import run_replay
from backend.app.replay.runner import ReplayFixtureError
from backend.app.runtime import ProductionAgent, build_production_agent

_RUNTIME_STATE_FIELDS = (
    "settings",
    "production_agent",
    "persistence",
    "runtime_composition",
    "agent_run_service",
    "account_autonomy_service",
    "owner_provider_settings_service",
    "performance_publisher",
    "owner_session_manager",
    "scheduler_authenticator",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    production: ProductionAgent | None = None
    try:
        if os.getenv("APP_RUNTIME_CONFIG_REQUIRED") == "true":
            settings = Settings()
            production = await build_production_agent(
                settings,
                Path(__file__).parents[2] / "migrations",
            )
            owner_session_manager = OwnerSessionManager(
                access_code=settings.app_owner_access_code.get_secret_value(),
                signing_secret=settings.app_session_secret.get_secret_value(),
                allowed_origin=settings.app_allowed_origin,
            )
            scheduler_authenticator = SchedulerAuthenticator(
                settings.scheduler_token.get_secret_value()
            )
            app.state.settings = settings
            app.state.production_agent = production
            app.state.persistence = production.persistence
            app.state.runtime_composition = production.runtime
            app.state.agent_run_service = production.service
            app.state.account_autonomy_service = production.autonomy
            app.state.owner_provider_settings_service = production.provider_settings
            app.state.performance_proof_reader = production.persistence.performance_proof_reader
            app.state.performance_publisher = production.persistence.performance_repository
            sessions = getattr(production.persistence, "sessions", None)
            app.state.competition_archive_reader = (
                SQLAlchemyCompetitionArchiveRepository(sessions)
                if sessions is not None
                else UnavailableCompetitionArchiveReader()
            )
            app.state.owner_session_manager = owner_session_manager
            app.state.scheduler_authenticator = scheduler_authenticator
        yield
    finally:
        if production is not None:
            for name in _RUNTIME_STATE_FIELDS:
                if hasattr(app.state, name):
                    delattr(app.state, name)
            app.state.performance_proof_reader = UnavailablePerformanceProofReader()
            app.state.competition_archive_reader = UnavailableCompetitionArchiveReader()
            await production.aclose()


app = FastAPI(
    title="alphadecay",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
)
app.state.performance_proof_reader = UnavailablePerformanceProofReader()
app.state.competition_archive_reader = UnavailableCompetitionArchiveReader()
_PROVIDER_SETTINGS_PATH = "/api/owner/provider-settings"
_NO_STORE_PATHS = frozenset(
    {
        "/",
        "/api/competition-record",
        "/api/health",
        "/api/proof",
        "/api/session",
        "/docs",
        "/openapi.json",
    }
)


def _runtime_enabled() -> bool:
    return os.getenv("APP_RUNTIME_CONFIG_REQUIRED") == "true"


def _build_identifier() -> str:
    return os.getenv("RENDER_GIT_COMMIT") or os.getenv("APP_BUILD_ID") or "development"


@app.middleware("http")
async def apply_response_policy(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in _NO_STORE_PATHS or request.url.path.startswith(
        ("/api/internal/", "/api/owner/")
    ):
        response.headers["Cache-Control"] = "no-store"
    script_sources = "'self'"
    style_sources = "'self'"
    image_sources = "'self' data:"
    if request.url.path == "/docs":
        script_sources += " 'unsafe-inline' https://cdn.jsdelivr.net"
        style_sources += " 'unsafe-inline' https://cdn.jsdelivr.net"
        image_sources += " https://fastapi.tiangolo.com"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src {script_sources}; style-src {style_sources}; "
        f"img-src {image_sources}; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


_LOGIN_RESPONSES = {
    401: {"description": "AUTHENTICATION_FAILED"},
    403: {"description": "ORIGIN_REJECTED"},
    429: {"description": "AUTHENTICATION_RATE_LIMITED"},
    503: {"description": "OWNER_SESSION_UNAVAILABLE"},
}
_OWNER_RESPONSES = {
    403: {"description": "OWNER_REQUEST_REJECTED"},
    503: {"description": "OWNER_SESSION_UNAVAILABLE"},
}
_OWNER_PUBLICATION_RESPONSES = {
    **_OWNER_RESPONSES,
    409: {"description": "NO_ELIGIBLE_PERFORMANCE_SNAPSHOT"},
    503: {"description": "PERFORMANCE_PROOF_UNAVAILABLE"},
}
_AGENT_RUN_RESPONSES = {
    **_OWNER_RESPONSES,
    422: {"description": "RUN_INPUT_REJECTED"},
    503: {"description": "AGENT_RUN_UNAVAILABLE"},
}
_AUTONOMY_RESPONSES = {
    **_OWNER_RESPONSES,
    409: {"description": "AUTONOMY_SERVER_DISABLED"},
    422: {"description": "AUTONOMY_INPUT_REJECTED"},
    503: {"description": "AUTONOMY_UNAVAILABLE"},
}
_PROVIDER_SETTINGS_RESPONSES = {
    **_OWNER_RESPONSES,
    422: {"description": "PROVIDER_SETTINGS_INPUT_REJECTED"},
}
_SCHEDULER_RUN_RESPONSES = {
    401: {"description": "SCHEDULER_AUTHENTICATION_FAILED"},
    422: {"description": "RUN_INPUT_REJECTED"},
    503: {"description": "AGENT_RUN_UNAVAILABLE"},
}


@dataclass(frozen=True)
class OwnerSession:
    manager: OwnerSessionManager
    token: str
    csrf: str


class _AutonomyStatus(Protocol):
    role: AccountRole
    server_enabled: bool
    account_enabled: bool
    effective: bool


@app.exception_handler(RequestValidationError)
async def redact_provider_settings_validation(
    request: Request,
    error: RequestValidationError,
) -> Response:
    if request.url.path == _PROVIDER_SETTINGS_PATH:
        return JSONResponse(
            status_code=422,
            content={"detail": "PROVIDER_SETTINGS_INPUT_REJECTED"},
            headers={"Cache-Control": "no-store"},
        )
    if request.url.path.startswith("/api/replays/") and any(
        item["loc"] == ("path", "scenario") and item["type"] == "enum" for item in error.errors()
    ):
        return JSONResponse(status_code=404, content={"detail": "UNKNOWN_REPLAY_SCENARIO"})
    return await request_validation_exception_handler(request, error)


def _owner_session_manager() -> OwnerSessionManager:
    manager = getattr(app.state, "owner_session_manager", None)
    if not isinstance(manager, OwnerSessionManager):
        raise HTTPException(status_code=503, detail="OWNER_SESSION_UNAVAILABLE")
    return manager


def _auth_error(error: SessionAuthError) -> HTTPException:
    code = str(error)
    if code == "AUTHENTICATION_RATE_LIMITED":
        return HTTPException(status_code=429, detail=code)
    if code == "AUTHENTICATION_FAILED":
        return HTTPException(status_code=401, detail=code)
    return HTTPException(status_code=403, detail=code)


def require_scheduler(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    authenticator = getattr(app.state, "scheduler_authenticator", None)
    if not isinstance(authenticator, SchedulerAuthenticator):
        raise HTTPException(status_code=503, detail="AGENT_RUN_UNAVAILABLE")
    prefix = "Bearer "
    supplied = (
        authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else None
    )
    try:
        authenticator.verify(supplied)
    except SchedulerAuthError as error:
        raise HTTPException(
            status_code=401,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def require_owner_session(
    origin: Annotated[str, Header()],
    csrf_header: Annotated[str, Header(alias="X-CSRF-Token")],
    session_cookie: Annotated[
        str,
        Cookie(
            alias=SESSION_COOKIE,
            title=OWNER_SESSION_COOKIE_TITLE,
            description=OWNER_SESSION_COOKIE_DESCRIPTION,
        ),
    ],
    csrf_cookie: Annotated[
        str,
        Cookie(
            alias=CSRF_COOKIE,
            title=CSRF_COOKIE_TITLE,
            description=CSRF_COOKIE_DESCRIPTION,
        ),
    ],
) -> OwnerSession:
    manager = _owner_session_manager()
    try:
        manager.require_origin(origin)
        if not hmac.compare_digest(csrf_header.encode(), csrf_cookie.encode()):
            raise SessionAuthError("CSRF_REJECTED")
        manager.verify(session_cookie, csrf_header)
    except SessionAuthError as error:
        raise _auth_error(error) from error
    return OwnerSession(manager=manager, token=session_cookie, csrf=csrf_header)


def require_owner_read_session(
    csrf_header: Annotated[str, Header(alias="X-CSRF-Token")],
    session_cookie: Annotated[
        str,
        Cookie(
            alias=SESSION_COOKIE,
            title=OWNER_SESSION_COOKIE_TITLE,
            description=OWNER_SESSION_COOKIE_DESCRIPTION,
        ),
    ],
    csrf_cookie: Annotated[
        str,
        Cookie(
            alias=CSRF_COOKIE,
            title=CSRF_COOKIE_TITLE,
            description=CSRF_COOKIE_DESCRIPTION,
        ),
    ],
    origin: Annotated[str | None, Header()] = None,
    referer: Annotated[str | None, Header()] = None,
) -> OwnerSession:
    manager = _owner_session_manager()
    try:
        if origin is not None:
            manager.require_origin(origin)
        else:
            manager.require_same_origin_referer(referer)
        if not hmac.compare_digest(csrf_header.encode(), csrf_cookie.encode()):
            raise SessionAuthError("CSRF_REJECTED")
        manager.verify(session_cookie, csrf_header)
    except SessionAuthError as error:
        raise _auth_error(error) from error
    return OwnerSession(manager=manager, token=session_cookie, csrf=csrf_header)


@app.get(
    "/api/health",
    response_model=HealthResponse,
    operation_id="anonymous_health",
    summary=HEALTH_SUMMARY,
    description=HEALTH_DESCRIPTION,
    tags=[ANONYMOUS_TAG],
)
def health() -> HealthResponse:
    return HealthResponse(
        build=_build_identifier(),
        runtime_mode="CONNECTED" if _runtime_enabled() else "REPLAY_ONLY",
    )


@app.post(
    "/api/session",
    response_model=SessionResponse,
    operation_id="owner_session_create",
    summary=SESSION_CREATE_SUMMARY,
    description=SESSION_CREATE_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_LOGIN_RESPONSES,
)
def create_session(
    payload: SessionCreateRequest,
    response: Response,
    origin: Annotated[str, Header()],
) -> SessionResponse:
    manager = _owner_session_manager()
    try:
        manager.require_origin(origin)
        token, csrf, expires_at = manager.create(payload.access_code)
    except SessionAuthError as error:
        raise _auth_error(error) from error
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=SESSION_MAX_AGE_SECONDS,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return SessionResponse(authenticated=True, expires_at=expires_at)


@app.delete(
    "/api/session",
    response_model=SessionResponse,
    operation_id="owner_session_delete",
    summary=SESSION_DELETE_SUMMARY,
    description=SESSION_DELETE_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_OWNER_RESPONSES,
)
def delete_session(
    response: Response,
    owner: Annotated[OwnerSession, Depends(require_owner_session)],
) -> SessionResponse:
    try:
        owner.manager.revoke(owner.token, owner.csrf)
    except SessionAuthError as error:
        raise _auth_error(error) from error
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return SessionResponse(authenticated=False)


@app.post(
    "/api/replays/{scenario}",
    response_model=ReplayResponse,
    operation_id="anonymous_replay",
    summary=REPLAY_SUMMARY,
    description=REPLAY_DESCRIPTION,
    tags=[ANONYMOUS_TAG],
    responses={404: {"description": REPLAY_NOT_FOUND_DESCRIPTION}},
)
def replay(scenario: ReplayScenario) -> ReplayResponse:
    try:
        return run_replay(scenario)
    except ReplayFixtureError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/proof",
    response_model=CompetitionPerformanceProofResponse,
    response_class=JSONResponse,
    operation_id="anonymous_competition_proof",
    summary=PROOF_SUMMARY,
    description=PROOF_DESCRIPTION,
    tags=[ANONYMOUS_TAG],
)
def competition_proof() -> Response:
    reader = app.state.performance_proof_reader
    if not _runtime_enabled() and isinstance(reader, UnavailablePerformanceProofReader):
        payload = CompetitionPerformanceProofResponse(
            publication_status="NOT_PUBLISHED",
            baseline_status=None,
            published_at=None,
            point=None,
            linked_certificate_ids=(),
            publication_hash=None,
            predecessor_hash=None,
        ).model_dump(mode="json")
        return Response(
            content=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            media_type="application/json",
        )
    try:
        payload = reader.latest_publication_text()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="PERFORMANCE_PROOF_UNAVAILABLE") from exc
    return Response(content=payload, media_type="application/json")


@app.get(
    "/api/competition-record",
    response_model=CompetitionRecordResponse,
    operation_id="anonymous_competition_record",
    summary=COMPETITION_RECORD_SUMMARY,
    description=COMPETITION_RECORD_DESCRIPTION,
    tags=[ANONYMOUS_TAG],
    responses={503: {"description": "COMPETITION_RECORD_UNAVAILABLE"}},
)
def competition_record() -> CompetitionRecordResponse:
    reader = app.state.competition_archive_reader
    if not _runtime_enabled() and isinstance(reader, UnavailableCompetitionArchiveReader):
        return CompetitionRecordResponse(publication_status="NOT_PUBLISHED", records=())
    try:
        records = reader.records()
        items = tuple(
            CompetitionRecordItem(
                kind=record.kind,
                public_record_id=record.public_record_id,
                occurred_at=record.occurred_at,
                published_at=record.published_at,
                payload=_competition_projection_payload(record),
                projection_hash=record.projection_hash,
                publication_hash=record.publication_hash,
                predecessor_hash=record.predecessor_hash,
            )
            for record in records
        )
        return CompetitionRecordResponse(
            publication_status="PUBLISHED" if items else "NOT_PUBLISHED",
            records=items,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="COMPETITION_RECORD_UNAVAILABLE") from exc


def _competition_projection_payload(record: CompetitionRecord) -> dict[str, object]:
    payload = dict(record.payload)
    envelope_keys = {"published_at", "publication_hash", "predecessor_hash"}
    present = envelope_keys.intersection(payload)
    if not present:
        return payload
    if present != envelope_keys:
        raise ValueError("competition publication envelope is incomplete")
    published_at = record.published_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if (
        payload.pop("published_at") != published_at
        or payload.pop("publication_hash") != record.publication_hash
        or payload.pop("predecessor_hash") != record.predecessor_hash
    ):
        raise ValueError("competition publication envelope is inconsistent")
    return payload


@app.post(
    "/api/owner/proof/publications",
    response_model=CompetitionPerformanceProofResponse,
    operation_id="owner_publish_competition_proof",
    summary=PROOF_PUBLICATION_SUMMARY,
    description=PROOF_PUBLICATION_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_OWNER_PUBLICATION_RESPONSES,
    dependencies=[Depends(require_owner_session)],
)
async def publish_competition_proof(
    request: Request,
    response: Response,
) -> CompetitionPerformanceProofResponse:
    if await request.body():
        raise HTTPException(status_code=422, detail="REQUEST_BODY_NOT_ALLOWED")
    publisher = getattr(app.state, "performance_publisher", None)
    if publisher is None:
        raise HTTPException(status_code=503, detail="PERFORMANCE_PROOF_UNAVAILABLE")
    try:
        proof = publisher.publish_latest_eligible()
    except NoEligiblePerformanceSnapshot as exc:
        raise HTTPException(status_code=409, detail="NO_ELIGIBLE_PERFORMANCE_SNAPSHOT") from exc
    except PerformanceProofIntegrityError as exc:
        raise HTTPException(status_code=503, detail="PERFORMANCE_PROOF_UNAVAILABLE") from exc
    response.headers["Cache-Control"] = "no-store"
    return proof


async def _run_agent(request: Request, response: Response, actor: Actor) -> SchedulerTickResponse:
    if request.query_params or await request.body():
        raise HTTPException(status_code=422, detail="RUN_INPUT_NOT_ALLOWED")
    service = getattr(app.state, "agent_run_service", None)
    run = getattr(service, "run", None)
    if not callable(run):
        raise HTTPException(status_code=503, detail="AGENT_RUN_UNAVAILABLE")
    try:
        result = await run(actor)
        payload = SchedulerTickResponse(
            tick_id=result.tick_id,
            accepted=True,
            code=result.terminal_code,
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="AGENT_RUN_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return payload


@app.post(
    "/api/owner/runs",
    response_model=SchedulerTickResponse,
    operation_id="owner_agent_tick",
    summary=OWNER_RUN_SUMMARY,
    description=OWNER_RUN_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_AGENT_RUN_RESPONSES,
    dependencies=[Depends(require_owner_session)],
)
async def start_owner_run(request: Request, response: Response) -> SchedulerTickResponse:
    return await _run_agent(request, response, Actor.OWNER)


def _autonomy_response(status: _AutonomyStatus) -> AutonomyStatusResponse:
    return AutonomyStatusResponse(
        role=status.role,
        server_enabled=status.server_enabled,
        account_enabled=status.account_enabled,
        effective=status.effective,
    )


async def _change_account_autonomy(
    request: Request,
    response: Response,
    operation_name: str,
) -> AutonomyStatusResponse:
    if request.query_params or await request.body():
        raise HTTPException(status_code=422, detail="AUTONOMY_INPUT_NOT_ALLOWED")
    service = getattr(app.state, "account_autonomy_service", None)
    operation = getattr(service, operation_name, None)
    if not callable(operation):
        raise HTTPException(status_code=503, detail="AUTONOMY_UNAVAILABLE")
    try:
        payload = _autonomy_response(operation(Actor.OWNER))
    except ExecutionBlocked as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
            headers={"Cache-Control": "no-store"},
        ) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="AUTONOMY_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return payload


@app.post(
    "/api/owner/autonomy/enable",
    response_model=AutonomyStatusResponse,
    operation_id="owner_autonomy_enable",
    summary=AUTONOMY_ENABLE_SUMMARY,
    description=AUTONOMY_ENABLE_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_AUTONOMY_RESPONSES,
    dependencies=[Depends(require_owner_session)],
)
async def enable_account_autonomy(
    request: Request,
    response: Response,
) -> AutonomyStatusResponse:
    return await _change_account_autonomy(request, response, "enable")


@app.post(
    "/api/owner/autonomy/disable",
    response_model=AutonomyStatusResponse,
    operation_id="owner_autonomy_disable",
    summary=AUTONOMY_DISABLE_SUMMARY,
    description=AUTONOMY_DISABLE_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_AUTONOMY_RESPONSES,
    dependencies=[Depends(require_owner_session)],
)
async def disable_account_autonomy(
    request: Request,
    response: Response,
) -> AutonomyStatusResponse:
    return await _change_account_autonomy(request, response, "disable")


@app.get(
    "/api/owner/autonomy",
    response_model=AutonomyStatusResponse,
    operation_id="owner_autonomy_status",
    summary=AUTONOMY_STATUS_SUMMARY,
    description=AUTONOMY_STATUS_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_OWNER_RESPONSES,
    dependencies=[Depends(require_owner_session)],
)
def account_autonomy_status(
    request: Request,
    response: Response,
) -> AutonomyStatusResponse:
    if request.query_params:
        raise HTTPException(status_code=422, detail="AUTONOMY_INPUT_NOT_ALLOWED")
    service = getattr(app.state, "account_autonomy_service", None)
    read_status = getattr(service, "status", None)
    if not callable(read_status):
        raise HTTPException(status_code=503, detail="AUTONOMY_UNAVAILABLE")
    try:
        payload = _autonomy_response(read_status())
    except Exception as error:
        raise HTTPException(status_code=503, detail="AUTONOMY_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return payload


def _provider_settings_service():
    service = getattr(app.state, "owner_provider_settings_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="PROVIDER_SETTINGS_UNAVAILABLE")
    return service


def _provider_settings_error(error: Exception) -> HTTPException:
    if isinstance(error, ProviderSettingsValidationError | CredentialCodecError):
        return HTTPException(
            status_code=422,
            detail="PROVIDER_SETTINGS_INPUT_REJECTED",
            headers={"Cache-Control": "no-store"},
        )
    return HTTPException(
        status_code=503,
        detail="PROVIDER_SETTINGS_UNAVAILABLE",
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/api/owner/provider-settings",
    response_model=ProviderSettingsResponse,
    operation_id="owner_provider_settings_status",
    summary=PROVIDER_SETTINGS_STATUS_SUMMARY,
    description=PROVIDER_SETTINGS_STATUS_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_PROVIDER_SETTINGS_RESPONSES,
    dependencies=[Depends(require_owner_read_session)],
)
async def provider_settings_status(
    request: Request,
    response: Response,
) -> ProviderSettingsResponse:
    if request.query_params or await request.body():
        raise HTTPException(status_code=422, detail="PROVIDER_SETTINGS_INPUT_REJECTED")
    try:
        payload = await run_in_threadpool(_provider_settings_service().status)
    except Exception as error:
        raise _provider_settings_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return payload


@app.put(
    "/api/owner/provider-settings",
    response_model=ProviderSettingsResponse,
    operation_id="owner_provider_settings_replace",
    summary=PROVIDER_SETTINGS_REPLACE_SUMMARY,
    description=PROVIDER_SETTINGS_REPLACE_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_PROVIDER_SETTINGS_RESPONSES,
    dependencies=[Depends(require_owner_session)],
)
def replace_provider_settings(
    request: Request,
    payload: ProviderSettingsUpdateRequest,
    response: Response,
) -> ProviderSettingsResponse:
    if request.query_params:
        raise HTTPException(status_code=422, detail="PROVIDER_SETTINGS_INPUT_REJECTED")
    try:
        result = _provider_settings_service().replace(payload)
    except Exception as error:
        raise _provider_settings_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@app.delete(
    "/api/owner/provider-settings",
    response_model=ProviderSettingsResponse,
    operation_id="owner_provider_settings_clear",
    summary=PROVIDER_SETTINGS_CLEAR_SUMMARY,
    description=PROVIDER_SETTINGS_CLEAR_DESCRIPTION,
    tags=[OWNER_TAG],
    responses=_PROVIDER_SETTINGS_RESPONSES,
    dependencies=[Depends(require_owner_session)],
)
async def clear_provider_settings(
    request: Request,
    response: Response,
) -> ProviderSettingsResponse:
    if request.query_params or await request.body():
        raise HTTPException(status_code=422, detail="PROVIDER_SETTINGS_INPUT_REJECTED")
    try:
        payload = await run_in_threadpool(_provider_settings_service().clear)
    except Exception as error:
        raise _provider_settings_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return payload


@app.post(
    "/api/internal/scheduler/tick",
    response_model=SchedulerTickResponse,
    operation_id="internal_scheduler_tick",
    summary=SCHEDULER_TICK_SUMMARY,
    description=SCHEDULER_TICK_DESCRIPTION,
    tags=[INTERNAL_TAG],
    responses=_SCHEDULER_RUN_RESPONSES,
    dependencies=[Depends(require_scheduler)],
)
async def start_scheduler_tick(request: Request, response: Response) -> SchedulerTickResponse:
    return await _run_agent(request, response, Actor.SCHEDULER)


_complete_openapi = app.openapi


def _deployment_openapi() -> dict:
    schema = _complete_openapi()
    if os.getenv("APP_RUNTIME_CONFIG_REQUIRED") != "false":
        return schema
    public_schema = deepcopy(schema)
    public_paths = {}
    for path, path_item in schema["paths"].items():
        operations = {
            method: operation
            for method, operation in path_item.items()
            if isinstance(operation, dict) and ANONYMOUS_TAG in operation.get("tags", ())
        }
        if operations:
            public_paths[path] = operations
    public_schema["paths"] = public_paths
    public_schema["tags"] = [
        tag for tag in public_schema.get("tags", ()) if tag.get("name") == ANONYMOUS_TAG
    ]
    source_schemas = schema.get("components", {}).get("schemas", {})
    required_schemas: set[str] = set()
    pending = list(_schema_references(public_paths))
    while pending:
        name = pending.pop()
        if name in required_schemas or name not in source_schemas:
            continue
        required_schemas.add(name)
        pending.extend(_schema_references(source_schemas[name]))
    public_schema["components"] = {
        "schemas": {name: source_schemas[name] for name in sorted(required_schemas)}
    }
    return public_schema


def _schema_references(value) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            references.add(reference.rsplit("/", 1)[-1])
        for nested in value.values():
            references.update(_schema_references(nested))
    elif isinstance(value, list):
        for nested in value:
            references.update(_schema_references(nested))
    return references


app.openapi = _deployment_openapi


frontend = Path(__file__).parents[2] / "dist"
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
