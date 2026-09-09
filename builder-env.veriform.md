<!-- Veriform build environment. Installed as `.builder_env.md` at the workspace
     root; distill.py copies it verbatim into every .clinerules. -->

# Services are already running. Never install one.

Every service below is a Docker container started from
`Backend/docker-compose.yml` and is running before this build starts. Installing,
provisioning or substituting any of them is forbidden: `apt-get install
postgresql`, `embedded-postgres`, `pg-mem` and a hand-rolled sqlite stand-in are
all wrong answers, and a schema built by one of them proves nothing about the 75
migrations the real database holds.

## Postgres — the one you will actually use

```
DATABASE_URL=postgres://vif:vif@veriform-postgres:5432/veriform_build
```

`$DATABASE_URL` and `$TEST_DATABASE_URL` are already set to it. Use them; do not
compose your own.

`localhost` / `127.0.0.1` / `host.docker.internal` DO NOT WORK. The host publishes
Postgres on loopback only, deliberately. You reach it as `veriform-postgres` over
the shared Docker network, and that is the only route.

Three databases exist on that server. Only one is yours:

| Database         | Whose            | May you write to it? |
|------------------|------------------|----------------------|
| `veriform_build` | yours            | Yes. Disposable.     |
| `veriform`       | the developer's dev data | **No.** Destroying it destroys real work. |
| `veriform_test`  | the developer's `npm test` | **No.** You would truncate tables mid-run under them. |

Migrations, run from `Backend/`:

```
node --import tsx ./scripts/migrate.ts     # == npm run db:migrate
```

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

When that happens: record it in `.cline_context/.build_issues.md`, mark the
DB-backed gates **BLOCKED — database unreachable**, and move on to work that does
not need a database. Do not report a blocked gate as passed, and do not spend
iterations trying to conjure a database. A skipped gate is not a passed gate.

## The other services

Same rule — running already, reached by container name, never installed.

| Service | Address from here | Notes |
|---------|-------------------|-------|
| Zitadel (identity) | `http://zitadel:8080` | Issuer is `http://localhost:8083`. Own Postgres, `veriform-zitadel-postgres` — never the app's. |
| Zitadel login UI | `http://zitadel-login:3000` | Host `:3100`. |
| OpenFGA (authorisation) | `http://openfga:8080` | Host `:8892`. Model: `npm run fga:apply`, tests `npm run test:fga`. |
| PgBouncer | `veriform-pgbouncer:6432` | Transaction pooling, for `npm run test:rls` only. Needs `npm run pool:config` first. |
| Mailpit (SMTP capture) | `mailpit:1025`, UI `:8025` | Verification email is captured here, never sent. |
| dnsmasq | `dnsmasq:53` | Host `127.0.0.1:5354`. Answers domain-verification TXT lookups; NXDOMAINs everything else. |
