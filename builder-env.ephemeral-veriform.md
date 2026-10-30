<!-- Veriform build environment, ephemeral services. Mounted read-only at
     /app/builder_env.md by docker-compose.ephemeral-veriform.yml; distill.py
     reads it in place of the workspace's .builder_env.md (BUILDER_ENV_FILE). -->

# Your database and Redis are already running. Never install one.

This build has its own Postgres 16 and Redis 7, started for it and destroyed
when it ends. They are laid out exactly as veriform-ui's CI lays them out, so
CI's commands work here as written. `apt-get install postgresql`,
`embedded-postgres`, `pg-mem` and sqlite stand-ins are all wrong answers.

| Service  | Address          | Notes |
|----------|------------------|-------|
| Postgres | `localhost:5432` | user `vif` / `vif`, superuser. Databases `veriform_test` (the suite's) and `veriform` (the isolation check's). Empty until migrated. |
| Redis    | `localhost:6379` | Persistence off. Leave `REDIS_URL` UNSET, as CI does: setting it switches api-limits to live enforcement, which the suite is not written against. |

Every connection string is already in the environment, as ci.yml states them:
`$TEST_DATABASE_URL`, `$DATABASE_URL`, `$MIGRATION_DATABASE_URL`,
`$PUBLIC_DATABASE_URL`, `$SYNC_DATABASE_URL`, `$INSPECT_DATABASE_URL` (all
`vif@localhost:5432/veriform_test`), and the RLS probes
`$RLS_PROBE_DATABASE_URL` (`veriform_app`) and `$RLS_PROBE_PUBLIC_DATABASE_URL`
(`veriform_public`). Use them; do not construct your own.

The roles `veriform_owner`, `veriform_app` and `veriform_public` already exist
WITH LOGIN (password `build-throwaway`). Do not ALTER or recreate them.

# Migrate before any DB-backed test

```
cd /workspace/Backend && npm run db:migrate
```

`npm test` / `npm run test:coverage` also migrate `veriform_test` first
(pretest-migrate). `npm run test:rls` does not: migrate first. The isolation
check reads `veriform`; migrate that too when you run it:
`MIGRATION_DATABASE_URL=postgres://vif:vif@localhost:5432/veriform npm run db:migrate`.

Everything here is disposable. Drop, recreate, truncate and re-migrate freely.

# What is NOT here

OpenFGA, Zitadel, Mailpit and PgBouncer are not running. The FGA model tests
(`npm run test:fga`), the pooled RLS probe and anything that signs in through
Zitadel cannot run in this build: report them as not runnable here, never as
passing, and never start or install one to make them run.

# Do not write the developer's configuration

`/workspace` is the developer's ACTUAL working tree. `Backend/.env` is theirs,
and here it reads as an EMPTY file whose writes go nowhere - as in CI, which
has none. That is deliberate: do not try to fill it, recreate it elsewhere or
work around it.
`Backend/.env.example` is documentation. Your credentials are the environment
variables above.

# Reachability

```
pg_isready -h localhost -p 5432 || node -e "require('net').connect(5432,'localhost').on('connect',()=>{console.log('REACHABLE');process.exit(0)}).on('error',e=>{console.log(e.message);process.exit(1)})"
```

A refused connection here is a broken build environment, not something to
work around. Report it and stop.
