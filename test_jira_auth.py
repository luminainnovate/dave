"""
Behaviour tests for `jira_auth.py` - Dave's OAuth sign-in to Jira Cloud.

Only `_http` is faked: it is the one thing that would otherwise reach
Atlassian. The token store, the refresh rotation and the browser callback
server are the real code, run against a throwaway folder and a local port.
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
import urllib.request

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
_spec = importlib.util.spec_from_file_location("jira_auth", os.path.join(_here, "jira_auth.py"))
jira_auth = importlib.util.module_from_spec(_spec)
sys.modules["jira_auth"] = jira_auth
_spec.loader.exec_module(jira_auth)

FAILURES = []
SITE = {"id": "cloud-123", "url": "https://luminainnovate.atlassian.net"}


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class FakeAtlassian:
    """Stands in for `_http`, answering from a script and recording each call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": body})
        if not self.answers:
            raise AssertionError(f"unexpected call {method} {url}")
        return self.answers.pop(0)


def workspace(store=None, env_lines=None):
    root = tempfile.mkdtemp(prefix="jira_auth_test_")
    env_path = os.path.join(root, ".env")
    with open(env_path, "w") as f:
        f.write("\n".join(env_lines or [
            "JIRA_BASE_URL=https://luminainnovate.atlassian.net",
            "JIRA_CLIENT_ID=cid",
            "JIRA_CLIENT_SECRET=csecret",
        ]) + "\n")
    if store is not None:
        with open(os.path.join(root, jira_auth.STORE_NAME), "w") as f:
            json.dump(store, f)
    return root, env_path


def stored(root):
    with open(os.path.join(root, jira_auth.STORE_NAME)) as f:
        return json.load(f)


def signed_in(expires_in):
    return {"access_token": "old-access", "refresh_token": "old-refresh",
            "expires_at": time.time() + expires_in, "cloud_id": SITE["id"],
            "site_url": SITE["url"]}


def with_fake(fake, fn):
    real = jira_auth._http
    jira_auth._http = fake
    try:
        return fn()
    finally:
        jira_auth._http = real


def test_authorize_url_asks_for_a_refresh_token():
    url = jira_auth.authorize_url("cid", "st8")
    check("offline_access requested (no refresh token without it)", "offline_access" in url)
    check("write scope requested (Dave moves issues and comments)", "write%3Ajira-work" in url)
    check("redirect is the local callback", "localhost%3A8765%2Fcallback" in url)
    check("state carried", "state=st8" in url)


def test_load_env_reads_dotenv_shapes():
    root, env_path = workspace(env_lines=[
        "# comment", "", "export JIRA_CLIENT_ID=abc", "JIRA_CLIENT_SECRET=\"q=uoted\"",
        "JIRA_BASE_URL='https://x.atlassian.net'"])
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("JIRA_")}
    try:
        env = jira_auth.load_env(env_path)
    finally:
        os.environ.update(saved)
    check("export prefix dropped", env.get("JIRA_CLIENT_ID") == "abc", env)
    check("quotes stripped, inner = kept", env.get("JIRA_CLIENT_SECRET") == "q=uoted", env)
    check("single quotes stripped", env.get("JIRA_BASE_URL") == "https://x.atlassian.net", env)
    check("store sits beside .env",
          jira_auth.store_path(env_path, env) == os.path.join(root, jira_auth.STORE_NAME))


def test_fresh_token_is_reused_without_calling_atlassian():
    _root, env_path = workspace(store=signed_in(3600))
    fake = FakeAtlassian()
    token = with_fake(fake, lambda: jira_auth.JiraAuth.from_env(env_path).access_token())
    check("cached token returned", token == "old-access", token)
    check("no network call", fake.calls == [], fake.calls)


