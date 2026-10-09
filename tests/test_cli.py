"""Tests de la ligne de commande (sans base ni API : `ask` est remplacé)."""

import json
from typing import Any

import pytest

from text_to_sql_mcp import cli
from text_to_sql_mcp.agent import AgentResult


def resultat(statut: str = "ok", **valeurs: Any) -> AgentResult:
    """Un AgentResult minimal."""
    return AgentResult(
        **{
            "question": "q",
            "model": "m",
            "prompt": "v0",
            "client_name": "c/0",
            "status": statut,
            "stop_reason": "end_turn",
            "turn_limit_reached": False,
            "answer": "Réponse de l'agent.",
            "executed_sql": None,
            "columns": None,
            "rows": None,
            "truncated": None,
            "tool_calls": [],
            "tool_errors": 0,
            "turns": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "duration_s": 0.1,
            **valeurs,
        }
    )


@pytest.fixture
def ask_simule(monkeypatch: pytest.MonkeyPatch, reglages_agent_de_test: Any) -> dict[str, Any]:
    """Remplace `ask` : enregistre ses arguments et renvoie un résultat choisi par le test."""
    appel: dict[str, Any] = {"resultat": resultat()}

    async def faux_ask(question: str, **options: Any) -> AgentResult:
        appel.update(question=question, **options)
        return appel["resultat"]

    monkeypatch.setattr(cli, "ask", faux_ask)
    monkeypatch.setattr(cli, "configure_logging", lambda niveau: None)
    return appel


def test_reponse_sur_stdout_code_0(ask_simule: dict, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["Combien de pistes ?"]) == 0
    sortie = capsys.readouterr()
    assert sortie.out == "Réponse de l'agent.\n"
    assert sortie.err == ""
    assert ask_simule["question"] == "Combien de pistes ?"
    assert (ask_simule["model"], ask_simule["prompt_name"], ask_simule["max_turns"]) == (
        None,
        None,
        None,
    )


def test_options_transmises(ask_simule: dict) -> None:
    cli.main(["q", "--model", "claude-haiku-5-5", "--prompt", "v0", "--max-turns", "3"])
    assert (ask_simule["model"], ask_simule["prompt_name"], ask_simule["max_turns"]) == (
        "claude-haiku-5-5",
        "v0",
        3,
    )


def test_json_sur_une_ligne(ask_simule: dict, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["q", "--json"])
    (ligne,) = capsys.readouterr().out.splitlines()
    assert json.loads(ligne)["status"] == "ok"


def test_autre_statut_code_1(ask_simule: dict, capsys: pytest.CaptureFixture[str]) -> None:
    ask_simule["resultat"] = resultat("max_tokens", stop_reason="max_tokens")
    assert cli.main(["q"]) == 1
    assert "statut max_tokens" in capsys.readouterr().err


def test_echec_d_infrastructure_code_2(
    monkeypatch: pytest.MonkeyPatch, ask_simule: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    async def ask_en_panne(question: str, **options: Any) -> AgentResult:
        raise ConnectionError("serveur MCP injoignable")

    monkeypatch.setattr(cli, "ask", ask_en_panne)
    assert cli.main(["q"]) == 2
    assert "Échec de l'agent" in capsys.readouterr().err


def test_cle_absente_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from text_to_sql_mcp.config import get_agent_settings

    monkeypatch.chdir(tmp_path)  # aucun .env ici
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_agent_settings.cache_clear()
    try:
        assert cli.main(["q"]) == 2
    finally:
        get_agent_settings.cache_clear()
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [["q", "--max-turns", "0"], ["q", "--prompt", "inconnu"], []])
def test_arguments_invalides_code_2(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as sortie:
        cli.main(arguments)
    assert sortie.value.code == 2
