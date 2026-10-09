"""Tests de la boucle agent.

Le client Anthropic est remplacé par un faux client qui rejoue des réponses écrites à l'avance et
enregistre chaque requête reçue. Le serveur MCP, lui, est le vrai : en mémoire (`Client(mcp)`),
sur la vraie base. On teste ainsi **notre code** (historique envoyé, exécution des outils,
statut, résultat), pas la qualité du SQL écrit par le modèle, mesurée au jalon 3.

Les tests marqués `api` appellent la vraie API (option --api).
"""

import logging
from typing import Any, get_args

import pytest
from anthropic.types import Message, StopReason
from mcp import Client
from mcp.types import Implementation
from pydantic import ValidationError

from text_to_sql_mcp.agent import (
    AgentResult,
    ToolCall,
    ask,
    available_prompts,
    load_prompt,
    run_agent,
    select_answer_query,
)
from text_to_sql_mcp.agent.boucle import (
    LAST_TURN_NOTICE,
    STATUS_BY_STOP_REASON,
    _single_exception,
    _with_last_turn_notice,
)
from text_to_sql_mcp.config import get_agent_settings
from text_to_sql_mcp.server import mcp

pytestmark = pytest.mark.anyio

CLIENT = Implementation(name="agent-test", version="0")


# --- Faux client Anthropic --------------------------------------------------------------------


def message(*blocs: dict[str, Any], stop_reason: str) -> Message:
    """Une réponse de l'API, construite à la main (même type que la vraie)."""
    reponse = Message.model_validate(
        {
            "id": "msg_faux",
            "type": "message",
            "role": "assistant",
            "model": "faux",
            "content": list(blocs),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
    )
    # model_copy ne revalide pas : permet un stop_reason inconnu du SDK.
    return reponse.model_copy(update={"stop_reason": stop_reason})


def outil(nom: str, id_: str, **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": nom, "input": arguments}


def texte(contenu: str) -> dict[str, Any]:
    return {"type": "text", "text": contenu}


class _FauxMessages:
    def __init__(self, reponses: list[Message]) -> None:
        self._reponses = list(reponses)
        self.requetes: list[dict[str, Any]] = []

    async def create(self, **requete: Any) -> Message:
        # Copie de la liste : la boucle la modifie ensuite, on garde l'état au moment de l'appel.
        self.requetes.append({**requete, "messages": list(requete["messages"])})
        return self._reponses.pop(0)


class FauxAnthropic:
    """Rejoue des réponses écrites à l'avance et enregistre les requêtes reçues."""

    def __init__(self, reponses: list[Message]) -> None:
        self.messages = _FauxMessages(reponses)


async def jouer(
    scenario: list[Message], max_turns: int = 10
) -> tuple[AgentResult, list[dict[str, Any]]]:
    """Fait tourner la boucle sur un scénario, avec le serveur MCP en mémoire."""
    faux = FauxAnthropic(scenario)
    async with Client(mcp, client_info=CLIENT) as client_mcp:
        resultat = await run_agent(
            "question de test",
            client_mcp=client_mcp,
            anthropic=faux,
            model="faux",
            prompt_name="v0",
            prompt=load_prompt("v0"),
            max_turns=max_turns,
            max_tokens=1000,
        )
    return resultat, faux.messages.requetes


# --- Sans base --------------------------------------------------------------------------------


def test_statuts_couvrent_tous_les_stop_reason_du_sdk() -> None:
    """Si le SDK ajoute un stop_reason, ce test le signale (sinon : arret_inattendu)."""
    assert set(STATUS_BY_STOP_REASON) == set(get_args(StopReason)) - {"tool_use"}


def test_prompt_v0_disponible() -> None:
    assert "v0" in available_prompts()
    assert "list_tables" in load_prompt("v0")


@pytest.mark.parametrize("nom", ["v9", "V0", "../server", "", "v0.md"])
def test_prompt_inconnu_ou_invalide(nom: str) -> None:
    with pytest.raises(ValueError, match=r"Prompts disponibles : .*v0"):
        load_prompt(nom)


def test_avertissement_ajoute_apres_les_tool_result() -> None:
    message_outils = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}
    resultat = _with_last_turn_notice(message_outils)
    assert [b["type"] for b in resultat["content"]] == ["tool_result", "text"]
    assert resultat["content"][-1]["text"] == LAST_TURN_NOTICE
    assert len(message_outils["content"]) == 1  # l'original n'est pas modifié


def test_avertissement_sur_la_question_initiale() -> None:
    resultat = _with_last_turn_notice({"role": "user", "content": "Combien de pistes ?"})
    assert resultat["content"] == [
        {"type": "text", "text": "Combien de pistes ?"},
        {"type": "text", "text": LAST_TURN_NOTICE},
    ]


def test_exception_unique_extraite_d_un_groupe_imbrique() -> None:
    erreur = ConnectionError("x")
    groupe = ExceptionGroup("externe", [ExceptionGroup("interne", [erreur])])
    assert _single_exception(groupe) is erreur
    assert _single_exception(ExceptionGroup("deux", [erreur, ValueError()])) is None


def appel_requete(sql: str, *, erreur: bool = False) -> ToolCall:
    return ToolCall(
        name="run_query",
        arguments={"sql": sql},
        is_error=erreur,
        duration_ms=1.0,
        structured=None if erreur else {"executed_sql": f"{sql} LIMIT 101", "rows": []},
    )


PRINCIPALE = appel_requete("SELECT c.first_name FROM customer c WHERE c.country = 'France'")
VERIFICATION = appel_requete("SELECT name FROM genre")


@pytest.mark.parametrize(
    ("reponse", "attendu"),
    [
        # SQL réécrit (alias, casse, espaces, LIMIT des garde-fous) : reconnu.
        (
            "Voici.\n```sql\nselect c.first_name\nfrom customer as c\n"
            "where c.country = 'France' limit 101\n```",
            (PRINCIPALE, "presented"),
        ),
        # Bloc sans langage précisé : reconnu aussi.
        ("```\nSELECT name FROM genre\n```", (VERIFICATION, "presented")),
        # Aucun bloc, ou un bloc qui n'a jamais été exécuté : dernière requête réussie.
        ("Pas de SQL affiché.", (VERIFICATION, "last_successful")),
        ("```sql\nSELECT 42\n```", (VERIFICATION, "last_successful")),
        ("```sql\nSQL illisible (((\n```", (VERIFICATION, "last_successful")),
    ],
)
def test_select_answer_query(reponse: str, attendu: tuple) -> None:
    assert select_answer_query([PRINCIPALE, VERIFICATION], reponse) == attendu


def test_select_answer_query_ignore_les_echecs() -> None:
    echec = appel_requete("SELECT name FROM genre", erreur=True)
    assert select_answer_query([echec], "```sql\nSELECT name FROM genre\n```") == (None, None)
    assert select_answer_query([], "rien") == (None, None)


def test_reglages_agent_cle_obligatoire_et_masquee(monkeypatch: pytest.MonkeyPatch) -> None:
    from text_to_sql_mcp.config import AgentSettings

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="anthropic_api_key"):
        AgentSettings(_env_file=None)
    reglages = AgentSettings(_env_file=None, anthropic_api_key="sk-ant-secret")
    assert "sk-ant-secret" not in repr(reglages)
    assert (reglages.agent_model, reglages.agent_prompt) == ("claude-sonnet-5-5", "v0")
    assert (reglages.agent_max_turns, reglages.agent_max_tokens) == (10, 8000)


