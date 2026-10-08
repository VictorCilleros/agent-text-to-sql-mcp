"""Serveur MCP exposant la base Chinook en lecture seule (transport stdio)."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from text_to_sql_mcp import db
from text_to_sql_mcp.config import get_settings
from text_to_sql_mcp.guardrails import GuardrailError
from text_to_sql_mcp.logging_config import LOGGER_NAME, configure_logging

logger = logging.getLogger(f"{LOGGER_NAME}.server")

INSTRUCTIONS = """\
Base PostgreSQL Chinook (magasin de musique en ligne : artistes, albums, pistes, clients,
factures), accessible en lecture seule.
Démarche : list_tables pour connaître les tables, get_schema pour leurs colonnes et leurs
clés, puis run_query pour exécuter une requête SELECT.
Tous les identifiants sont en snake_case et au singulier (invoice_line, media_type).
Les données (titres, noms d'artistes, genres, pays) sont en anglais.
"""

LECTURE_SEULE = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

mcp = MCPServer("text-to-sql-mcp", instructions=INSTRUCTIONS)


def client_name(ctx: Context) -> str:
    """Identifie le client MCP qui appelle l'outil, d'après son `clientInfo`.

    Notre agent y indiquera le modèle et la variante de prompt testés ; Claude Desktop ou
    l'Inspector y mettent leur propre nom.

    Args:
        ctx: Contexte de la requête MCP en cours.

    Returns:
        `nom/version` du client, ou `inconnu` s'il ne s'est pas présenté.
    """
    params = ctx.session.client_params
    if params is None:
        return "inconnu"
    return f"{params.client_info.name}/{params.client_info.version}"


@contextmanager
def trace_call(ctx: Context, tool: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Trace un appel d'outil en une ligne de log, et traduit les erreurs pour le modèle.

    Le bloc protégé peut enrichir le dictionnaire renvoyé (nombre de lignes, SQL exécuté…).
    Statuts : `ok`, `refus` (garde-fou ou table inconnue), `erreur` (PostgreSQL a refusé ou
    échoué) et `echec` (exception imprévue, masquée au modèle par le SDK).

    Args:
        ctx: Contexte de la requête MCP, pour identifier le client.
        tool: Nom de l'outil appelé.
        **fields: Champs à tracer dès le début (table demandée, SQL soumis…).

    Yields:
        Le dictionnaire des champs du log, à compléter.

    Raises:
        ToolError: Pour un refus ou une erreur SQL, avec un message lisible par le modèle.
    """
    trace: dict[str, Any] = {"client": client_name(ctx), "outil": tool, **fields}
    debut = time.perf_counter()
    niveau, statut, erreur_outil = logging.INFO, "ok", None
    try:
        yield trace
    except GuardrailError as erreur:
        niveau, statut, erreur_outil = logging.WARNING, "refus", erreur
        trace["regle"] = erreur.rule
    except db.UnknownTableError as erreur:
        niveau, statut, erreur_outil = logging.WARNING, "refus", erreur
        trace["regle"] = "table_inconnue"
    except db.QueryError as erreur:
        niveau, statut, erreur_outil = logging.WARNING, "erreur", erreur
        trace["sqlstate"] = erreur.sqlstate
    except Exception:
        trace |= {"statut": "echec", "duree_ms": _duree_ms(debut)}
        logger.exception("appel_outil", extra=trace)
        raise
    trace |= {"statut": statut, "duree_ms": _duree_ms(debut)}
    if erreur_outil is not None:
        trace["detail"] = str(erreur_outil)
    logger.log(niveau, "appel_outil", extra=trace)
    if erreur_outil is not None:
        raise ToolError(str(erreur_outil)) from erreur_outil


def _duree_ms(debut: float) -> float:
    """Durée écoulée depuis `debut`, en millisecondes arrondies."""
    return round((time.perf_counter() - debut) * 1000, 1)


@mcp.tool(annotations=LECTURE_SEULE)
def list_tables(ctx: Context) -> list[str]:
    """Liste les tables de la base interrogeables en SQL.

    À appeler en premier, puis get_schema sur les tables utiles à la question.
    """
    with trace_call(ctx, "list_tables") as trace:
        tables = db.list_table_names()
        trace["lignes"] = len(tables)
    return tables


@mcp.tool(annotations=LECTURE_SEULE)
def get_schema(
    table: Annotated[
        str,
        Field(description="Nom exact d'une table renvoyé par list_tables (ex. invoice_line)."),
    ],
    ctx: Context,
) -> list[db.ColumnInfo]:
    """Décrit les colonnes d'une table : type PostgreSQL, nullabilité et clés.

    `primary_key` signale les colonnes de la clé primaire. `foreign_key` donne la colonne
    référencée sous la forme `table.colonne` : c'est la condition de jointure à utiliser.
    """
    with trace_call(ctx, "get_schema", table=table) as trace:
        colonnes = db.describe_table(table)
        trace["lignes"] = len(colonnes)
    return colonnes


@mcp.tool(annotations=LECTURE_SEULE)
def run_query(
    sql: Annotated[
        str,
        Field(description="Une seule requête PostgreSQL de lecture (SELECT ou WITH … SELECT)."),
    ],
    ctx: Context,
) -> db.QueryResult:
    """Exécute une requête SQL en lecture seule et renvoie les colonnes et les lignes.

    Utiliser uniquement les tables et colonnes renvoyées par list_tables et get_schema.
    Toute écriture, toute table système et les fonctions d'administration (pg_*, set_config…)
    sont refusées. La requête est normalisée avant exécution : `executed_sql` contient le SQL
    réellement exécuté, avec un LIMIT imposé. Le résultat est plafonné à `row_cap` lignes : si
    `truncated` vaut true, préférer une agrégation ou un LIMIT explicite. Une requête trop
    longue est annulée. Les valeurs numeric sont renvoyées en texte (valeur exacte) et les
    dates au format ISO 8601.
    """
    with trace_call(ctx, "run_query", sql=sql) as trace:
        resultat = db.run_query(sql, row_cap=get_settings().row_cap)
        trace |= {
            "sql_execute": resultat.executed_sql,
            "lignes": resultat.row_count,
            "tronque": resultat.truncated,
        }
    return resultat


def main() -> None:
    """Point d'entrée : configure les logs JSON sur stderr et lance le serveur sur stdio."""
    configure_logging(get_settings().log_level)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
