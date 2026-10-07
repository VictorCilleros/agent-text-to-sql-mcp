# Agent text-to-SQL via serveur MCP

Un agent qui traduit des questions métier en langage naturel en **requêtes SQL sûres**, exposé à Claude
via un **serveur MCP maison**, avec un **harnais d'évaluation** comparant prompts et modèles.

> **Statut :** en construction, jalon 1 (serveur MCP minimal en lecture seule).

## Stack

Python 3.13 · [SDK MCP officiel v2](https://py.sdk.modelcontextprotocol.io/) · PostgreSQL 17 + Chinook ·
uv · ruff · pytest · pre-commit

## Installation (développement)

Prérequis : [uv](https://docs.astral.sh/uv/) (il installe lui-même Python 3.13 si besoin).

```bash
uv sync                       # crée .venv et installe les dépendances
uv run pre-commit install     # active les hooks de qualité au commit
uv run pytest                 # lance les tests
```

Les tests marqués `integration` utilisent la base Docker (voir ci-dessous) et sont sautés,
avec un message explicite, si elle n'est pas joignable. Pour ne lancer que les tests unitaires :
`uv run pytest -m "not integration"`.

## Base de données (Docker)

Prérequis : Docker et le plugin `docker compose`.

```bash
cp .env.example .env          # puis remplacer les mots de passe
docker compose up -d          # premier démarrage : chargement de Chinook + rôle lecture seule
docker compose ps             # attendre l'état « healthy »
docker compose down -v        # tout supprimer, données comprises (repartir de zéro)
```

La base `chinook` est exposée sur `127.0.0.1` uniquement. L'agent s'y connecte avec le rôle
`agent_lecteur`, qui n'a que des droits de lecture (`SELECT`) et des transactions en lecture
seule par défaut.

### Données

[Chinook](https://github.com/lerocha/chinook-database) v1.4.5, script PostgreSQL officiel
(identifiants en snake_case), versionné sans modification dans `docker/initdb/01_chinook.sql`.

- Source : `https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_PostgreSql.sql`
- SHA-256 : `e3fde5c1a5b51a2a91429a702c9ca6e69ba56e6c7f5e112724d70c3d03db695e`
- Licence : MIT, © Luis Rocha (voir [`docker/LICENSE-chinook.md`](docker/LICENSE-chinook.md))

## Serveur MCP

Trois outils en lecture seule : `list_tables`, `get_schema` (colonnes, types, clés) et
`run_query` (une requête de lecture, résultat plafonné à `ROW_CAP` lignes).

```bash
uv run text-to-sql-mcp                                   # lance le serveur sur stdio
npx @modelcontextprotocol/inspector uv run text-to-sql-mcp   # interface de test (Node requis)
```

## Licence

[MIT](LICENSE)
