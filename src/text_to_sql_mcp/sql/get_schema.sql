-- Colonnes, types et clés d'une table.
-- Lu dans pg_catalog : pour un rôle qui n'a que SELECT, information_schema masque les
-- contraintes (clés primaires et étrangères).
-- Le nom de table doit avoir été vérifié contre list_tables avant l'appel.
-- Paramètres : %(schema)s, %(table)s
SELECT
    a.attname AS column_name,
    format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull AS nullable,
    EXISTS (
        SELECT 1
        FROM pg_constraint AS pk
        WHERE pk.conrelid = c.oid
          AND pk.contype = 'p'
          AND a.attnum = ANY (pk.conkey)
    ) AS primary_key,
    (
        SELECT ft.relname || '.' || fa.attname
        FROM pg_constraint AS fk
        JOIN pg_class AS ft ON ft.oid = fk.confrelid
        JOIN pg_attribute AS fa
          ON fa.attrelid = fk.confrelid
         AND fa.attnum = fk.confkey[array_position(fk.conkey, a.attnum)]
        WHERE fk.conrelid = c.oid
          AND fk.contype = 'f'
          AND a.attnum = ANY (fk.conkey)
        LIMIT 1
    ) AS foreign_key
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_attribute AS a ON a.attrelid = c.oid
WHERE n.nspname = %(schema)s
  AND c.relname = %(table)s
  AND c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum;