def test_expiring_token_is_refreshed_and_rotation_saved():
    root, env_path = workspace(store=signed_in(30))  # inside the expiry margin
    fake = FakeAtlassian((200, {"access_token": "new-access", "refresh_token": "new-refresh",
                                "expires_in": 3600, "scope": "read:jira-work"}))
    auth = jira_auth.JiraAuth.from_env(env_path)
    token = with_fake(fake, auth.access_token)
    check("new access token returned", token == "new-access", token)
    body = fake.calls[0]["body"] if fake.calls else {}
    check("refresh grant used with the old refresh token",
          body.get("grant_type") == "refresh_token" and body.get("refresh_token") == "old-refresh",
          body)
    check("client credentials sent", body.get("client_id") == "cid"
          and body.get("client_secret") == "csecret", body)
    data = stored(root)
    check("rotated refresh token saved to disk", data.get("refresh_token") == "new-refresh", data)
    check("site kept across refresh", data.get("cloud_id") == SITE["id"], data)
    mode = stat.S_IMODE(os.stat(os.path.join(root, jira_auth.STORE_NAME)).st_mode)
    check("store is owner-only (0600)", mode == 0o600, oct(mode))
    check("api base targets the cloud id",
          auth.api_base == f"https://api.atlassian.com/ex/jira/{SITE['id']}", auth.api_base)


def test_refresh_done_by_another_process_is_used_not_repeated():
    root, env_path = workspace(store=signed_in(3600))
    auth = jira_auth.JiraAuth.from_env(env_path)
    auth.access_token()  # this process now holds old-access / old-refresh in memory
    # Another process (the orchestrator) refreshes: old-refresh is spent.
    with open(os.path.join(root, jira_auth.STORE_NAME), "w") as f:
        json.dump({**signed_in(3600), "access_token": "their-access",
                   "refresh_token": "their-refresh"}, f)
    fake = FakeAtlassian()
    token = with_fake(fake, lambda: auth.access_token(force_refresh=True))
    check("the other process's fresh token is adopted", token == "their-access", token)
    check("the spent refresh token is never sent", fake.calls == [], fake.calls)

    # Expired on disk too: refresh with the token on DISK, not the stale copy.
    with open(os.path.join(root, jira_auth.STORE_NAME), "w") as f:
        json.dump({**signed_in(-10), "refresh_token": "disk-refresh"}, f)
    fake = FakeAtlassian((200, {"access_token": "a3", "refresh_token": "r3", "expires_in": 3600}))
    with_fake(fake, lambda: auth.access_token(force_refresh=True))
    sent = fake.calls[0]["body"]["refresh_token"] if fake.calls else None
    check("refresh uses the refresh token on disk", sent == "disk-refresh", sent)
    check("lock file sits beside the store",
          os.path.exists(os.path.join(root, jira_auth.STORE_NAME + ".lock")))


def test_refused_refresh_says_to_log_in_and_keeps_store():
    root, env_path = workspace(store=signed_in(-10))
    before = stored(root)
    fake = FakeAtlassian((403, {"error": "unauthorized_client",
                                "error_description": "refresh_token is invalid"}))
    try:
        with_fake(fake, jira_auth.JiraAuth.from_env(env_path).access_token)
        check("refusal raises", False)
    except jira_auth.JiraAuthError as e:
        check("refusal raises JiraAuthError naming login", "jira_auth.py login" in str(e), str(e))
        check("Atlassian's reason passed on", "refresh_token is invalid" in str(e), str(e))
    check("store untouched on failure", stored(root) == before)


def test_not_signed_in_says_to_log_in():
    _root, env_path = workspace()
    try:
        jira_auth.JiraAuth.from_env(env_path).access_token()
        check("missing store raises", False)
    except jira_auth.JiraAuthError as e:
        check("missing store names login", "jira_auth.py login" in str(e), str(e))


def test_missing_client_credentials_named():
    _root, env_path = workspace(store=signed_in(-10),
                               env_lines=["JIRA_BASE_URL=https://luminainnovate.atlassian.net"])
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("JIRA_")}
    try:
        jira_auth.JiraAuth.from_env(env_path).access_token()
        check("no client id raises", False)
    except jira_auth.JiraAuthError as e:
        check("message names JIRA_CLIENT_ID", "JIRA_CLIENT_ID" in str(e), str(e))
    finally:
        os.environ.update(saved)


