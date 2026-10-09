"""Boucle tool use de l'agent text-to-SQL, écrite à la main.

`run_agent` est la boucle pure : elle reçoit un client MCP déjà ouvert et un client Anthropic,
sans savoir s'ils sont réels (production) ou simulés (tests). `ask` est l'enveloppe de
production : elle lit la configuration, lance le serveur MCP en sous-processus stdio et appelle
`run_agent`.

Le résultat (`AgentResult`) est construit à partir de ce que **notre code observe** (appels
d'outils, résultats structurés du serveur, décompte de tokens de l'API), jamais à partir de ce
que le modèle affirme dans sa réponse.
"""

import logging
import re
import sys
import time
from collections.abc import Sequence
from functools import cache
from importlib.resources import files
from typing import Any, Literal

from anthropic import AsyncAnthropic
from mcp import Client, StdioServerParameters
from mcp.types import Implementation
from pydantic import BaseModel, JsonValue

from text_to_sql_mcp import __version__
from text_to_sql_mcp.config import get_agent_settings
from text_to_sql_mcp.logging_config import LOGGER_NAME

logger = logging.getLogger(f"{LOGGER_NAME}.agent")

Status = Literal[
    "ok",
    "max_tours",
    "max_tokens",
    "stop_sequence",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
    "arret_inattendu",
]
"""Comment la boucle s'est terminée.

- `ok` : réponse finale normale (`stop_reason` `end_turn`) ;
- un `stop_reason` de l'API, repris tel quel, pour toute autre fin connue ;
- `max_tours` : le modèle demandait encore des outils au dernier tour autorisé (ne devrait pas
  arriver : les outils y sont désactivés) ;
- `arret_inattendu` : `stop_reason` absent ou inconnu de cette version du code.
"""

# stop_reason de l'API -> statut. "tool_use" n'y figure pas : c'est le cas où la boucle continue.
# Un test vérifie que cette table couvre toutes les valeurs connues du SDK Anthropic.
STATUS_BY_STOP_REASON: dict[str, Status] = {
    "end_turn": "ok",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    # Propre aux outils exécutés côté serveur Anthropic (recherche web…), que l'agent n'utilise
    # pas : la doc conseille alors de renvoyer la réponse telle quelle pour continuer.
    "pause_turn": "pause_turn",
    "refusal": "refusal",
    "model_context_window_exceeded": "model_context_window_exceeded",
}

LAST_TURN_NOTICE = (
    "Dernier tour : tu n'as plus d'appel d'outil disponible. Réponds maintenant à la question "
    "avec les résultats déjà obtenus. Si ce n'est pas possible, réponds que tu ne peux pas "
    "répondre à cette question en aussi peu de tours."
)
"""Ajouté au dernier message `user` avant le dernier appel autorisé à l'API."""

_NOM_PROMPT = re.compile(r"[a-z0-9_]+")


class ToolCall(BaseModel):
    """Un appel d'outil observé par la boucle."""

    name: str
    arguments: dict[str, Any]
    is_error: bool
    duration_ms: float
    structured: dict[str, Any] | None = None
    error: str | None = None


class AgentResult(BaseModel):
    """Ce que l'agent a fait pour une question, tel qu'observé par notre code.

    `executed_sql`, `columns`, `rows` et `truncated` viennent de la dernière requête
    `run_query` **réussie** : c'est son résultat que l'évaluation comparera à la référence.
    Ils valent `None` si aucune requête n'a abouti (question sans réponse, par exemple).
    """

    question: str
    model: str
    prompt: str
    client_name: str
    status: Status
    stop_reason: str | None
    turn_limit_reached: bool
    answer: str
    executed_sql: str | None
    columns: list[str] | None
    rows: list[list[JsonValue]] | None
    truncated: bool | None
    tool_calls: list[ToolCall]
    tool_errors: int
    turns: int
    input_tokens: int
    output_tokens: int
    duration_s: float


# --- Prompts --------------------------------------------------------------------------------


