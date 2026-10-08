"""Validation du SQL écrit par l'agent, avant toute exécution (sqlglot).

Le module est pur : ni connexion, ni MCP. Il reçoit le texte SQL et la liste blanche des tables,
et renvoie le SQL à exécuter, régénéré à partir de l'arbre validé. On exécute donc exactement ce
qui a été vérifié, et non un texte que PostgreSQL pourrait comprendre autrement que sqlglot.

Ces règles complètent les protections de la base (privilèges `SELECT` seuls, transaction en
lecture seule, curseur serveur, timeout) ; elles ne les remplacent pas.
"""

from collections.abc import Collection

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.optimizer.scope import traverse_scope

# Nœuds interdits n'importe où dans l'arbre, même sous un SELECT.
_NOEUDS_INTERDITS: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,  # écriture, y compris dans une CTE
    exp.Into,  # SELECT … INTO nouvelle_table
    exp.Lock,  # FOR UPDATE / FOR SHARE
    exp.Command,  # SHOW, EXPLAIN, COPY… : syntaxe non analysée finement
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
)

# Liste noire de fonctions (noms en minuscules). Les fonctions d'administration de PostgreSQL
# commencent presque toutes par pg_ ; les fonctions *_to_xml et ts_stat exécutent du SQL passé
# en texte, ce qui contournerait l'analyse de l'arbre.
_PREFIXES_INTERDITS = (
    "pg_",
    "dblink",
    "lo_",
    "query_to_xml",
    "cursor_to_xml",
    "table_to_xml",
    "schema_to_xml",
    "database_to_xml",
)
_FONCTIONS_INTERDITES = frozenset(
    {"set_config", "current_setting", "nextval", "setval", "currval", "ts_stat", "ts_rewrite"}
)


class GuardrailError(ValueError):
    """Requête refusée par une règle, avec un message destiné à l'agent."""

    def __init__(self, rule: str, message: str) -> None:
        """Construit le refus.

        Args:
            rule: Nom court de la règle déclenchée (tracé dans les logs).
            message: Explication lisible par l'agent, pour qu'il corrige sa requête.
        """
        self.rule = rule
        super().__init__(f"Requête refusée ({rule}) : {message}")


def validate_query(
    sql: str, allowed_tables: Collection[str], row_cap: int, schema: str = "public"
) -> str:
    """Valide une requête de l'agent et renvoie le SQL à exécuter.

    Règles, dans l'ordre :
    1. une seule instruction, qui est une lecture, sans écriture ni verrou cachés ;
    2. uniquement des tables de la liste blanche (ou des CTE définies dans la requête) ;
    3. aucune fonction de la liste noire ;
    4. un `LIMIT row_cap + 1` imposé (le `+1` permet de détecter la troncature), sauf si la
       requête a déjà une limite littérale plus petite.

    Args:
        sql: Requête écrite par l'agent.
        allowed_tables: Tables métier autorisées (celles de `list_tables`).
        row_cap: Nombre maximal de lignes renvoyées.
        schema: Seul schéma autorisé pour les noms qualifiés (`schema.table`).

    Returns:
        Le SQL régénéré par sqlglot à partir de l'arbre validé (dialecte PostgreSQL), sans
        les commentaires.

    Raises:
        GuardrailError: Si une règle est enfreinte.
    """
    arbre = _parse_single_read(sql)
    _check_tables(arbre, frozenset(allowed_tables), schema)
    _check_functions(arbre)
    return _impose_limit(arbre, row_cap).sql(dialect="postgres", comments=False)


def _parse_single_read(sql: str) -> exp.Query:
    """Règle 1 : une seule instruction de lecture, sans construction interdite."""
    try:
        arbres = [a for a in sqlglot.parse(sql, read="postgres") if a is not None]
    except ParseError as erreur:
        detail = erreur.errors[0]["description"] if erreur.errors else str(erreur)
        raise GuardrailError("syntaxe", f"SQL illisible ({detail}).") from None
    if not arbres:
        raise GuardrailError("instruction_unique", "aucune requête reçue.")
    if len(arbres) > 1:
        raise GuardrailError(
            "instruction_unique", f"une seule requête est acceptée, {len(arbres)} reçues."
        )
    arbre = arbres[0]
    if not isinstance(arbre, exp.Query):
        raise GuardrailError(
            "lecture_seule", f"seule une lecture (SELECT) est acceptée, pas {arbre.key.upper()}."
        )
    noeud = arbre.find(*_NOEUDS_INTERDITS)
    if noeud is not None:
        raise GuardrailError("construction", f"{noeud.key.upper()} est interdit.")
    return arbre


def _normalize(identifier: exp.Identifier | None) -> str:
    """Nom tel que PostgreSQL le comprend : minuscules, sauf s'il est entre guillemets."""
    if identifier is None:
        return ""
    return identifier.name if identifier.quoted else identifier.name.lower()


def _check_tables(arbre: exp.Query, allowed: frozenset[str], schema: str) -> None:
    """Règle 2 : uniquement des tables métier, ou des CTE définies dans la requête.

    La portée des CTE est résolue par l'analyse de portée de sqlglot : une CTE nommée
    `pg_user` dans une sous-requête ne doit pas autoriser `pg_user` ailleurs dans la requête,
    où PostgreSQL lirait la vraie vue système.
    """
    try:
        reelles = {
            id(source)
            for portee in traverse_scope(arbre)
            for source in portee.sources.values()
            if isinstance(source, exp.Table)
        }
    except SqlglotError:
        raise GuardrailError(
            "table", "structure de requête non analysable ; simplifier la requête."
        ) from None
    for table in arbre.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            continue  # fonction dans le FROM (generate_series…) : vérifiée par la règle 3
        nom = _normalize(table.this if isinstance(table.this, exp.Identifier) else None)
        schema_table = _normalize(table.args.get("db"))
        if schema_table and schema_table != schema:
            raise GuardrailError("table", f"schéma non autorisé : {schema_table}.")
        if id(table) in reelles and nom not in allowed:
            raise GuardrailError(
                "table",
                f"table non autorisée : {nom}. Tables disponibles : {', '.join(sorted(allowed))}.",
            )


def _check_functions(arbre: exp.Query) -> None:
    """Règle 3 : aucune fonction d'administration ni d'exécution de SQL dynamique.

    sqlglot représente les fonctions qu'il ne connaît pas par des nœuds `Anonymous` ; les
    fonctions d'administration de PostgreSQL en font toutes partie.
    """
    for fonction in arbre.find_all(exp.Anonymous):
        nom = fonction.name.lower()
        if nom.startswith(_PREFIXES_INTERDITS) or nom in _FONCTIONS_INTERDITES:
            raise GuardrailError("fonction", f"la fonction {nom} est interdite.")


def _impose_limit(arbre: exp.Query, row_cap: int) -> exp.Query:
    """Règle 4 : `LIMIT row_cap + 1`, sauf limite littérale déjà plus petite."""
    plafond = row_cap + 1
    limite = arbre.args.get("limit")
    if isinstance(limite, exp.Limit):
        valeur = limite.expression
    elif isinstance(limite, exp.Fetch):  # FETCH FIRST n ROWS ONLY
        valeur = limite.args.get("count")
    else:
        valeur = None
    if isinstance(valeur, exp.Literal) and valeur.is_int and int(valeur.this) <= plafond:
        return arbre
    arbre.set("limit", None)
    return arbre.limit(plafond, copy=False)
