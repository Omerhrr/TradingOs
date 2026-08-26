"""Deterministic indicator, backtest, and learning services with no model dependency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import sqrt
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Candle, FeatureSnapshot, LearningEpisode, StrategyEvaluation, StrategyStatus, StrategyVersion


FEATURE_VERSION = "features-v1"


@dataclass(frozen=True)
class CandlePoint:
    open_time: datetime
    close: float
    high: float
    low: float


def _ema(values: list[float], window: int) -> list[float | None]:
    if window <= 0:
        raise ValueError("EMA window must be positive.")
    multiplier = 2 / (window + 1)
    results: list[float | None] = []
    current: float | None = None
    for index, value in enumerate(values):
        if index + 1 < window:
            results.append(None)
            continue
        if current is None:
            current = sum(values[index + 1 - window : index + 1]) / window
        else:
            current = (value - current) * multiplier + current
        results.append(current)
    return results


def _returns(values: list[float]) -> list[float | None]:
    output: list[float | None] = [None]
    for previous, current in zip(values, values[1:]):
        output.append((current / previous) - 1 if previous else None)
    return output


def _rolling_volatility(returns: list[float | None], window: int) -> list[float | None]:
    output: list[float | None] = []
    for index in range(len(returns)):
        sample = [value for value in returns[max(0, index + 1 - window) : index + 1] if value is not None]
        if len(sample) < window:
            output.append(None)
            continue
        mean = sum(sample) / len(sample)
        output.append(sqrt(sum((value - mean) ** 2 for value in sample) / len(sample)))
    return output


def calculate_features(candles: list[CandlePoint], fast_window: int = 12, slow_window: int = 26, volatility_window: int = 20) -> list[dict]:
    """Compute features only from candles at or before the current index."""
    if fast_window >= slow_window:
        raise ValueError("fast_window must be smaller than slow_window.")
    closes = [candle.close for candle in candles]
    fast = _ema(closes, fast_window)
    slow = _ema(closes, slow_window)
    returns = _returns(closes)
    volatility = _rolling_volatility(returns, volatility_window)
    output: list[dict] = []
    for index, candle in enumerate(candles):
        signal = "HOLD"
        if fast[index] is not None and slow[index] is not None:
            signal = "CALL" if fast[index] > slow[index] else "PUT" if fast[index] < slow[index] else "HOLD"
        output.append({"fast_ema": fast[index], "slow_ema": slow[index], "return_1": returns[index], "volatility": volatility[index], "signal": signal})
    return output


def persist_features(session: Session, symbol: str, timeframe_seconds: int, candles: list[Candle], feature_definition: dict) -> int:
    points = [CandlePoint(open_time=candle.open_time, close=candle.close_price, high=candle.high_price, low=candle.low_price) for candle in candles]
    features = calculate_features(points, fast_window=int(feature_definition.get("fast_window", 12)), slow_window=int(feature_definition.get("slow_window", 26)), volatility_window=int(feature_definition.get("volatility_window", 20)))
    inserted = 0
    for candle, values in zip(candles, features):
        existing = session.scalar(select(FeatureSnapshot).where(FeatureSnapshot.symbol == symbol, FeatureSnapshot.timeframe_seconds == timeframe_seconds, FeatureSnapshot.candle_open_time == candle.open_time, FeatureSnapshot.feature_version == FEATURE_VERSION))
        if existing is None:
            session.add(FeatureSnapshot(symbol=symbol, timeframe_seconds=timeframe_seconds, candle_open_time=candle.open_time, feature_version=FEATURE_VERSION, values=values))
            inserted += 1
        else:
            existing.values = values
    return inserted


def _max_drawdown(equity: list[float]) -> float:
    peak = 1.0
    maximum = 0.0
    for value in equity:
        peak = max(peak, value)
        maximum = max(maximum, (peak - value) / peak if peak else 0.0)
    return maximum


def evaluate_ema_strategy(candles: list[Candle], definition: dict, censor_gap_seconds: int) -> dict:
    """Walk forward with the decision at candle i and entry/exit strictly after its censor gap."""
    if len(candles) < 30:
        raise ValueError("At least 30 candles are required for a meaningful evaluation.")
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    features = calculate_features([CandlePoint(c.open_time, c.close_price, c.high_price, c.low_price) for c in ordered], fast_window=int(definition.get("fast_window", 12)), slow_window=int(definition.get("slow_window", 26)), volatility_window=int(definition.get("volatility_window", 20)))
    trades: list[float] = []
    equity = [1.0]
    for index in range(max(int(definition.get("slow_window", 26)), 1), len(ordered) - 2):
        decision_time = ordered[index].open_time
        entry = ordered[index + 1]
        exit_candle = ordered[index + 2]
        if (entry.open_time - decision_time).total_seconds() < censor_gap_seconds:
            continue
        signal = features[index]["signal"]
        if signal == "HOLD":
            continue
        move = (exit_candle.close_price / entry.close_price) - 1 if entry.close_price else 0.0
        trade_return = move if signal == "CALL" else -move
        trades.append(trade_return)
        equity.append(equity[-1] * (1 + trade_return))
    wins = sum(1 for value in trades if value > 0)
    total_return = equity[-1] - 1
    return {"trades": len(trades), "wins": wins, "win_rate": wins / len(trades) if trades else 0.0, "total_return": total_return, "max_drawdown": _max_drawdown(equity), "average_trade_return": sum(trades) / len(trades) if trades else 0.0, "method": "ema_cross_walk_forward", "censor_gap_seconds": censor_gap_seconds}


def evaluate_strategy(session: Session, strategy: StrategyVersion, symbol: str, timeframe_seconds: int, censor_gap_seconds: int) -> StrategyEvaluation:
    candles = list(session.scalars(select(Candle).where(Candle.symbol == symbol.upper(), Candle.timeframe_seconds == timeframe_seconds).order_by(Candle.open_time)))
    metrics = evaluate_ema_strategy(candles, strategy.definition, censor_gap_seconds)
    accepted = metrics["trades"] >= 10 and metrics["total_return"] > 0 and metrics["max_drawdown"] <= float(strategy.definition.get("max_drawdown", 0.05))
    evaluation = StrategyEvaluation(strategy_version_id=strategy.id, dataset_start=candles[0].open_time, dataset_end=candles[-1].open_time, censor_gap_seconds=censor_gap_seconds, metrics=metrics, accepted=accepted)
    strategy.validation_summary = metrics
    strategy.status = StrategyStatus.VALIDATED.value if accepted else StrategyStatus.DRAFT.value
    session.add(evaluation)
    session.add(LearningEpisode(strategy_version_id=strategy.id, episode_type="STRATEGY_EVALUATION", conclusion="Validation accepted the candidate for practice review." if accepted else "Validation did not meet the activation threshold; candidate remains a draft.", evidence=metrics))
    return evaluation


def utc_now() -> datetime:
    return datetime.now(UTC)
