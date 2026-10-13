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

    def wait_for(self, text):
        deadline = time.monotonic() + 15
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
        os.close(self.master)
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
        loaded = observer.request("thread/loaded/list", {})
        new_threads = [identifier for identifier in loaded["data"] if identifier != thread_id]
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


if __name__ == "__main__":
    unittest.main()
