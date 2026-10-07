"""Serveur MCP exposant la base Chinook en lecture seule (transport stdio)."""

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from text_to_sql_mcp import db
from text_to_sql_mcp.config import get_settings

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


@mcp.tool(annotations=LECTURE_SEULE)
def list_tables() -> list[str]:
    """Liste les tables de la base interrogeables en SQL.

    À appeler en premier, puis get_schema sur les tables utiles à la question.
    """
    return db.list_table_names()


@mcp.tool(annotations=LECTURE_SEULE)
def get_schema(
    table: Annotated[
        str,
        Field(description="Nom exact d'une table renvoyé par list_tables (ex. invoice_line)."),
    ],
) -> list[db.ColumnInfo]:
    """Décrit les colonnes d'une table : type PostgreSQL, nullabilité et clés.

    `primary_key` signale les colonnes de la clé primaire. `foreign_key` donne la colonne
    référencée sous la forme `table.colonne` : c'est la condition de jointure à utiliser.
    """
    try:
        return db.describe_table(table)
    except db.UnknownTableError as erreur:
        raise ToolError(str(erreur)) from erreur


@mcp.tool(annotations=LECTURE_SEULE)
def run_query(
    sql: Annotated[
        str,
        Field(description="Une seule requête PostgreSQL de lecture (SELECT ou WITH … SELECT)."),
    ],
) -> db.QueryResult:
    """Exécute une requête SQL en lecture seule et renvoie les colonnes et les lignes.

    Utiliser uniquement les tables et colonnes renvoyées par list_tables et get_schema.
    Toute écriture est refusée. Le résultat est plafonné à `row_cap` lignes : si `truncated`
    vaut true, préférer une agrégation ou un LIMIT explicite. Les valeurs numeric sont
    renvoyées en texte (valeur exacte) et les dates au format ISO 8601.
    """
    try:
        return db.run_query(sql, row_cap=get_settings().row_cap)
    except db.QueryError as erreur:
        raise ToolError(str(erreur)) from erreur


def main() -> None:
    """Point d'entrée : lance le serveur MCP sur stdio."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
