"""Tests d'intégration de l'accès base (nécessitent `docker compose up -d`)."""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from text_to_sql_mcp import db
from text_to_sql_mcp.config import get_settings
from text_to_sql_mcp.guardrails import GuardrailError

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("base_disponible")]

TABLES_CHINOOK = [
    "album",
    "artist",
    "customer",
    "employee",
    "genre",
    "invoice",
    "invoice_line",
    "media_type",
    "playlist",
    "playlist_track",
    "track",
]


def test_connexion_avec_le_role_lecture_seule() -> None:
    with db.connect() as conn:
        utilisateur, lecture_seule = conn.execute(
            "SELECT current_user, current_setting('transaction_read_only')"
        ).fetchone()
    assert utilisateur == "agent_lecteur"
    assert lecture_seule == "on"


def test_list_table_names() -> None:
    assert db.list_table_names() == TABLES_CHINOOK


def test_describe_table_colonnes_types_et_cles() -> None:
    colonnes = {colonne.column_name: colonne for colonne in db.describe_table("track")}
    assert next(iter(colonnes)) == "track_id"
    assert colonnes["track_id"].primary_key is True
    assert colonnes["album_id"].foreign_key == "album.album_id"
    assert colonnes["unit_price"].data_type == "numeric(10,2)"
    assert colonnes["composer"].nullable is True
    assert colonnes["name"].nullable is False


def test_describe_table_cle_primaire_composite() -> None:
    colonnes = {c.column_name: c for c in db.describe_table("playlist_track")}
    assert all(c.primary_key for c in colonnes.values())
    assert colonnes["playlist_id"].foreign_key == "playlist.playlist_id"
    assert colonnes["track_id"].foreign_key == "track.track_id"


@pytest.mark.parametrize(
    "nom",
    ["tracks", "Track", "pg_authid", "track; DROP TABLE track", "track' OR '1'='1"],
)
def test_describe_table_refuse_hors_liste_blanche(nom: str) -> None:
    with pytest.raises(db.UnknownTableError) as erreur:
        db.describe_table(nom)
    assert erreur.value.available == TABLES_CHINOOK
    assert "Tables disponibles" in str(erreur.value)


@pytest.mark.parametrize(
    "requete",
    [
        "INSERT INTO genre (genre_id, name) VALUES (999, 'Test')",
        "UPDATE genre SET name = 'Hack' WHERE genre_id = 1",
        "DELETE FROM genre WHERE genre_id = 1",
        "DROP TABLE genre",
        "CREATE TABLE pirate (id int)",
    ],
)
def test_ecriture_refusee(requete: str) -> None:
    with db.connect() as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(requete)


def test_set_config_ne_desactive_pas_la_transaction_lecture_seule() -> None:
    with db.connect() as conn, pytest.raises(psycopg.errors.ActiveSqlTransaction):
        conn.execute("SELECT set_config('transaction_read_only', 'off', false)")


def test_privileges_bloquent_meme_sans_filet_lecture_seule() -> None:
    """Sans aucun mode lecture seule, les privilèges du rôle refusent encore l'écriture."""
    with psycopg.connect(get_settings().conninfo(), autocommit=True) as conn:
        conn.execute("SELECT set_config('default_transaction_read_only', 'off', false)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO genre (genre_id, name) VALUES (999, 'Test')")


# --- run_query ---------------------------------------------------------------------------


def test_run_query_colonnes_et_lignes() -> None:
    resultat = db.run_query("SELECT genre_id, name FROM genre ORDER BY genre_id LIMIT 3", 10)
    assert resultat.columns == ["genre_id", "name"]
    assert resultat.rows == [[1, "Rock"], [2, "Jazz"], [3, "Metal"]]
    assert (resultat.row_count, resultat.truncated) == (3, False)


def test_run_query_plafonne_et_signale_la_troncature() -> None:
    resultat = db.run_query("SELECT track_id FROM track", 50)
    assert (resultat.row_count, resultat.truncated, resultat.row_cap) == (50, True, 50)


def test_run_query_pile_au_plafond_non_tronque() -> None:
    resultat = db.run_query("SELECT genre_id FROM genre", 25)  # Chinook a 25 genres
    assert (resultat.row_count, resultat.truncated) == (25, False)


def test_run_query_convertit_les_types_en_json() -> None:
    resultat = db.run_query(
        "SELECT total, invoice_date FROM invoice ORDER BY invoice_id LIMIT 1", 10
    )
    total, date = resultat.rows[0]
    assert total == "1.98"
    assert date.startswith("2021-01-01")


def test_run_query_pourcent_litteral() -> None:
    resultat = db.run_query("SELECT count(*) FROM genre WHERE name LIKE 'Rock%'", 10)
    assert resultat.rows == [[2]]  # Rock, Rock And Roll


@pytest.mark.parametrize(
    "requete",
    [
        "INSERT INTO genre (genre_id, name) VALUES (999, 'Test')",
        "SELECT 1; DROP TABLE genre",
        "WITH d AS (DELETE FROM genre RETURNING *) SELECT * FROM d",
        "SHOW search_path",
        "SELECT usename FROM pg_user",
        "SELECT pg_sleep(10)",
    ],
)
def test_run_query_refuse_par_les_garde_fous(requete: str) -> None:
    with pytest.raises(GuardrailError):
        db.run_query(requete, 10)


def test_run_query_execute_le_sql_valide_avec_limit() -> None:
    resultat = db.run_query("SELECT name FROM genre", 10)
    assert resultat.executed_sql == "SELECT name FROM genre LIMIT 11"
    assert (resultat.row_count, resultat.truncated) == (10, True)


def test_run_query_erreur_lisible_pour_l_agent() -> None:
    with pytest.raises(db.QueryError, match="42703") as erreur:
        db.run_query("SELECT nom FROM genre", 10)
    assert 'column "nom" does not exist' in str(erreur.value)


def test_run_query_timeout_message_pour_l_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATEMENT_TIMEOUT", "1")
    get_settings.cache_clear()
    try:
        with pytest.raises(db.QueryError, match="délai de 1 s") as erreur:
            db.run_query("SELECT count(*) FROM track a CROSS JOIN track b CROSS JOIN genre c", 10)
    finally:
        get_settings.cache_clear()
    assert erreur.value.sqlstate == "57014"


def test_timeout_de_session_prioritaire_sur_celui_du_role() -> None:
    with db.connect() as conn:
        delai = conn.execute("SHOW statement_timeout").fetchone()[0]
    assert delai == f"{get_settings().statement_timeout}s"


def test_timeout_du_role_en_filet() -> None:
    """Sans réglage de session, le rôle plafonne lui-même ses requêtes à 30 s."""
    sans_option = make_conninfo(get_settings().conninfo(), options="")
    with psycopg.connect(sans_option) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "30s"
