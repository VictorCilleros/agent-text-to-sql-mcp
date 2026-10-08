"""Tests du format des logs JSON (sans base de données)."""

import json
import logging

import pytest

from text_to_sql_mcp.logging_config import LOGGER_NAME, JsonFormatter, configure_logging


def ligne_json(**extra: object) -> dict:
    """Formate un enregistrement de test et renvoie le JSON décodé."""
    record = logging.LogRecord(LOGGER_NAME, logging.INFO, __file__, 1, "appel_outil", None, None)
    record.__dict__.update(extra)
    return json.loads(JsonFormatter().format(record))


def test_une_ligne_json_avec_les_champs_extra() -> None:
    ligne = ligne_json(outil="run_query", lignes=3)
    assert ligne["evenement"] == "appel_outil"
    assert (ligne["outil"], ligne["lignes"]) == ("run_query", 3)


def test_client_place_avant_l_evenement() -> None:
    cles = list(ligne_json(outil="list_tables", client="agent-test/modele-x"))
    assert cles[:4] == ["horodatage", "niveau", "client", "evenement"]


def test_logs_sur_stderr_jamais_sur_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    racine = logging.getLogger()
    etat = (racine.handlers[:], racine.level, logging.getLogger(LOGGER_NAME).level)
    configure_logging("INFO")
    try:
        logging.getLogger(f"{LOGGER_NAME}.test").info("essai", extra={"outil": "x"})
        logging.getLogger("mcp.test").warning("message du SDK")
        logging.getLogger("mcp.test").info("ignoré : la racine est au niveau WARNING")
        sortie = capsys.readouterr()
    finally:
        racine.handlers, racine.level = etat[0], etat[1]
        logging.getLogger(LOGGER_NAME).setLevel(etat[2])
    assert sortie.out == ""
    lignes = [json.loads(ligne) for ligne in sortie.err.splitlines()]
    assert [ligne["evenement"] for ligne in lignes] == ["essai", "message du SDK"]