# --- Avec la base : boucle complète sur le serveur en mémoire -----------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible")
class TestBoucle:
    """La boucle, pilotée par le faux client, contre le vrai serveur MCP."""

    async def test_scenario_nominal(self) -> None:
        resultat, _ = await jouer(
            [
                message(outil("list_tables", "t1"), stop_reason="tool_use"),
                message(
                    outil("run_query", "t2", sql="SELECT name FROM genre ORDER BY genre_id"),
                    stop_reason="tool_use",
                ),
                message(texte("Rock, Jazz…"), stop_reason="end_turn"),
            ]
        )
        assert (resultat.status, resultat.stop_reason, resultat.turns) == ("ok", "end_turn", 3)
        assert resultat.turn_limit_reached is False
        assert resultat.answer == "Rock, Jazz…"
        assert resultat.executed_sql == "SELECT name FROM genre ORDER BY genre_id LIMIT 101"
        assert resultat.columns == ["name"]
        assert resultat.rows[:2] == [["Rock"], ["Jazz"]]
        assert (resultat.truncated, resultat.tool_errors) == (False, 0)
        assert [c.name for c in resultat.tool_calls] == ["list_tables", "run_query"]
        assert (resultat.input_tokens, resultat.output_tokens) == (300, 30)
        assert resultat.client_name == "agent-test/0"

    async def test_requete_envoyee_a_l_api(self) -> None:
        _, requetes = await jouer(
            [
                message(outil("list_tables", "t1"), stop_reason="tool_use"),
                message(texte("fin"), stop_reason="end_turn"),
            ]
        )
        premiere, seconde = requetes
        assert {o["name"] for o in premiere["tools"]} == {"list_tables", "get_schema", "run_query"}
        assert premiere["system"].startswith(load_prompt("v0"))
        assert "snake_case" in premiere["system"]  # instructions du serveur ajoutées
        assert "tool_choice" not in premiere  # outils libres hors dernier tour
        # 2e tour : question, réponse de Claude telle quelle, puis le tool_result relié à t1.
        assert [m["role"] for m in seconde["messages"]] == ["user", "assistant", "user"]
        (tool_result,) = seconde["messages"][-1]["content"]
        assert (tool_result["type"], tool_result["tool_use_id"]) == ("tool_result", "t1")
        assert tool_result["is_error"] is False
        assert "track" in tool_result["content"]

    async def test_plusieurs_outils_dans_un_meme_tour(self) -> None:
        resultat, requetes = await jouer(
            [
                message(
                    outil("get_schema", "a", table="album"),
                    outil("get_schema", "b", table="artist"),
                    stop_reason="tool_use",
                ),
                message(texte("fin"), stop_reason="end_turn"),
            ]
        )
        ids = [b["tool_use_id"] for b in requetes[1]["messages"][-1]["content"]]
        assert ids == ["a", "b"]
        assert [c.arguments["table"] for c in resultat.tool_calls] == ["album", "artist"]

    async def test_erreur_sql_puis_correction(self) -> None:
        resultat, requetes = await jouer(
            [
                message(
                    outil("run_query", "t1", sql="SELECT nom FROM genre"), stop_reason="tool_use"
                ),
                message(
                    outil("run_query", "t2", sql="SELECT name FROM genre LIMIT 2"),
                    stop_reason="tool_use",
                ),
                message(texte("Rock et Jazz."), stop_reason="end_turn"),
            ]
        )
        assert resultat.status == "ok"
        assert resultat.tool_errors == 1
        assert "42703" in resultat.tool_calls[0].error
        assert requetes[1]["messages"][-1]["content"][0]["is_error"] is True
        # Le SQL retenu est celui de la dernière requête réussie.
        assert resultat.executed_sql == "SELECT name FROM genre LIMIT 2"
        assert resultat.rows == [["Rock"], ["Jazz"]]

    async def test_requete_presentee_retenue_malgre_une_verification(self) -> None:
        """Règle B : une requête de vérification après la réponse ne la remplace pas."""
        resultat, _ = await jouer(
            [
                message(
                    outil("run_query", "t1", sql="SELECT name FROM genre WHERE genre_id = 2"),
                    stop_reason="tool_use",
                ),
                message(
                    outil("run_query", "t2", sql="SELECT COUNT(*) FROM genre"),
                    stop_reason="tool_use",
                ),
                message(
                    texte(
                        "C'est Jazz.\n"
                        "```sql\nSELECT name FROM genre WHERE genre_id = 2 LIMIT 101\n```"
                    ),
                    stop_reason="end_turn",
                ),
            ]
        )
        assert resultat.rows == [["Jazz"]]
        assert resultat.selected_query == "presented"

    async def test_refus_des_garde_fous_renvoye_au_modele(self) -> None:
        resultat, requetes = await jouer(
            [
                message(outil("run_query", "t1", sql="DELETE FROM genre"), stop_reason="tool_use"),
                message(texte("Je ne peux pas supprimer."), stop_reason="end_turn"),
            ]
        )
        assert resultat.tool_errors == 1
        assert resultat.executed_sql is None  # aucune requête réussie
        assert (
            "Requête refusée (lecture_seule)"
            in requetes[1]["messages"][-1]["content"][0]["content"]
        )

    async def test_dernier_tour_avertissement_et_outils_desactives(self) -> None:
        resultat, requetes = await jouer(
            [
                message(outil("list_tables", "t1"), stop_reason="tool_use"),
                message(
                    texte("Je ne peux pas répondre en aussi peu de tours."), stop_reason="end_turn"
                ),
            ],
            max_turns=2,
        )
        assert "tool_choice" not in requetes[0]
        dernier = requetes[1]
        assert dernier["tool_choice"] == {"type": "none"}
        blocs = dernier["messages"][-1]["content"]
        assert [b["type"] for b in blocs] == ["tool_result", "text"]
        assert blocs[-1]["text"] == LAST_TURN_NOTICE
        assert (resultat.status, resultat.turn_limit_reached, resultat.turns) == ("ok", True, 2)

    async def test_un_seul_tour_autorise(self) -> None:
        resultat, requetes = await jouer(
            [message(texte("Pas assez de tours."), stop_reason="end_turn")], max_turns=1
        )
        (requete,) = requetes
        assert requete["tool_choice"] == {"type": "none"}
        assert requete["messages"][0]["content"][-1]["text"] == LAST_TURN_NOTICE
        assert (resultat.status, resultat.turn_limit_reached) == ("ok", True)

    async def test_outils_demandes_au_dernier_tour_non_executes(self) -> None:
        """Filet : si le modèle demande quand même un outil au dernier tour, rien ne s'exécute."""
        resultat, _ = await jouer(
            [
                message(outil("list_tables", "t1"), stop_reason="tool_use"),
                message(outil("list_tables", "t2"), stop_reason="tool_use"),
            ],
            max_turns=2,
        )
        assert (resultat.status, resultat.turns) == ("max_tours", 2)
        assert [c.arguments for c in resultat.tool_calls] == [{}]  # seul t1 a été exécuté

    @pytest.mark.parametrize(
        ("stop_reason", "statut"),
        [
            ("max_tokens", "max_tokens"),
            ("stop_sequence", "stop_sequence"),
            ("pause_turn", "pause_turn"),
            ("refusal", "refusal"),
            ("model_context_window_exceeded", "model_context_window_exceeded"),
            ("nouvelle_raison_inconnue", "arret_inattendu"),
        ],
    )
    async def test_statut_selon_stop_reason(self, stop_reason: str, statut: str) -> None:
        resultat, _ = await jouer([message(texte("…"), stop_reason=stop_reason)])
        assert (resultat.status, resultat.stop_reason) == (statut, stop_reason)
        assert resultat.tool_calls == []

    async def test_une_ligne_de_log_par_tour_et_un_bilan(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="text_to_sql_mcp")
        await jouer(
            [
                message(outil("list_tables", "t1"), stop_reason="tool_use"),
                message(texte("fin"), stop_reason="end_turn"),
            ]
        )
        tours = [r for r in caplog.records if r.getMessage() == "tour_api"]
        assert [(r.tour, r.stop_reason, r.dernier_tour) for r in tours] == [
            (1, "tool_use", False),
            (2, "end_turn", False),
        ]
        (bilan,) = [r for r in caplog.records if r.getMessage() == "fin_agent"]
        assert (bilan.statut, bilan.tours, bilan.tokens_entree) == ("ok", 2, 200)
        # Même champ client que les lignes appel_outil du serveur : les deux flux se recoupent.
        (outil_log,) = [r for r in caplog.records if r.getMessage() == "appel_outil"]
        assert tours[0].client == bilan.client == outil_log.client == "agent-test/0"


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible", "reglages_agent_de_test")
async def test_ask_lance_le_serveur_en_sous_processus() -> None:
    """L'enveloppe de production : vrai serveur en stdio, faux client Anthropic."""
    faux = FauxAnthropic(
        [
            message(
                outil("run_query", "t1", sql="SELECT count(*) FROM track"), stop_reason="tool_use"
            ),
            message(texte("3503 pistes."), stop_reason="end_turn"),
        ]
    )
    resultat = await ask("Combien de pistes ?", model="faux-modele", anthropic=faux)
    assert resultat.status == "ok"
    assert resultat.rows == [[3503]]
    assert resultat.client_name.startswith("text-to-sql-agent:faux-modele:v0/")
    assert faux.messages.requetes[0]["max_tokens"] == get_agent_settings().agent_max_tokens


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible", "reglages_agent_de_test")
async def test_ask_rend_l_erreur_d_origine() -> None:
    """Une erreur de l'API sort de `ask` sous son propre type, pas dans un ExceptionGroup."""

    class ApiEnPanne:
        class messages:  # imite l'attribut `messages` du client Anthropic
            @staticmethod
            async def create(**requete: Any) -> Message:
                raise ConnectionError("API injoignable")

    with pytest.raises(ConnectionError, match="API injoignable"):
        await ask("Combien de pistes ?", anthropic=ApiEnPanne())


@pytest.mark.api
@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible")
async def test_vraie_api_de_bout_en_bout() -> None:
    """Fumée : une vraie question, avec le modèle économique. Coût : quelques milliers de tokens."""
    get_agent_settings.cache_clear()
    try:
        get_agent_settings()
    except ValidationError:
        pytest.skip("ANTHROPIC_API_KEY absent du .env")
    resultat = await ask("Combien y a-t-il de genres musicaux ?", model="claude-haiku-5-5")
    assert resultat.status == "ok"
    assert resultat.executed_sql is not None
    assert "25" in resultat.answer