def test_pick_site_matches_base_url():
    other = {"id": "cloud-999", "url": "https://elsewhere.atlassian.net"}
    fake = FakeAtlassian((200, [other, SITE]))
    site = with_fake(fake, lambda: jira_auth.pick_site("tok", "https://LuminaInnovate.atlassian.net/"))
    check("site chosen by JIRA_BASE_URL, case and slash ignored", site == SITE, site)
    check("bearer token sent", fake.calls[0]["headers"].get("Authorization") == "Bearer tok")

    fake = FakeAtlassian((200, [other]))
    try:
        with_fake(fake, lambda: jira_auth.pick_site("tok", SITE["url"]))
        check("uncovered site raises", False)
    except jira_auth.JiraAuthError as e:
        check("uncovered site named with what is covered", "elsewhere" in str(e), str(e))

    fake = FakeAtlassian((200, [other, SITE]))
    try:
        with_fake(fake, lambda: jira_auth.pick_site("tok", ""))
        check("ambiguous site raises", False)
    except jira_auth.JiraAuthError as e:
        check("ambiguity asks for JIRA_BASE_URL", "JIRA_BASE_URL" in str(e), str(e))


def test_request_retries_a_401_once_with_a_forced_refresh():
    _root, env_path = workspace(store=signed_in(3600))
    fake = FakeAtlassian(
        (401, {"message": "revoked"}),
        (200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}),
        (200, {"displayName": "Agent Dave"}),
    )
    status, me = with_fake(fake, lambda: jira_auth.JiraAuth.from_env(env_path)
                           .request("GET", "/rest/api/3/myself"))
    check("second attempt succeeds", status == 200 and me.get("displayName") == "Agent Dave", me)
    check("retry used the refreshed token",
          fake.calls[-1]["headers"].get("Authorization") == "Bearer new-access", fake.calls[-1])
    check("REST call went to the cloud-id base",
          fake.calls[0]["url"] == f"https://api.atlassian.com/ex/jira/{SITE['id']}/rest/api/3/myself",
          fake.calls[0]["url"])


def _hit_callback(query):
    time.sleep(0.3)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{jira_auth.CALLBACK_PORT}/callback?{query}",
                               timeout=5).read()
    except OSError:
        pass  # a 400 (HTTPError, an OSError) for the refused case is expected


def test_callback_returns_code_and_rejects_wrong_state():
    t = threading.Thread(target=_hit_callback, args=("code=abc&state=good",))
    t.start()
    code = jira_auth._wait_for_callback("good", timeout=10)
    t.join()
    check("code taken from a matching callback", code == "abc", code)

    t = threading.Thread(target=_hit_callback, args=("code=abc&state=forged",))
    t.start()
    try:
        jira_auth._wait_for_callback("good", timeout=10)
        check("forged state refused", False)
    except jira_auth.JiraAuthError as e:
        check("forged state refused", "state mismatch" in str(e), str(e))
    t.join()


def test_pasted_callback_address():
    good = "http://localhost:8765/callback?state=good&code=xyz"
    check("code read from a pasted address",
          jira_auth.code_from_pasted_url(f"  {good}\n", "good") == "xyz")
    for pasted, needle in (
            ("http://localhost:8765/callback?state=old&code=xyz", "state mismatch"),
            ("http://localhost:8765/callback?state=good", "no `code=`"),
            (("http://localhost:8765/callback?state=good&error=access_denied"
              "&error_description=User+did+not+authorize"), "did not authorize")):
        try:
            jira_auth.code_from_pasted_url(pasted, "good")
            check(f"refused: {needle}", False)
        except jira_auth.JiraAuthError as e:
            check(f"refused: {needle}", needle in str(e), str(e))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception:  # noqa: BLE001 - report every test, not just the first to crash
            import traceback
            traceback.print_exc()
            FAILURES.append(t.__name__)
    print("\n" + ("-" * 60))
    print("FAILED:" if FAILURES else "ALL PASSED", len(FAILURES) or "")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1 if FAILURES else 0)
