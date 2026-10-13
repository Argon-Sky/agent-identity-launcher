"""Optional real-server proof without account credentials or a model turn.

ARGON_TEST_CODEX="$(command -v codex)" python3 -m unittest discover -s tests -p test_codex_live.py -v

Requires websocat. Uses a new short /tmp home and stops only that home's daemon. Normal test runs
skip this file; installing Codex is not required for the offline suite or CI.
"""

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
CODEX = os.environ.get("ARGON_TEST_CODEX")


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
        result = self.command("app-server", "daemon", "stop")
        self.assertEqual(result.returncode, 0, "could not stop the isolated test daemon")

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


if __name__ == "__main__":
    unittest.main()
