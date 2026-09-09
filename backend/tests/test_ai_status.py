"""LLM readiness probe for the bounded AI research path.

The research service already speaks any OpenAI-compatible endpoint through
airpy's OpenAIProvider (DeepSeek included). The status endpoint exists so an
operator can verify readiness — enabled flag, key, base URL, model, and today's
budget burn — before running research, without the API key ever leaving the
server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, settings
from app.models import AIResearchRun, ModelUsageRecord
from app.database import SessionLocal

ADMIN = {"X-TradingOS-Token": "test-local-admin-token"}


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _ai_world(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", False)
    monkeypatch.setattr(settings, "ai_api_key", None)
    monkeypatch.setattr(settings, "ai_base_url", None)
    monkeypatch.setattr(settings, "ai_model", "deepseek-chat")
    yield
    with SessionLocal() as session:
        session.query(ModelUsageRecord).delete()
        session.query(AIResearchRun).delete()
        session.commit()


def test_ai_status_requires_admin(client) -> None:
    assert client.get("/api/v1/ai/status").status_code == 401


def test_ai_status_reports_not_ready_by_default(client) -> None:
    body = client.get("/api/v1/ai/status", headers=ADMIN).json()
    assert body["ai_enabled"] is False
    assert body["api_key_configured"] is False
    assert body["ready"] is False
    assert body["model"] == "deepseek-chat"
    assert body["budget"]["tokens_used_today"] == 0
    assert body["budget"]["runs_today"] == 0


def test_ai_status_flags_ready_when_fully_configured(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_api_key", "sk-deepseek-secret-value")
    monkeypatch.setattr(settings, "ai_base_url", "https://api.deepseek.com")
    response = client.get("/api/v1/ai/status", headers=ADMIN)
    body = response.json()
    assert body["ready"] is True
    assert body["api_key_configured"] is True
    assert body["base_url"] == "https://api.deepseek.com"
    # The key itself must never appear anywhere in the response.
    assert "sk-deepseek-secret-value" not in response.text


def test_ai_status_budget_reflects_today_usage(client) -> None:
    with SessionLocal() as session:
        session.add(ModelUsageRecord(workflow_name="tradingos-research", model_name="deepseek-chat", prompt_tokens=120, completion_tokens=30, request_hash="hash-usage"))
        session.add(AIResearchRun(status="SUCCEEDED", model_name="deepseek-chat", input_digest={"request_hash": "hash-run"}))
        session.commit()
    body = client.get("/api/v1/ai/status", headers=ADMIN).json()
    assert body["budget"]["tokens_used_today"] == 150
    assert body["budget"]["runs_today"] == 1
    assert body["budget"]["token_budget"] == settings.ai_daily_token_budget
    assert body["budget"]["run_limit"] == settings.ai_daily_run_limit


def test_partial_configuration_is_never_ready(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_api_key", "sk-deepseek-secret-value")
    monkeypatch.setattr(settings, "ai_base_url", None)
    body = client.get("/api/v1/ai/status", headers=ADMIN).json()
    assert body["ready"] is False
