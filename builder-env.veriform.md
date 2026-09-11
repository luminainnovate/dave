<!-- Veriform build environment. Installed as `.builder_env.md` at the workspace
     root; distill.py copies it verbatim into every .clinerules. -->

# Services are already running. Never install one.

Every service below is a Docker container started from
`Backend/docker-compose.yml` and is running before this build starts. Installing,
provisioning or substituting any of them is forbidden: `apt-get install
postgresql`, `embedded-postgres`, `pg-mem` and a hand-rolled sqlite stand-in are
all wrong answers, and a schema built by one of them proves nothing about the
migrations in `Backend/drizzle/` that the real database holds.

# Do not write the developer's configuration.

`/workspace` is a bind mount of the developer's ACTUAL working tree, not a copy.
Everything you write there, they get. Two files are theirs, and editing either
one breaks their machine while your own build still passes:

| Path                  | Rule |
|-----------------------|------|
| `Backend/.env`         | **Never create, edit, append to or "fix" it.** |
| `Backend/.env.example` | Documentation. Change only if the task is about that documentation. |

Your database credentials arrive as **environment variables** — `$DATABASE_URL`,
`$TEST_DATABASE_URL`, `$MIGRATION_DATABASE_URL` — already set, from the compose
override. They are not in `Backend/.env` and must never be written to it.

The failure this prevents is specific and has happened three times. `Backend/.env`
is read by the DEVELOPER's `npm run dev` on the HOST, where container hostnames do
not resolve. Rewriting it to the container-network form leaves them with
`EAI_AGAIN veriform-postgres` and a dev server that will not boot, and rewriting
the role passwords leaves them with `28P01 password authentication failed`.

If a connection fails, the answer is never to edit `Backend/.env`. See "If Postgres
is unreachable" below.

## Postgres — the one you will actually use

`$DATABASE_URL`, `$TEST_DATABASE_URL` and `$MIGRATION_DATABASE_URL` are already
set. **Use them; do not compose your own, and do not substitute the superuser.**

```
DATABASE_URL           veriform_app   @ veriform-postgres:5432/veriform_build
MIGRATION_DATABASE_URL veriform_owner @ veriform-postgres:5432/veriform_build
```

Two roles, deliberately, and the separation is the point:

- `veriform_app` owns nothing and is `NOBYPASSRLS`. Row-level security is ENFORCED
  against it. Queries fail with **"no tenant or person context is set"** until the
  request sets a tenant. That error is the system working, not a bug to route
  around.
- `veriform_owner` owns the tables and runs migrations. Also `NOBYPASSRLS`.

**Do not connect as `vif`.** It is a superuser with `BYPASSRLS`, so every
tenant-isolation assertion silently passes under it and proves nothing — the same
query that returns 0 rows as `veriform_app` returns every tenant's rows as `vif`.
A green isolation gate reached as `vif` is a false negative, not a pass. `vif` is
for `CREATE ROLE` and disaster recovery only.

`localhost` / `127.0.0.1` / `host.docker.internal` DO NOT WORK. The host publishes
Postgres on loopback only, deliberately. You reach it as `veriform-postgres` over
the shared Docker network, and that is the only route.

Five Veriform databases exist on that server. **Only one is yours. Default to
"not mine" for anything not on this list.**

| Database               | Whose                              | May you write to it? |
|------------------------|------------------------------------|----------------------|
| `veriform_build`       | yours                              | Yes. Disposable.     |
| `veriform`             | the developer's dev data           | **No.** Destroying it destroys real work. |
| `veriform_test`        | the developer's `npm test`         | **No.** You would truncate tables mid-run under them. |
| `veriform_uitest`      | the Playwright/UI suite            | **No.** Reset out of band; not yours to migrate. |
| `veriform_restoretest` | the backup/restore drill           | **No.** Its contents ARE the fixture under test. |

Migrations, run from `Backend/`:

```
node --import tsx ./scripts/migrate.ts     # == npm run db:migrate
```

That script reads `$MIGRATION_DATABASE_URL` (falling back to `$DATABASE_URL`), so
it applies to `veriform_build` as `veriform_owner` without further arguments.

