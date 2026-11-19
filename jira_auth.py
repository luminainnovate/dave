"""
Jira Cloud access for Agent DAVE, through OAuth 2.0 (3LO) rather than an API token.

API tokens on the luminainnovate site are refused with a bare 401, however they
are made, so Dave signs in the way Atlassian recommends for a long-running
service: an OAuth app the owner approves once in a browser. What that approval
leaves behind is a refresh token, kept in a 0600 file next to `.env`. Dave
trades it for a short-lived access token whenever the last one is about to run
out.

Atlassian rotates refresh tokens: every refresh returns a new one and the old
one stops working soon after. The new one is written to disk before the access
token is handed out, so a crash between the two cannot lock Dave out.

Setup, once:
  1. developer.atlassian.com/console/myapps -> Create -> OAuth 2.0 integration.
     Permissions -> Jira API: read:jira-work, write:jira-work, read:jira-user.
     Authorization -> callback URL http://localhost:8765/callback
  2. Put the app's Client ID and Secret in `.env`:
       JIRA_CLIENT_ID=...
       JIRA_CLIENT_SECRET=...
     JIRA_BASE_URL (https://<site>.atlassian.net) picks the site when the
     account can reach more than one.
  3. python3 jira_auth.py login       # opens the browser, saves the token
     python3 jira_auth.py check       # proves it works: prints who Dave is

Standard library only, so `login` runs from any Python without a venv.
"""
import argparse
import fcntl
import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
API_ROOT = "https://api.atlassian.com/ex/jira"

# offline_access is what makes Atlassian issue a refresh token at all.
SCOPES = ("read:jira-work", "write:jira-work", "read:jira-user", "offline_access")

CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

STORE_NAME = ".jira_oauth.json"

# Refresh this long before the access token's stated expiry, so a request
# started just before it runs out does not arrive with a dead token.
EXPIRY_MARGIN_SECS = 120

HTTP_TIMEOUT = 30


class JiraAuthError(Exception):
    """Dave cannot get a working token. The message says what a human must do."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def default_env_path() -> str:
    return os.environ.get("JIRA_ENV_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path: str) -> dict:
    """KEY=VALUE lines from `path`, then the process environment on top."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    except FileNotFoundError:
        pass
    for key in ("JIRA_CLIENT_ID", "JIRA_CLIENT_SECRET", "JIRA_BASE_URL", "JIRA_OAUTH_STORE"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def store_path(env_path: str, env: dict) -> str:
    return env.get("JIRA_OAUTH_STORE") or os.path.join(
        os.path.dirname(os.path.abspath(env_path)), STORE_NAME)


def _client(env: dict) -> tuple:
    client_id = env.get("JIRA_CLIENT_ID", "")
    secret = env.get("JIRA_CLIENT_SECRET", "")
    if not client_id or not secret:
        raise JiraAuthError(
            "JIRA_CLIENT_ID and JIRA_CLIENT_SECRET are not set. Copy them from the "
            "OAuth app's Settings page at developer.atlassian.com/console/myapps "
            "into .env.")
    return client_id, secret


# --------------------------------------------------------------------------- #
# HTTP (one function, so tests can replace it)
# --------------------------------------------------------------------------- #

def _http(method: str, url: str, headers: dict | None = None, body: dict | None = None) -> tuple:
    """(status, parsed JSON or {}). Never raises for an HTTP error status."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except urllib.error.URLError as e:
        raise JiraAuthError(f"Could not reach {urllib.parse.urlparse(url).netloc}: {e.reason}")
    try:
        return status, (json.loads(raw) if raw else {})
    except ValueError:
        return status, {"raw": raw[:300].decode("utf-8", "replace")}


def _error_text(payload: dict) -> str:
    return (payload.get("error_description") or payload.get("error")
            or payload.get("message") or payload.get("raw") or "no detail")


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #

def read_store(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise JiraAuthError(f"{path} is not valid JSON ({e}). Run `python3 jira_auth.py login`.")


def write_store(path: str, data: dict) -> None:
    """Atomic, and readable by the owner only: it holds a live refresh token."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".jira_oauth.", dir=folder)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# The OAuth exchange
# --------------------------------------------------------------------------- #

def authorize_url(client_id: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    })
    return f"{AUTHORIZE_URL}?{query}"


def _token_request(body: dict) -> dict:
    status, payload = _http("POST", TOKEN_URL, body=body)
    if status != 200 or not payload.get("access_token"):
        raise JiraAuthError(f"Atlassian refused the token request (HTTP {status}): "
                            f"{_error_text(payload)}")
    if not payload.get("refresh_token"):
        raise JiraAuthError("Atlassian sent no refresh token. The app needs the "
                            "offline_access scope; run `python3 jira_auth.py login` again.")
    return payload


