"""Tests du serveur MCP, via le client en mémoire du SDK (sans sous-processus)."""

import pytest
from mcp import Client

from text_to_sql_mcp.config import get_settings
from text_to_sql_mcp.server import mcp

pytestmark = pytest.mark.anyio


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

    async def test_run_query_ecriture_refusee_message_pour_l_agent(self) -> None:
        async with Client(mcp) as client:
            resultat = await client.call_tool("run_query", {"sql": "DELETE FROM genre"})
        assert resultat.is_error
        assert "lecture unique" in resultat.content[0].text
