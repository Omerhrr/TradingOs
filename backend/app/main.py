"""Practice-first FastAPI service for the TradingOS control plane."""

from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_session
from app.models import AIResearchRun, AccountConfig, AccountSnapshot, AuditEvent, Candle, EncryptedBrokerCredential, FeatureSnapshot, LoopRun, MarketAsset, OrderIntent, OrderRecord, PositionSnapshot, ReconciliationRun, RiskPolicy, StrategyEvaluation, StrategyVersion, SystemState, TradeOutcome, TwoFactorSecret, WatchlistItem
from app.schemas import AccountStateResponse, AuditEventResponse, AuthLoginInput, AuthLoginResponse, AuthSessionResponse, BacktestRunInput, BacktestRunResponse, BacktestSweepInput, BacktestSweepResponse, BrokerConnectionResponse, BrokerCredentialInput, CandleResponse, FeatureResponse, HealthResponse, LoopRunResponse, LoopStatusResponse, MarketAssetResponse, MarketChartResponse, OrderIntentInput, OrderIntentResponse, OrderResponse, PositionResponse, ReconciliationResponse, ResearchRunResponse, RiskPolicyResponse, StrategyComparisonResponse, StrategyCreateInput, StrategyEvaluationInput, StrategyEvaluationResponse, StrategyFromSweepInput, StrategyResponse, StrategyStatusUpdateInput, SweepPickSaveResponse, SymbolDrilldownResponse, TotpProvisionResponse, TotpStatusResponse, TradeAnalyticsResponse, TradeResponse, WatchlistItemResponse, WatchlistUpdate
from app.services.analytics import strategy_comparison, symbol_drilldown, trade_analytics
from app.services.auth import SESSION_COOKIE, LoginGate, SessionConfigurationError, SessionManager, credential_ok, generate_totp_secret, otpauth_uri, totp_qr_svg, totp_verify
from app.services.backtest import run_backtest, run_sweep, save_sweep_pick, SweepPickRejected
from app.services.broker import IQAirBrokerAdapter
from app.services.credentials import BrokerCredentials, CredentialConfigurationError, CredentialVault
from app.services.events import event_bus, publish_event
from app.services.market_view import market_chart
from app.services.worker import BrokerWorker
from app.services.strategy import evaluate_strategy, persist_features, utc_now
from app.services.research import ResearchBudgetExceeded, ResearchService
from app.services.execution import ExecutionService
from app.services.loop import LoopEngine
from app.services.runtime import LocalRuntime


settings = get_settings()
worker = BrokerWorker(IQAirBrokerAdapter(), settings.broker_candle_count)
loop_engine = LoopEngine(worker, settings)
runtime = LocalRuntime(settings, SessionLocal, worker, loop_engine)
login_gate = LoginGate()


def _seed_control_plane(session: Session) -> None:
    if session.scalar(select(AccountConfig.id).limit(1)) is None:
        session.add(AccountConfig(account_label="Primary account", mode="PRACTICE", real_execution_enabled=False, system_state=SystemState.PAUSED.value))
    if session.scalar(select(RiskPolicy.id).where(RiskPolicy.active.is_(True)).limit(1)) is None:
        session.add(RiskPolicy())
    session.flush()
    if session.scalar(select(AuditEvent.id).limit(1)) is None:
        session.add(AuditEvent(event_type="SYSTEM_BOOTSTRAPPED", severity="INFO", message="Practice-first control plane initialized; broker execution remains disabled.", payload={"real_execution_enabled": False}))
    session.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        _seed_control_plane(session)
    # Bind the event bus to this loop so worker threads can reach WebSocket senders.
    event_bus.attach_loop(asyncio.get_running_loop())
    runtime.start()
    try:
        yield
    finally:
        runtime.stop()


app = FastAPI(title="TradingOS API", version="0.2.0", description="Practice-only local TradingOS control plane. Real broker execution is unavailable.", lifespan=lifespan)
# X-TradingOS-Token must be preflight-allowed: the Nuxt setup page sends it as a
# custom header from the browser, and a CORS 400 here silently breaks every
# local admin control (credential storage, practice connect, reconciliation).
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "Authorization", "X-TradingOS-Token"])

# Remote-access gate: when TRADINGOS_REMOTE_ACCESS_ENABLED is set, every HTTP
# request must present the admin token or a login session, except the health
# probe and the auth endpoints themselves. With the gate off, the historical
# local-first behavior is preserved byte for byte.
_AUTH_EXEMPT_PATHS: set[str] | None = None


def _auth_exempt_paths() -> set[str]:
    global _AUTH_EXEMPT_PATHS
    if _AUTH_EXEMPT_PATHS is None:
        _AUTH_EXEMPT_PATHS = {
            f"{settings.api_prefix}/health",
            f"{settings.api_prefix}/auth/login",
            f"{settings.api_prefix}/auth/session",
        }
    return _AUTH_EXEMPT_PATHS


