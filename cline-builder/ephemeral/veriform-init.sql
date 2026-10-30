-- One-time setup for a build's ephemeral veriform Postgres
-- (docker-compose.ephemeral-veriform.yml). Runs as `vif` on the empty cluster,
-- in `veriform_test`, before the build container is allowed to start.
--
-- The roles are created here WITH LOGIN, before any migration has run. The
-- migrations that introduce them (0025, 0027, 0046) all guard their CREATE ROLE
-- with IF NOT EXISTS, and none ever alters a role, so the login survives them.
-- That replaces isolation.yml's "Provision unprivileged roles" step, which has
-- to run between two migrations because it ALTERs roles the first one created.
--
-- The password is a throwaway on a database that lives for one build.

CREATE ROLE veriform_owner  LOGIN PASSWORD 'build-throwaway' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE veriform_app    LOGIN PASSWORD 'build-throwaway' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE veriform_public LOGIN PASSWORD 'build-throwaway' NOSUPERUSER NOBYPASSRLS;

-- What isolation.yml grants by hand because migrations cannot: drizzle-kit
-- issues CREATE SCHEMA IF NOT EXISTS on every run.
GRANT CREATE ON DATABASE veriform_test TO veriform_owner;
GRANT CREATE, USAGE ON SCHEMA public TO veriform_owner;

-- `veriform`, for the structural isolation check, which inspects a database
-- of its own in isolation.yml.
CREATE DATABASE veriform OWNER vif;
GRANT CREATE ON DATABASE veriform TO veriform_owner;
