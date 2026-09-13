"""
Behaviour tests for the build agent's Phase 2 preflight.

Extracts the three shell functions that decide whether Phase 2 may start and
runs them against a stub that impersonates a vLLM server, so the assertions are
against the real script text rather than a paraphrase of it.

The bug they pin down: with --enable-sleep-mode a vLLM engine offloads its
weights to CPU RAM, and while asleep it still answers /health AND still reports
its full max_model_len on /v1/models. The preflight asked only those two
questions, so a resumed !approve run printed

    ✓ Cline CTX: 131072 tokens confirmed by the vllm server
    [hook:agent_start]

and then hung forever on a completion no engine was ever going to serve.
"""
import http.server
import json
import os
import re
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRYPOINT = os.path.join(HERE, "cline-builder", "entrypoint.sh")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def shell_func(name):
    """Lift one top-level function out of entrypoint.sh, definition intact."""
    src = open(ENTRYPOINT, encoding="utf-8").read()
    m = re.search(r"^%s\(\) \{.*?^\}" % re.escape(name), src, re.S | re.M)
    if not m:
        raise AssertionError(f"{name}() not found in entrypoint.sh")
    return m.group(0)


class Server(http.server.BaseHTTPRequestHandler):
    """A vLLM that can be asleep, awake, or missing sleep mode entirely."""

    ctx = 131072
    sleeping = False          # True | False | None (endpoint absent -> 404)
    loads = []                # /internal/model/load payloads received

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": "qwen3.8-27b", "max_model_len": type(self).ctx}]})
        elif self.path == "/is_sleeping":
            if type(self).sleeping is None:
                self._send(404, {"detail": "Not Found"})
            else:
                self._send(200, {"is_sleeping": type(self).sleeping})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/internal/model/load":
            type(self).loads.append(body)
            # The real endpoint wakes the engine before reporting ready.
            type(self).sleeping = False if type(self).sleeping is not None else None
            self._send(200, {"status": "ok"})
        elif self.path == "/wake_up":
            type(self).sleeping = False
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"detail": "Not Found"})

    def log_message(self, *a):
        pass


def serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def run(funcs, call, base_url, provider="vllm", orch_url=None, extra=""):
    script = "\n".join([
        "set -u",
        f'CLINE_PROVIDER="{provider}"',
        f'CLINE_BASE_URL="{base_url}"',
        f'ORCHESTRATOR_URL="{orch_url or base_url}"',
        'CLINE_MODEL="qwen3.8-27b"',
        'CLINE_ASSUMED_CTX=128000',
        'CLINE_CTX=131072',
        'VLLM_API_KEY=""',
        'CLINE_CTX_ACTUAL=""',
        'PROPS_HTTP=""', 'PROPS_CTX=""', 'PROPS_SLEEPING=""',
        extra,
    ] + [shell_func(f) for f in funcs] + [call])
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


PREFLIGHT = ["vllm_props", "llamacpp_props", "assert_cline_ctx"]


def test_an_awake_engine_passes():
    srv, url = serve()
    Server.sleeping, Server.ctx = False, 131072
    try:
        rc, out = run(PREFLIGHT, "assert_cline_ctx", url)
    finally:
        srv.shutdown()
    check("a working engine is allowed through", rc == 0, out.strip()[-200:])
    check("and the window is reported confirmed", "131072 tokens confirmed" in out, out)


def test_a_sleeping_engine_is_refused():
    srv, url = serve()
    Server.sleeping, Server.ctx = True, 131072
    try:
        rc, out = run(PREFLIGHT, "assert_cline_ctx", url)
    finally:
        srv.shutdown()
    check("a sleeping engine is fatal", rc == 1, out.strip()[-200:])
    check("and is named as asleep, not as a window problem",
          "ASLEEP" in out and "confirmed" not in out, out.strip()[-300:])
    check("and the operator is told how to wake it", "/wake_up" in out, out)


def test_sleep_mode_off_is_not_read_as_asleep():
    srv, url = serve()
    Server.sleeping, Server.ctx = None, 131072      # /is_sleeping 404s
    try:
        rc, out = run(PREFLIGHT, "assert_cline_ctx", url)
    finally:
        srv.shutdown()
    check("an unanswerable question is not a positive answer", rc == 0,
          out.strip()[-200:])


def test_a_dead_socket_is_still_fatal():
    rc, out = run(PREFLIGHT, "assert_cline_ctx", "http://127.0.0.1:1")
    check("nothing listening is fatal", rc == 1, out.strip()[-120:])
    check("and says so plainly", "no model server answering" in out, out)


def test_a_short_window_is_still_fatal():
    srv, url = serve()
    Server.sleeping, Server.ctx = False, 32768
    try:
        rc, out = run(PREFLIGHT, "assert_cline_ctx", url)
    finally:
        srv.shutdown()
    check("a window under Cline's hardcoded assumption is fatal", rc == 1,
          out.strip()[-200:])
    check("and is reported as a window problem", "smaller than Cline assumes" in out, out)


def test_phase_2_brings_a_vllm_engine_up():
    srv, url = serve()
    Server.sleeping, Server.loads = True, []
    try:
        rc, out = run(["ensure_cline_model_loaded"], "ensure_cline_model_loaded", url)
        loads = list(Server.loads)
        slept_after = Server.sleeping
    finally:
        srv.shutdown()
    check("the loader runs for vllm", rc == 0, out.strip()[-200:])
    check("and asks the orchestrator to load the model", len(loads) == 1, loads)
    check("naming the provider it needs",
          loads and loads[0].get("provider") == "vllm", loads)
    check("which leaves the engine awake", slept_after is False, slept_after)


def test_the_preflight_order_survives_a_sleeping_start():
    """Together: the loader wakes it, so the assertion that follows passes."""
    srv, url = serve()
    Server.sleeping, Server.ctx, Server.loads = True, 131072, []
    try:
        rc, out = run(["ensure_cline_model_loaded"] + PREFLIGHT,
                      "ensure_cline_model_loaded && assert_cline_ctx", url)
    finally:
        srv.shutdown()
    check("a build starting against a sleeping engine recovers", rc == 0,
          out.strip()[-300:])
    check("and does not print the asleep verdict", "ASLEEP" not in out, out)


def test_ollama_is_left_alone():
    srv, url = serve()
    Server.loads = []
    try:
        rc, out = run(["ensure_cline_model_loaded"], "ensure_cline_model_loaded",
                      url, provider="ollama")
        loads = list(Server.loads)
    finally:
        srv.shutdown()
    check("ollama needs no orchestrator load", rc == 0 and loads == [], (rc, loads))


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
