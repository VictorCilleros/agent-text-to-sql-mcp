"""Exécution d'une évaluation : chaque question du gold set passe par l'agent, puis est notée.

Une évaluation = un modèle × un prompt × des questions × un nombre de répétitions. Chaque
réponse produit un `EvalRecord`, écrit au fil de l'eau dans un fichier JSONL : une évaluation
interrompue garde ce qui a déjà été mesuré, et un notebook peut relire le fichier sans relancer
l'agent.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, JsonValue

from text_to_sql_mcp import db
from text_to_sql_mcp.agent import AgentResult, ask
from text_to_sql_mcp.config import get_agent_settings
from text_to_sql_mcp.evaluation.comparison import Reason, grade
from text_to_sql_mcp.evaluation.gold import GoldQuestion
from text_to_sql_mcp.evaluation.metrics import cost_usd
from text_to_sql_mcp.logging_config import LOGGER_NAME

logger = logging.getLogger(f"{LOGGER_NAME}.evaluation")

REFERENCE_ROW_CAP = 1000
"""Plafond de lignes des requêtes de référence (l'agent, lui, est plafonné à ROW_CAP)."""

ReferenceRows = dict[str, list[list[list[JsonValue]]]]
"""Pour chaque question : les lignes de chacune de ses références."""


class EvalRecord(BaseModel):
    """Une réponse de l'agent à une question, et sa note."""

    run_id: str
    question_id: str
    question: str
    difficulty: str
    answerable: bool
    repetition: int
    model: str
    prompt: str
    correct: bool
    reason: Reason
    reference_index: int | None
    status: str | None
    error: str | None
    turns: int
    tool_errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    duration_s: float
    executed_sql: str | None
    answer: str
    agent: AgentResult | None
    """Résultat complet de l'agent (appels d'outils, lignes…), `None` en cas d'erreur."""


def compute_references(questions: Iterable[GoldQuestion]) -> ReferenceRows:
    """Exécute les requêtes de référence, avec le rôle lecture seule de l'agent.

    Args:
        questions: Questions du gold set.

    Returns:
        Les lignes de chaque référence, par identifiant de question.

    Raises:
        ValueError: Si une référence dépasse `REFERENCE_ROW_CAP` lignes (gold set à corriger).
    """
    resultats: ReferenceRows = {}
    for question in questions:
        lignes = []
        for sql in question.references:
            resultat = db.run_query(sql, row_cap=REFERENCE_ROW_CAP)
            if resultat.truncated:
                raise ValueError(
                    f"{question.id} : référence de plus de {REFERENCE_ROW_CAP} lignes."
                )
            lignes.append(resultat.rows)
        resultats[question.id] = lignes
    return resultats


def new_run_id(label: str) -> str:
    """Identifiant d'évaluation horodaté, utilisable comme nom de fichier."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{label}"


def _record(
    run_id: str,
    question: GoldQuestion,
    repetition: int,
    model: str,
    prompt_name: str,
    references: ReferenceRows,
    result: AgentResult | None,
    error: str | None,
    duration_s: float,
) -> EvalRecord:
    """Construit l'enregistrement d'une réponse (ou d'une erreur de l'agent)."""
    base: dict[str, Any] = {
        "run_id": run_id,
        "question_id": question.id,
        "question": question.question,
        "difficulty": question.difficulty,
        "answerable": question.answerable,
        "repetition": repetition,
        "model": model,
        "prompt": prompt_name,
    }
    if result is None:
        return EvalRecord(
            **base,
            correct=False,
            reason="erreur_agent",
            reference_index=None,
            status=None,
            error=error,
            turns=0,
            tool_errors=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
            duration_s=duration_s,
            executed_sql=None,
            answer="",
            agent=None,
        )
    verdict = grade(question, result, references.get(question.id, []))
    return EvalRecord(
        **base,
        correct=verdict.correct,
        reason=verdict.reason,
        reference_index=verdict.reference_index,
        status=result.status,
        error=None,
        turns=result.turns,
        tool_errors=result.tool_errors,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=cost_usd(model, result.input_tokens, result.output_tokens),
        duration_s=result.duration_s,
        executed_sql=result.executed_sql,
        answer=result.answer,
        agent=result,
    )


async def evaluate(
    questions: Sequence[GoldQuestion],
    *,
    model: str,
    prompt_name: str,
    references: ReferenceRows,
    repetitions: int = 1,
    concurrency: int = 4,
    output: Path | None = None,
    run_id: str | None = None,
    anthropic: Any = None,
    on_record: Callable[[EvalRecord], None] | None = None,
) -> list[EvalRecord]:
    """Fait passer chaque question `repetitions` fois par l'agent, et note chaque réponse.

    Les questions sont traitées en parallèle (`concurrency` agents à la fois, chacun avec son
    serveur MCP). Une erreur de l'agent (API injoignable…) est notée `erreur_agent` sans
    interrompre l'évaluation.

    Args:
        questions: Questions à poser.
        model: Identifiant du modèle.
        prompt_name: Prompt système de l'agent.
        references: Résultats des références (`compute_references`).
        repetitions: Nombre de passages par question.
        concurrency: Nombre d'agents en parallèle.
        output: Fichier JSONL où ajouter chaque enregistrement dès qu'il est prêt.
        run_id: Identifiant de l'évaluation ; par défaut horodaté.
        anthropic: Client Anthropic à utiliser ; par défaut, un client créé avec la clé du .env.
        on_record: Fonction appelée à chaque enregistrement (suivi de progression).

    Returns:
        Les enregistrements, triés par question puis par répétition.
    """
    run_id = run_id or new_run_id(f"{model}_{prompt_name}")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    client = anthropic
    if client is None:
        cle = get_agent_settings().anthropic_api_key.get_secret_value()
        client = AsyncAnthropic(api_key=cle, max_retries=4)

    async def une_question(question: GoldQuestion, repetition: int) -> EvalRecord:
        async with semaphore:
            debut = asyncio.get_running_loop().time()
            resultat, erreur = None, None
            try:
                resultat = await ask(
                    question.question, model=model, prompt_name=prompt_name, anthropic=client
                )
            except Exception as exception:
                erreur = f"{type(exception).__name__}: {exception}"
                logger.warning("erreur_agent", extra={"question": question.id, "erreur": erreur})
            duree = round(asyncio.get_running_loop().time() - debut, 2)
        enregistrement = _record(
            run_id, question, repetition, model, prompt_name, references, resultat, erreur, duree
        )
        if output is not None:
            with output.open("a", encoding="utf-8") as fichier:
                fichier.write(enregistrement.model_dump_json() + "\n")
        if on_record is not None:
            on_record(enregistrement)
        return enregistrement

    try:
        taches = [
            une_question(question, repetition)
            for question in questions
            for repetition in range(1, repetitions + 1)
        ]
        enregistrements = await asyncio.gather(*taches)
    finally:
        if anthropic is None:
            await client.close()
    return sorted(enregistrements, key=lambda e: (e.question_id, e.repetition))


def load_records(*paths: Path) -> list[EvalRecord]:
    """Relit un ou plusieurs fichiers JSONL d'évaluation.

    Args:
        paths: Fichiers écrits par `evaluate`.

    Returns:
        Tous les enregistrements, dans l'ordre des fichiers.
    """
    return [
        EvalRecord.model_validate_json(ligne)
        for path in paths
        for ligne in path.read_text(encoding="utf-8").splitlines()
        if ligne.strip()
    ]