Run this before any DB-backed test. Migrations are applied to `veriform_build`
only when something applies them; a stale schema fails tests for reasons that
have nothing to do with your code.

## If Postgres is unreachable, STOP — do not work around it

Probe first:

```
timeout 5 bash -c "</dev/null >/dev/tcp/veriform-postgres/5432" && echo REACHABLE
```

`ECONNREFUSED` or a failed probe is an ENVIRONMENT FAULT, not a defect in your
code and not a task. The container stack is down, or this build was started
without the `docker-compose.veriform.yml` override that joins its network.
Neither is fixable from in here.

A `28P01 password authentication failed` is the SAME class of fault: the
credentials come from the override, so a bad one is an environment problem. Do not
guess passwords, do not `ALTER ROLE` (the roles are cluster-wide — you would break
the developer's dev database and every other worktree at once), and do not fall
back to `vif`.

When that happens: record it in `.cline_context/.build_issues.md`, mark the
DB-backed gates **BLOCKED — database unreachable**, and move on to work that does
not need a database. Do not report a blocked gate as passed, and do not spend
iterations trying to conjure a database. A skipped gate is not a passed gate.

This applies to the isolation suites especially. `Backend/test/rls-enforced.test.ts`
and the tenant-isolation tests swallow a failed connection and `describe.skipIf`
themselves out, so the run still exits 0. **Report the test COUNT, not the exit
code** — a suite that skipped its database half still prints "passed".

## The other services

Same rule — running already, reached by container name, never installed.

| Service | Address from here | Notes |
|---------|-------------------|-------|
| Zitadel (identity) | `http://zitadel:8080` | Issuer is `http://localhost:8083`. Own Postgres, `veriform-zitadel-postgres` — never the app's. |
| Zitadel login UI | `http://zitadel-login:3000` | Host `:3100`. |
| OpenFGA (authorisation) | `http://openfga:8080` | Host `:8892`. Own Postgres, `veriform-openfga-postgres` — never the app's. Model: `npm run fga:apply`, tests `npm run test:fga`. |
| PgBouncer | `veriform-pgbouncer:6432` | Transaction pooling, for `npm run test:rls` only. Needs `npm run pool:config` on the HOST first; without it the config mount is empty and the container exits immediately. |
| Mailpit (SMTP capture) | `mailpit:1025`, UI `:8025` | Verification email is captured here, never sent. |
| dnsmasq | `dnsmasq:53` | Host `127.0.0.1:5354`. Answers domain-verification TXT lookups; NXDOMAINs everything else. |
| Redis (rate-limit counters) | `veriform-redis:6379` | Host `:6379`. Client: `ioredis`, ALREADY in `Backend/package.json` — import it, do not install another. |

### Redis holds counters, never facts

The counter store is `redis://veriform-redis:6379`. Read it from `$REDIS_URL`,
falling back to that literal — the variable is supplied by the compose override
and the fallback is what makes the code run on the developer's host too, where
the same server is published on `127.0.0.1:6379`.

It is the rate-limit and quota counter store, and the only operations it exists
for here are `INCR`, `EXPIRE`, `TTL` and `GET` on short-lived keys.

It runs with `--save "" --appendonly no`: **a restart drops every key.** That is
deliberate. A lost counter costs at most one window's worth of extra requests,
which is why counters may live here — and why nothing else may. Anything that has
to survive a restart belongs in Postgres. `maxmemory-policy` is `allkeys-lru`, so
a key you wrote can be evicted before its TTL expires; code that cannot tolerate
a missing key does not belong here.

Unreachable Redis is an ENVIRONMENT FAULT, handled exactly like an unreachable
Postgres above — record it, mark the gate BLOCKED, and do not work around it with
an in-process Map. An in-memory counter is not a smaller version of this: it is a
different and wrong design, because every API process would then enforce the full
limit separately and the effective limit would be N times the configured one.

Probe:

```
timeout 5 bash -c "</dev/null >/dev/tcp/veriform-redis/6379" && echo REACHABLE
```
