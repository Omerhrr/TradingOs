"""The AI path is opt-in and must refuse calls before a model endpoint is configured."""

import pytest

from app.config import Settings
from app.services.research import ResearchService


def test_research_refuses_when_opt_in_is_disabled() -> None:
    service = ResearchService(Settings(ai_enabled=False))
    with pytest.raises(RuntimeError, match="disabled"):
        service.run(None)  # type: ignore[arg-type]
