"""Tests de l'évaluation : gold set, notation, métriques, et harnais complet en simulation."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from text_to_sql_mcp.agent import AgentResult, load_prompt
from text_to_sql_mcp.evaluation import (
    ABSTENTION_MARKER,
    GoldQuestion,
    OracleAnthropic,
    compare_results,
    compute_references,
    cost_usd,
    evaluate,
    grade,
    load_gold_set,
    load_records,
    normalize_value,
    pass_at_k,
    pass_hat_k,
    wilson_interval,
)

# --- Gold set -----------------------------------------------------------------------------------


def test_gold_set_charge() -> None:
    questions = load_gold_set()
    assert len(questions) == 44
    assert sum(q.answerable for q in questions) == 39
    assert len({q.id for q in questions}) == 44
    assert {q.difficulty for q in questions} == {"facile", "moyen", "difficile"}


def test_question_repondable_sans_reference_refusee() -> None:
    with pytest.raises(ValueError, match="doit avoir une référence"):
        GoldQuestion(id="x", question="q", question_en="q", difficulty="facile")


def test_prompt_v1_contient_le_marqueur() -> None:
    assert ABSTENTION_MARKER in load_prompt("v1")


# --- Normalisation et comparaison ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("10", 10),
        (10, 10.0),
        ("10.00", Decimal("10")),
        (5.657, "5.66"),
        ("2025-01-01T00:00:00", "2025-01-01"),
        ("  Rock ", "Rock"),
        (-0.001, 0),
    ],
)
def test_valeurs_equivalentes(a: object, b: object) -> None:
    assert normalize_value(a) == normalize_value(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [("10", 11), ("Rock", "rock"), (None, 0), ("2025-01-01T10:30:00", "2025-01-01"), ("1e3", 1000)],
)
def test_valeurs_differentes(a: object, b: object) -> None:
    assert normalize_value(a) != normalize_value(b)


REFERENCE = [["USA", 13], ["Canada", 8], ["France", 5]]


@pytest.mark.parametrize(
    ("obtenu", "ordered", "motif"),
    [
        (REFERENCE, True, "ok"),
        ([["USA", "13"], ["Canada", "8"], ["France", "5"]], True, "ok"),  # types différents
        ([[13, "USA", 1], [8, "Canada", 2], [5, "France", 3]], True, "ok"),  # colonnes en plus
        ([["Canada", 8], ["USA", 13], ["France", 5]], False, "ok"),  # ordre libre
        ([["Canada", 8], ["USA", 13], ["France", 5]], True, "ordre"),
        ([["USA"], ["Canada"], ["France"]], True, "colonnes"),
        (REFERENCE[:2], True, "nb_lignes"),
        ([["USA", 13], ["Canada", 8], ["Brazil", 5]], True, "valeurs"),
        # Les colonnes concordent une à une, mais pas les lignes : valeurs mal associées.
        ([["USA", 8], ["Canada", 13], ["France", 5]], False, "valeurs"),
    ],
)
def test_compare_results(obtenu: list, ordered: bool, motif: str) -> None:
    assert compare_results(REFERENCE, obtenu, ordered=ordered) == motif


def test_resultats_vides() -> None:
    assert compare_results([], [], ordered=False) == "ok"
    assert compare_results([], [["x"]], ordered=False) == "nb_lignes"


def test_lignes_en_double_comptees() -> None:
    """Un multiensemble : ['Berlin', 'Berlin'] n'est pas ['Berlin', 'Stuttgart']."""
    assert (
        compare_results([["Berlin"], ["Berlin"]], [["Berlin"], ["Stuttgart"]], ordered=False)
        != "ok"
    )


# --- Notation d'une réponse ---------------------------------------------------------------------


def resultat(
    answer: str = "Réponse.", rows: list | None = None, truncated: bool = False
) -> AgentResult:
    return AgentResult(
        question="q",
        model="m",
        prompt="v1",
        client_name="c/0",
        status="ok",
        stop_reason="end_turn",
        turn_limit_reached=False,
        answer=answer,
        executed_sql=None if rows is None else "SELECT …",
        columns=None if rows is None else ["x"],
        rows=rows,
        truncated=None if rows is None else truncated,
        tool_calls=[],
        tool_errors=0,
        turns=2,
        input_tokens=1000,
        output_tokens=100,
        duration_s=1.0,
    )


REPONDABLE = GoldQuestion(
    id="q1", question="q", question_en="q", difficulty="facile", references=("SELECT 1", "SELECT 2")
)
SANS_REPONSE = GoldQuestion(
    id="q2", question="q", question_en="q", difficulty="facile", answerable=False
)


def test_reponse_juste_sur_une_reference_alternative() -> None:
    verdict = grade(REPONDABLE, resultat(rows=[[2]]), [[[1]], [[2]]])
    assert (verdict.correct, verdict.reason, verdict.reference_index) == (True, "ok", 1)


def test_motif_de_la_premiere_reference_en_cas_d_echec() -> None:
    verdict = grade(REPONDABLE, resultat(rows=[[3], [4]]), [[[1]], [[2]]])
    assert (verdict.correct, verdict.reason) == (False, "nb_lignes")


