"""Ligne de commande de l'agent : une question, une réponse.

    text-to-sql-agent "Quels sont les 5 artistes qui ont publié le plus d'albums ?"

La réponse va sur stdout ; les logs JSON (agent et serveur MCP) vont sur stderr. Les deux se
séparent donc à la redirection : `text-to-sql-agent "…" 2> trace.jsonl`.

Codes de sortie : 0 si l'agent a répondu normalement (statut `ok`), 1 pour toute autre fin
(plafond de tours, réponse tronquée, refus…), 2 pour une erreur d'arguments, de configuration
ou d'infrastructure (API ou base injoignable).
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

import anthropic
from pydantic import ValidationError

from text_to_sql_mcp.agent import AgentResult, ask, available_prompts
from text_to_sql_mcp.config import get_agent_settings
from text_to_sql_mcp.logging_config import LOGGER_NAME, configure_logging

logger = logging.getLogger(f"{LOGGER_NAME}.cli")

SORTIE_OK, SORTIE_STATUT, SORTIE_ERREUR = 0, 1, 2


def _positive_int(valeur: str) -> int:
    """Type argparse : entier strictement positif."""
    try:
        nombre = int(valeur)
    except ValueError:
        raise argparse.ArgumentTypeError(f"entier attendu : {valeur!r}") from None
    if nombre < 1:
        raise argparse.ArgumentTypeError(f"entier strictement positif attendu : {nombre}")
    return nombre


def build_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments.

    Les options non précisées prennent leur valeur dans le .env (`AGENT_*`).

    Returns:
        L'analyseur configuré.
    """
    parser = argparse.ArgumentParser(
        prog="text-to-sql-agent",
        description="Répond à une question métier en interrogeant la base Chinook (lecture seule).",
        epilog="Réponse sur stdout, logs JSON sur stderr. Codes de sortie : 0 ok, 1 autre "
        "statut, 2 erreur.",
    )
    parser.add_argument("question", help="Question en langage naturel.")
    parser.add_argument("--model", help="Identifiant du modèle (défaut : AGENT_MODEL).")
    parser.add_argument(
        "--prompt",
        choices=available_prompts(),
        help="Prompt système (défaut : AGENT_PROMPT).",
    )
    parser.add_argument(
        "--max-turns",
        type=_positive_int,
        help="Nombre maximal d'appels à l'API (défaut : AGENT_MAX_TURNS).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Affiche le résultat complet en JSON (une ligne JSONL, format de l'évaluation).",
    )
    return parser


def render(resultat: AgentResult, as_json: bool) -> str:
    """Texte à afficher sur stdout.

    Args:
        resultat: Résultat de l'agent.
        as_json: `True` pour le résultat complet en JSON sur une ligne.

    Returns:
        La réponse de l'agent, ou le JSON.
    """
    if as_json:
        return resultat.model_dump_json()
    return resultat.answer


def main(argv: Sequence[str] | None = None) -> int:
    """Point d'entrée de la commande `text-to-sql-agent`.

    Args:
        argv: Arguments (par défaut, ceux de la ligne de commande).

    Returns:
        Le code de sortie.
    """
    args = build_parser().parse_args(argv)
    try:
        reglages = get_agent_settings()
    except ValidationError as erreur:
        manquants = ", ".join(str(e["loc"][0]).upper() for e in erreur.errors())
        print(f"Configuration invalide ou incomplète dans le .env : {manquants}.", file=sys.stderr)
        return SORTIE_ERREUR
    configure_logging(reglages.log_level)
    try:
        resultat = asyncio.run(
            ask(args.question, model=args.model, prompt_name=args.prompt, max_turns=args.max_turns)
        )
    except anthropic.APIError as erreur:
        print(f"Erreur de l'API Anthropic : {erreur}", file=sys.stderr)
        return SORTIE_ERREUR
    except Exception:
        logger.exception("echec_agent")
        print("Échec de l'agent (détail dans le log ci-dessus).", file=sys.stderr)
        return SORTIE_ERREUR
    print(render(resultat, args.json))
    if resultat.status != "ok":
        print(f"Arrêt de l'agent : statut {resultat.status}.", file=sys.stderr)
        return SORTIE_STATUT
    return SORTIE_OK


if __name__ == "__main__":
    sys.exit(main())
