"""Tests du serveur MCP, via le client en mémoire du SDK (sans sous-processus)."""

import logging

import pytest
from mcp import Client
from mcp.types import Implementation

from text_to_sql_mcp.config import get_settings
from text_to_sql_mcp.server import mcp

pytestmark = pytest.mark.anyio

AGENT = Implementation(name="agent-test", version="modele-x")


def appels_traces(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Enregistrements de log produits par les appels d'outils."""
    return [r for r in caplog.records if r.getMessage() == "appel_outil"]


async def test_outils_exposes_et_lecture_seule() -> None:
    async with Client(mcp) as client:
        outils = {outil.name: outil for outil in (await client.list_tools()).tools}
    assert set(outils) == {"list_tables", "get_schema", "run_query"}
    for outil in outils.values():
        assert outil.annotations is not None
        assert outil.annotations.read_only_hint is True


async def test_parametres_documentes_dans_le_schema() -> None:
    async with Client(mcp) as client:
        outils = {outil.name: outil for outil in (await client.list_tools()).tools}
    assert "list_tables" in outils["get_schema"].input_schema["properties"]["table"]["description"]
    assert "SELECT" in outils["run_query"].input_schema["properties"]["sql"]["description"]


async def test_contexte_absent_du_schema_vu_par_le_modele() -> None:
    async with Client(mcp) as client:
        for outil in (await client.list_tools()).tools:
            assert "ctx" not in outil.input_schema.get("properties", {})


async def test_instructions_du_serveur() -> None:
    async with Client(mcp) as client:
        assert "snake_case" in (client.instructions or "")


@pytest.mark.integration
@pytest.mark.usefixtures("base_disponible")
class TestAvecBase:
    """Appels d'outils de bout en bout, sur la base Docker."""

    async def test_list_tables(self) -> None:
        async with Client(mcp) as client:
            resultat = await client.call_tool("list_tables", {})
        assert not resultat.is_error
        assert "track" in resultat.structured_content["result"]

    async def test_get_schema(self) -> None:
        async with Client(mcp) as client:
            resultat = await client.call_tool("get_schema", {"table": "album"})
        colonnes = {c["column_name"]: c for c in resultat.structured_content["result"]}
        assert colonnes["artist_id"]["foreign_key"] == "artist.artist_id"

    async def test_get_schema_table_inconnue_message_pour_l_agent(self) -> None:
        async with Client(mcp) as client:
            resultat = await client.call_tool("get_schema", {"table": "tracks"})
        assert resultat.is_error
        assert "Tables disponibles" in resultat.content[0].text

    async def test_run_query(self) -> None:
        async with Client(mcp) as client:
            resultat = await client.call_tool(
                "run_query", {"sql": "SELECT name FROM media_type ORDER BY media_type_id"}
            )
        assert not resultat.is_error
        assert resultat.structured_content["columns"] == ["name"]
        assert resultat.structured_content["row_cap"] == get_settings().row_cap
        assert resultat.structured_content["executed_sql"].endswith(
            f"LIMIT {get_settings().row_cap + 1}"
        )

    async def test_run_query_ecriture_refusee_message_pour_l_agent(self) -> None:
        async with Client(mcp) as client:
            resultat = await client.call_tool("run_query", {"sql": "DELETE FROM genre"})
        assert resultat.is_error
        assert "Requête refusée (lecture_seule)" in resultat.content[0].text

    async def test_log_d_un_appel_reussi(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="text_to_sql_mcp")
        async with Client(mcp, client_info=AGENT) as client:
            await client.call_tool("run_query", {"sql": "SELECT name FROM genre"})
        (trace,) = appels_traces(caplog)
        assert trace.client == "agent-test/modele-x"
        assert (trace.outil, trace.statut, trace.lignes, trace.tronque) == (
            "run_query",
            "ok",
            25,
            False,
        )
        assert trace.sql == "SELECT name FROM genre"
        assert trace.sql_execute == f"SELECT name FROM genre LIMIT {get_settings().row_cap + 1}"
        assert trace.duree_ms >= 0

    async def test_log_d_un_refus(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="text_to_sql_mcp")
        async with Client(mcp, client_info=AGENT) as client:
            resultat = await client.call_tool("run_query", {"sql": "SELECT pg_sleep(30)"})
        assert resultat.is_error
        assert "fonction pg_sleep est interdite" in resultat.content[0].text
        (trace,) = appels_traces(caplog)
        assert (trace.statut, trace.regle, trace.levelname) == ("refus", "fonction", "WARNING")

    async def test_log_d_une_erreur_sql(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="text_to_sql_mcp")
        async with Client(mcp, client_info=AGENT) as client:
            resultat = await client.call_tool("run_query", {"sql": "SELECT nom FROM genre"})
        assert resultat.is_error
        (trace,) = appels_traces(caplog)
        assert (trace.statut, trace.sqlstate) == ("erreur", "42703")
