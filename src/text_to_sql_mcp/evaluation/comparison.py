"""Notation d'une réponse de l'agent : execution accuracy et abstention.

Règles de l'execution accuracy (on compare des **résultats**, jamais le texte SQL) :

- même nombre de lignes que la référence ; exception pour les questions de classement
  (`GoldQuestion.ordered`) : des lignes **en trop à la fin** ne sont pas une faute (« quel
  artiste a le plus vendu ? » répondu par un top 3 dont le premier est le bon), on compare le
  début du résultat ;
- chaque colonne de la référence doit se retrouver dans le résultat de l'agent, quels que soient
  son nom et sa position ; les colonnes **en trop** ne sont pas une faute ;
- l'ordre des lignes ne compte que si la question l'implique (`GoldQuestion.ordered`) ;
- les valeurs sont normalisées avant comparaison : le texte `"10"`, l'entier `10` et le
  décimal `10.00` sont égaux ; les nombres sont arrondis à 2 décimales ; une date-heure à minuit
  (`2025-01-01T00:00:00`) vaut la date `2025-01-01`.

Le résultat noté est celui de la requête que l'agent présente dans sa réponse, choisie parmi
les requêtes réellement exécutées (`AgentResult.rows`, voir `agent.select_answer_query`).

Questions sans réponse : l'agent doit commencer sa réponse par `ABSTENTION_MARKER` (consigne du
prompt v1). Le marqueur sur une question répondable est un faux refus.
"""

import json
import re
from collections import Counter
from collections.abc import Hashable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from itertools import permutations
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from text_to_sql_mcp.agent import AgentResult
from text_to_sql_mcp.evaluation.gold import GoldQuestion

ABSTENTION_MARKER = "[SANS_REPONSE]"

Reason = Literal[
    "ok",
    "abstention_correcte",
    "reponse_inventee",
    "faux_refus",
    "aucune_requete",
    "tronque",
    "nb_lignes",
    "colonnes",
    "valeurs",
    "ordre",
    "erreur_agent",
]
"""Motif du verdict. `ok` et `abstention_correcte` sont les deux motifs de réussite."""

_NOMBRE = re.compile(r"-?\d+(\.\d+)?")
_MINUIT = re.compile(r"(\d{4}-\d{2}-\d{2})T00:00:00(\+00:00|Z)?")
_CENTIEME = Decimal("0.01")
_MAX_AFFECTATIONS = 50_000  # garde-fou contre l'explosion combinatoire (colonnes identiques)


class Verdict(BaseModel):
    """Note d'une réponse de l'agent sur une question."""

    correct: bool
    reason: Reason
    reference_index: int | None = None
    """Indice de la référence égalée, si la réponse est juste."""


