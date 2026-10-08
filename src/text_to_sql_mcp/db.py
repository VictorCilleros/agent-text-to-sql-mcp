"""Accès à PostgreSQL en lecture seule (psycopg 3).

Ce module ne connaît pas MCP : il expose des fonctions Python, des modèles de données et des
exceptions métier. C'est le serveur qui les traduit en outils et en messages pour le modèle.
"""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from functools import cache
from importlib.resources import files
from typing import Any

import psycopg
from psycopg.rows import class_row
from pydantic import BaseModel, JsonValue

from text_to_sql_mcp.config import get_settings
from text_to_sql_mcp.guardrails import validate_query


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


class QueryError(Exception):
    """Requête refusée ou en échec côté PostgreSQL, avec un message exploitable par l'agent."""

    def __init__(self, sqlstate: str | None, message: str, hint: str | None = None) -> None:
        """Construit le message à partir du diagnostic PostgreSQL.

        Args:
            sqlstate: Code d'erreur SQLSTATE (ex. 42703 pour une colonne inconnue).
            message: Message principal de PostgreSQL.
            hint: Indice éventuel fourni par PostgreSQL.
        """
        self.sqlstate = sqlstate
        texte = f"La requête a échoué (SQLSTATE {sqlstate}) : {message}"
        if hint:
            texte += f" Indice : {hint}"
        super().__init__(texte)


class ColumnInfo(BaseModel):
    """Description d'une colonne, telle que renvoyée par get_schema."""

    column_name: str
    data_type: str
    nullable: bool
    primary_key: bool
    foreign_key: str | None


class QueryResult(BaseModel):
    """Résultat plafonné d'une requête, au format compact colonnes + lignes."""

    executed_sql: str
    columns: list[str]
    rows: list[list[JsonValue]]
    row_count: int
    truncated: bool
    row_cap: int


# Erreurs de forme : requête vide, plusieurs instructions, écriture, ou instruction autre
# qu'une lecture (SHOW, EXPLAIN…). Normalement arrêtées par guardrails ; le curseur serveur
# les rejette aussi, en seconde barrière.
_SQLSTATE_FORME = {"42601", "0A000"}
_RAPPEL_FORME = "Seule une requête de lecture unique est acceptée (SELECT, ou WITH … SELECT)."


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


def describe_table(table: str) -> list[ColumnInfo]:
    """Décrit les colonnes d'une table : type, nullabilité, clé primaire, clé étrangère.

    Le nom est d'abord vérifié contre la liste blanche des tables, puis passé en paramètre
    de la requête : il n'est jamais interpolé dans le SQL.

    Args:
        table: Nom exact d'une table du schéma exposé.

    Returns:
        Une description par colonne, dans l'ordre de la table.

    Raises:
        UnknownTableError: Si la table n'existe pas ou n'est pas lisible par le rôle.
    """
    with connect() as conn:
        available = _table_names(conn)
        if table not in available:
            raise UnknownTableError(table, available)
        with conn.cursor(row_factory=class_row(ColumnInfo)) as cur:
            cur.execute(
                load_query("get_schema"),
                {"schema": get_settings().db_schema, "table": table},
            )
            return cur.fetchall()


def to_json_value(value: Any) -> JsonValue:
    """Convertit une valeur renvoyée par psycopg en valeur sérialisable en JSON.

    Les `numeric` (Decimal) deviennent du texte pour garder leur valeur exacte ; les dates et
    heures passent au format ISO 8601 ; les tableaux et JSON PostgreSQL sont convertis
    récursivement ; tout autre type est rendu par `str()`.

    Args:
        value: Valeur d'une cellule de résultat.

    Returns:
        Une valeur compatible JSON.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_json_value(item) for key, item in value.items()}
    return str(value)


def run_query(sql: str, row_cap: int) -> QueryResult:
    """Valide puis exécute une requête de lecture, et renvoie au plus `row_cap` lignes.

    La requête est d'abord validée par `guardrails.validate_query` (lecture unique, tables de
    la liste blanche, pas de fonction interdite, LIMIT imposé). C'est le SQL **régénéré** à
    partir de l'arbre validé qui est exécuté, renvoyé dans `executed_sql`.

    L'exécution passe par un curseur côté serveur (`DECLARE … CURSOR FOR`) : seules les lignes
    lues traversent le réseau, et PostgreSQL rejette lui aussi tout ce qui n'est pas une
    lecture unique. Le délai est borné par le `statement_timeout` de la connexion.
    La requête n'est jamais paramétrée : un `%` (par exemple dans un LIKE) reste littéral.

    Args:
        sql: Requête écrite par l'agent.
        row_cap: Nombre maximal de lignes renvoyées.

    Returns:
        Le SQL exécuté, les colonnes, les lignes converties en JSON et l'indicateur de
        troncature.

    Raises:
        GuardrailError: Si la requête enfreint une règle de validation.
        QueryError: Si PostgreSQL refuse la requête, échoue ou dépasse le délai.
    """
    with connect() as conn:
        executed_sql = validate_query(
            sql, _table_names(conn), row_cap=row_cap, schema=get_settings().db_schema
        )
        try:
            with conn.cursor(name="run_query") as cur:
                cur.execute(executed_sql)
                rows = cur.fetchmany(row_cap + 1)
                columns = [column.name for column in cur.description or []]
        except psycopg.errors.QueryCanceled as erreur:
            delai = get_settings().statement_timeout
            raise QueryError(
                erreur.sqlstate,
                f"la requête a dépassé le délai de {delai} s et a été annulée.",
                "Filtrer davantage, agréger, ou éviter les produits cartésiens.",
            ) from erreur
        except psycopg.OperationalError:
            raise  # panne de connexion : erreur d'infrastructure, masquée au modèle
        except psycopg.Error as erreur:
            hint = erreur.diag.message_hint
            if erreur.sqlstate in _SQLSTATE_FORME:
                hint = f"{hint} {_RAPPEL_FORME}" if hint else _RAPPEL_FORME
            message = erreur.diag.message_primary or str(erreur)
            raise QueryError(erreur.sqlstate, message, hint) from erreur
    truncated = len(rows) > row_cap
    rows = rows[:row_cap]
    return QueryResult(
        executed_sql=executed_sql,
        columns=columns,
        rows=[[to_json_value(value) for value in row] for row in rows],
        row_count=len(rows),
        truncated=truncated,
        row_cap=row_cap,
    )
