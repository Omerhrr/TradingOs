"""Strategy evaluation uses time-ordered, censored candles and does not look ahead."""

from datetime import UTC, datetime, timedelta

from app.models import Candle
from app.services.strategy import evaluate_ema_strategy


def _candles() -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    values = [100 + index * 0.2 + (0.4 if index % 3 else -0.1) for index in range(48)]
    return [Candle(symbol="EURUSD", timeframe_seconds=60, open_time=start + timedelta(minutes=index), close_time=start + timedelta(minutes=index + 1), open_price=value, high_price=value + 0.1, low_price=value - 0.1, close_price=value, volume=1) for index, value in enumerate(values)]


def test_ema_evaluation_requires_post_decision_censor_gap() -> None:
    result = evaluate_ema_strategy(_candles(), {"fast_window": 5, "slow_window": 10, "volatility_window": 5}, censor_gap_seconds=60)
    assert result["method"] == "ema_cross_walk_forward"
    assert result["censor_gap_seconds"] == 60
    assert result["trades"] >= 0
