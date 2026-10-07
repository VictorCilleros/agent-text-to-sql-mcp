"""Accès à PostgreSQL en lecture seule (psycopg 3).

Ce module ne connaît pas MCP : il expose des fonctions Python et des exceptions métier.
C'est le serveur qui les traduit en outils et en messages pour le modèle.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib.resources import files
from typing import Any

import psycopg
from psycopg.rows import dict_row

from text_to_sql_mcp.config import get_settings


class UnknownTableError(ValueError):
    """Nom de table absent de la liste blanche des tables exposées."""

    def __init__(self, table: str, available: list[str]) -> None:
        """Construit un message qui liste les tables valides, pour permettre la correction.

        Args:
            table: Nom de table demandé.
            available: Tables réellement disponibles.
        """
        self.table = table
        self.available = available
        super().__init__(
            f"Table inconnue : {table!r}. Tables disponibles : {', '.join(available)}."
        )


@cache
def load_query(name: str) -> str:
    """Charge une requête SQL externalisée du package (dossier `sql/`).

    Args:
        name: Nom du fichier, sans l'extension `.sql`.

    Returns:
        Le texte de la requête.
    """
    return files("text_to_sql_mcp").joinpath("sql", f"{name}.sql").read_text(encoding="utf-8")


@contextmanager
def connect() -> Iterator[psycopg.Connection[Any]]:
    """Ouvre une connexion courte avec le rôle lecture seule.

    Une connexion par appel : aucun état n'est partagé entre deux requêtes de l'agent
    (transaction avortée, réglage modifié par `set_config`…). `read_only = True` fait
    démarrer chaque transaction par `BEGIN READ ONLY`, en plus du réglage par défaut du rôle
    et de ses privilèges limités à `SELECT`. La transaction est annulée en cas d'erreur, et la
    connexion toujours fermée.

    Yields:
        Une connexion psycopg ouverte.
    """
    with psycopg.connect(get_settings().conninfo()) as conn:
        conn.read_only = True
        yield conn


def _table_names(conn: psycopg.Connection[Any]) -> list[str]:
    """Liste les tables du schéma exposé, sur une connexion existante."""
    rows = conn.execute(load_query("list_tables"), {"schema": get_settings().db_schema})
    return [row[0] for row in rows]


def list_table_names() -> list[str]:
    """Liste les tables que le rôle lecture seule peut lire.

    Returns:
        Les noms de tables, triés par ordre alphabétique.
    """
    with connect() as conn:
        return _table_names(conn)


def describe_table(table: str) -> list[dict[str, Any]]:
    """Décrit les colonnes d'une table : type, nullabilité, clé primaire, clé étrangère.

    Le nom est d'abord vérifié contre la liste blanche des tables, puis passé en paramètre
    de la requête : il n'est jamais interpolé dans le SQL.

    Args:
        table: Nom exact d'une table du schéma exposé.

    Returns:
        Une ligne par colonne, dans l'ordre de la table, avec les clés `column_name`,
        `data_type`, `nullable`, `primary_key` et `foreign_key` (`table.colonne` ou None).

    Raises:
        UnknownTableError: Si la table n'existe pas ou n'est pas lisible par le rôle.
    """
    with connect() as conn:
        available = _table_names(conn)
        if table not in available:
            raise UnknownTableError(table, available)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                load_query("get_schema"),
                {"schema": get_settings().db_schema, "table": table},
            )
            return cur.fetchall()
