// Give the test gate the database CI gives every run: a fresh one.
//
// Run by entrypoint.sh (TEST_GATE_PREPARE) immediately before each test gate,
// inside a build that uses docker-compose.ephemeral-veriform.yml.
//
// WHY: the build agent runs tests against this same database while it works,
// and sometimes leaves a full `npm test` running in the background or kills one
// part-way. Rows those runs leave behind (an organisation still referenced by
// an employer_verification_request, say) were still there when the gate ran,
// and seed.test.ts audits the whole database, so the gate failed on code that
// passes in CI, where the database is always new. KAN-142 and KAN-143 both
// stopped this way on 2026-11-20 (leaked row created 18 min before the gate).
//
// WHAT: stop any test runner still going, then drop and recreate both
// databases exactly as veriform-init.sql left them. The roles are cluster-wide
// and survive, so only the per-database grants are repeated. `npm test`
// re-applies the migrations itself (pretest-migrate), as it does in CI.

import { execSync } from "node:child_process";
import { createRequire } from "node:module";

try {
  execSync("pkill -f 'vitest' || true", { stdio: "ignore" });
} catch {
  // Nothing running: the usual case.
}

const require = createRequire("/workspace/Backend/package.json");
const pg = require("pg");
const base = "postgres://vif:vif@localhost:5432";

const admin = new pg.Client({ connectionString: `${base}/postgres` });
await admin.connect();
for (const db of ["veriform_test", "veriform"]) {
  await admin.query(`DROP DATABASE IF EXISTS ${db} WITH (FORCE)`);
  await admin.query(`CREATE DATABASE ${db} OWNER vif`);
  await admin.query(`GRANT CREATE ON DATABASE ${db} TO veriform_owner`);
}
await admin.end();

const test = new pg.Client({ connectionString: `${base}/veriform_test` });
await test.connect();
await test.query("GRANT CREATE, USAGE ON SCHEMA public TO veriform_owner");
await test.end();

console.log("veriform_test and veriform recreated empty for the test gate.");
