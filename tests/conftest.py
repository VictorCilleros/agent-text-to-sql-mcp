"""Fixtures partagées par les tests."""

import psycopg
import pytest
from pydantic import ValidationError

from text_to_sql_mcp import db


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