def normalize_value(value: Any) -> Hashable:
    """Forme canonique d'une cellule, pour comparer des valeurs de types différents.

    Args:
        value: Cellule d'un résultat (valeur JSON renvoyée par le serveur MCP).

    Returns:
        Un `Decimal` à 2 décimales pour tout nombre (y compris écrit en texte), une date
        `AAAA-MM-JJ` pour une date-heure à minuit, le texte sans espaces de bord sinon.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        return _arrondi(Decimal(str(value)))
    if isinstance(value, str):
        texte = value.strip()
        if _NOMBRE.fullmatch(texte):
            return _arrondi(Decimal(texte))
        minuit = _MINUIT.fullmatch(texte)
        return minuit.group(1) if minuit else texte
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _arrondi(nombre: Decimal) -> Decimal:
    return nombre.quantize(_CENTIEME, rounding=ROUND_HALF_UP) + 0  # + 0 : -0.00 devient 0.00


def compare_results(
    expected: Sequence[Sequence[JsonValue]],
    obtained: Sequence[Sequence[JsonValue]],
    *,
    ordered: bool,
) -> Reason:
    """Compare le résultat de l'agent à celui d'une référence.

    Chaque colonne de la référence est associée à une colonne distincte de l'agent dont les
    valeurs concordent ; on essaie toutes les associations possibles, puis on vérifie les
    lignes entières (une à une si `ordered`, comme des multiensembles sinon).

    Si `ordered`, les lignes de l'agent au-delà du nombre de lignes de la référence sont
    ignorées : seul le début du classement est comparé.

    Args:
        expected: Lignes de la référence.
        obtained: Lignes de l'agent.
        ordered: L'ordre des lignes doit-il être respecté ?

    Returns:
        `ok`, ou le motif de l'écart : `nb_lignes`, `colonnes` (il en manque), `ordre` (bonnes
        lignes, mauvais ordre) ou `valeurs`.
    """
    if ordered and expected and len(obtained) > len(expected):
        obtained = obtained[: len(expected)]
    if len(expected) != len(obtained):
        return "nb_lignes"
    if not expected:
        return "ok"
    attendu = [tuple(normalize_value(v) for v in ligne) for ligne in expected]
    obtenu = [tuple(normalize_value(v) for v in ligne) for ligne in obtained]
    if len(obtenu[0]) < len(attendu[0]):
        return "colonnes"
    if _concordent(attendu, obtenu, ordered=ordered):
        return "ok"
    if ordered and _concordent(attendu, obtenu, ordered=False):
        return "ordre"
    return "valeurs"


def _concordent(attendu: list[tuple], obtenu: list[tuple], *, ordered: bool) -> bool:
    """Existe-t-il une association colonnes de référence -> colonnes de l'agent qui concorde ?"""

    def cle(colonne: tuple) -> Any:
        return colonne if ordered else Counter(colonne)

    cols_attendues = [cle(tuple(ligne[j] for ligne in attendu)) for j in range(len(attendu[0]))]
    cols_obtenues = [cle(tuple(ligne[k] for ligne in obtenu)) for k in range(len(obtenu[0]))]
    candidats = [
        [k for k, col in enumerate(cols_obtenues) if col == attendue] for attendue in cols_attendues
    ]
    if any(not c for c in candidats):
        return False
    lignes_attendues = attendu if ordered else Counter(attendu)
    for essai, affectation in enumerate(_affectations(candidats)):
        if essai >= _MAX_AFFECTATIONS:
            return False
        projection = [tuple(ligne[k] for k in affectation) for ligne in obtenu]
        if (projection if ordered else Counter(projection)) == lignes_attendues:
            return True
    return False


def _affectations(candidats: list[list[int]]):
    """Énumère les choix d'une colonne candidate distincte par colonne de référence."""
    for choix in permutations(sorted({k for c in candidats for k in c}), len(candidats)):
        if all(k in c for k, c in zip(choix, candidats, strict=True)):
            yield choix


def is_abstention(answer: str) -> bool:
    """La réponse commence-t-elle par le marqueur d'abstention ?"""
    return answer.lstrip().startswith(ABSTENTION_MARKER)


def grade(
    question: GoldQuestion,
    result: AgentResult,
    references: Sequence[Sequence[Sequence[JsonValue]]],
) -> Verdict:
    """Note la réponse de l'agent à une question.

    Args:
        question: Question du gold set.
        result: Résultat de l'agent.
        references: Lignes de chaque requête de référence, dans l'ordre de `question.references`.

    Returns:
        Le verdict : juste ou faux, et pourquoi.
    """
    abstention = is_abstention(result.answer)
    if not question.answerable:
        if abstention:
            return Verdict(correct=True, reason="abstention_correcte")
        return Verdict(correct=False, reason="reponse_inventee")
    if abstention:
        return Verdict(correct=False, reason="faux_refus")
    if result.rows is None:
        return Verdict(correct=False, reason="aucune_requete")
    if result.truncated:
        return Verdict(correct=False, reason="tronque")
    motifs = [compare_results(ref, result.rows, ordered=question.ordered) for ref in references]
    if "ok" in motifs:
        return Verdict(correct=True, reason="ok", reference_index=motifs.index("ok"))
    return Verdict(correct=False, reason=motifs[0])
