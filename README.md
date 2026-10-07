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

## Licence

[MIT](LICENSE)
