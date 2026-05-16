#!/bin/bash
# Creates the read-only role used by the agent's sql() escape-hatch tool.
# Runs after init.sql (alphabetical order). Pulls credentials from env
# so the SQL file stays secret-free and grep-able.

set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Idempotent: drop and recreate to apply any privilege changes
    -- cleanly on re-init. (initdb only runs once per fresh volume,
    -- so this is belt-and-braces.)
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${POSTGRES_READONLY_USER}') THEN
            CREATE ROLE ${POSTGRES_READONLY_USER} WITH LOGIN PASSWORD '${POSTGRES_READONLY_PASSWORD}';
        ELSE
            ALTER ROLE ${POSTGRES_READONLY_USER} WITH LOGIN PASSWORD '${POSTGRES_READONLY_PASSWORD}';
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${POSTGRES_READONLY_USER};
    GRANT USAGE   ON SCHEMA public           TO ${POSTGRES_READONLY_USER};
    GRANT SELECT  ON ALL TABLES IN SCHEMA public TO ${POSTGRES_READONLY_USER};

    -- Future tables in this schema also default to SELECT-only for the RO role.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT ON TABLES TO ${POSTGRES_READONLY_USER};
EOSQL
