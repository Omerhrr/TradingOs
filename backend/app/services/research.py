"""Bounded aircore research. Outputs hypotheses only; no broker or order service is imported."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIResearchRun, FeatureSnapshot, ModelUsageRecord, StrategyEvaluation, StrategyVersion, WatchlistItem


class ResearchProposal(BaseModel):
    disposition: Literal["OBSERVE", "RESEARCH", "REJECT"]
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(min_length=1, max_length=1_200)
    risk_flags: list[str] = Field(max_length=8)
    next_validation_steps: list[str] = Field(max_length=6)


class ResearchBudgetExceeded(RuntimeError):
    """Raised before a model call when the deterministic daily budget is exhausted."""


def _as_jsonable(value: object) -> dict:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise TypeError("Research workflow did not return a structured proposal.")


class ResearchService:
    """A low-frequency, manual research path with capped context and recorded usage."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _usage_before_today(self, session: Session) -> tuple[int, int]:
        cutoff = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        records = list(session.scalars(select(ModelUsageRecord).where(ModelUsageRecord.created_at >= cutoff)))
        token_total = sum((record.prompt_tokens or 0) + (record.completion_tokens or 0) for record in records)
        runs = session.scalar(select(func.count()).select_from(AIResearchRun).where(AIResearchRun.created_at >= cutoff)) or 0
        return token_total, runs

    def _digest(self, session: Session, strategy: StrategyVersion | None) -> dict:
        watchlist = list(session.scalars(select(WatchlistItem).where(WatchlistItem.enabled.is_(True)).order_by(WatchlistItem.symbol)))
        symbols = [item.symbol for item in watchlist]
        latest_features: list[dict] = []
        for item in watchlist[:12]:
            feature = session.scalar(select(FeatureSnapshot).where(FeatureSnapshot.symbol == item.symbol, FeatureSnapshot.timeframe_seconds == item.timeframe_seconds).order_by(FeatureSnapshot.candle_open_time.desc()).limit(1))
            if feature:
                latest_features.append({"symbol": item.symbol, "timeframe_seconds": item.timeframe_seconds, "as_of": feature.candle_open_time.isoformat(), "values": feature.values})
        evaluations = []
        if strategy is not None:
            evaluations = [{"accepted": row.accepted, "metrics": row.metrics, "created_at": row.created_at.isoformat()} for row in session.scalars(select(StrategyEvaluation).where(StrategyEvaluation.strategy_version_id == strategy.id).order_by(StrategyEvaluation.created_at.desc()).limit(3))]
        return {"watchlist": symbols, "strategy": {"id": strategy.id, "key": strategy.strategy_key, "version": strategy.version, "definition": strategy.definition, "validation": strategy.validation_summary} if strategy else None, "latest_features": latest_features, "evaluations": evaluations, "guardrails": ["Do not recommend an order, size, or execution time.", "Treat missing/stale evidence as a reason to observe or reject.", "Return a testable hypothesis only."]}

    def run(self, session: Session, strategy: StrategyVersion | None = None) -> AIResearchRun:
        if not self.settings.ai_enabled:
            raise RuntimeError("AI research is disabled. Set TRADINGOS_AI_ENABLED=true only after configuring a dedicated model endpoint and budget.")
        if not self.settings.ai_api_key or not self.settings.ai_base_url:
            raise RuntimeError("Set TRADINGOS_AI_API_KEY and TRADINGOS_AI_BASE_URL before enabling AI research.")
        used_tokens, used_runs = self._usage_before_today(session)
        if used_tokens >= self.settings.ai_daily_token_budget:
            raise ResearchBudgetExceeded("Daily AI token budget is exhausted; no model call was made.")
        if used_runs >= self.settings.ai_daily_run_limit:
            raise ResearchBudgetExceeded("Daily AI research-run limit is exhausted; no model call was made.")
        digest = self._digest(session, strategy)
        prompt = json.dumps(digest, separators=(",", ":"), sort_keys=True)
        if len(prompt) > self.settings.ai_max_prompt_characters:
            prompt = prompt[: self.settings.ai_max_prompt_characters]
            digest["truncated"] = True
        request_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        duplicate = session.scalar(select(AIResearchRun).where(AIResearchRun.input_digest["request_hash"].as_string() == request_hash, AIResearchRun.created_at >= datetime.now(UTC) - timedelta(hours=6)).limit(1))
        if duplicate:
            return duplicate
        run = AIResearchRun(strategy_version_id=strategy.id if strategy else None, status="RUNNING", model_name=self.settings.ai_model, input_digest={"request_hash": request_hash, "context": digest})
        session.add(run)
        session.flush()
        try:
            from aircore import Policy, Workflow
            from aircore.tools import Tool
            from airpy import ModelAgent
            from airpy.openai_provider import OpenAIProvider

            def market_context() -> dict:
                """Return the prefiltered, read-only research context; it contains no broker credentials or execution controls."""
                return digest

            context_tool = Tool(market_context, name="market_context", idempotent=True, description="Retrieve compact, read-only market and validation context.")
            provider = OpenAIProvider(model=self.settings.ai_model, api_key=self.settings.ai_api_key, base_url=self.settings.ai_base_url)
            agent = ModelAgent(name="hypothesis_researcher", provider=provider, model=self.settings.ai_model, prompt="Use the market_context tool first. Produce a conservative, typed research proposal. You are forbidden from recommending an order, size, or execution time.", tools=[context_tool], max_turns=2, output_schema=ResearchProposal, use_mindgraph=True)
            workflow = Workflow("tradingos-research", policy=Policy(max_runtime=45, max_parallel=1, max_cost=None))
            workflow.step(agent, as_="proposal")
            workflow.run()
            proposal = _as_jsonable(workflow.bindings["proposal"])
            usage = agent.last_response.usage if agent.last_response else None
            run.status = "SUCCEEDED"
            run.output = proposal
            run.completed_at = datetime.now(UTC)
            session.add(ModelUsageRecord(workflow_name="tradingos-research", model_name=self.settings.ai_model, prompt_tokens=getattr(usage, "prompt_tokens", None), completion_tokens=getattr(usage, "completion_tokens", None), cost_usd=getattr(usage, "cost_usd", None), request_hash=request_hash))
            session.commit()
            return run
        except Exception as exc:
            run.status = "FAILED"
            run.error_message = str(exc)
            run.completed_at = datetime.now(UTC)
            session.commit()
            raise
