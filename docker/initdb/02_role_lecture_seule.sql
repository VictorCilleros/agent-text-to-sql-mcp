-- =============================================================================
-- Rôle lecture seule utilisé par le serveur MCP (agent text-to-SQL).
--
-- Exécuté une seule fois par l'image postgres, au premier démarrage du
-- conteneur (volume de données vide), juste après 01_chinook.sql.
-- Le mot de passe est lu dans la variable d'environnement AGENT_DB_PASSWORD
-- (fichier .env), il n'apparaît jamais dans le repo.
-- =============================================================================

\set ON_ERROR_STOP on

-- Les scripts d'init sont lancés sur la base par défaut ; Chinook vit dans sa
-- propre base, créée par 01_chinook.sql.
\connect chinook

-- Lecture du mot de passe depuis l'environnement du conteneur (psql >= 15).
\getenv agent_password AGENT_DB_PASSWORD
\if :{?agent_password}
\else
    \warn 'AGENT_DB_PASSWORD est absent : rôle lecture seule non créé.'
    SELECT 1 / 0 AS arret_volontaire;
\endif

-- 1. Le rôle : il peut se connecter, et rien d'autre.
CREATE ROLE agent_lecteur
    LOGIN
    PASSWORD :'agent_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- 2. Toute transaction ouverte par ce rôle est en lecture seule par défaut.
--    C'est un filet, pas la barrière : une session peut repasser en écriture
--    avec SET ou BEGIN READ WRITE. La vraie barrière, ce sont les privilèges.
ALTER ROLE agent_lecteur SET default_transaction_read_only = on;

-- 3. Base : on retire les droits implicites de PUBLIC (dont TEMPORARY, qui
--    permettrait de créer des tables temporaires), puis on autorise la
--    connexion pour l'agent uniquement.
REVOKE ALL ON DATABASE chinook FROM PUBLIC;
GRANT CONNECT ON DATABASE chinook TO agent_lecteur;

-- 4. Schéma : personne ne crée d'objets dans public (déjà le défaut depuis
--    PostgreSQL 15, rendu explicite ici) ; l'agent peut seulement y accéder.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO agent_lecteur;

-- 5. Tables : lecture seule, sur les tables existantes et sur celles qui
--    seraient créées plus tard par l'administrateur.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_lecteur;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_lecteur;

-- Jalon 2 : statement_timeout sur ce rôle (garde-fou anti-requêtes longues).
