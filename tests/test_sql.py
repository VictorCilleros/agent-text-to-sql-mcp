"""Tests du chargement des requêtes SQL externalisées (sans base de données)."""

import pytest

from text_to_sql_mcp.db import load_query


@pytest.mark.parametrize(
    ("nom", "parametres"),
    [
        ("list_tables", ["%(schema)s"]),
        ("get_schema", ["%(schema)s", "%(table)s"]),
    ],
)
def test_requete_chargee_et_parametree(nom: str, parametres: list[str]) -> None:
    requete = load_query(nom)
    assert requete.lstrip().startswith("--")
    for parametre in parametres:
        assert parametre in requete


def test_requete_inconnue() -> None:
    with pytest.raises(FileNotFoundError):
        load_query("n_existe_pas")
