"""
`!jira <STORY>`: work one Jira Story's subtasks to completion, one at a time.

The Story says what the work is for. Its subtasks are the units of delivery.
Each subtask goes through exactly the path a person drives by hand today:

    design pass (`!architect`, or `!bugfix` for a subtask labelled `bug`)
      -> approve -> build -> ship confirm -> CI gate -> merged and green

Every step a person approved by hand is now approved automatically, but only
when the gate that already guards it passes:

  * design   the document exists and is not marked UNVERIFIED
  * build    the build left changes and wrote `.build_complete` with VERIFIED
             and SAFE (the project's own tests passed)
  * ship     `ship.propose` accepts the branch
  * CI       every check passes on the PR and then on the base branch; a
             failure this change introduced gets a fix build, up to
             MAX_FIX_ROUNDS times

The first gate that fails stops the whole Story. The reason is commented on the
subtask, which is labelled `dave-blocked`, and no later subtask is started:
subtasks are ordered, and the next one may depend on this one.

Scope is the one Story given. Only its own subtasks are read, in board order.
A Story with no subtasks is itself the unit of work.

A subtask labelled `human` is work only a person can do (spending money,
production access, a sign-off). The run pauses before it, comments on it to
hand it over, and starts nothing after it. Once the person moves it to Done,
`!jira <STORY>` carries on past it.

Re-running `!jira <STORY>` resumes. Done and Cancelled subtasks are skipped,
and a subtask Dave had started (it carries the `agent-dave` label) is picked up
where it was: waiting on CI if its pull request is still open, else from its
design pass.

The orchestrator supplies an Engine (see `Engine` below), so this module has no
idea how containers, the GPU or GitHub work, and the tests can drive it without
any of them.
"""
import asyncio
import json
import logging
import os
import time

logger = logging.getLogger("jira_runner")

# --- Workflow vocabulary (veriform's statuses; override per site) -----------

def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


STATUS_IN_PROGRESS = _env("JIRA_STATUS_IN_PROGRESS", "In Progress")
STATUS_IN_REVIEW = _env("JIRA_STATUS_IN_REVIEW", "In Review")
STATUS_DONE = _env("JIRA_STATUS_DONE", "Done")
# Finished one way or another: never worked on.
SKIP_STATUSES = {s.strip().lower() for s in _env("JIRA_SKIP_STATUSES", "Done,Cancelled").split(",")}
# Parked for later (veriform's MVP+): skipped, and said so in the plan.
DEFERRED_STATUSES = {s.strip().lower() for s in _env("JIRA_DEFERRED_STATUSES", "MVP+").split(",")}

LABEL_ACTIVE = "agent-dave"      # Dave started this subtask
LABEL_BLOCKED = "dave-blocked"   # Dave stopped on it; a person is needed
BUGFIX_LABELS = {"bug", "bugfix"}
# A person does this subtask: the run pauses before it until it is Done.
LABEL_HUMAN = _env("JIRA_HUMAN_LABEL", "human").lower()


def needs_person(issue: dict) -> bool:
    return LABEL_HUMAN in {lbl.lower() for lbl in issue["labels"]}

MAX_FIX_ROUNDS = int(_env("JIRA_MAX_FIX_ROUNDS", "2"))

# How often the runner looks at a running build and at the CI gate.
POLL_SECS = 15
# After a build container exits, how long to wait for its ship marker before
# deciding the build left nothing (stopped on a blocker, or changed nothing).
BUILD_MARKER_GRACE_SECS = 60
# How long a just-launched build may take to show up in `docker inspect`.
# `docker compose run -d` returns before the container exists, and a build
# with ephemeral services waits for their health checks first. Until the
# container has been seen running, not seeing it means "not started yet".
BUILD_START_GRACE_SECS = int(_env("JIRA_BUILD_START_GRACE_SECS", "900"))


def _now() -> float:
    """The runner's clock; tests replace it."""
    return time.monotonic()

# Longest design document excerpt copied into a Jira comment.
DESIGN_EXCERPT_CHARS = 3000

RUN_FILE = "jira_run.json"

# Ship states (mirrors ship.py; repeated so this module imports nothing heavy).
S_READY, S_CHECKS, S_BASE = "ready", "awaiting_checks", "awaiting_base_ci"
S_FAILED, S_FIXING, S_STALLED, S_INTEGRATED = "ci_failed", "fixing", "stalled", "integrated"


class Blocked(Exception):
    """A gate failed. The message is the reason, for Jira and for chat."""