@pytest.mark.parametrize(
    ("question", "agent", "attendu"),
    [
        (
            SANS_REPONSE,
            resultat(f"{ABSTENTION_MARKER} Pas de salaires."),
            (True, "abstention_correcte"),
        ),
        (SANS_REPONSE, resultat("  " + ABSTENTION_MARKER + " …"), (True, "abstention_correcte")),
        (SANS_REPONSE, resultat("Le salaire moyen est 3 000 €."), (False, "reponse_inventee")),
        (SANS_REPONSE, resultat(f"Je réponds. {ABSTENTION_MARKER}"), (False, "reponse_inventee")),
        (REPONDABLE, resultat(f"{ABSTENTION_MARKER} …", rows=[[1]]), (False, "faux_refus")),
        (REPONDABLE, resultat(), (False, "aucune_requete")),
        (REPONDABLE, resultat(rows=[[1]], truncated=True), (False, "tronque")),
    ],
)
def test_grade(question: GoldQuestion, agent: AgentResult, attendu: tuple) -> None:
    verdict = grade(question, agent, [[[1]], [[2]]] if question.answerable else [])
    assert (verdict.correct, verdict.reason) == attendu


# --- Métriques ----------------------------------------------------------------------------------


def test_cout() -> None:
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(3.0)
    assert cost_usd("claude-haiku-5-5", 10_000, 1_000) == pytest.approx(0.0015)
    assert cost_usd("modele-inconnu", 1, 1) is None


def test_wilson() -> None:
    bas, haut = wilson_interval(24, 30)
    assert bas < 24 / 30 < haut
    assert (round(bas, 2), round(haut, 2)) == (0.63, 0.9)
    assert wilson_interval(30, 30)[1] == 1.0
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_pass_k() -> None:
    # 5 essais, 4 réussis.
    assert pass_hat_k(5, 4, 1) == pytest.approx(0.8)
    assert pass_at_k(5, 4, 1) == pytest.approx(0.8)
    assert pass_hat_k(5, 4, 5) == 0.0  # pas tous réussis
    assert pass_at_k(5, 4, 5) == 1.0
    assert pass_hat_k(5, 5, 5) == 1.0
    assert pass_hat_k(5, 4, 2) == pytest.approx(0.6)  # C(4,2)/C(5,2)
    with pytest.raises(ValueError):
        pass_hat_k(5, 6, 1)


# --- Avec la base : références et harnais complet en simulation ----------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible")
def test_toutes_les_references_s_executent() -> None:
    questions = load_gold_set()
    references = compute_references(questions)
    for question in questions:
        assert len(references[question.id]) == len(question.references)
        for lignes in references[question.id]:
            assert len(lignes) <= 100  # sinon l'agent, plafonné à 100 lignes, ne peut pas égaler


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible", "reglages_agent_de_test")
@pytest.mark.anyio
async def test_evaluation_simulee_de_bout_en_bout(tmp_path: Path) -> None:
    """Oracle parfait : vrai agent, vrai serveur MCP, vraie base ; tout doit être juste."""
    questions = [
        q for q in load_gold_set() if q.id in {"chinook_001", "chinook_030", "chinook_044"}
    ]
    sortie = tmp_path / "eval.jsonl"
    vus = []
    enregistrements = await evaluate(
        questions,
        model="claude-haiku-5-5",
        prompt_name="v1",
        references=compute_references(questions),
        repetitions=2,
        concurrency=3,
        output=sortie,
        anthropic=OracleAnthropic(questions, latency_s=0),
        on_record=vus.append,
    )
    assert len(enregistrements) == len(vus) == 6
    assert all(e.correct for e in enregistrements), [
        (e.question_id, e.reason) for e in enregistrements
    ]
    assert {e.reason for e in enregistrements} == {"ok", "abstention_correcte"}
    assert all(e.cost_usd and e.cost_usd > 0 for e in enregistrements)
    relus = load_records(sortie)
    assert sorted((e.question_id, e.repetition) for e in relus) == [
        (e.question_id, e.repetition) for e in enregistrements
    ]
    assert json.loads(sortie.read_text().splitlines()[0])["agent"]["client_name"].startswith(
        "text-to-sql-agent:claude-haiku-5-5:v1/"
    )


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible", "reglages_agent_de_test")
@pytest.mark.anyio
async def test_evaluation_simulee_erreurs_detectees() -> None:
    questions = [q for q in load_gold_set() if q.id in {"chinook_001", "chinook_044"}]
    enregistrements = await evaluate(
        questions,
        model="claude-haiku-5-5",
        prompt_name="v1",
        references=compute_references(questions),
        anthropic=OracleAnthropic(questions, error_rate=1.0, latency_s=0),
    )
    assert {(e.question_id, e.correct, e.reason) for e in enregistrements} == {
        ("chinook_001", False, "nb_lignes"),
        ("chinook_044", False, "reponse_inventee"),
    }


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible", "reglages_agent_de_test")
@pytest.mark.anyio
async def test_erreur_agent_n_interrompt_pas_l_evaluation() -> None:
    class ApiEnPanne:
        class messages:  # imite l'attribut `messages` du client Anthropic
            @staticmethod
            async def create(**requete: object) -> None:
                raise ConnectionError("API injoignable")

    questions = [q for q in load_gold_set() if q.id in {"chinook_001", "chinook_002"}]
    enregistrements = await evaluate(
        questions,
        model="claude-haiku-5-5",
        prompt_name="v1",
        references=compute_references(questions),
        anthropic=ApiEnPanne(),
    )
    assert [e.reason for e in enregistrements] == ["erreur_agent", "erreur_agent"]
    assert "API injoignable" in enregistrements[0].error
