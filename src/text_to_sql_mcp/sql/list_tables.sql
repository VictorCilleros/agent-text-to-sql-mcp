-- Tables du schéma exposé à l'agent.
-- information_schema ne montre que les objets sur lesquels le rôle courant a des droits :
-- l'agent ne voit que les tables qu'il peut lire.
-- Paramètre : %(schema)s
SELECT table_name
FROM information_schema.tables
WHERE table_schema = %(schema)s
  AND table_type = 'BASE TABLE'
ORDER BY table_name;
