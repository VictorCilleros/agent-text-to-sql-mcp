"""Chargement du gold set : questions, références SQL et règles de notation."""

import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

Difficulty = Literal["facile", "moyen", "difficile"]


class GoldQuestion(BaseModel):
    """Une question du gold set et la façon de noter la réponse de l'agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    question: str
    question_en: str
    difficulty: Difficulty
    tags: tuple[str, ...] = ()
    ordered: bool = False
    """L'ordre des lignes fait partie de la réponse (classement, « top N »…)."""
    answerable: bool = True
    """`False` : la base ne contient pas l'information, l'agent doit le dire."""
    references: tuple[str, ...] = ()
    """Requêtes de référence : la réponse est juste si elle égale le résultat de l'une d'elles."""

    @model_validator(mode="after")
    def _references_coherentes(self) -> Self:
        if self.answerable and not self.references:
            raise ValueError(f"{self.id} : une question répondable doit avoir une référence.")
        if not self.answerable and self.references:
            raise ValueError(f"{self.id} : une question sans réponse n'a pas de référence.")
        return self


def load_gold_set(path: Path | None = None) -> list[GoldQuestion]:
    """Charge le gold set (par défaut, celui livré avec le package).

    Args:
        path: Fichier TOML à charger à la place du gold set Chinook du package.

    Returns:
        Les questions, dans l'ordre du fichier.

    Raises:
        ValueError: Si deux questions ont le même identifiant.
    """
    if path is None:
        texte = files("text_to_sql_mcp.evaluation").joinpath("gold_chinook.toml").read_text("utf-8")
    else:
        texte = path.read_text(encoding="utf-8")
    questions = [GoldQuestion.model_validate(q) for q in tomllib.loads(texte)["question"]]
    ids = [q.id for q in questions]
    doublons = sorted({i for i in ids if ids.count(i) > 1})
    if doublons:
        raise ValueError(f"Identifiants en double dans le gold set : {', '.join(doublons)}.")
    return questions