def available_prompts() -> list[str]:
    """Liste les prompts système disponibles (fichiers `agent/prompts/*.md`).

    Returns:
        Les noms des prompts, sans extension, triés.
    """
    dossier = files("text_to_sql_mcp.agent").joinpath("prompts")
    return sorted(f.name.removesuffix(".md") for f in dossier.iterdir() if f.name.endswith(".md"))


@cache
def load_prompt(name: str) -> str:
    """Charge un prompt système versionné du package.

    Args:
        name: Nom du prompt (ex. `v0`) : minuscules, chiffres et `_` uniquement.

    Returns:
        Le texte du prompt.

    Raises:
        ValueError: Si le nom est invalide ou si le prompt n'existe pas.
    """
    disponibles = available_prompts()
    if not _NOM_PROMPT.fullmatch(name) or name not in disponibles:
        raise ValueError(
            f"Prompt inconnu : {name!r}. Prompts disponibles : {', '.join(disponibles)}."
        )
    fichier = files("text_to_sql_mcp.agent").joinpath("prompts", f"{name}.md")
    return fichier.read_text(encoding="utf-8")


# --- Boucle ---------------------------------------------------------------------------------


def _client_name(client_mcp: Client) -> str:
    """Nom du client tel que le serveur le logue (`nom/version`), pour relier les deux flux."""
    infos = client_mcp.client_info
    return f"{infos.name}/{infos.version}" if infos else "inconnu"


def _with_last_turn_notice(message: dict[str, Any]) -> dict[str, Any]:
    """Renvoie le message `user` complété par l'avertissement de dernier tour.

    Le texte est placé **après** les blocs `tool_result` : l'API exige qu'ils viennent en
    premier dans le message.
    """
    contenu = message["content"]
    blocs = [{"type": "text", "text": contenu}] if isinstance(contenu, str) else list(contenu)
    return {**message, "content": [*blocs, {"type": "text", "text": LAST_TURN_NOTICE}]}


async def _execute_tool(client_mcp: Client, bloc: Any) -> tuple[ToolCall, dict[str, Any]]:
    """Exécute un bloc `tool_use` sur le serveur MCP.

    Returns:
        L'appel observé, et le bloc `tool_result` à renvoyer au modèle.
    """
    debut = time.perf_counter()
    resultat = await client_mcp.call_tool(bloc.name, bloc.input)
    texte = "\n".join(c.text for c in resultat.content if c.type == "text")
    appel = ToolCall(
        name=bloc.name,
        arguments=bloc.input,
        is_error=resultat.is_error,
        duration_ms=round((time.perf_counter() - debut) * 1000, 1),
        structured=resultat.structured_content,
        error=texte if resultat.is_error else None,
    )
    tool_result = {
        "type": "tool_result",
        "tool_use_id": bloc.id,  # relie le résultat à la demande du modèle
        "content": texte,
        "is_error": resultat.is_error,  # le modèle sait qu'il doit corriger sa requête
    }
    return appel, tool_result