def exchange_code(client_id: str, secret: str, code: str) -> dict:
    return _token_request({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": secret,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })


def pick_site(access_token: str, base_url: str = "") -> dict:
    """The Jira site this token reaches: the one at `base_url` when given."""
    status, payload = _http("GET", RESOURCES_URL,
                            headers={"Authorization": f"Bearer {access_token}"})
    if status != 200 or not isinstance(payload, list):
        raise JiraAuthError(f"Could not list the sites this approval covers (HTTP {status}).")
    if not payload:
        raise JiraAuthError("The approval covers no Jira site. Run login again and pick "
                            "the site on Atlassian's consent screen.")
    want = base_url.rstrip("/").lower()
    if want:
        for site in payload:
            if (site.get("url") or "").rstrip("/").lower() == want:
                return site
        names = ", ".join(s.get("url", "?") for s in payload)
        raise JiraAuthError(f"The approval does not cover {base_url} (it covers {names}). "
                            f"Run login again and pick that site on the consent screen.")
    if len(payload) > 1:
        names = ", ".join(s.get("url", "?") for s in payload)
        raise JiraAuthError(f"The approval covers several sites ({names}). Set "
                            f"JIRA_BASE_URL in .env to choose one.")
    return payload[0]


def _record(tokens: dict, site: dict) -> dict:
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        "scope": tokens.get("scope", ""),
        "cloud_id": site["id"],
        "site_url": site.get("url", ""),
    }


# --------------------------------------------------------------------------- #
# What the rest of Dave uses
# --------------------------------------------------------------------------- #

class _ProcessLock:
    """An exclusive lock file, held for the length of a `with` block."""

    def __init__(self, path: str):
        self._path = path
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)


class JiraAuth:
    """
    Hands out a live access token and the REST base for the approved site.

        auth = JiraAuth.from_env()
        url = f"{auth.api_base}/rest/api/3/issue/KAN-140"
        headers = {"Authorization": f"Bearer {auth.access_token()}"}

    The thread lock keeps two threads in a process from spending the same
    refresh token, which Atlassian would treat as a replay; a lock file next to
    the store does the same for two processes sharing it.
    """

    def __init__(self, env: dict, path: str):
        self._env = env
        self._path = path
        self._lock = threading.Lock()
        self._data = None

    @classmethod
    def from_env(cls, env_path: str | None = None) -> "JiraAuth":
        env_path = env_path or default_env_path()
        env = load_env(env_path)
        return cls(env, store_path(env_path, env))

    def _load(self) -> dict:
        if self._data is None:
            data = read_store(self._path)
            if not data.get("refresh_token") or not data.get("cloud_id"):
                raise JiraAuthError(
                    f"Dave is not signed in to Jira ({self._path} is missing or "
                    f"incomplete). Run `python3 jira_auth.py login` once.")
            self._data = data
        return self._data

    @property
    def cloud_id(self) -> str:
        return self._load()["cloud_id"]

    @property
    def site_url(self) -> str:
        return self._load().get("site_url", "")

    @property
    def api_base(self) -> str:
        return f"{API_ROOT}/{self.cloud_id}"

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            data = self._load()
            fresh = data.get("expires_at", 0) - EXPIRY_MARGIN_SECS > time.time()
            if fresh and not force_refresh and data.get("access_token"):
                return data["access_token"]
            # Another process (the orchestrator, a CLI) shares the store. Across
            # the refresh: one process at a time, and each starts from what is on
            # disk, never from its own copy - the refresh token in that copy may
            # already have been spent and replaced by the other process.
            with _ProcessLock(self._path + ".lock"):
                stale = data.get("access_token")
                self._data = None
                data = self._load()
                fresh = data.get("expires_at", 0) - EXPIRY_MARGIN_SECS > time.time()
                if fresh and data.get("access_token") and (
                        not force_refresh or data["access_token"] != stale):
                    return data["access_token"]
                return self._refresh(data)

    def _refresh(self, data: dict) -> str:
        """Spend the refresh token for a new pair. Call with both locks held."""
        client_id, secret = _client(self._env)
        try:
            tokens = _token_request({
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": secret,
                "refresh_token": data["refresh_token"],
            })
        except JiraAuthError as e:
            raise JiraAuthError(f"{e} The saved sign-in may have expired or been "
                                f"revoked: run `python3 jira_auth.py login`.") from e
        updated = {**data, **_record(tokens, {"id": data["cloud_id"],
                                              "url": data.get("site_url", "")})}
        # Saved before use: the old refresh token is dead from here on.
        write_store(self._path, updated)
        self._data = updated
        return updated["access_token"]

    def request(self, method: str, path: str, body: dict | None = None) -> tuple:
        """(status, JSON) for a Jira REST path such as `/rest/api/3/myself`.
        A 401 is retried once with a forced refresh: the token may have been
        revoked server-side before its stated expiry."""
        url = f"{self.api_base}{path}"
        for attempt in (0, 1):
            token = self.access_token(force_refresh=attempt == 1)
            status, payload = _http(method, url,
                                    headers={"Authorization": f"Bearer {token}"}, body=body)
            if status != 401:
                return status, payload
        return status, payload


