"""Tests unitaires des garde-fous SQL (sans base de données)."""

import pytest

from text_to_sql_mcp.guardrails import GuardrailError, validate_query

TABLES = [
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
CAP = 100


def valider(sql: str) -> str:
    """Valide avec la liste blanche Chinook et un plafond de 100 lignes."""
    return validate_query(sql, TABLES, row_cap=CAP)


# --- Requêtes légitimes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM genre",
        "SELECT billing_country, sum(total) FROM invoice GROUP BY 1 ORDER BY 2 DESC",
        "SELECT a.title FROM album a JOIN artist ar ON ar.artist_id = a.artist_id",
        "SELECT * FROM genre g WHERE EXISTS (SELECT 1 FROM track t WHERE t.genre_id = g.genre_id)",
        "WITH top AS (SELECT artist_id FROM album) SELECT * FROM top",
        "WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM n WHERE i < 3) "
        "SELECT * FROM n",
        "SELECT 1 AS x UNION SELECT 2",
        "SELECT first_name, age(now(), birth_date) FROM employee",
        "SELECT * FROM public.genre",
        "SELECT * FROM GENRE",  # non cité : PostgreSQL le met en minuscules
        "SELECT * FROM generate_series(1, 3)",
        "SELECT name FROM genre WHERE name ILIKE '%rock%'",
    ],
)
def test_requete_legitime_acceptee(sql: str) -> None:
    assert "LIMIT" in valider(sql)


# --- Règle 1 : une seule lecture, sans construction interdite ------------------------------


@pytest.mark.parametrize(
    ("sql", "regle"),
    [
        ("", "instruction_unique"),
        ("   ", "instruction_unique"),
        ("SELECT 1; DROP TABLE genre", "instruction_unique"),
        ("SELEC 1", "syntaxe"),
        ("DELETE FROM genre", "lecture_seule"),
        ("INSERT INTO genre (genre_id, name) VALUES (999, 'x')", "lecture_seule"),
        ("SHOW search_path", "lecture_seule"),
        ("VALUES (1)", "lecture_seule"),
        ("WITH d AS (DELETE FROM genre RETURNING *) SELECT * FROM d", "construction"),
        ("SELECT * INTO pirate FROM genre", "construction"),
        ("SELECT * FROM genre FOR UPDATE", "construction"),
    ],
)
def test_regle_lecture_unique(sql: str, regle: str) -> None:
    with pytest.raises(GuardrailError) as erreur:
        valider(sql)
    assert erreur.value.rule == regle


# --- Règle 2 : tables de la liste blanche -------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT usename FROM pg_user",
        "SELECT * FROM pg_catalog.pg_stat_activity",
        "SELECT * FROM information_schema.tables",
        'SELECT * FROM "Genre"',  # cité : nom sensible à la casse, inexistant
        "SELECT count(*) FROM genre WHERE EXISTS (SELECT 1 FROM pg_roles)",
        # Une CTE nommée pg_user dans une sous-requête n'autorise pas pg_user ailleurs :
        # PostgreSQL lirait la vraie vue système dans la requête externe.
        "SELECT * FROM pg_user, (WITH pg_user AS (SELECT 1 AS a) SELECT * FROM pg_user) s",
    ],
)
def test_regle_tables(sql: str) -> None:
    with pytest.raises(GuardrailError) as erreur:
        valider(sql)
    assert erreur.value.rule == "table"


def test_cte_qui_masque_un_nom_systeme_reste_une_cte() -> None:
    """Dans sa propre portée, la CTE est prioritaire sur la table du même nom."""
    assert "WITH pg_user AS" in valider("WITH pg_user AS (SELECT 1 AS a) SELECT * FROM pg_user")


def test_message_table_liste_les_tables_disponibles() -> None:
    with pytest.raises(GuardrailError, match="Tables disponibles : album"):
        valider("SELECT * FROM tracks")


# --- Règle 3 : fonctions interdites -------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(30)",
        "SELECT PG_SLEEP(30)",
        "SELECT pg_catalog.pg_sleep(30)",
        "SELECT * FROM pg_ls_dir('.')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT current_setting('data_directory')",
        "SELECT query_to_xml('SELECT * FROM pg_authid', true, true, '')",
        "SELECT name FROM genre WHERE genre_id IN (SELECT 1 WHERE pg_sleep(1) IS NOT NULL)",
        "SELECT nextval('genre_genre_id_seq')",
    ],
)
def test_regle_fonctions(sql: str) -> None:
    with pytest.raises(GuardrailError) as erreur:
        valider(sql)
    assert erreur.value.rule == "fonction"


# --- Règle 4 : LIMIT imposé ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "attendu"),
    [
        ("SELECT * FROM genre", "SELECT * FROM genre LIMIT 101"),
        ("SELECT * FROM genre LIMIT 5", "SELECT * FROM genre LIMIT 5"),
        ("SELECT * FROM genre LIMIT 5000", "SELECT * FROM genre LIMIT 101"),
        ("SELECT * FROM genre LIMIT 5 + 5", "SELECT * FROM genre LIMIT 101"),
        (
            "SELECT * FROM genre ORDER BY name LIMIT ALL OFFSET 3",
            "SELECT * FROM genre ORDER BY name LIMIT 101 OFFSET 3",
        ),
        (
            "SELECT * FROM genre FETCH FIRST 5 ROWS ONLY",
            "SELECT * FROM genre FETCH FIRST 5 ROWS ONLY",
        ),
        ("SELECT * FROM genre FETCH FIRST 500 ROWS ONLY", "SELECT * FROM genre LIMIT 101"),
        ("SELECT 1 AS x UNION SELECT 2", "SELECT 1 AS x UNION SELECT 2 LIMIT 101"),
    ],
)
def test_regle_limit(sql: str, attendu: str) -> None:
    assert valider(sql) == attendu


def test_commentaires_retires_du_sql_execute() -> None:
    assert valider("SELECT 1 -- */ , pg_sleep(30) /*") == "SELECT 1 LIMIT 101"
