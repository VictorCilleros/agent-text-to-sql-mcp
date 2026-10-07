"""Tests d'intégration de l'accès base (nécessitent `docker compose up -d`)."""

import psycopg
import pytest

from text_to_sql_mcp import db
from text_to_sql_mcp.config import get_settings

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
    colonnes = {colonne["column_name"]: colonne for colonne in db.describe_table("track")}
    assert next(iter(colonnes)) == "track_id"
    assert colonnes["track_id"]["primary_key"] is True
    assert colonnes["album_id"]["foreign_key"] == "album.album_id"
    assert colonnes["unit_price"]["data_type"] == "numeric(10,2)"
    assert colonnes["composer"]["nullable"] is True
    assert colonnes["name"]["nullable"] is False


def test_describe_table_cle_primaire_composite() -> None:
    colonnes = {c["column_name"]: c for c in db.describe_table("playlist_track")}
    assert all(c["primary_key"] for c in colonnes.values())
    assert colonnes["playlist_id"]["foreign_key"] == "playlist.playlist_id"
    assert colonnes["track_id"]["foreign_key"] == "track.track_id"


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