# --------------------------------------------------------------------------- #
# CLI: login once, check any time
# --------------------------------------------------------------------------- #

def _wait_for_callback(state: str, timeout: int) -> str:
    """Serve one request on the callback port and return the `code` in it."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch (a stale or forged callback)"
            elif "error" in q:
                result["error"] = q.get("error_description", q["error"])[0]
            else:
                result["code"] = q.get("code", [""])[0]
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Agent DAVE is signed in to Jira. You can close this tab."
                              if ok else f"Sign-in failed: {result['error']}").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    server.timeout = 1
    deadline = time.time() + timeout
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" in result:
        return result["code"]
    raise JiraAuthError(result.get("error") or f"No browser callback within {timeout}s.")


def code_from_pasted_url(pasted: str, state: str) -> str:
    """The `code` in a callback address copied from the browser's address bar.

    For when the browser cannot reach this machine's port 8765 (signing in from
    another computer, or over SSH): Atlassian still redirects there, the page
    fails to load, and the address bar holds everything the callback would have
    received."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query)
    if "error" in q:
        raise JiraAuthError(q.get("error_description", q["error"])[0])
    if q.get("state", [""])[0] != state:
        raise JiraAuthError("state mismatch: that address is from a different sign-in "
                            "attempt. Use the address from this run's link.")
    code = q.get("code", [""])[0]
    if not code:
        raise JiraAuthError("no `code=` in the pasted address. Copy the whole address "
                            "after Atlassian redirects to localhost:8765/callback.")
    return code


def login(env_path: str, timeout: int = 300, paste: bool = False) -> dict:
    env = load_env(env_path)
    client_id, secret = _client(env)
    state = secrets.token_urlsafe(24)
    url = authorize_url(client_id, state)
    print("Open this link and approve Agent DAVE for your Jira site:\n")
    print(f"  {url}\n")
    if not paste:
        try:
            webbrowser.open(url)
        except webbrowser.Error as e:
            print(f"(Could not open a browser: {e}. Open the link by hand.)")
        print(f"Waiting for the callback on {REDIRECT_URI} ...")
        try:
            code = _wait_for_callback(state, timeout)
        except OSError as e:
            print(f"Cannot listen on port {CALLBACK_PORT} ({e}); falling back to paste.")
            paste = True
    if paste:
        print("After you click Accept, the browser goes to localhost:8765/callback and "
              "may show 'site can't be reached'. That is fine: copy the whole address "
              "from the address bar and paste it here.")
        code = code_from_pasted_url(input("Address: "), state)
    tokens = exchange_code(client_id, secret, code)
    site = pick_site(tokens["access_token"], env.get("JIRA_BASE_URL", ""))
    record = _record(tokens, site)
    path = store_path(env_path, env)
    write_store(path, record)
    return {"store": path, "site_url": record["site_url"], "cloud_id": record["cloud_id"]}


def check(env_path: str) -> str:
    auth = JiraAuth.from_env(env_path)
    status, me = auth.request("GET", "/rest/api/3/myself")
    if status != 200:
        raise JiraAuthError(f"Jira answered HTTP {status}: {_error_text(me)}")
    return f"{me.get('displayName', '?')} on {auth.site_url}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sign Agent DAVE in to Jira Cloud.")
    parser.add_argument("command", choices=("login", "check"))
    parser.add_argument("--env", default=default_env_path(),
                        help="the .env holding JIRA_CLIENT_ID / JIRA_CLIENT_SECRET")
    parser.add_argument("--paste", action="store_true",
                        help="login: paste the callback address instead of listening on "
                             f"port {CALLBACK_PORT} (browser on another machine)")
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            info = login(args.env, paste=args.paste)
            print(f"\nSigned in to {info['site_url']} (cloud id {info['cloud_id']}).")
            print(f"Saved to {info['store']}.")
        print(f"Jira sees Dave as: {check(args.env)}")
        return 0
    except JiraAuthError as e:
        print(f"\nJira sign-in failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
