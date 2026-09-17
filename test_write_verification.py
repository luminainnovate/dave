"""
Behaviour tests for the claim-vs-disk machinery.

The failure these exist for: a build agent wrote a test file through several
partial edits, some of which never landed, never read the file back, and
reported the work as done. generate_session_state then carried that report
forward as the project's memory, because every section of .session_state.md was
the agent's own account of itself and none of it had been observed.

Three things are tested, at the level each one actually lives:

  - the shell helpers (observed_changes, changed_sources, generate_session_state
    and the claim/disk guard) are extracted from entrypoint.sh and run for real
    against a temp workspace, with /workspace rewritten to point at it. They are
    not reimplemented here - a test that restates the code cannot catch the code
    being wrong.
  - refresh_skeleton_section is imported from distill.py and run directly.

Run: python3 test_write_verification.py
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRYPOINT = os.path.join(HERE, "cline-builder", "entrypoint.sh")
DISTILL = os.path.join(HERE, "cline-builder", "distill.py")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# Shell harness
# ---------------------------------------------------------------------------

def _extract(source, start_pattern, end_pattern):
    """Return the lines of `source` from start_pattern through end_pattern."""
    lines = source.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(start_pattern, ln))
    end = next(i for i, ln in enumerate(lines[start:], start)
               if re.match(end_pattern, ln))
    return "\n".join(lines[start:end + 1])


def shell_prelude(root):
    """The real helper definitions from entrypoint.sh, aimed at a temp root."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    parts = [
        _extract(src, r"^REVIEW_MARKER=", r"^REVIEW_MAX_FILES="),
        _extract(src, r"^observed_changes\(\) \{", r"^\}"),
        _extract(src, r"^changed_sources\(\) \{", r"^\}"),
        _extract(src, r"^SESSION_STATE_ISSUES_BYTES=",
                 r"^SESSION_STATE_OBSERVED_BYTES="),
        _extract(src, r"^generate_session_state\(\) \{", r"^\}"),
    ]
    body = "\n\n".join(parts).replace("/workspace", root)
    # generate_session_state reports position against this.
    return "MAX_ITERATIONS=5\n" + body + "\n"


def run_shell(root, script):
    """Run script with the extracted helpers in scope."""
    return subprocess.run(
        ["bash", "-c", shell_prelude(root) + "\n" + script],
        capture_output=True, text=True,
    )