@app.middleware("http")
async def remote_access_gate(request: Request, call_next):
    if settings.remote_access_enabled:
        scope_headers = {key.decode().lower(): value.decode() for key, value in request.scope.get("headers", [])}
        authorized = credential_ok(
            settings=settings,
            headers=scope_headers,
            cookies=dict(request.cookies),
        )
        if not authorized and request.url.path not in _auth_exempt_paths():
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Authentication required. Sign in through /api/v1/auth/login or present the admin token."})
    return await call_next(request)


def _account(session: Session) -> AccountConfig:
    account = session.scalar(select(AccountConfig).limit(1))
    if account is None:
        raise HTTPException(status_code=500, detail="Account configuration was not initialized.")
    return account


def _require_local_admin(request: Request, x_tradingos_token: str | None = Header(default=None)) -> None:
    """Credential and broker controls require the local admin token.

    A valid login session is accepted as an equivalent credential so a remote
    operator who signed in through /auth/login can drive admin controls
    without re-pasting the token into every browser tab. The header path
    keeps working for scripts and for the local-first flow.
    """
    if not settings.local_admin_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Set TRADINGOS_LOCAL_ADMIN_TOKEN before enabling local broker controls.")
    if x_tradingos_token and hmac.compare_digest(x_tradingos_token, settings.local_admin_token):
        return
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
        if hmac.compare_digest(bearer, settings.local_admin_token):
            return
        if SessionManager(settings).verify(bearer):
            return
    cookie_token = request.cookies.get(SESSION_COOKIE)
    if cookie_token and SessionManager(settings).verify(cookie_token):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Local broker control token is invalid.")


def _vault() -> CredentialVault:
    try:
        return CredentialVault(settings.credential_encryption_key)
    except CredentialConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


# --------------------------------------------------------------------- auth


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.post(f"{settings.api_prefix}/auth/login", response_model=AuthLoginResponse, tags=["auth"])
def login(payload: AuthLoginInput, request: Request, response: Response) -> AuthLoginResponse:
    """Exchange the local admin token for a signed, expiring session.

    Failed attempts are rate limited per source IP and audited; the supplied
    secret is never written to the ledger or the response.
    """
    if not settings.local_admin_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Set TRADINGOS_LOCAL_ADMIN_TOKEN before enabling logins.")
    ip = _client_ip(request)
    lock_remaining = login_gate.check(ip)
    if lock_remaining is not None:
        publish_event("auth.locked", {"source": ip})
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=f"Too many failed sign-ins; retry in {lock_remaining}s.", headers={"Retry-After": str(lock_remaining)})
    if not hmac.compare_digest(payload.token, settings.local_admin_token):
        login_gate.record_failure(ip)
        with SessionLocal() as audit_session:
            audit_session.add(AuditEvent(event_type="AUTH_LOGIN_FAILED", severity="WARNING", message="A sign-in attempt presented an invalid admin token.", payload={"source": ip}))
            audit_session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin token is invalid.")
    if settings.totp_required:
        # Fail-closed second factor for the interactive exchange: no provisioned
        # secret, no session. Header/bearer admin-token access is unchanged.
        with SessionLocal() as totp_session:
            totp_row = totp_session.scalar(select(TwoFactorSecret).limit(1))
            totp_enabled = bool(totp_row and totp_row.enabled)
        if not totp_enabled:
            with SessionLocal() as audit_session:
                audit_session.add(AuditEvent(event_type="AUTH_TOTP_UNPROVISIONED", severity="ERROR", message="A sign-in reached the required two-factor gate with no provisioned authenticator secret.", payload={"source": ip}))
                audit_session.commit()
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Two-factor authentication is required but no authenticator secret is provisioned; provision one with the admin token via POST /api/v1/auth/totp/provision.")
        try:
            totp_secret = _vault().decrypt(totp_row.secret_ciphertext)
        except CredentialConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        if not totp_verify(totp_secret, payload.totp_code):
            login_gate.record_failure(ip)
            with SessionLocal() as audit_session:
                audit_session.add(AuditEvent(event_type="AUTH_LOGIN_FAILED", severity="WARNING", message="A sign-in attempt presented an invalid verification code.", payload={"source": ip, "reason": "totp_code_invalid"}))
                audit_session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="The verification code is invalid or expired.")
    login_gate.record_success(ip)
    try:
        manager = SessionManager(settings)
    except SessionConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    token, expires_at = manager.issue()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=manager.ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=settings.remote_public_tls,
        path="/",
    )
    with SessionLocal() as audit_session:
        audit_session.add(AuditEvent(event_type="AUTH_LOGIN_SUCCESS", severity="INFO", message="A remote operator signed in; a session was issued.", payload={"source": ip, "expires_at": expires_at.isoformat()}))
        audit_session.commit()
    return AuthLoginResponse(session_token=token, expires_at=expires_at, cookie_name=SESSION_COOKIE)


