"""Optional real-server and terminal proof without account credentials or paid model calls.

ARGON_TEST_CODEX="$(command -v codex)" python3 -m unittest discover -s tests -p test_codex_live.py -v

Requires websocat. Uses a new short /tmp home and stops only that home's daemon. Normal test runs
skip this file; installing Codex is not required for the offline suite or CI.
"""

import fcntl
import http.server
import json
import os
from pathlib import Path
import pty
import select
import selectors
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
CODEX = os.environ.get("ARGON_TEST_CODEX")
CODEXBAR = os.environ.get("ARGON_TEST_CODEXBAR")


class Terminal:
    def __init__(self, command, environment, cwd):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 120, 0, 0))
        try:
            self.process = subprocess.Popen(command, env=dict(environment, TERM="xterm-256color"), cwd=cwd, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.output = b""

    def wait_for(self, text, timeout=15):
        deadline = time.monotonic() + timeout
        while text.encode() not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
                raise AssertionError("terminal timed out: " + repr(self.output[-2000:]))
            try:
                data = os.read(self.master, 65536)
            except OSError as error:
                raise AssertionError("terminal exited before expected text: " + repr(self.output[-2000:])) from error
            if not data:
                raise AssertionError("terminal disconnected before expected text: " + repr(self.output[-2000:]))
            self.output += data
            if b"\x1b[6n" in data:
                os.write(self.master, b"\x1b[1;1R")

    def close(self):
        # Release terminal output before waiting; the TUI can otherwise block while restoring it.
        if self.master is None:
            return
        os.close(self.master)
        self.master = None
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class LocalModel:
    """Keep a synthetic turn active so real clients can resume it without OpenAI access."""
    def __init__(self):
        self.received = threading.Event()
        self.release = threading.Event()
        model = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                model.received.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_synthetic_test","status":"in_progress","output":[]}}\n\n')
                self.wfile.flush()
                model.release.wait(60)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Proxy:
    def __init__(self, executable, socket, environment):
        self.process = subprocess.Popen([executable, "-t", "--ws-c-uri", "ws://localhost/", "-", "ws-c:unix:" + socket], env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.identifier = 0

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params):
        self.identifier += 1
        identifier = self.identifier
        self.send(dict(id=identifier, method=method, params=params))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise AssertionError("isolated Codex proxy disconnected")
                self.buffer += data
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            message = json.loads(line)
            if message.get("id") == identifier:
                if "error" in message:
                    # The test sends no credentials or user content.
                    raise AssertionError("Codex rejected " + method + ": " + json.dumps(message["error"]))
                return message["result"]
        raise AssertionError("isolated Codex proxy timed out during " + method)

    def initialize(self):
        self.request("initialize", {"clientInfo":{"name":"argon-launcher-live-test", "version":"1"}, "capabilities":{"experimentalApi":True}})
        self.send({"method":"initialized"})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()


@unittest.skipUnless(CODEX, "set ARGON_TEST_CODEX to run the isolated real-Codex proof")
class LiveCodexTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(Path(CODEX).is_absolute(), "ARGON_TEST_CODEX must be an absolute executable path")
        self.websocat = shutil.which("websocat")
        self.assertIsNotNone(self.websocat, "install websocat to run the real-Codex proof")
        temporary = tempfile.TemporaryDirectory(prefix="argon-live-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.codex_home = self.home / "codex"
        self.codex_home.mkdir(mode=0o700)
        tools = self.home / "bin"
        tools.mkdir()
        (tools / "gh").write_text("#!/bin/sh\nexit 97\n")
        (tools / "gh").chmod(0o755)
        # Allowlist avoids inheriting real authentication or Git/Codex configuration.
        self.env = {"PATH":str(tools) + os.pathsep + os.environ["PATH"], "HOME":str(self.home), "TMPDIR":str(self.home), "CODEX_HOME":str(self.codex_home), "ARGON_AGENT":"live-test", "ARGON_REAL_GH":str(tools / "gh"), "GIT_AUTHOR_NAME":"offline-test[bot]", "GIT_AUTHOR_EMAIL":"offline-test@example.invalid", "GIT_COMMITTER_NAME":"offline-test[bot]", "GIT_COMMITTER_EMAIL":"offline-test@example.invalid", "GH_CONFIG_DIR":str(self.home / "gh")}
        # Registered before start so even a later assertion failure stops our daemon.
        self.addCleanup(self.stop_daemon)
        result = self.command("app-server", "daemon", "start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.socket = json.loads(result.stdout)["socketPath"]

    def command(self, *arguments):
        return subprocess.run([CODEX, *arguments], env=self.env, capture_output=True, text=True, timeout=25)

    def stop_daemon(self):
        failed = True
        try:
            result = self.command("app-server", "daemon", "stop")
            self.assertEqual(result.returncode, 0, "could not stop the isolated test daemon")
            failed = False
        finally:
            # Codex 0.162.1 leaves its package updater running after daemon stop, even if the home is removed.
            # A failed stop must also release this test's daemon. Match only binaries installed in our home.
            prefix = str(self.codex_home / "packages/app-server-daemon/releases") + "/"
            processes = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
            for line in processes.splitlines():
                pid, command = line.strip().split(None, 1)
                if command.startswith(prefix) and (failed or command.endswith(" app-server daemon pid-update-loop")):
                    try:
                        os.kill(int(pid), signal.SIGKILL if failed else signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def verify_server(self, environment):
        script = 'set -euo pipefail\nAGENT=$ARGON_AGENT\ndie() { printf "argon-agent: %s\\n" "$*" >&2; exit 1; }\nsource "$1"\ncodex_remote_socket "$2" 1\n'
        return subprocess.run(["/bin/bash", "-c", script, "argon-live-test", str(REPO / "lib/codex-remote.sh"), CODEX], env=environment, capture_output=True, text=True, timeout=25)

    def test_actual_server_identity_matches_and_refuses_mismatched_client(self):
        result = self.verify_server(self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.socket)
        mismatched = dict(self.env, GIT_AUTHOR_EMAIL="wrong-client@example.invalid")
        result = self.verify_server(mismatched)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different GitHub identity", result.stderr)
        self.assertFalse(list(self.home.glob("argon-codex.*")))
        self.assertFalse((self.codex_home / "auth.json").exists())

    def test_actual_server_must_hold_the_launch_service_account_token(self):
        token = dict(self.env, OP_SERVICE_ACCOUNT_TOKEN="ops_synthetic_live_token")
        result = self.verify_server(token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not get the 1Password service account token", result.stderr)
        for policy in ("", "[shell_environment_policy]\nignore_default_excludes = false\n"):
            with self.subTest(policy=policy):
                self.stop_daemon()
                (self.codex_home / "config.toml").write_text(policy)
                result = subprocess.run([CODEX, "app-server", "daemon", "start"], env=token, capture_output=True, text=True, timeout=25)
                self.assertEqual(result.returncode, 0, result.stderr)
                for environment, accepted in ((token, not policy), (dict(token, OP_SERVICE_ACCOUNT_TOKEN="ops_synthetic_replaced_token"), False), (self.env, bool(policy))):
                    result = self.verify_server(environment)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)
                    if not accepted:
                        self.assertIn("do not get the 1Password service account token", result.stderr)
                    self.assertNotIn("ops_synthetic", result.stdout + result.stderr)

    def test_two_proxy_clients_share_a_loaded_thread_without_model_turn(self):
        first = Proxy(self.websocat, self.socket, self.env)
        self.addCleanup(first.close)
        second = Proxy(self.websocat, self.socket, self.env)
        self.addCleanup(second.close)
        first.initialize()
        second.initialize()
        started = first.request("thread/start", {"cwd":str(self.home), "approvalPolicy":"never", "sandbox":"danger-full-access", "ephemeral":False})
        thread_id = started["thread"]["id"]
        # Codex cannot resume before the first user message materializes storage.
        # Read the live backend instead: the second independent connection must
        # see the exact thread created by the first, with no model/API turn.
        loaded = second.request("thread/loaded/list", {})
        self.assertIn(thread_id, loaded["data"])
        read = second.request("thread/read", {"threadId":thread_id, "includeTurns":False})
        self.assertEqual(read["thread"]["id"], thread_id)
        self.assertEqual(read["thread"]["turns"], [])
        self.assertFalse((self.codex_home / "auth.json").exists())

    def test_management_refuses_mismatched_real_server_but_keeps_recovery_available(self):
        config = self.home / ".config/argon-agents"
        cache = self.home / ".cache/argon-agents"
        tokens = self.home / ("argon-agents-" + str(os.getuid()))
        for directory in (config, cache, tokens):
            directory.mkdir(mode=0o700, parents=True)
        (config / "live-test.env").write_text('ARGON_APP_ID="synthetic-test"\nARGON_KEY_REF="/does-not-exist"\nARGON_COMMAND=(' + shlex.quote(CODEX) + ')\nARGON_CODEX_REMOTE=1\nARGON_CODEX_HOME=' + shlex.quote(str(self.codex_home)) + '\n')
        # Deliberately different from the already-running server's identity.
        (cache / "live-test.meta").write_text('META_KEY="synthetic-test/"\nAPP_SLUG="different-test"\nINSTALLATION_ID="synthetic"\nBOT_EMAIL="different@example.invalid"\n')
        (tokens / "live-test.token").write_text(str(int(time.time()) + 3600) + " ghs_synthetic_offline_test\n")
        for arguments in (("remote-control",), ("remote-control", "pair"), ("app-server", "daemon", "enable-remote-control")):
            with self.subTest(arguments=arguments):
                result = subprocess.run(["/bin/bash", str(REPO / "bin/argon-agent"), "codex", "live-test", *arguments], env=self.env, cwd=self.home, capture_output=True, text=True, timeout=25)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("different GitHub identity", result.stderr)
                self.assertNotIn("ghs_synthetic_offline_test", result.stdout + result.stderr)
        result = subprocess.run(["/bin/bash", str(REPO / "bin/argon-agent"), "codex", "live-test", "app-server", "daemon", "version"], env=self.env, cwd=self.home, capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(self.home.glob("argon-codex.*")))
        self.assertFalse((self.codex_home / "auth.json").exists())

    def test_real_terminals_resume_active_thread_without_configured_permission_overrides(self):
        self.stop_daemon()
        model = LocalModel()
        self.addCleanup(model.close)
        (self.codex_home / "config.toml").write_text('model="synthetic-test"\nmodel_provider="local_test"\n[model_providers.local_test]\nname="Local test"\nbase_url="http://127.0.0.1:' + str(model.server.server_port) + '/v1"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\n')
        result = self.command("app-server", "daemon", "start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.socket = json.loads(result.stdout)["socketPath"]
        first = Proxy(self.websocat, self.socket, self.env)
        self.addCleanup(first.close)
        observer = Proxy(self.websocat, self.socket, self.env)
        self.addCleanup(observer.close)
        first.initialize()
        observer.initialize()
        started = first.request("thread/start", {"cwd":str(self.home), "approvalPolicy":"on-request", "sandbox":"workspace-write", "ephemeral":False})
        thread_id = started["thread"]["id"]
        prompt = "Synthetic local launcher test."
        turn = first.request("turn/start", {"threadId":thread_id, "input":[{"type":"text", "text":prompt}]})
        self.assertTrue(model.received.wait(10), "the turn must use only the loopback fake model")
        # Control: the real CLI must reproduce the error that the old fake missed.
        rejected = Terminal([CODEX, "--remote", "unix://" + self.socket, "--no-alt-screen", "--yolo", "resume", thread_id], self.env, self.home)
        self.addCleanup(rejected.close)
        rejected.wait_for("Permission overrides are not supported when resuming a remote task.")
        self.assertEqual(rejected.process.wait(timeout=5), 1)
        script = 'set -euo pipefail\nsource "$1"\nARGON_COMMAND=("$2" --no-alt-screen "$5")\ncodex_remote_run "$2" "$3" resume "$4"\n'
        terminals = []
        for flag in ("--yolo", "--approve-for-me"):
            terminal = Terminal(["/bin/bash", "-c", script, "argon-live-test", str(REPO / "lib/codex-remote.sh"), CODEX, self.socket, thread_id, flag], self.env, self.home)
            self.addCleanup(terminal.close)
            terminal.wait_for(prompt)
            terminals.append(terminal)
        for terminal in terminals:
            self.assertIsNone(terminal.process.poll(), "both terminals must remain attached")
        # The daemon was started elsewhere. A new terminal thread must use the invoking directory.
        invoking = self.home / "invoking project"
        invoking.mkdir()
        # This directory is empty and belongs to this fixture; approve only it for the terminal test.
        with (self.codex_home / "config.toml").open("a") as config:
            config.write('\n[projects.' + json.dumps(str(invoking)) + ']\ntrust_level="trusted"\n')
        new_prompt = "Synthetic caller directory test."
        new_script = 'set -euo pipefail\nsource "$1"\nARGON_COMMAND=("$2" --no-alt-screen --yolo)\ncodex_remote_run "$2" "$3" "$4"\n'
        new_terminal = Terminal(["/bin/bash", "-c", new_script, "argon-live-test", str(REPO / "lib/codex-remote.sh"), CODEX, self.socket, new_prompt], self.env, invoking)
        self.addCleanup(new_terminal.close)
        new_terminal.wait_for(new_prompt)
        deadline = time.monotonic() + 10
        new_threads = []
        while not new_threads and time.monotonic() < deadline:
            loaded = observer.request("thread/loaded/list", {})
            for identifier in loaded["data"]:
                summary = observer.request("thread/read", {"threadId":identifier, "includeTurns":False})["thread"]
                if identifier != thread_id and not summary.get("ephemeral") and summary.get("cwd") == str(invoking):
                    new_threads.append(identifier)
            if not new_threads:
                time.sleep(0.05)
        self.assertEqual(len(new_threads), 1)
        created = observer.request("thread/read", {"threadId":new_threads[0], "includeTurns":True})
        self.assertEqual(created["thread"]["cwd"], str(invoking))
        observer.request("turn/interrupt", {"threadId":new_threads[0], "turnId":created["thread"]["turns"][-1]["id"]})
        resumed = observer.request("thread/resume", {"threadId":thread_id})
        self.assertEqual(resumed["thread"]["id"], thread_id)
        self.assertEqual(resumed["thread"]["turns"][-1]["status"], "inProgress")
        self.assertEqual(resumed["approvalPolicy"], "on-request")
        self.assertEqual(resumed["sandbox"]["type"], "workspaceWrite")
        observer.request("turn/interrupt", {"threadId":thread_id, "turnId":turn["turn"]["id"]})
        self.assertFalse((self.codex_home / "auth.json").exists())

    @unittest.skipUnless(CODEXBAR, "set ARGON_TEST_CODEXBAR to test the optional terminal sidebar")
    def test_sidebar_tracks_two_terminals_on_the_same_identity_verified_daemon(self):
        self.assertTrue(Path(CODEXBAR).is_absolute())
        self.stop_daemon()
        model = LocalModel()
        self.addCleanup(model.close)
        (self.codex_home / "config.toml").write_text('model="synthetic-test"\nmodel_provider="local_test"\n[model_providers.local_test]\nname="Local test"\nbase_url="http://127.0.0.1:' + str(model.server.server_port) + '/v1"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\n[projects.' + json.dumps(str(self.home)) + ']\ntrust_level="trusted"\n')
        (self.codex_home / "herdr-codexbar.json").write_text(json.dumps({"command": CODEXBAR}))
        state = self.home / "herdr.json"
        herdr = self.home / "bin/herdr"
        herdr.write_text('''#!/usr/bin/env python3
import fcntl, json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_STATE"], "a+") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    f.seek(0)
    data = json.loads(f.read() or "{}")
    pane = data.setdefault(args[2], {"pane_id": args[2], "agent": "codex", "tokens": {}})
    if args[1] == "report-agent-session" and args[args.index("--source") + 1] == "herdr:codex":
        pane["agent_session"] = {"kind": "id", "value": args[args.index("--agent-session-id") + 1]}
    elif args[1] == "report-metadata":
        for i, arg in enumerate(args):
            if arg == "--token":
                key, _, value = args[i + 1].partition("=")
                pane["tokens"][key] = value
            elif arg == "--clear-token":
                pane["tokens"].pop(args[i + 1], None)
    f.seek(0)
    f.truncate()
    json.dump(data, f)
    print(json.dumps({"result": {"pane": pane}}))
''')
        herdr.chmod(0o755)
        # The daemon deliberately retains a dead pane. Each terminal has its own current context.
        self.env.update(HERDR_ENV="1", HERDR_PANE_ID="wOld:p1", HERDR_SOCKET_PATH=str(self.home / "herdr.sock"), HERDR_BIN_PATH=str(herdr), FAKE_HERDR_STATE=str(state), HERDR_CODEXBAR_STATE=str(self.home / "sidebar-state"))
        result = self.command("app-server", "daemon", "start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.socket = json.loads(result.stdout)["socketPath"]
        observer = Proxy(self.websocat, self.socket, self.env)
        self.addCleanup(observer.close)
        observer.initialize()

        def interrupt_threads():
            for identifier in observer.request("thread/loaded/list", {})["data"]:
                summary = observer.request("thread/read", {"threadId": identifier, "includeTurns": False})["thread"]
                if not summary.get("path"):
                    continue  # The identity probe also creates an ephemeral thread.
                thread = observer.request("thread/read", {"threadId": identifier, "includeTurns": True})["thread"]
                for turn in thread["turns"]:
                    if turn["status"] == "inProgress":
                        observer.request("turn/interrupt", {"threadId": identifier, "turnId": turn["id"]})

        self.addCleanup(interrupt_threads)
        script = 'set -euo pipefail\nsource "$1"\nARGON_COMMAND=("$2" --no-alt-screen --yolo)\ncodex_remote_run "$2" "$3" "$4"\n'
        terminals = []
        for pane in ("w1:p1", "w1:p2"):
            model.received.clear()
            prompt = "Synthetic sidebar test for " + pane
            terminal = Terminal(["/bin/bash", "-c", script, "argon-live-test", str(REPO / "lib/codex-remote.sh"), CODEX, self.socket, prompt], dict(self.env, HERDR_PANE_ID=pane), self.home)
            self.addCleanup(terminal.close)
            terminal.wait_for(prompt, timeout=30)
            self.assertTrue(model.received.wait(10), "wait for the synthetic turn to reach the local model")
            terminals.append(terminal)

        def sidebar_ready():
            try:
                # Read under the same lock as the fake Herdr writer.
                with state.open() as f:
                    fcntl.flock(f, fcntl.LOCK_SH)
                    data = json.load(f)
                if all(data.get(pane, {}).get("tokens", {}).get("hc_model") == "synthetic-test" for pane in ("w1:p1", "w1:p2")):
                    return data
            except (OSError, ValueError):
                pass

        deadline = time.monotonic() + 10
        data = sidebar_ready()
        while data is None and time.monotonic() < deadline:
            time.sleep(0.05)
            data = sidebar_ready()
        self.assertIsNotNone(data, "both terminal connections must publish their own model")
        self.assertNotIn("wOld:p1", data)
        threads = [data[pane]["agent_session"]["value"] for pane in ("w1:p1", "w1:p2")]
        self.assertEqual(len(set(threads)), 2)
        loaded = observer.request("thread/loaded/list", {})
        self.assertTrue(set(threads).issubset(loaded["data"]))
        for thread_id in threads:
            read = observer.request("thread/read", {"threadId": thread_id, "includeTurns": True})
            self.assertEqual(read["thread"]["cwd"], str(self.home))
            # An independent client can attach to the terminal's exact thread on the same daemon.
            resumed = observer.request("thread/resume", {"threadId": thread_id})
            self.assertEqual(resumed["thread"]["id"], thread_id)
            self.assertEqual(resumed["sandbox"]["type"], "dangerFullAccess")
        resume_script = 'set -euo pipefail\nsource "$1"\nARGON_COMMAND=("$2" --no-alt-screen --yolo)\ncodex_remote_run "$2" "$3" resume "$4"\n'
        resumed_terminal = Terminal(["/bin/bash", "-c", resume_script, "argon-live-test", str(REPO / "lib/codex-remote.sh"), CODEX, self.socket, threads[0]], dict(self.env, HERDR_PANE_ID="w1:p3"), self.home)
        self.addCleanup(resumed_terminal.close)
        resumed_terminal.wait_for("Synthetic sidebar test for w1:p1", timeout=30)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = sidebar_ready()
            if data and data.get("w1:p3", {}).get("tokens", {}).get("hc_model") == "synthetic-test":
                break
            time.sleep(0.05)
        self.assertEqual(data["w1:p3"]["agent_session"]["value"], threads[0])
        self.assertEqual(data["w1:p3"]["tokens"]["hc_model"], "synthetic-test")
        interrupt_threads()
        # The startup picker and /resume open additional connections to the same endpoint.
        picker_script = 'set -euo pipefail\nsource "$1"\nARGON_COMMAND=("$2" --no-alt-screen --yolo)\ncodex_remote_run "$2" "$3" resume\n'
        picker = Terminal(["/bin/bash", "-c", picker_script, "argon-live-test", str(REPO / "lib/codex-remote.sh"), CODEX, self.socket], dict(self.env, HERDR_PANE_ID="w1:p4"), self.home)
        self.addCleanup(picker.close)
        picker.wait_for("Synthetic sidebar test for", timeout=20)
        os.write(picker.master, b"\r")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = sidebar_ready()
            if data and data.get("w1:p4", {}).get("agent_session", {}).get("value") in threads:
                break
            time.sleep(0.05)
        self.assertIn(data.get("w1:p4", {}).get("agent_session", {}).get("value"), threads)
        resumed_terminal.output = b""
        os.write(resumed_terminal.master, b"/resume")
        resumed_terminal.wait_for("resume", timeout=10)
        os.write(resumed_terminal.master, b"\r")
        resumed_terminal.wait_for("Resume a previous session", timeout=20)
        resumed_terminal.wait_for("Synthetic sidebar test for", timeout=20)
        resumed_terminal.output = b""
        os.write(resumed_terminal.master, b"w1:p2")
        resumed_terminal.wait_for("w1:p2", timeout=10)
        os.write(resumed_terminal.master, b"\r")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = sidebar_ready()
            if data and data.get("w1:p3", {}).get("agent_session", {}).get("value") == threads[1]:
                break
            time.sleep(0.05)
        self.assertEqual(data["w1:p3"]["agent_session"]["value"], threads[1])
        self.assertEqual(self.verify_server(self.env).returncode, 0)
        terminals[0].close()
        loaded = observer.request("thread/loaded/list", {})
        self.assertIn(threads[0], loaded["data"], "closing a terminal must leave the daemon's thread available")
        self.assertIsNone(terminals[1].process.poll())
        self.assertIsNone(resumed_terminal.process.poll())
        self.assertFalse((self.codex_home / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
