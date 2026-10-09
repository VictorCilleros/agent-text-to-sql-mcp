# Agent text-to-SQL via serveur MCP

Un agent qui traduit des questions métier en langage naturel en **requêtes SQL sûres**, exposé à Claude
via un **serveur MCP maison**, avec un **harnais d'évaluation** comparant prompts et modèles.

> **Statut :** en construction, jalon 3. Le serveur MCP en lecture seule, ses garde-fous,
> l'agent (boucle tool use + CLI) et le harnais d'évaluation sont en place et testés ; première
> évaluation à lancer (`notebooks/06_evaluation.ipynb`).

## Feuille de route

1. ✅ Serveur MCP minimal (`list_tables`, `get_schema`, `run_query`) en lecture seule sur Chinook
2. ✅ Agent text-to-SQL + garde-fous (rôle lecture seule, validation sqlglot, `LIMIT`, timeout)
3. 🚧 Harnais d'évaluation : gold set, execution accuracy, comparaison de variantes, mini red-team
4. Documentation, rapport d'évaluation et démo (Claude Desktop / Claude Code)

## Stack

Python 3.13 · [SDK MCP officiel v2](https://py.sdk.modelcontextprotocol.io/) · PostgreSQL 17 + Chinook ·
sqlglot · psycopg 3 · SDK Anthropic · uv · ruff · pytest · pre-commit

## Installation (développement)

Prérequis : [uv](https://docs.astral.sh/uv/) (il installe lui-même Python 3.13 si besoin).

```bash
uv sync                       # crée .venv, installe les dépendances et les commandes
uv run pre-commit install     # active les hooks de qualité au commit
uv run pytest                 # lance les tests
```

Les tests marqués `integration` utilisent la base Docker (voir ci-dessous) et sont sautés,
avec un message explicite, si elle n'est pas joignable. Pour ne lancer que les tests unitaires :
`uv run pytest -m "not integration"`.

Les tests de l'agent remplacent l'API Anthropic par un faux client qui rejoue des réponses
écrites à l'avance : ils sont gratuits et déterministes. Les tests marqués `api` appellent la
vraie API (payant) et sont sautés par défaut : `uv run pytest --api -m api`.

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
uv run text-to-sql-mcp                                       # lance le serveur sur stdio
npx @modelcontextprotocol/inspector uv run text-to-sql-mcp   # interface de test (Node requis)
```

La commande `text-to-sql-mcp` est déclarée dans `[project.scripts]` du `pyproject.toml` et
installée par `uv sync`. Équivalent sans la commande : `uv run python -m text_to_sql_mcp.server`.

### Garde-fous (défense en profondeur)

| Couche | Où | Ce qu'elle bloque |
|---|---|---|
| Privilèges `SELECT` seuls | rôle PostgreSQL | toute écriture, DDL, escalade |
| Transaction `READ ONLY` | connexion (`db.py`) | écriture, même après un `set_config` |
| Validation sqlglot | `guardrails.py` | plusieurs instructions, écriture cachée (CTE, `SELECT INTO`, `FOR UPDATE`), tables système, fonctions d'administration (`pg_*`, `set_config`…) |
| `LIMIT` imposé + plafond de lignes | `guardrails.py`, `db.py` | résultats massifs, tris coûteux |
| Curseur côté serveur | `db.py` | tout ce qui n'est pas une lecture unique (seconde barrière) |
| `statement_timeout` | connexion (5 s) et rôle (30 s) | requêtes trop longues |

Le SQL exécuté est celui **régénéré par sqlglot** à partir de l'arbre validé (renvoyé dans
`executed_sql`) : on exécute exactement ce qui a été vérifié.

### Logs

Une ligne JSON par appel d'outil sur stderr (stdout est réservé au protocole MCP), avec le
client appelant (`clientInfo`), l'outil, le statut (`ok`, `refus`, `erreur`, `echec`), la durée,
le nombre de lignes, le SQL soumis et le SQL exécuté.

## Agent

Une boucle *tool use* écrite à la main (`src/text_to_sql_mcp/agent/boucle.py`) : l'agent envoie
la question à Claude avec les trois outils du serveur, exécute chaque demande d'outil sur le
serveur MCP (lancé en sous-processus stdio), renvoie les résultats, et recommence jusqu'à la
réponse finale.

```bash
uv run text-to-sql-agent "Quels sont les 5 artistes qui ont publié le plus d'albums ?"
uv run text-to-sql-agent "…" --model claude-haiku-5-5 --max-turns 5
uv run text-to-sql-agent "…" --json 2> trace.jsonl   # résultat complet en JSON, logs à part
```

La clé `ANTHROPIC_API_KEY` et les réglages par défaut (`AGENT_MODEL`, `AGENT_PROMPT`,
`AGENT_MAX_TURNS`, `AGENT_MAX_TOKENS`) sont lus dans le `.env`. La commande se lance depuis la
racine du repo : le serveur MCP y lit lui aussi le `.env`.

- **Prompts versionnés** : un fichier par variante dans `agent/prompts/` (`v0.md`), choisi par
  son nom (`--prompt v0`) et tracé dans les logs.
- **Plafond de tours** : au dernier appel autorisé, l'agent est prévenu qu'il n'a plus d'outil
  et les outils sont désactivés (`tool_choice: none`) ; il répond avec ce qu'il a obtenu, ou
  dit qu'il ne peut pas répondre en aussi peu de tours.
- **Erreurs d'outils** (refus des garde-fous, erreur SQL) renvoyées à Claude avec `is_error` :
  il corrige sa requête ; le nombre d'erreurs est tracé.
- **Résultat observé** (`AgentResult`) : SQL exécuté, colonnes et lignes de la dernière requête
  réussie, appels d'outils, tours, tokens, durée. Construit par le code à partir de ce qu'il
  observe, jamais à partir du texte du modèle.
- **Statut de fin** : `ok`, ou le `stop_reason` de l'API pour toute autre fin (`max_tokens`,
  `refusal`, `pause_turn`, `model_context_window_exceeded`, `stop_sequence`), `max_tours` en
  filet, `arret_inattendu` pour un `stop_reason` inconnu.

Codes de sortie du CLI : `0` statut `ok`, `1` autre statut, `2` erreur (arguments,
configuration, API ou base injoignable).

**Logs** : une ligne JSON `tour_api` par appel à l'API (tour, `stop_reason`, tokens, durée) et
une ligne `fin_agent` (bilan), sur stderr. Leur champ `client`
(`text-to-sql-agent:<modèle>:<prompt>/<version>`) est le même que celui des lignes `appel_outil`
du serveur : une question se suit d'un flux à l'autre.

## Évaluation

Le harnais (`src/text_to_sql_mcp/evaluation/`) pose chaque question du gold set à l'agent, note
la réponse et écrit un enregistrement JSONL par réponse dans `evals/resultats/`. Le notebook
`06_evaluation.ipynb` lance les évaluations et affiche les résultats (plotly).

**Gold set** (`evaluation/gold_chinook.toml`) : 44 questions en français, adaptées du dataset
*Chinook gold* du projet [nl2sql](https://github.com/nadeem4/nl2sql) (licence MIT, voir
`evaluation/LICENSE-gold-nl2sql.md`) : 39 questions répondables (faciles, moyennes,
difficiles) et 5 questions sans réponse. Le SQL a été traduit vers PostgreSQL snake_case, les
dates décalées de +12 ans (Chinook v1.4.5), et chaque traduction vérifiée contre le résultat
publié par la source.

**Métriques**

- **Execution accuracy** : on compare le résultat de l'agent à celui d'une requête de
  référence, pas le texte SQL. Même nombre de lignes ; chaque colonne de la référence présente
  (colonnes en plus tolérées) ; ordre comparé seulement si la question l'implique ; valeurs
  normalisées (`"10"`, `10` et `10.00` égaux, arrondi à 2 décimales).
- **Abstention** : sur une question sans réponse, l'agent (prompt `v1`) doit commencer sa
  réponse par `[SANS_REPONSE]` ; le marqueur sur une question répondable est un faux refus.
- **Latence et coût** par question (tarifs publics, réflexion comprise).
- **pass^k** (et pass@k) sur les questions difficiles posées *k* fois : la fiabilité.

Un oracle simulé (`OracleAnthropic`) permet une répétition à blanc du harnais et du notebook,
sans API ni coût.

## Notebooks (playgrounds)

Le dossier `notebooks/` contient les essais qui précèdent chaque incrément de code, avec le kernel
du `.venv` :

| Notebook | Sujet |
|---|---|
| `01_playground_mcp.ipynb` | premiers pas avec le SDK MCP |
| `02_playground_postgres.ipynb` | connexion psycopg et rôle lecture seule |
| `03_playground_garde_fous.ipynb` | validation sqlglot et contournements testés |
| `04_playground_agent.ipynb` | boucle tool use, `tool_runner`, comparaison de modèles |
| `05_playground_boucle_agent.ipynb` | boucle avec dépendances injectées, faux client, statuts |
| `06_evaluation.ipynb` | évaluation 1 (justesse, abstention, latence, coût) et 2 (pass^k) |

Les notebooks 04 à 06 appellent l'API Anthropic : ils attendent `ANTHROPIC_API_KEY` dans le
`.env` (jamais commité) et consomment des tokens.

## Licence

[MIT](LICENSE)