async def run_agent(
    question: str,
    *,
    client_mcp: Client,
    anthropic: Any,
    model: str,
    prompt_name: str,
    prompt: str,
    max_turns: int,
    max_tokens: int,
) -> AgentResult:
    """Répond à une question avec la boucle tool use, sur des clients déjà ouverts.

    À chaque tour : appel à l'API avec tout l'historique, ajout de la réponse telle quelle
    (blocs `thinking` compris), puis exécution des outils demandés et renvoi de leurs résultats.
    La boucle s'arrête dès que le modèle ne demande plus d'outil.

    Au dernier tour autorisé, l'avertissement `LAST_TURN_NOTICE` est ajouté au dernier message
    et les outils sont désactivés (`tool_choice` `none`) : le modèle doit répondre avec ce
    qu'il a obtenu, ou dire qu'il ne peut pas répondre en aussi peu de tours.

    Les erreurs d'outils (refus des garde-fous, erreurs SQL) sont renvoyées au modèle avec
    `is_error` : il peut corriger sa requête, et `tool_errors` en garde la trace.

    Args:
        question: Question en langage naturel.
        client_mcp: Client MCP connecté au serveur text-to-SQL.
        anthropic: Client Anthropic asynchrone (ou tout objet exposant `messages.create`).
        model: Identifiant du modèle.
        prompt_name: Nom du prompt système, pour la traçabilité.
        prompt: Texte du prompt système ; les instructions du serveur y sont ajoutées.
        max_turns: Nombre maximal d'appels à l'API.
        max_tokens: Plafond de tokens de sortie par appel.

    Returns:
        Le résultat observé, avec son statut de fin.

    Raises:
        anthropic.APIError: Si l'API est injoignable ou renvoie une erreur HTTP.
    """
    debut = time.perf_counter()
    client_name = _client_name(client_mcp)
    systeme = f"{prompt}\nInformations sur la base :\n{client_mcp.instructions or ''}"
    outils = [
        {"name": o.name, "description": o.description, "input_schema": o.input_schema}
        for o in (await client_mcp.list_tools()).tools
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    journal: list[ToolCall] = []
    usages: list[Any] = []
    reponse = None
    statut: Status = "arret_inattendu"

    for tour in range(1, max_turns + 1):
        dernier = tour == max_turns
        options: dict[str, Any] = {}
        if dernier:
            messages[-1] = _with_last_turn_notice(messages[-1])
            options["tool_choice"] = {"type": "none"}
        debut_tour = time.perf_counter()
        reponse = await anthropic.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=systeme,
            tools=outils,
            messages=messages,
            **options,
        )
        usages.append(reponse.usage)
        logger.info(
            "tour_api",
            extra={
                "client": client_name,
                "tour": tour,
                "dernier_tour": dernier,
                "stop_reason": reponse.stop_reason,
                "tokens_entree": reponse.usage.input_tokens,
                "tokens_sortie": reponse.usage.output_tokens,
                "duree_ms": round((time.perf_counter() - debut_tour) * 1000, 1),
            },
        )
        messages.append({"role": "assistant", "content": reponse.content})
        if reponse.stop_reason != "tool_use":
            statut = STATUS_BY_STOP_REASON.get(reponse.stop_reason or "", "arret_inattendu")
            break
        if dernier:
            statut = "max_tours"  # filet : les outils demandés ne sont pas exécutés
            break
        resultats = []
        for bloc in reponse.content:
            if bloc.type == "tool_use":
                appel, tool_result = await _execute_tool(client_mcp, bloc)
                journal.append(appel)
                resultats.append(tool_result)
        messages.append({"role": "user", "content": resultats})

    resultat = _build_result(
        question=question,
        model=model,
        prompt_name=prompt_name,
        client_name=client_name,
        statut=statut,
        reponse=reponse,
        turn_limit_reached=len(usages) == max_turns,
        journal=journal,
        usages=usages,
        duree_s=round(time.perf_counter() - debut, 2),
    )
    logger.info(
        "fin_agent",
        extra={
            "client": client_name,
            "statut": resultat.status,
            "plafond_atteint": resultat.turn_limit_reached,
            "tours": resultat.turns,
            "erreurs_outils": resultat.tool_errors,
            "tokens_entree": resultat.input_tokens,
            "tokens_sortie": resultat.output_tokens,
            "duree_s": resultat.duration_s,
            "sql_execute": resultat.executed_sql,
        },
    )
    return resultat


def _build_result(
    *,
    question: str,
    model: str,
    prompt_name: str,
    client_name: str,
    statut: Status,
    reponse: Any,
    turn_limit_reached: bool,
    journal: Sequence[ToolCall],
    usages: Sequence[Any],
    duree_s: float,
) -> AgentResult:
    """Assemble l'`AgentResult` à partir des observations de la boucle."""
    reussies = [c for c in journal if c.name == "run_query" and not c.is_error]
    donnees = (reussies[-1].structured or {}) if reussies else {}
    texte = [b.text for b in reponse.content if b.type == "text"] if reponse else []
    return AgentResult(
        question=question,
        model=model,
        prompt=prompt_name,
        client_name=client_name,
        status=statut,
        stop_reason=reponse.stop_reason if reponse else None,
        turn_limit_reached=turn_limit_reached,
        answer="\n".join(texte),
        executed_sql=donnees.get("executed_sql"),
        columns=donnees.get("columns"),
        rows=donnees.get("rows"),
        truncated=donnees.get("truncated"),
        tool_calls=list(journal),
        tool_errors=sum(c.is_error for c in journal),
        turns=len(usages),
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        duration_s=duree_s,
    )