@app.post(f"{settings.api_prefix}/auth/logout", tags=["auth"])
def logout(request: Request, response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    with SessionLocal() as audit_session:
        audit_session.add(AuditEvent(event_type="AUTH_LOGOUT", severity="INFO", message="A session signed out; the session cookie was cleared.", payload={"source": _client_ip(request)}))
        audit_session.commit()
    return {"detail": "Signed out."}


@app.get(f"{settings.api_prefix}/auth/session", response_model=AuthSessionResponse, tags=["auth"])
def auth_session(request: Request) -> AuthSessionResponse:
    """Bootstrap probe for the UI: is remote access on, and is this browser in?"""
    if not settings.remote_access_enabled:
        # The login exchange demands a TOTP code whenever totp_required is set,
        # gate or no gate, so the UI must see the flag in both modes.
        return AuthSessionResponse(authenticated=False, remote_access=False, totp_required=settings.totp_required)
    scope_headers = {key.decode().lower(): value.decode() for key, value in request.scope.get("headers", [])}
    authorized = credential_ok(settings=settings, headers=scope_headers, cookies=dict(request.cookies))
    expires_at = None
    cookie_token = request.cookies.get(SESSION_COOKIE)
    if cookie_token:
        parts = cookie_token.split(".")
        if len(parts) == 4 and parts[1].isdigit():
            from datetime import UTC as _UTC, datetime as _datetime

            expires_at = _datetime.fromtimestamp(int(parts[1]), tz=_UTC)
    return AuthSessionResponse(authenticated=authorized, remote_access=True, expires_at=expires_at, totp_required=settings.totp_required)


@app.post(f"{settings.api_prefix}/auth/totp/provision", response_model=TotpProvisionResponse, dependencies=[Depends(_require_local_admin)], tags=["auth"])
def provision_totp(session: Session = Depends(get_session)) -> TotpProvisionResponse:
    """Create or rotate the TOTP shared secret. The plaintext is returned exactly once."""
    vault = _vault()
    secret = generate_totp_secret()
    row = session.scalar(select(TwoFactorSecret).limit(1))
    if row is None:
        row = TwoFactorSecret(secret_ciphertext=vault.encrypt(secret), enabled=True)
        session.add(row)
    else:
        row.secret_ciphertext = vault.encrypt(secret)
        row.enabled = True
        row.rotated_at = utc_now()
    session.add(AuditEvent(event_type="AUTH_TOTP_PROVISIONED", severity="WARNING", message="A TOTP authenticator secret was provisioned for the login gate; the plaintext was shown once and never stored.", payload={}))
    session.commit()
    # The QR encodes the same provisioning URI — it is exactly as sensitive as
    # the secret, so it ships only in this one response and is never re-served.
    return TotpProvisionResponse(secret=secret, otpauth_uri=otpauth_uri(secret), qr_svg=totp_qr_svg(otpauth_uri(secret)))


@app.get(f"{settings.api_prefix}/auth/totp/status", response_model=TotpStatusResponse, dependencies=[Depends(_require_local_admin)], tags=["auth"])
def totp_status(session: Session = Depends(get_session)) -> TotpStatusResponse:
    row = session.scalar(select(TwoFactorSecret).limit(1))
    return TotpStatusResponse(required=settings.totp_required, provisioned=bool(row and row.enabled))


@app.post(f"{settings.api_prefix}/auth/totp/disable", response_model=TotpStatusResponse, dependencies=[Depends(_require_local_admin)], tags=["auth"])
def disable_totp(session: Session = Depends(get_session)) -> TotpStatusResponse:
    """Disable the provisioned secret. A required-but-disabled gate fails closed."""
    row = session.scalar(select(TwoFactorSecret).limit(1))
    if row is not None and row.enabled:
        row.enabled = False
        row.rotated_at = utc_now()
        session.add(AuditEvent(event_type="AUTH_TOTP_DISABLED", severity="WARNING", message="The TOTP authenticator secret was disabled; a required two-factor gate now fails closed until a new secret is provisioned.", payload={}))
        session.commit()
    return TotpStatusResponse(required=settings.totp_required, provisioned=False)


@app.get(f"{settings.api_prefix}/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", api_version=app.version, broker_connection=worker.adapter.health().state, database="configured", live_execution=settings.real_execution_enabled)


@app.get(f"{settings.api_prefix}/state", response_model=AccountStateResponse, tags=["system"])
def state(session: Session = Depends(get_session)) -> AccountStateResponse:
    account = _account(session)
    open_orders = session.scalar(select(func.count()).select_from(OrderRecord).where(OrderRecord.status.in_(["SUBMITTED", "OPEN", "UNKNOWN"]))) or 0
    active_watchlist_items = session.scalar(select(func.count()).select_from(WatchlistItem).where(WatchlistItem.enabled.is_(True))) or 0
    risk = session.scalar(select(RiskPolicy).where(RiskPolicy.active.is_(True)).limit(1))
    return AccountStateResponse(account_label=account.account_label, account_mode=account.mode, system_state=account.system_state, real_execution_enabled=account.real_execution_enabled, broker_connection=worker.adapter.health().state, open_orders=open_orders, active_watchlist_items=active_watchlist_items, active_risk_policy=risk.version if risk else None)


@app.get(f"{settings.api_prefix}/watchlist", response_model=list[WatchlistItemResponse], tags=["watchlist"])
def list_watchlist(session: Session = Depends(get_session)) -> list[WatchlistItem]:
    return list(session.scalars(select(WatchlistItem).order_by(WatchlistItem.symbol, WatchlistItem.timeframe_seconds)))


@app.put(f"{settings.api_prefix}/watchlist", response_model=list[WatchlistItemResponse], tags=["watchlist"])
def replace_watchlist(payload: WatchlistUpdate, session: Session = Depends(get_session)) -> list[WatchlistItem]:
    unique_keys = {(item.symbol.upper(), item.timeframe_seconds) for item in payload.items}
    if len(unique_keys) != len(payload.items):
        raise HTTPException(status_code=422, detail="Watchlist entries must be unique by symbol and timeframe.")
    session.query(WatchlistItem).delete()
    replacements = [WatchlistItem(symbol=item.symbol.upper(), category=item.category.lower(), timeframe_seconds=item.timeframe_seconds, enabled=item.enabled) for item in payload.items]
    session.add_all(replacements)
    session.add(AuditEvent(event_type="WATCHLIST_REPLACED", severity="INFO", message="Watchlist updated through the control plane.", payload={"count": len(replacements)}))
    session.commit()
    return replacements


@app.get(f"{settings.api_prefix}/risk", response_model=RiskPolicyResponse, tags=["risk"])
def active_risk_policy(session: Session = Depends(get_session)) -> RiskPolicy:
    policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active.is_(True)).order_by(RiskPolicy.id.desc()).limit(1))
    if policy is None:
        raise HTTPException(status_code=500, detail="No active risk policy exists.")
    return policy


