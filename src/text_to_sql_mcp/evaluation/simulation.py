"""Client Anthropic simulé, pour une répétition à blanc de l'évaluation (sans API, sans coût).

L'« oracle » joue le rôle du modèle : sur une question répondable, il exécute la première
requête de référence via `run_query`, puis répond ; sur une question sans réponse, il
s'abstient avec le marqueur. Tout le reste est réel : boucle de l'agent, serveur MCP en
sous-processus, garde-fous, base, notation. Avec `error_rate > 0`, il se trompe volontairement
sur une partie des questions, pour vérifier que les échecs sont bien détectés et affichés.

Il sert à vérifier le harnais (et le notebook) avant de dépenser des tokens ; ses scores ne
mesurent évidemment rien du modèle.
"""

import asyncio
import random
from typing import Any

from anthropic.types import Message

from text_to_sql_mcp.evaluation.comparison import ABSTENTION_MARKER
from text_to_sql_mcp.evaluation.gold import GoldQuestion

_REQUETE_FAUSSE = "SELECT 1 AS faux"


class _OracleMessages:
    def __init__(self, oracle: "OracleAnthropic") -> None:
        self._oracle = oracle

    async def create(self, *, messages: list[dict[str, Any]], **_: Any) -> Message:
        return await self._oracle.reply(messages)


class OracleAnthropic:
    """Faux client Anthropic qui répond à partir du gold set.

    Args:
        questions: Questions du gold set (texte exact posé à l'agent).
        error_rate: Probabilité de se tromper sur une question (0 : oracle parfait).
        seed: Graine du tirage des erreurs.
        latency_s: Délai simulé de chaque appel à l'API.
    """

    def __init__(
        self,
        questions: list[GoldQuestion],
        *,
        error_rate: float = 0.0,
        seed: int = 0,
        latency_s: float = 0.05,
    ) -> None:
        self._questions = {q.question: q for q in questions}
        self._error_rate = error_rate
        self._hasard = random.Random(seed)
        self._latence = latency_s
        self.messages = _OracleMessages(self)

    async def reply(self, messages: list[dict[str, Any]]) -> Message:
        """Réponse simulée au tour courant de la conversation."""
        await asyncio.sleep(self._latence)
        question = self._questions[_texte_question(messages[0])]
        tour = 1 + sum(m["role"] == "assistant" for m in messages)
        if not question.answerable:
            texte = "Je ne sais pas." if self._se_trompe() else f"{ABSTENTION_MARKER} Simulation."
            return _message([{"type": "text", "text": texte}], "end_turn", tour)
        if tour == 1:
            sql = _REQUETE_FAUSSE if self._se_trompe() else question.references[0]
            bloc = {
                "type": "tool_use",
                "id": "oracle_1",
                "name": "run_query",
                "input": {"sql": sql},
            }
            return _message([bloc], "tool_use", tour)
        return _message([{"type": "text", "text": "Réponse simulée."}], "end_turn", tour)

    def _se_trompe(self) -> bool:
        return self._hasard.random() < self._error_rate


def _texte_question(message: dict[str, Any]) -> str:
    """Texte de la question dans le premier message (chaîne, ou blocs au dernier tour)."""
    contenu = message["content"]
    return contenu if isinstance(contenu, str) else contenu[0]["text"]


def _message(blocs: list[dict[str, Any]], stop_reason: str, tour: int) -> Message:
    return Message.model_validate(
        {
            "id": f"msg_oracle_{tour}",
            "type": "message",
            "role": "assistant",
            "model": "oracle",
            "content": blocs,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1500 * tour, "output_tokens": 120},
        }
    )