# --- Enveloppe de production ------------------------------------------------------------------


def server_parameters() -> StdioServerParameters:
    """Paramètres de lancement du serveur MCP en sous-processus.

    Même interpréteur que l'agent (pas de dépendance au PATH). Le sous-processus hérite du
    répertoire courant, où il lit le .env ; le SDK MCP ne lui transmet qu'une liste réduite
    de variables d'environnement, donc jamais la clé Anthropic.
    """
    return StdioServerParameters(command=sys.executable, args=["-m", "text_to_sql_mcp.server"])


async def ask(
    question: str,
    *,
    model: str | None = None,
    prompt_name: str | None = None,
    max_turns: int | None = None,
    max_tokens: int | None = None,
    anthropic: Any = None,
) -> AgentResult:
    """Répond à une question : lance le serveur MCP en stdio, puis la boucle.

    Les paramètres laissés à `None` prennent leur valeur dans `AgentSettings` (.env).
    Le client MCP s'annonce sous le nom `text-to-sql-agent:<modèle>:<prompt>` : c'est le champ
    `client` des logs du serveur, qui distingue les variantes testées.

    Args:
        question: Question en langage naturel.
        model: Identifiant du modèle.
        prompt_name: Nom du prompt système.
        max_turns: Nombre maximal d'appels à l'API.
        max_tokens: Plafond de tokens de sortie par appel.
        anthropic: Client Anthropic à utiliser ; par défaut, un client créé avec la clé du
            .env et fermé à la fin.

    Returns:
        Le résultat observé.

    Raises:
        ValueError: Si le prompt demandé n'existe pas.
        anthropic.APIError: Si l'API est injoignable ou renvoie une erreur HTTP.
    """
    reglages = get_agent_settings()
    model = model or reglages.agent_model
    prompt_name = prompt_name or reglages.agent_prompt
    prompt = load_prompt(prompt_name)
    infos = Implementation(name=f"text-to-sql-agent:{model}:{prompt_name}", version=__version__)
    try:
        async with Client(server_parameters(), client_info=infos) as client_mcp:
            arguments: dict[str, Any] = {
                "client_mcp": client_mcp,
                "model": model,
                "prompt_name": prompt_name,
                "prompt": prompt,
                "max_turns": max_turns or reglages.agent_max_turns,
                "max_tokens": max_tokens or reglages.agent_max_tokens,
            }
            if anthropic is not None:
                return await run_agent(question, anthropic=anthropic, **arguments)
            cle = reglages.anthropic_api_key.get_secret_value()
            async with AsyncAnthropic(api_key=cle) as client_anthropic:
                return await run_agent(question, anthropic=client_anthropic, **arguments)
    except BaseExceptionGroup as groupe:
        # Le client MCP exécute la session dans un groupe de tâches (anyio), qui enveloppe toute
        # exception levée dans le bloc. On rend à l'appelant l'exception d'origine quand elle est
        # unique (cas normal), pour qu'il puisse l'intercepter par son type.
        seule = _single_exception(groupe)
        if seule is None:
            raise
        raise seule from None


def _single_exception(groupe: BaseExceptionGroup) -> BaseException | None:
    """L'unique exception d'un groupe (même imbriqué), ou `None` s'il en contient plusieurs."""
    feuilles: list[BaseException] = []
    a_parcourir: list[BaseException] = [groupe]
    while a_parcourir:
        exception = a_parcourir.pop()
        if isinstance(exception, BaseExceptionGroup):
            a_parcourir.extend(exception.exceptions)
        else:
            feuilles.append(exception)
    return feuilles[0] if len(feuilles) == 1 else None