class Engine:
    """
    What the runner needs from the orchestrator. Every method may block for a
    long time; all are awaited.

    design(messages, pass_key) -> (document_text_or_None, chat_text)
        Run the design pass and wait for it. The document is None when the pass
        did not produce a buildable one (no document, or UNVERIFIED).
    build(messages, pass_key) -> (container_name_or_None, chat_text)
        Start the approved build. Returns once the container is launched.
    container_running(name) -> bool
    read_marker() -> dict or None          the workspace's ship marker
    build_passed_gates() -> bool           `.build_complete` has VERIFIED and SAFE
    ship(title) -> (ok, chat_text)         propose with this title, then confirm
    watch_ci() -> None                     make sure the CI gate is being polled
    fix_ci(messages) -> (ok, chat_text)    the `!approve` for a CI failure
    note(text) -> None                     an event shown above the next chat reply
    wait() -> None                         the pause between two polls
    """
    workspace = ""

    async def wait(self) -> None:
        await asyncio.sleep(POLL_SECS)


def unit_plan(story: dict, subtasks: list) -> list:
    """
    [(issue, action, why)] in order. action is "work", "resume", "skip" or "stop".
    "stop" means a person is on this subtask, or it is labelled `human` and a
    person must do it: nothing at or after it is touched.
    """
    if not subtasks:
        return [(story, "work", "the Story has no subtasks, so it is the unit of work")]
    plan, stopped = [], False
    for sub in subtasks:
        status = sub["status"].lower()
        if stopped:
            plan.append((sub, "skip", "after a subtask Dave cannot take"))
        elif status in SKIP_STATUSES:
            plan.append((sub, "skip", sub["status"]))
        elif status in DEFERRED_STATUSES:
            plan.append((sub, "skip", f"{sub['status']} (deferred)"))
        elif needs_person(sub):
            plan.append((sub, "stop", f"{sub['status']}, labelled {LABEL_HUMAN}: "
                                      f"a person does this one"))
            stopped = True
        elif LABEL_ACTIVE in sub["labels"] and status not in ("to do",):
            plan.append((sub, "resume", f"{sub['status']}, started by Dave"))
        elif sub["status_category"] == "new":
            plan.append((sub, "work", sub["status"]))
        else:
            plan.append((sub, "stop", (f"{sub['status']} and not started by Dave: "
                                       f"someone else is on it")))
            stopped = True
    return plan


def plan_text(story: dict, plan: list, browse) -> str:
    icons = {"work": "▶️", "resume": "⏯️", "skip": "⏭️", "stop": "🛑"}
    lines = [f"**{story['key']} — {story['summary']}** ({story['status']})",
             browse(story["key"]), ""]
    for issue, action, why in plan:
        lines.append(f"{icons[action]} `{issue['key']}` {issue['summary']} — *{why}*")
    todo = [p for p in plan if p[1] in ("work", "resume")]
    stop = next((p[0] for p in plan if p[1] == "stop"), None)
    lines.append("")
    if stop and needs_person(stop):
        lines.append((f"Dave would deliver {len(todo)} unit(s), then pause at `{stop['key']}`: "
                      if todo else f"⏸️ `{stop['key']}` comes first and ")
                     + f"it needs a person. Move it to Done once it is done, "
                       f"and Dave carries on past it.")
    elif stop:
        lines.append(f"Dave would deliver {len(todo)} unit(s), then stop at `{stop['key']}`. "
                     f"Move it to Done, or back to To Do, for Dave to carry on past it."
                     if todo else
                     f"🛑 `{stop['key']}` comes first and someone else has it, so Dave "
                     f"would do nothing. Move it to Done, or back to To Do, first.")
    else:
        lines.append(f"{len(todo)} unit(s) to deliver, one at a time." if todo else
                     "Nothing to do: every subtask is finished or deferred.")
    return "\n".join(lines)


def pass_for(issue: dict) -> str:
    labels = {lbl.lower() for lbl in issue["labels"]}
    return "bugfix" if (labels & BUGFIX_LABELS or issue["type"].lower() == "bug") else "architect"


def unit_prompt(story: dict, issue: dict) -> str:
    """The message the design pass reads in place of a pasted ticket."""
    if issue["key"] == story["key"]:
        return "\n\n".join([
            f"# {story['key']}: {story['summary']}",
            story["description"] or "(The Story has no description.)",
        ])
    return "\n\n".join([
        f"# {issue['key']}: {issue['summary']}",
        (f"This is one subtask of the Story {story['key']}. Deliver ONLY this "
         f"subtask. Later subtasks of the Story are delivered separately; do not "
         f"start them."),
        "## This subtask",
        issue["description"] or "(No description beyond the title.)",
        f"## Background: the Story {story['key']} — {story['summary']}",
        story["description"] or "(The Story has no description.)",
    ])