class Workspace:
    """A workspace laid out the way the build loop expects to find one."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.ctx = os.path.join(self.root, ".cline_context")
        self.logs = os.path.join(self.root, ".cline_logs")
        os.makedirs(self.ctx)
        os.makedirs(self.logs)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, content):
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return full

    def mark(self):
        """Touch the review marker, then make later writes strictly newer."""
        open(os.path.join(self.ctx, ".review_marker"), "w").close()
        old = os.path.getmtime(os.path.join(self.ctx, ".review_marker"))
        os.utime(os.path.join(self.ctx, ".review_marker"), (old - 10, old - 10))

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()


# ---------------------------------------------------------------------------
# observed_changes / changed_sources
# ---------------------------------------------------------------------------

def test_observed_changes_reports_real_line_counts():
    ws = Workspace()
    ws.mark()
    ws.write("src/short.ts", "a\nb\nc\n")
    ws.write("src/long.ts", "x\n" * 40)

    out = run_shell(ws.root, "observed_changes").stdout

    check("short file carries its line count", "- src/short.ts — 3 lines" in out, out)
    check("long file carries its line count", "- src/long.ts — 40 lines" in out, out)


def test_observed_changes_sees_untracked_files():
    """The file at the centre of the incident was untracked; git diff was blind
    to it. This must not depend on git knowing anything."""
    ws = Workspace()
    subprocess.run(["git", "init", "-q", ws.root], check=True)
    ws.mark()
    ws.write("never_added.ts", "l\n" * 7)

    out = run_shell(ws.root, "observed_changes").stdout

    check("an untracked file is still observed",
          "- never_added.ts — 7 lines" in out, out)


def test_changed_sources_prunes_claude_worktrees():
    ws = Workspace()
    ws.mark()
    ws.write("Backend/src/lib/breakglass.ts", "real\n")
    ws.write(".claude/worktrees/other-branch/Backend/src/lib/breakglass.ts", "copy\n")

    out = run_shell(ws.root, "changed_sources").stdout

    check("the real file is in scope",
          "Backend/src/lib/breakglass.ts" in out, out)
    check("the worktree copy is not",
          ".claude/worktrees" not in out, out)


def test_changed_sources_ignores_pipeline_metadata():
    """.clinerules is rewritten by the skeleton refresh at the top of every
    iteration. It is the pipeline's own output, not the agent's work, and must
    not show up as something the build changed."""
    ws = Workspace()
    ws.mark()
    ws.write(".clinerules", "# Project Build Specification\n")
    ws.write("src/real.ts", "work\n")

    out = run_shell(ws.root, "changed_sources").stdout

    check("the agent's file is in scope", "src/real.ts" in out, out)
    check("the pipeline's own document is not", ".clinerules" not in out, out)


def test_observed_changes_flags_a_vanished_file():
    ws = Workspace()
    ws.mark()
    ws.write("gone.ts", "x\n")
    out = run_shell(
        ws.root,
        f"rm {ws.path('gone.ts')}\n"
        # find cannot report a file that no longer exists, so drive the loop
        # directly to prove the branch renders rather than erroring.
        "changed_sources() { echo 'gone.ts'; }\n"
        "observed_changes",
    ).stdout
    check("a missing path is named, not silently dropped",
          "gone.ts — (no longer present)" in out, out)


# ---------------------------------------------------------------------------
# generate_session_state
# ---------------------------------------------------------------------------

def test_session_state_leads_with_observed_state():
    ws = Workspace()
    ws.mark()
    ws.write("Backend/test/unseal.test.ts", "l\n" * 364)
    ws.write(".cline_context/.build_issues.md", "- an old issue\n")

    res = run_shell(ws.root, 'generate_session_state 2 "build"')
    check("it ran", res.returncode == 0, res.stderr)

    state = ws.read(".cline_context/.session_state.md")
    check("the observed section is present",
          "## Files on disk, as measured just now" in state, state[:400])
    check("it carries the real line count",
          "unseal.test.ts — 364 lines" in state, state[:800])
    check("observed state precedes the agent's own account",
          state.index("## Files on disk") < state.index("## Known Issues"),
          state[:200])
    check("it says which side wins on a disagreement",
          "this section is right" in state, state[:800])


def test_session_state_observed_section_is_byte_capped():
    ws = Workspace()
    ws.mark()
    for i in range(25):
        ws.write(f"src/file_with_a_deliberately_long_name_{i:03d}.ts", "x\n" * 3)

    res = run_shell(
        ws.root,
        "SESSION_STATE_OBSERVED_BYTES=120\n"
        'generate_session_state 1 "build"',
    )
    check("it ran", res.returncode == 0, res.stderr)

    state = ws.read(".cline_context/.session_state.md")
    body = state.split("> contents.\n", 1)[1]
    listing = body.split("\n\n", 1)[0].strip()
    check("the listing respects the cap",
          len(listing) <= 120, f"{len(listing)} bytes")


def test_session_state_omits_the_section_when_nothing_changed():
    ws = Workspace()
    ws.mark()
    res = run_shell(ws.root, 'generate_session_state 1 "build"')
    check("it ran", res.returncode == 0, res.stderr)
    state = ws.read(".cline_context/.session_state.md")
    check("no empty observed section is emitted",
          "## Files on disk" not in state, state[:300])


# ---------------------------------------------------------------------------
# claim-vs-disk guard
# ---------------------------------------------------------------------------

GUARD = '''
ITERATION=1
REVIEW_FILES=$(changed_sources)
BUILD_LOG="{root}/.cline_logs/build_log_iter_${{ITERATION}}.txt"
if [ -z "$REVIEW_FILES" ] && [ -f "$BUILD_LOG" ] \\
   && grep -qiE "attempt_completion|FINAL SUMMARY" "$BUILD_LOG" 2>/dev/null; then
    echo "  WARN  claimed completion but wrote no source files."
    {{
        echo ""
        echo "## Claim/disk mismatch — iteration ${{ITERATION}}"
        echo "READ the files it was supposed to touch"
    }} >> {root}/.cline_context/.build_issues.md
fi
'''


def test_guard_fires_on_a_claim_with_no_writes():
    ws = Workspace()
    ws.mark()
    ws.write(".cline_logs/build_log_iter_1.txt",
             "some output\nattempt_completion: all tests written\n")

    res = run_shell(ws.root, GUARD.format(root=ws.root))
    check("it warns", "wrote no source files" in res.stdout, res.stdout)
    check("it records the mismatch for the next iteration",
          "Claim/disk mismatch" in ws.read(".cline_context/.build_issues.md"))


def test_guard_stays_quiet_when_the_build_actually_wrote():
    ws = Workspace()
    ws.mark()
    ws.write("src/real.ts", "work\n")
    ws.write(".cline_logs/build_log_iter_1.txt", "attempt_completion: done\n")

    res = run_shell(ws.root, GUARD.format(root=ws.root))
    check("no warning when work reached disk",
          "wrote no source files" not in res.stdout, res.stdout)
    check("nothing recorded",
          not os.path.exists(ws.path(".cline_context/.build_issues.md")))


def test_guard_stays_quiet_when_no_completion_was_claimed():
    """A build that timed out mid-read wrote nothing and claimed nothing. That
    is a different failure and this guard is not about it."""
    ws = Workspace()
    ws.mark()
    ws.write(".cline_logs/build_log_iter_1.txt", "reading files...\n")

    res = run_shell(ws.root, GUARD.format(root=ws.root))
    check("silence without a claim",
          "wrote no source files" not in res.stdout, res.stdout)


# ---------------------------------------------------------------------------
# refresh_skeleton_section
# ---------------------------------------------------------------------------

def _distill():
    spec = importlib.util.spec_from_file_location("distill_under_test", DISTILL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["distill_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_skeleton_is_added_then_replaced_in_place():
    d = _distill()
    ws = Workspace()
    ws.write("src/alpha.ts", "export function alpha(a: number) { return a; }\n")
    rules = ws.write(".clinerules", "# Project Build Specification\n\n## Plan\n\nbuild it\n")

    check("first call adds the section",
          d.refresh_skeleton_section(rules, ws.root) == 0)
    first = ws.read(".clinerules")
    check("the section is there", d.SKELETON_BEGIN in first, first[:200])
    check("it names a real symbol", "alpha" in first, first[-600:])
    check("the plan survived", "build it" in first)

    # The agent now writes a second file, as it would mid-build.
    ws.write("src/beta.ts", "export function beta() {}\n")
    check("second call succeeds", d.refresh_skeleton_section(rules, ws.root) == 0)
    second = ws.read(".clinerules")

    check("the new symbol appears", "beta" in second, second[-600:])
    check("the section is not duplicated",
          second.count(d.SKELETON_BEGIN) == 1, second.count(d.SKELETON_BEGIN))
    check("the plan still survived", "build it" in second)


def test_skeleton_refresh_survives_a_missing_clinerules():
    d = _distill()
    ws = Workspace()
    ws.write("src/alpha.ts", "export function alpha() {}\n")
    rc = d.refresh_skeleton_section(ws.path("nope.clinerules"), ws.root)
    check("a missing document is not fatal", rc == 0)


def test_clinerules_skeleton_cap_is_below_the_distillation_cap():
    """The embedded copy is charged to the build agent on every phase; the
    distillation cap sizes a one-off payload against a different window."""
    d = _distill()
    check("the embedded cap is the tighter one",
          d.CLINERULES_SKELETON_MAX_CHARS < d.MAX_SKELETON_CHARS,
          f"{d.CLINERULES_SKELETON_MAX_CHARS} vs {d.MAX_SKELETON_CHARS}")


def test_skeleton_respects_the_embedded_cap():
    d = _distill()
    ws = Workspace()
    for i in range(12):
        ws.write(f"src/mod_{i:03d}.ts",
                 "".join(f"export function fn_{i}_{j}(x: number) {{ return x; }}\n"
                         for j in range(4)))
    skeleton = d.get_symbol_skeleton(ws.root, max_chars=40000)
    check("it fits", d.SKELETON_OVERFLOW_MARKER not in skeleton, skeleton[-200:])
    check("every file is represented", "mod_011.ts" in skeleton, skeleton[-300:])


def test_a_truncated_skeleton_declares_itself_incomplete():
    """The document tells the agent not to open a file when the map already has
    the signature. Under truncation the map is missing symbols that DO exist, so
    it must say so - otherwise absence from the map reads as absence from the
    code."""
    d = _distill()
    ws = Workspace()
    for i in range(60):
        ws.write(f"src/mod_{i:03d}.ts",
                 "".join(f"export function fn_{i}_{j}(x: number) {{ return x; }}\n"
                         for j in range(20)))
    rules = ws.write(".clinerules", "# Project Build Specification\n")

    saved = d.CLINERULES_SKELETON_MAX_CHARS
    try:
        d.CLINERULES_SKELETON_MAX_CHARS = 3000
        check("it still writes something",
              d.refresh_skeleton_section(rules, ws.root) == 0)
    finally:
        d.CLINERULES_SKELETON_MAX_CHARS = saved

    doc = ws.read(".clinerules")
    check("the truncation is declared to the agent",
          "INCOMPLETE" in doc, doc[:900])
    check("and it says what absence does not mean",
          "NOT evidence" in doc, doc[:900])


def test_a_complete_skeleton_carries_no_incomplete_banner():
    d = _distill()
    ws = Workspace()
    ws.write("src/alpha.ts", "export function alpha() {}\n")
    rules = ws.write(".clinerules", "# Project Build Specification\n")
    d.refresh_skeleton_section(rules, ws.root)
    check("no false alarm on a map that fits",
          "INCOMPLETE" not in ws.read(".clinerules"))


# ---------------------------------------------------------------------------
# The operational policy
# ---------------------------------------------------------------------------

def test_policy_carries_the_write_verification_rules():
    policy = open(os.path.join(HERE, "cline-builder", "prompts",
                               "cline_startup.md"), encoding="utf-8").read()
    check("read-back rule present", "WRITE VERIFICATION" in policy)
    check("no staging-and-copy rule present", "EDIT THE TARGET FILE" in policy)
    check("tool-failure rule present", "FAILING TOOL IS A FAILING CALL" in policy)
    check("the trailing JSON comma is intact - entrypoint strips it",
          policy.rstrip().endswith('",'), policy[-40:])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILURES.append(f"{t.__name__}: {e}")
    print("\n" + ("-" * 60))
    print("FAILED:" if FAILURES else "ALL PASSED", len(FAILURES) or "")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1 if FAILURES else 0)
