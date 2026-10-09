"""Fixtures partagées par les tests."""

import psycopg
import pytest
from pydantic import ValidationError

from text_to_sql_mcp import db
from text_to_sql_mcp.config import get_agent_settings


def pytest_addoption(parser: pytest.Parser) -> None:
    """Ajoute l'option --api, qui active les tests appelant la vraie API Anthropic."""
    parser.addoption(
        "--api",
        action="store_true",
        help="lance aussi les tests marqués `api` (vraie API Anthropic, payant)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Saute les tests marqués `api`, sauf si l'option --api est donnée."""
    if config.getoption("--api"):
        return
    saut = pytest.mark.skip(reason="appelle la vraie API Anthropic : relancer avec --api")
    for item in items:
        if "api" in item.keywords:
            item.add_marker(saut)


@pytest.fixture(scope="session")
def base_disponible() -> None:
    """Saute les tests d'intégration si la base Docker n'est pas joignable."""
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    except ValidationError:
        pytest.skip("AGENT_DB_PASSWORD absent : copier .env.example en .env")
    except psycopg.OperationalError as erreur:
        pytest.skip(f"Base PostgreSQL injoignable, lancer `docker compose up -d` ({erreur})")


@pytest.fixture
def reglages_agent_de_test(monkeypatch: pytest.MonkeyPatch):
    """Réglages de l'agent avec une fausse clé, sans dépendre de la clé du .env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-cle-de-test")
    get_agent_settings.cache_clear()
    yield get_agent_settings()
    get_agent_settings.cache_clear()
