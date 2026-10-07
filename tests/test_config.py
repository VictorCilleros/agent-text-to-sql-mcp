"""Tests de la configuration (sans base de données)."""

import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import ValidationError

from text_to_sql_mcp.config import Settings

VARIABLES = [
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "DB_SCHEMA",
    "AGENT_DB_USER",
    "AGENT_DB_PASSWORD",
    "CONNECT_TIMEOUT",
]


@pytest.fixture(autouse=True)
def environnement_vide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isole chaque test des variables d'environnement de la machine."""
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def reglages(**valeurs: object) -> Settings:
    """Construit des réglages de test sans lire le .env du repo."""
    return Settings(_env_file=None, **{"agent_db_password": "mdp-de-test", **valeurs})


def test_valeurs_par_defaut() -> None:
    s = reglages()
    assert (s.postgres_host, s.postgres_port, s.postgres_db) == ("127.0.0.1", 5432, "chinook")
    assert (s.db_schema, s.agent_db_user) == ("public", "agent_lecteur")


def test_mot_de_passe_obligatoire() -> None:
    with pytest.raises(ValidationError, match="agent_db_password"):
        Settings(_env_file=None)


def test_mot_de_passe_jamais_affiche() -> None:
    s = reglages()
    assert "mdp-de-test" not in repr(s)
    assert "mdp-de-test" not in str(s)


def test_variable_environnement_lue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    assert reglages().postgres_port == 5433


def test_port_invalide_refuse() -> None:
    with pytest.raises(ValidationError):
        reglages(postgres_port=70_000)


def test_conninfo_echappe_les_caracteres_speciaux() -> None:
    mot_de_passe = "p@ss w'rd %s"
    params = conninfo_to_dict(reglages(agent_db_password=mot_de_passe).conninfo())
    assert params["password"] == mot_de_passe
    assert params["user"] == "agent_lecteur"
    assert params["dbname"] == "chinook"
    assert params["application_name"] == "text-to-sql-mcp"
