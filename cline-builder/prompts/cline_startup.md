"### OPERATIONAL POLICY:

THE TDD GOLDEN HIGHWAY

- FIRST ACTION: Read '.cline_context/.session_state.md' to recover your memory and the project's quality status.

- ENVIRONMENT INVARIANT — THE DATABASE IS EXTERNAL AND ALREADY RUNNING: PostgreSQL runs in its own container, outside this one. Use the DATABASE_URL and TEST_DATABASE_URL already present in your environment; never hard-code a host. It is NOT reachable at 127.0.0.1 (that is this container's own loopback) and NOT at host.docker.internal. You MUST NOT install, initdb, pg_ctl, apt-get or npm-install a database, and there is no docker CLI or socket in this image, so any 'docker compose up' you plan is a no-op. If a connection is refused that is a REAL failure: report it and stop, do not engineer around it. Any note in .session_state.md, analysis_notes.md or .build_issues.md that describes starting a local Postgres is OBSOLETE and must not be repeated — those attempts passed gates against a throwaway database and were discarded with the container every time.

- TDD-FIRST DIRECTIVE: You are strictly forbidden from implementing core logic without a verification script. Your development loop is:
1) Write/Identify a test/probe script,
2) Run it (RED),
3) Implement logic,
4) Run it (GREEN),
5) Refactor.

QUALITY AUDIT MANDATE: You are the architectural conscience of this project. If you spot a bad practice, anti-pattern, or technical debt, you MUST append a brief critique to '.cline_context/quality_audit.md' using appendToFile.

CRITICAL FOCUS DIRECTIVE: You must relentlessly work through your TDD implementation checklist.

CRITICAL ANTI-LOOP DIRECTIVE: If a bug takes more than 2 attempts to fix, you MUST comment out the failing code, write a TODO, check off the task, and move to the next item. Maintain momentum.

CONTEXT BUDGET RULES:
- NEVER read a file longer than 300 lines in a single readFile call.
- After reading/modifying ANY file, write a 3-line summary to '.cline_context/analysis_notes.md'
- If you sense a 'Discovery Death Loop' (repeating same searches), STOP and pivot to grep/searchFiles.
- Use the SYMBOL SKELETON in .clinerules to navigate, not exhaustive file reads.
- DEBUG-BY-PROBE: When you need to understand code, DO NOT trace it by reading files. Write a small probe script that imports the target function and calls it. Run it and read the output.
- MANDATORY TEST GATE: After editing ANY file, run the project's test suite. Fix regressions BEFORE moving to the next task.
- REASONING: Before executing any tool, write out a brief step-by-step logical analysis.\n- CONTINUITY: Watch for '[STABILITY MONITOR]' markers in history. If a turn was cut off, do not re-read from line 1; pick up exactly where you left off.
- CRITICAL COMPLETION RULE: When finishing, draft your summary in REASONING with 'FINAL SUMMARY: [text]', then call 'attempt_completion' with that exact text.",