@app.get(f"{settings.api_prefix}/strategies", response_model=list[StrategyResponse], tags=["strategies"])
def list_strategies(session: Session = Depends(get_session)) -> list[StrategyVersion]:
    return list(session.scalars(select(StrategyVersion).order_by(StrategyVersion.id.desc())))


@app.post(f"{settings.api_prefix}/strategies", response_model=StrategyResponse, dependencies=[Depends(_require_local_admin)], tags=["strategies"])
def create_strategy(payload: StrategyCreateInput, session: Session = Depends(get_session)) -> StrategyVersion:
    definition = {"kind": "ema_cross", "fast_window": 12, "slow_window": 26, "volatility_window": 20, "max_drawdown": 0.05, **payload.definition}
    if definition["kind"] != "ema_cross" or int(definition["fast_window"]) >= int(definition["slow_window"]):
        raise HTTPException(status_code=422, detail="Only valid EMA-cross strategies with fast_window < slow_window are accepted.")
    existing = session.scalar(select(StrategyVersion).where(StrategyVersion.strategy_key == payload.strategy_key, StrategyVersion.version == payload.version))
    if existing:
        raise HTTPException(status_code=409, detail="A strategy with this key and version already exists.")
    strategy = StrategyVersion(strategy_key=payload.strategy_key, version=payload.version, definition=definition)
    session.add(strategy)
    session.add(AuditEvent(event_type="STRATEGY_CREATED", severity="INFO", message="A deterministic EMA-cross strategy candidate was created for validation.", payload={"strategy_key": payload.strategy_key, "version": payload.version}))
    session.commit()
    session.refresh(strategy)
    return strategy


@app.post(f"{settings.api_prefix}/strategies/from-sweep", response_model=SweepPickSaveResponse, dependencies=[Depends(_require_local_admin)], tags=["strategies"])
def create_strategy_from_sweep(payload: StrategyFromSweepInput, session: Session = Depends(get_session)) -> dict:
    """Promote a lab sweep pick to a DRAFT strategy version.

    Evidence is recomputed server-side over the candles stored right now, so
    the persisted validation summary can never be fabricated by the client.
    VALIDATED status is still earned only through the persisted evaluation.
    """
    try:
        return save_sweep_pick(
            session,
            strategy_key=payload.strategy_key,
            version=payload.version,
            symbol=payload.symbol,
            timeframe_seconds=payload.timeframe_seconds,
            censor_gap_seconds=payload.censor_gap_seconds,
            fast_window=payload.fast_window,
            slow_window=payload.slow_window,
            volatility_window=payload.volatility_window,
        )
    except SweepPickRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/strategies/{{strategy_id}}/evaluate", response_model=StrategyEvaluationResponse, dependencies=[Depends(_require_local_admin)], tags=["strategies"])