def ship_title(issue: dict) -> str:
    return f"{issue['key']}: {issue['summary']}"[:72]


class StoryRun:
    """One `!jira <STORY>` run. Create, then `await run()`."""

    def __init__(self, jira, engine: Engine, story_key: str, anchor_message: str):
        self.jira = jira
        self.engine = engine
        self.story_key = story_key.upper()
        # The conversation's first user message. The orchestrator finds a chat's
        # workspace by hashing it, so every message list the runner builds
        # starts with it.
        self.anchor = anchor_message
        self.state = {"story": self.story_key, "status": "starting", "current": "",
                      "phase": "", "done": [], "started": time.time(), "reason": ""}

    # -- bookkeeping -------------------------------------------------------

    def _save(self, **changes) -> None:
        self.state.update(changes, updated=time.time())
        path = os.path.join(self.engine.workspace, ".cline_context", RUN_FILE)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except OSError as e:
            logger.warning(f"Could not save the Jira run state: {e}")

    async def _jira(self, method: str, *args):
        """A Jira write. Failing to tell Jira is logged, never fatal to the work."""
        try:
            return await asyncio.to_thread(getattr(self.jira, method), *args)
        except Exception as e:  # noqa: BLE001 - no Jira trouble may stop the delivery
            logger.error(f"Jira {method}{args[:1]} failed: {e}")
            self.engine.note(f"⚠️ Jira update failed ({method} {args[0] if args else ''}): {e}")
            return None

    def _messages(self, prompt: str, command: str) -> list:
        return [{"role": "user", "content": self.anchor},
                {"role": "user", "content": prompt},
                {"role": "user", "content": command}]

    # -- the run -------------------------------------------------------------

    async def run(self) -> str:
        try:
            story = await asyncio.to_thread(self.jira.issue, self.story_key)
            refusal = self._refuse(story)
            if refusal:
                self._save(status="refused", reason=refusal)
                return refusal
            subtasks = await asyncio.to_thread(self.jira.children, self.story_key)
            plan = unit_plan(story, subtasks)
            # unit_plan skips everything after a "stop", so these all come before it.
            units = [(i, a) for i, a, _ in plan if a in ("work", "resume")]
            stop_at = next((i for i, a, _ in plan if a == "stop"), None)

            self._save(status="running", plan=[(i["key"], a, w) for i, a, w in plan])
            self.engine.note(f"🎫 Jira run started for {self.story_key}: "
                             f"{len(units)} unit(s) to deliver.")
            if units and story["status"].lower() not in SKIP_STATUSES:
                await self._jira("transition", self.story_key, STATUS_IN_PROGRESS)

            for issue, action in units:
                await self._deliver(story, issue, resume=(action == "resume"))
                self.state["done"].append(issue["key"])
                self._save()

            return await self._finish(story, plan, stop_at)

        except Blocked as e:
            reason = str(e)
            key = self.state.get("current") or self.story_key
            await self._jira("add_label", key, LABEL_BLOCKED)
            await self._jira("comment", key,
                             f"🛑 Agent DAVE stopped on {key} and will not start later "
                             f"subtasks of {self.story_key}.\n\n{reason}\n\n"
                             f"When this is resolved, run !jira {self.story_key} again: "
                             f"finished subtasks are skipped.")
            self._save(status="blocked", reason=reason)
            self.engine.note(f"🛑 Jira run for {self.story_key} stopped on {key}: {reason[:300]}")
            return f"🛑 **Stopped on `{key}`.** {reason}"
        except asyncio.CancelledError:
            key = self.state.get("current") or self.story_key
            await self._jira("comment", key, "⏹️ Agent DAVE's run was stopped by hand (!jira stop). "
                                             "Anything already running (a build, a CI gate) "
                                             "carries on until it finishes.")
            self._save(status="stopped", reason="stopped by !jira stop")
            raise
        except Exception as e:
            logger.exception("Jira run failed")
            self._save(status="error", reason=str(e))
            self.engine.note(f"❌ Jira run for {self.story_key} failed: {e}")
            return f"❌ **Jira run failed:** {e}"

    def _refuse(self, story: dict) -> str:
        if story["is_subtask"]:
            return (f"🛑 `{story['key']}` is a subtask. Give Dave its Story instead "
                    f"(`!jira {story['parent']}`): the Story sets the order its subtasks "
                    f"are delivered in.")
        if story["hierarchy"] and story["hierarchy"] > 0:
            return (f"🛑 `{story['key']}` is an {story['type']}. Dave works one Story at a "
                    f"time; give it one of the Stories under this {story['type']}.")
        if story["status"].lower() in SKIP_STATUSES:
            return (f"🛑 `{story['key']}` is {story['status']}. Move it back to To Do or "
                    f"{STATUS_IN_PROGRESS} if its remaining subtasks should still be delivered.")
        return ""

    async def _finish(self, story: dict, plan: list, stop_at) -> str:
        if stop_at and needs_person(stop_at):
            key = stop_at["key"]
            msg = (f"Delivered {len(self.state['done'])} unit(s). Paused before {key}: "
                   f"it is labelled {LABEL_HUMAN}, so a person does it.")
            await self._jira("comment", key,
                             f"⏸️ Agent DAVE paused {self.story_key} here. This subtask "
                             f"needs a person, so Dave will not start it or anything after "
                             f"it.\n\nWhen it is done, move it to Done and run "
                             f"!jira {self.story_key} again.")
            await self._jira("comment", self.story_key, f"⏸️ Agent DAVE: {msg}")
            self._save(status="paused", reason=msg, waiting_on=key)
            self.engine.note(f"⏸️ Jira run for {self.story_key} is waiting on a person: "
                             f"{key} {stop_at['summary']}")
            return (f"⏸️ **Waiting on a person.** {msg} When it is done, move it to Done "
                    f"and run `!jira {self.story_key}` again.")
        if stop_at:
            msg = (f"Delivered {len(self.state['done'])} unit(s). Stopped before "
                   f"{stop_at['key']}: it is {stop_at['status']} and Dave did not start it.")
            await self._jira("comment", self.story_key, f"⏸️ Agent DAVE: {msg}")
            self._save(status="paused", reason=msg)
            return f"⏸️ **{msg}**"
        # Re-read: a person may have changed a subtask while Dave worked.
        subtasks = await asyncio.to_thread(self.jira.children, self.story_key)
        open_ = [s for s in subtasks if s["status"].lower() not in SKIP_STATUSES]
        done = ", ".join(self.state["done"]) or "nothing new"
        if not open_ and self.state["done"]:
            await self._jira("transition", self.story_key, STATUS_DONE)
            await self._jira("comment", self.story_key,
                             f"✅ Agent DAVE delivered every subtask ({done}). Story moved to Done.")
            self._save(status="complete")
            return f"✅ **{self.story_key} complete.** Delivered: {done}."
        left = ", ".join(f"{s['key']} ({s['status']})" for s in open_) or "none"
        self._save(status="complete", reason=f"left open: {left}")
        return f"✅ **Run finished for {self.story_key}.** Delivered: {done}. Still open: {left}."

    # -- one unit -------------------------------------------------------------

    async def _deliver(self, story: dict, issue: dict, resume: bool) -> None:
        key = issue["key"]
        self._save(current=key, phase="start")
        marker = self.engine.read_marker() or {}
        if resume and marker.get("state") in (S_CHECKS, S_BASE, S_FAILED, S_FIXING, S_STALLED):
            self.engine.note(f"⏯️ {key}: resuming at the CI gate.")
            await self._through_ci(story, issue)
            return
        if marker.get("state") in (S_CHECKS, S_BASE, S_FAILED, S_FIXING, S_STALLED):
            raise Blocked(f"Another pull request is still going through the CI gate "
                          f"({marker.get('pr_url') or marker.get('branch')}, {marker.get('state')}). "
                          f"Finish or close it first (`!ship status`).")

        pass_key = pass_for(issue)
        await self._jira("transition", key, STATUS_IN_PROGRESS)
        await self._jira("add_label", key, LABEL_ACTIVE)
        await self._jira("remove_label", key, LABEL_BLOCKED)
        await self._jira("comment", key, f"🤖 Agent DAVE started {key} ({pass_key} pass), "
                                         f"as part of {self.story_key}.")
        self.engine.note(f"▶️ {key}: {pass_key} pass started.")

        prompt = unit_prompt(story, issue)
        self._save(phase="design")
        document, text = await self.engine.design(self._messages(prompt, f"!{pass_key}"), pass_key)
        if not document:
            raise Blocked(f"The {pass_key} pass did not produce a design that can be "
                          f"approved:\n\n{_plain(text)[:1500]}")
        excerpt = document[:DESIGN_EXCERPT_CHARS]
        more = "\n\n(truncated)" if len(document) > DESIGN_EXCERPT_CHARS else ""
        await self._jira("comment", key, f"📐 Design approved automatically ({pass_key} pass):"
                                         f"\n\n{excerpt}{more}")

        self._save(phase="build")
        container, text = await self.engine.build(self._messages(prompt, "!approve"), pass_key)
        if not container:
            raise Blocked(f"The build did not start:\n\n{_plain(text)[:1000]}")
        await self._await_build(container)
        if not self.engine.build_passed_gates():
            raise Blocked("The build finished without passing its own gates: "
                          "`.build_complete` is missing VERIFIED and SAFE, so the project's "
                          "tests or the safety review did not pass. `!logs` has the detail.")

        self._save(phase="ship")
        ok, text = self.engine.ship(ship_title(issue))
        if not ok:
            raise Blocked(f"Shipping was refused:\n\n{_plain(text)[:1000]}")
        pr = (self.engine.read_marker() or {}).get("pr_url", "")
        await self._jira("transition", key, STATUS_IN_REVIEW)
        await self._jira("comment", key, f"🚦 Pull request open, waiting for CI before merging: {pr}")
        await self._through_ci(story, issue)

    async def _await_build(self, container: str) -> None:
        launched = _now()
        seen = False      # has the container been seen running yet?
        gone_since = None
        while True:
            marker = self.engine.read_marker() or {}
            if marker.get("state") == S_READY and marker.get("container") == container:
                return
            if await self.engine.container_running(container):
                seen, gone_since = True, None
            elif seen or _now() - launched > BUILD_START_GRACE_SECS:
                # Gone after running, or never appeared at all: give the
                # safety monitor its grace to write the ship marker, then stop.
                gone_since = gone_since or _now()
                if _now() - gone_since > BUILD_MARKER_GRACE_SECS:
                    if not seen:
                        raise Blocked(f"The build (`{container}`) never started: no such "
                                      f"container after {BUILD_START_GRACE_SECS // 60} "
                                      f"minutes. `!logs` shows why.")
                    raise Blocked(f"The build (`{container}`) ended without leaving anything "
                                  f"to ship: it stopped on a blocker, or changed nothing. "
                                  f"`!logs` shows why.")
            await self.engine.wait()

    async def _through_ci(self, story: dict, issue: dict) -> None:
        key = issue["key"]
        self._save(phase="ci")
        self.engine.watch_ci()
        seen_merge = False
        while True:
            marker = self.engine.read_marker()
            state = (marker or {}).get("state", "")
            if state == S_BASE:
                seen_merge = True
            if state == S_INTEGRATED or (not marker and seen_merge):
                pr = (marker or {}).get("pr_url", "")
                sha = (marker or {}).get("merge_sha", "")
                await self._jira("transition", key, STATUS_DONE)
                await self._jira("comment", key, f"✅ Merged and green on the base branch"
                                                 f"{' as ' + sha[:7] if sha else ''}. {pr}".strip())
                self.engine.note(f"✅ {key} delivered.")
                return
            if not marker:
                raise Blocked("The ship record disappeared before the merge was confirmed. "
                              "Check the pull request by hand.")
            if state == S_STALLED:
                raise Blocked(f"The CI gate needs a person: {marker.get('stall_reason', 'stalled')}.\n\n"
                              f"{_plain(marker.get('notice') or '')[:1200]}")
            if state == S_FAILED:
                rounds = int(marker.get("fix_attempts", 0))
                if rounds >= MAX_FIX_ROUNDS:
                    raise Blocked(f"CI is still failing after {rounds} fix round(s).\n\n"
                                  f"{_plain(marker.get('notice') or '')[:1200]}")
                self._save(phase=f"ci-fix-{rounds + 1}")
                ok, text = await self.engine.fix_ci(
                    self._messages(unit_prompt(story, issue), "!approve"))
                if not ok:
                    raise Blocked(f"CI failed and no fix build could be started:\n\n"
                                  f"{_plain(text)[:1200]}")
                await self._jira("comment", key, f"🔧 CI failed; fix round {rounds + 1} of "
                                                 f"{MAX_FIX_ROUNDS} started automatically.")
            elif state in (S_CHECKS, S_BASE):
                self.engine.watch_ci()
            await self.engine.wait()


def _plain(text: str) -> str:
    """Chat Markdown, readable as a Jira comment."""
    return (text or "").replace("**", "").replace("`", "").strip()


def read_run_state(workspace: str) -> dict | None:
    try:
        with open(os.path.join(workspace, ".cline_context", RUN_FILE), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
