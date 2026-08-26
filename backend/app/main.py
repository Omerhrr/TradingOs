"""Practice-first FastAPI service for the TradingOS control plane."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_session
from app.models import AIResearchRun, AccountConfig, AccountSnapshot, AuditEvent, Candle, EncryptedBrokerCredential, FeatureSnapshot, MarketAsset, OrderIntent, OrderRecord, PositionSnapshot, ReconciliationRun, RiskPolicy, StrategyEvaluation, StrategyVersion, SystemState, WatchlistItem
from app.schemas import AccountStateResponse, AuditEventResponse, BrokerConnectionResponse, BrokerCredentialInput, CandleResponse, FeatureResponse, HealthResponse, MarketAssetResponse, OrderIntentInput, OrderIntentResponse, OrderResponse, PositionResponse, ReconciliationResponse, ResearchRunResponse, RiskPolicyResponse, StrategyCreateInput, StrategyEvaluationInput, StrategyEvaluationResponse, StrategyResponse, TradeResponse, WatchlistItemResponse, WatchlistUpdate
from app.services.broker import IQAirBrokerAdapter
from app.services.credentials import BrokerCredentials, CredentialConfigurationError, CredentialVault
from app.services.worker import BrokerWorker
from app.services.strategy import evaluate_strategy, persist_features
from app.services.research import ResearchBudgetExceeded, ResearchService
from app.services.execution import ExecutionService
from app.services.runtime import LocalRuntime


settings = get_settings()
worker = BrokerWorker(IQAirBrokerAdapter(), settings.broker_candle_count)
runtime = LocalRuntime(settings, SessionLocal, worker)


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
    runtime.start()
    try:
        yield
    finally:
        runtime.stop()


app = FastAPI(title="TradingOS API", version="0.2.0", description="Practice-only local TradingOS control plane. Real broker execution is unavailable.", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "Authorization"])


def _account(session: Session) -> AccountConfig:
    account = session.scalar(select(AccountConfig).limit(1))
    if account is None:
        raise HTTPException(status_code=500, detail="Account configuration was not initialized.")
    return account


def _require_local_admin(x_tradingos_token: str | None = Header(default=None)) -> None:
    """Credential and broker controls require a configured local admin token."""
    if not settings.local_admin_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Set TRADINGOS_LOCAL_ADMIN_TOKEN before enabling local broker controls.")
    if x_tradingos_token != settings.local_admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Local broker control token is invalid.")


def _vault() -> CredentialVault:
    try:
        return CredentialVault(settings.credential_encryption_key)
    except CredentialConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


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
    return list(session.scalars(select(StrategyVersion).order_by(StrategyVersion.created_at.desc())))


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
    return list(session.scalars(select(AIResearchRun).order_by(AIResearchRun.created_at.desc()).limit(100)))


@app.get(f"{settings.api_prefix}/orders", response_model=list[OrderResponse], tags=["orders"])
def list_orders(session: Session = Depends(get_session)) -> list[OrderRecord]:
    return list(session.scalars(select(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(200)))


@app.get(f"{settings.api_prefix}/order-intents", response_model=list[OrderIntentResponse], tags=["orders"])
def list_order_intents(session: Session = Depends(get_session)) -> list[OrderIntent]:
    return list(session.scalars(select(OrderIntent).order_by(OrderIntent.created_at.desc()).limit(200)))


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
    return list(session.scalars(select(TradeOutcome).order_by(TradeOutcome.settled_at.desc()).limit(200)))


@app.get(f"{settings.api_prefix}/events", response_model=list[AuditEventResponse], tags=["audit"])
def list_events(session: Session = Depends(get_session)) -> list[AuditEvent]:
    return list(session.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(200)))


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


@app.get(f"{settings.api_prefix}/positions", response_model=list[PositionResponse], tags=["positions"])
def list_positions(session: Session = Depends(get_session)) -> list[PositionSnapshot]:
    return list(session.scalars(select(PositionSnapshot).order_by(PositionSnapshot.observed_at.desc()).limit(500)))


@app.get(f"{settings.api_prefix}/reconciliation", response_model=list[ReconciliationResponse], tags=["broker"])
def list_reconciliation_runs(session: Session = Depends(get_session)) -> list[ReconciliationRun]:
    return list(session.scalars(select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc()).limit(100)))


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
    return state(session)


@app.post(f"{settings.api_prefix}/system/resume", response_model=AccountStateResponse, tags=["system"])
def resume_system(session: Session = Depends(get_session)) -> AccountStateResponse:
    account = _account(session)
    if account.mode != "PRACTICE" or worker.adapter.health().state != "CONNECTED":
        session.add(AuditEvent(event_type="SYSTEM_RESUME_REJECTED", severity="WARNING", message="Resume requires a connected practice broker; no state change occurred.", payload={}))
        session.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot resume: practice mode and a reconciled broker connection are required.")
    account.system_state = SystemState.ACTIVE.value
    session.commit()
    return state(session)


@app.post(f"{settings.api_prefix}/mode/enable-real", tags=["system"])
def enable_real_mode() -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Real mode is hard-disabled in this scaffold. No broker credentials or live execution path are configured.")