def validate_strategy(strategy_id: int, payload: StrategyEvaluationInput, session: Session = Depends(get_session)) -> StrategyEvaluation:
    strategy = session.get(StrategyVersion, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy version does not exist.")
    candles = list(session.scalars(select(Candle).where(Candle.symbol == payload.symbol.upper(), Candle.timeframe_seconds == payload.timeframe_seconds).order_by(Candle.open_time)))
    try:
        feature_count = persist_features(session, payload.symbol.upper(), payload.timeframe_seconds, candles, strategy.definition)
        evaluation = evaluate_strategy(session, strategy, payload.symbol, payload.timeframe_seconds, payload.censor_gap_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.add(AuditEvent(event_type="STRATEGY_EVALUATED", severity="INFO", message="Strategy validation completed with time-ordered, censored data.", payload={"strategy_id": strategy_id, "features_updated": feature_count, "accepted": evaluation.accepted}))
    session.commit()
    session.refresh(evaluation)
    return evaluation


@app.post(f"{settings.api_prefix}/backtest/run", response_model=BacktestRunResponse, dependencies=[Depends(_require_local_admin)], tags=["backtest"])
def run_backtest_view(payload: BacktestRunInput, session: Session = Depends(get_session)) -> dict:
    """Read-only walk-forward preview over stored candles; nothing is persisted."""
    if payload.strategy_version_id is not None:
        strategy = session.get(StrategyVersion, payload.strategy_version_id)
        if strategy is None:
            raise HTTPException(status_code=404, detail="Strategy version does not exist.")
        definition = dict(strategy.definition)
    else:
        definition = {"kind": "ema_cross", "fast_window": 12, "slow_window": 26, "volatility_window": 20, "max_drawdown": 0.05, **(payload.definition or {})}
        if definition.get("kind") != "ema_cross" or int(definition["fast_window"]) >= int(definition["slow_window"]):
            raise HTTPException(status_code=422, detail="Only valid EMA-cross parameter sets with fast_window < slow_window are accepted.")
    try:
        return run_backtest(session, definition, payload.symbol, payload.timeframe_seconds, payload.censor_gap_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/backtest/sweep", response_model=BacktestSweepResponse, dependencies=[Depends(_require_local_admin)], tags=["backtest"])
def run_backtest_sweep_view(payload: BacktestSweepInput, session: Session = Depends(get_session)) -> dict:
    """Bounded fast/slow EMA grid over one candle set; invalid pairs are reported, not fatal."""
    try:
        return run_sweep(session, payload.symbol, payload.timeframe_seconds, payload.censor_gap_seconds, payload.fast_windows, payload.slow_windows, payload.volatility_window)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(f"{settings.api_prefix}/features", response_model=list[FeatureResponse], tags=["research"])
def list_features(symbol: str = Query(min_length=1, max_length=80), timeframe_seconds: int = Query(60, ge=1, le=86_400), limit: int = Query(250, ge=1, le=1_000), session: Session = Depends(get_session)) -> list[FeatureSnapshot]:
    return list(session.scalars(select(FeatureSnapshot).where(FeatureSnapshot.symbol == symbol.upper(), FeatureSnapshot.timeframe_seconds == timeframe_seconds).order_by(FeatureSnapshot.candle_open_time.desc()).limit(limit)))


@app.post(f"{settings.api_prefix}/research/run", response_model=ResearchRunResponse, dependencies=[Depends(_require_local_admin)], tags=["research"])
def run_ai_research(strategy_id: int | None = None, session: Session = Depends(get_session)) -> AIResearchRun:
    strategy = session.get(StrategyVersion, strategy_id) if strategy_id is not None else None
    if strategy_id is not None and strategy is None:
        raise HTTPException(status_code=404, detail="Strategy version does not exist.")
    try:
        result = ResearchService(settings).run(session, strategy)
    except ResearchBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="AI research failed; no strategy or order state changed.") from exc
    session.add(AuditEvent(event_type="AI_RESEARCH_COMPLETED", severity="INFO", message="Bounded AI research completed without access to any order or broker operation.", payload={"run_id": result.id, "status": result.status}))
    session.commit()
    return result


@app.get(f"{settings.api_prefix}/research/runs", response_model=list[ResearchRunResponse], tags=["research"])
def list_research_runs(session: Session = Depends(get_session)) -> list[AIResearchRun]:
    return list(session.scalars(select(AIResearchRun).order_by(AIResearchRun.id.desc()).limit(100)))


@app.get(f"{settings.api_prefix}/strategies/{{strategy_id}}/evaluations", response_model=list[StrategyEvaluationResponse], tags=["strategies"])
def list_strategy_evaluations(strategy_id: int, session: Session = Depends(get_session)) -> list[StrategyEvaluation]:
    """Evaluation history for a strategy version, newest first."""
    if session.get(StrategyVersion, strategy_id) is None:
        raise HTTPException(status_code=404, detail="Strategy version does not exist.")
    return list(session.scalars(select(StrategyEvaluation).where(StrategyEvaluation.strategy_version_id == strategy_id).order_by(StrategyEvaluation.id.desc()).limit(20)))


@app.put(f"{settings.api_prefix}/strategies/{{strategy_id}}/status", response_model=StrategyResponse, dependencies=[Depends(_require_local_admin)], tags=["strategies"])
def update_strategy_status(strategy_id: int, payload: StrategyStatusUpdateInput, session: Session = Depends(get_session)) -> StrategyVersion:
    """Move a strategy between DRAFT and RETIRED. VALIDATED is earned only by evaluation."""
    strategy = session.get(StrategyVersion, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy version does not exist.")
    if strategy.status == payload.status:
        return strategy
    previous = strategy.status
    strategy.status = payload.status
    session.add(AuditEvent(event_type="STRATEGY_STATUS_CHANGED", severity="INFO", message=f"Strategy {strategy.strategy_key} v{strategy.version} moved from {previous} to {payload.status}.", payload={"strategy_id": strategy_id, "from_status": previous, "to_status": payload.status}))
    session.commit()
    session.refresh(strategy)
    return strategy


@app.get(f"{settings.api_prefix}/analytics/trades", response_model=TradeAnalyticsResponse, tags=["analytics"])
def trade_outcome_analytics(session: Session = Depends(get_session)) -> dict:
    """Read-only projection over settled trade outcomes: KPIs, equity curve, groups."""
    return trade_analytics(session)


@app.get(f"{settings.api_prefix}/analytics/strategies/compare", response_model=StrategyComparisonResponse, tags=["analytics"])
def compare_strategies(session: Session = Depends(get_session)) -> dict:
    """Side-by-side evidence for every strategy: live outcomes, gate activity, evaluation."""
    return strategy_comparison(session)


@app.get(f"{settings.api_prefix}/analytics/symbols/{{symbol}}", response_model=SymbolDrilldownResponse, tags=["analytics"])
def symbol_drilldown_view(symbol: str, session: Session = Depends(get_session)) -> dict:
    """Per-symbol drill-down over settled evidence: KPIs, own-slice equity, side/strategy splits."""
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=422, detail="A symbol is required.")
    try:
        return symbol_drilldown(session, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(f"{settings.api_prefix}/orders", response_model=list[OrderResponse], tags=["orders"])
def list_orders(session: Session = Depends(get_session)) -> list[OrderRecord]:
    return list(session.scalars(select(OrderRecord).order_by(OrderRecord.id.desc()).limit(200)))


@app.get(f"{settings.api_prefix}/order-intents", response_model=list[OrderIntentResponse], tags=["orders"])
def list_order_intents(session: Session = Depends(get_session)) -> list[OrderIntent]:
    return list(session.scalars(select(OrderIntent).order_by(OrderIntent.id.desc()).limit(200)))


@app.post(f"{settings.api_prefix}/order-intents", response_model=OrderIntentResponse, dependencies=[Depends(_require_local_admin)], tags=["orders"])
def create_order_intent(payload: OrderIntentInput, session: Session = Depends(get_session)) -> OrderIntent:
    intent, _ = ExecutionService(worker.adapter, settings).create_intent(session, idempotency_key=payload.idempotency_key, strategy_id=payload.strategy_version_id, symbol=payload.symbol, side=payload.side, amount=payload.amount, timeframe_seconds=payload.timeframe_seconds, duration_minutes=payload.duration_minutes)
    return intent


@app.post(f"{settings.api_prefix}/order-intents/{{intent_id}}/submit", response_model=OrderResponse, dependencies=[Depends(_require_local_admin)], tags=["orders"])
def submit_practice_order(intent_id: int, session: Session = Depends(get_session)) -> OrderRecord:
    try:
        return ExecutionService(worker.adapter, settings).submit_approved_intent(session, intent_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(f"{settings.api_prefix}/trades", response_model=list[TradeResponse], tags=["trades"])
def list_trades(session: Session = Depends(get_session)) -> list[object]:
    from app.models import TradeOutcome
    return list(session.scalars(select(TradeOutcome).order_by(TradeOutcome.id.desc()).limit(200)))


@app.get(f"{settings.api_prefix}/events", response_model=list[AuditEventResponse], tags=["audit"])
def list_events(session: Session = Depends(get_session)) -> list[AuditEvent]:
    # id ordering guarantees append-only sequence: SQLite's CURRENT_TIMESTAMP
    # has 1-second granularity, so created_at alone is ambiguous within a second.
    return list(session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)))


@app.post(f"{settings.api_prefix}/broker/credentials", response_model=BrokerConnectionResponse, dependencies=[Depends(_require_local_admin)], tags=["broker"])
def store_broker_credentials(payload: BrokerCredentialInput, session: Session = Depends(get_session)) -> BrokerConnectionResponse:
    vault = _vault()
    credential = session.scalar(select(EncryptedBrokerCredential).limit(1))
    if credential is None:
        credential = EncryptedBrokerCredential(email_ciphertext=vault.encrypt(payload.email), password_ciphertext=vault.encrypt(payload.password))
        session.add(credential)
    else:
        credential.email_ciphertext = vault.encrypt(payload.email)
        credential.password_ciphertext = vault.encrypt(payload.password)
    session.add(AuditEvent(event_type="BROKER_CREDENTIALS_STORED", severity="WARNING", message="Encrypted local broker credentials were updated; no broker connection was opened.", payload={}))
    session.commit()
    health = worker.adapter.health()
    return BrokerConnectionResponse(state=health.state, detail="Encrypted local credentials were stored. Connect explicitly to verify PRACTICE mode.")


@app.post(f"{settings.api_prefix}/broker/connect", response_model=BrokerConnectionResponse, dependencies=[Depends(_require_local_admin)], tags=["broker"])
def connect_practice_broker(session: Session = Depends(get_session)) -> BrokerConnectionResponse:
    credential = session.scalar(select(EncryptedBrokerCredential).limit(1))
    if credential is None:
        raise HTTPException(status_code=409, detail="Store encrypted broker credentials before connecting.")
    vault = _vault()
    try:
        account = worker.connect_practice(session, BrokerCredentials(email=vault.decrypt(credential.email_ciphertext), password=vault.decrypt(credential.password_ciphertext)))
        session.add(AuditEvent(event_type="BROKER_PRACTICE_CONNECTED", severity="INFO", message="Broker connection verified against PRACTICE mode; no order was submitted.", payload={"currency": account.currency}))
        session.commit()
        return BrokerConnectionResponse(state=worker.adapter.health().state, detail=worker.adapter.health().detail, account_mode=account.mode, balance=account.balance, currency=account.currency)
    except Exception as exc:
        session.add(AuditEvent(event_type="BROKER_CONNECT_FAILED", severity="ERROR", message="Practice broker connection failed; system remains fail-closed.", payload={"error_type": type(exc).__name__}))
        session.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/broker/disconnect", response_model=BrokerConnectionResponse, dependencies=[Depends(_require_local_admin)], tags=["broker"])
def disconnect_broker() -> BrokerConnectionResponse:
    worker.disconnect()
    health = worker.adapter.health()
    return BrokerConnectionResponse(state=health.state, detail=health.detail)


@app.post(f"{settings.api_prefix}/reconciliation/run", response_model=ReconciliationResponse, dependencies=[Depends(_require_local_admin)], tags=["broker"])
def reconcile_practice_broker(session: Session = Depends(get_session)) -> ReconciliationRun:
    try:
        return worker.reconcile(session)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get(f"{settings.api_prefix}/market/assets", response_model=list[MarketAssetResponse], tags=["market"])
def list_market_assets(session: Session = Depends(get_session)) -> list[MarketAsset]:
    return list(session.scalars(select(MarketAsset).order_by(MarketAsset.category, MarketAsset.ticker).limit(2_000)))


@app.get(f"{settings.api_prefix}/market/candles", response_model=list[CandleResponse], tags=["market"])
def list_candles(symbol: str = Query(min_length=1, max_length=80), timeframe_seconds: int = Query(60, ge=1, le=86_400), limit: int = Query(250, ge=1, le=1_000), session: Session = Depends(get_session)) -> list[Candle]:
    return list(session.scalars(select(Candle).where(Candle.symbol == symbol.upper(), Candle.timeframe_seconds == timeframe_seconds).order_by(Candle.open_time.desc()).limit(limit)))


@app.get(f"{settings.api_prefix}/market/chart", response_model=MarketChartResponse, tags=["market"])
def market_chart_view(symbol: str = Query(min_length=1, max_length=80), timeframe_seconds: int = Query(60, ge=1, le=86_400), limit: int = Query(120, ge=10, le=500), session: Session = Depends(get_session)) -> dict:
    """Candles plus loop-signal markers for the loop-panel chart."""
    return market_chart(session, symbol, timeframe_seconds, limit)


@app.get(f"{settings.api_prefix}/positions", response_model=list[PositionResponse], tags=["positions"])
def list_positions(session: Session = Depends(get_session)) -> list[PositionSnapshot]:
    return list(session.scalars(select(PositionSnapshot).order_by(PositionSnapshot.observed_at.desc()).limit(500)))


@app.get(f"{settings.api_prefix}/reconciliation", response_model=list[ReconciliationResponse], tags=["broker"])
def list_reconciliation_runs(session: Session = Depends(get_session)) -> list[ReconciliationRun]:
    return list(session.scalars(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(100)))


@app.post(f"{settings.api_prefix}/loop/run", response_model=LoopRunResponse, dependencies=[Depends(_require_local_admin)], tags=["loop"])
def run_strategy_loop(session: Session = Depends(get_session)) -> LoopRun:
    """One manual pass of the strategy -> intent -> practice-execution loop."""
    try:
        return loop_engine.tick(session)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get(f"{settings.api_prefix}/loop/status", response_model=LoopStatusResponse, tags=["loop"])
def loop_status(session: Session = Depends(get_session)) -> LoopStatusResponse:
    last_run = session.scalar(select(LoopRun).order_by(LoopRun.id.desc()).limit(1))
    account = session.scalar(select(AccountConfig).limit(1))
    return LoopStatusResponse(
        loop_enabled=settings.loop_enabled,
        practice_execution_enabled=settings.practice_execution_enabled,
        broker_connection=worker.adapter.health().state,
        system_state=account.system_state if account else None,
        last_run=last_run,
    )


@app.get(f"{settings.api_prefix}/loop/runs", response_model=list[LoopRunResponse], tags=["loop"])
def list_loop_runs(session: Session = Depends(get_session)) -> list[LoopRun]:
    return list(session.scalars(select(LoopRun).order_by(LoopRun.id.desc()).limit(100)))


@app.get(f"{settings.api_prefix}/account/snapshots", tags=["account"])
def list_account_snapshots(session: Session = Depends(get_session)) -> list[dict]:
    snapshots = list(session.scalars(select(AccountSnapshot).order_by(AccountSnapshot.captured_at.desc()).limit(200)))
    return [{"captured_at": snapshot.captured_at, "mode": snapshot.account_mode, "balance": snapshot.balance, "currency": snapshot.currency} for snapshot in snapshots]


@app.post(f"{settings.api_prefix}/system/pause", response_model=AccountStateResponse, tags=["system"])
def pause_system(session: Session = Depends(get_session)) -> AccountStateResponse:
    account = _account(session)
    account.system_state = SystemState.PAUSED.value
    session.add(AuditEvent(event_type="SYSTEM_PAUSED", severity="WARNING", message="New exposure is paused by the control plane.", payload={}))
    session.commit()
    publish_event("system.state_changed", {"system_state": account.system_state})
    return state(session)


@app.post(f"{settings.api_prefix}/system/resume", response_model=AccountStateResponse, tags=["system"])
def resume_system(session: Session = Depends(get_session)) -> AccountStateResponse:
    account = _account(session)
    if account.mode != "PRACTICE" or worker.adapter.health().state != "CONNECTED":
        session.add(AuditEvent(event_type="SYSTEM_RESUME_REJECTED", severity="WARNING", message="Resume requires a connected practice broker; no state change occurred.", payload={}))
        session.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot resume: practice mode and a reconciled broker connection are required.")
    account.system_state = SystemState.ACTIVE.value
    session.add(AuditEvent(event_type="SYSTEM_RESUMED", severity="INFO", message="New exposure resumed against a connected practice broker.", payload={"account_mode": account.mode}))
    session.commit()
    publish_event("system.state_changed", {"system_state": account.system_state})
    return state(session)


@app.post(f"{settings.api_prefix}/mode/enable-real", tags=["system"])
def enable_real_mode() -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Real mode is hard-disabled in this scaffold. No broker credentials or live execution path are configured.")


@app.websocket(f"{settings.api_prefix}/ws/loop")
async def loop_events_socket(websocket: WebSocket) -> None:
    """Live loop channel: hello snapshot, then every loop/execution/system event.

    The client may send "ping" to keep intermediaries from idling the socket
    out; each ping is answered with a pong frame. Disconnects (browser tab
    closed, network drop) release the subscriber queue immediately.
    When remote access is enabled the socket demands the same credentials as
    HTTP: cookie, bearer/admin token, or an ``access_token`` query parameter.
    """
    scope_headers = {key.decode().lower(): value.decode() for key, value in websocket.scope.get("headers", [])}
    authorized = credential_ok(
        settings=settings,
        headers=scope_headers,
        cookies=dict(websocket.cookies),
        query_token=websocket.query_params.get("access_token"),
    )
    await websocket.accept()
    if not authorized:
        await websocket.send_json({"type": "error", "payload": {"code": "unauthorized", "detail": "Sign in before opening the live channel."}})
        await websocket.close(code=4401)
        return
    queue = event_bus.subscribe()
    try:
        with SessionLocal() as session:
            snapshot = loop_status(session).model_dump(mode="json")
        await websocket.send_json({"type": "hello", "payload": snapshot})

        async def sender() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        forwarder = asyncio.create_task(sender())
        try:
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_json({"type": "pong", "payload": {}})
        except WebSocketDisconnect:
            pass
        finally:
            forwarder.cancel()
            try:
                await forwarder
            except (asyncio.CancelledError, Exception):
                pass
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(queue)
