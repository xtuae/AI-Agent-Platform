#!/usr/bin/env bash
# Creates the two application roles and the pgvector extension. Runs ONCE as the Postgres
# superuser: automatically via /docker-entrypoint-initdb.d on first container start, and
# explicitly in CI and local test setup (same script, so all three environments match).
#
#   DB_OWNER_USER  owns the schema and every table; runs Alembic migrations. Never used by the app.
#   DB_APP_USER    what api/worker/scheduler connect as. NOSUPERUSER, NOBYPASSRLS, owns nothing,
#                  so Row-Level Security always applies to it (01_architecture §4.3).
#
# Required env: POSTGRES_DB, DB_OWNER_USER, DB_OWNER_PASSWORD, DB_APP_USER, DB_APP_PASSWORD.
# Optional: POSTGRES_USER (superuser, default postgres), PGHOST/PGPORT for non-socket connections.
set -euo pipefail

: "${POSTGRES_DB:?}" "${DB_OWNER_USER:?}" "${DB_OWNER_PASSWORD:?}" "${DB_APP_USER:?}" "${DB_APP_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER:-postgres}" \
     --dbname "$POSTGRES_DB" \
     -v owner="$DB_OWNER_USER" -v owner_pw="$DB_OWNER_PASSWORD" \
     -v app="$DB_APP_USER" -v app_pw="$DB_APP_PASSWORD" \
     -v db="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
              :'owner', :'owner_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') \gexec

SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD %L',
              :'app', :'app_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app') \gexec

-- pgvector must be created by a superuser; the owner role then uses it.
CREATE EXTENSION IF NOT EXISTS vector;

-- The owner role owns the schema; the app role can use it but cannot create objects in it.
ALTER SCHEMA public OWNER TO :"owner";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app";
REVOKE CREATE ON SCHEMA public FROM :"app";
GRANT CONNECT ON DATABASE :"db" TO :"owner", :"app";

-- Every table/sequence the owner creates (i.e. every migration) is usable by the app role —
-- DML only. No TRUNCATE, no REFERENCES, no TRIGGER, and no ownership.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app";
SQL

echo "roles ready: owner=$DB_OWNER_USER app=$DB_APP_USER db=$POSTGRES_DB"
