"""Offline launcher regression tests, using real Bash and an isolated fake Codex.

Run with python3 -m unittest discover -s tests -v. No keys, network, or real
Codex state are accessed. Python is a test dependency only.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
OP_TOKEN = "ops_synthetic_service_account_token"
BASH = "/bin/bash"

FAKE_CODEX = r'''
import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
scenario = os.environ.get("FAKE_SCENARIO", "")
def log(event, **data):
    with open(os.environ["FAKE_LOG"], "a") as stream:
        stream.write(json.dumps(dict(event=event, **data)) + "\n")
if Path(sys.argv[0]).name == "websocat":
    log("transport", args=args)
    if args != ["-t", "--ws-c-uri", "ws://localhost/", "-", "ws-c:unix:" + os.environ["FAKE_SOCKET"]]:
        sys.exit(96)
    args = ["app-server", "proxy"]
log("call", args=args)
if args == ["--version"]:
    print("codex-cli " + os.environ.get("FAKE_VERSION", "0.162.1"))
elif args == ["--help"]:
    print("usage: codex " + ("" if scenario == "old_help" else "--no-daemon"))
    if scenario == "large_help":
        print("additional help\n" * 100000)
elif args == ["app-server", "daemon", "start"]:
    if scenario == "bad_lifecycle":
        print("not json")
    elif scenario == "relative_socket":
        print(json.dumps({"socketPath": "relative.sock"}))
    elif scenario == "start_failure":
        sys.exit(1)
    else:
        print(json.dumps({"socketPath": os.environ["FAKE_SOCKET"]}))
elif args[:2] == ["app-server", "proxy"]:
    for line in sys.stdin:
        request = json.loads(line)
        method = request["method"]
        log("rpc", method=method, params=request.get("params"))
        if "id" not in request:
            continue
        if scenario == "disconnect":
            break
        if scenario == "invalid_json":
            print("not json", flush=True)
            break
        if scenario == "rpc_error":
            print(json.dumps({"id":request["id"], "error":{"code":-1,"message":"synthetic failure"}}), flush=True)
            continue
        if scenario == "notifications":
            print(json.dumps({"method":"server/status", "params":{}}), flush=True)
            print(json.dumps({"id":999, "result":{}}), flush=True)
        if method == "command/exec":
            environment = os.environ.copy()
            if scenario == "wrong_identity":
                environment["GIT_AUTHOR_EMAIL"] = "different@example.invalid"
            if scenario == "stale_token":
                environment["GH_TOKEN"] = "synthetic-stale-token"
            if scenario == "stale_github_token":
                environment["GITHUB_TOKEN"] = "synthetic-stale-token"
            if scenario == "missing_op_token":
                environment.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
            if scenario == "replaced_op_token":
                environment["OP_SERVICE_ACCOUNT_TOKEN"] = "ops_synthetic_replaced_token"
            if scenario == "wrong_gh":
                environment["PATH"] = os.pathsep.join(part for part in environment["PATH"].split(os.pathsep) if not part.endswith("/argon-agents/shims"))
            completed = subprocess.run(request["params"]["command"], env=environment, capture_output=True, text=True)
            result = dict(exitCode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
        elif method == "remoteControl/enable":
            result = {"status":"denied" if scenario == "enable_denied" else "connected"}
        else:
            result = {}
        print(json.dumps({"id":request["id"], "result":result}), flush=True)
else:
    identity = {key:value for key,value in os.environ.items() if key.startswith("GIT_") or key in ("ARGON_AGENT", "ARGON_REAL_GH", "GH_CONFIG_DIR")}
    log("attached", args=args, identity=identity, codex_home=os.environ.get("CODEX_HOME"), sqlite_home=os.environ.get("CODEX_SQLITE_HOME"), tokens_present=any(key in os.environ for key in ("GH_TOKEN", "GITHUB_TOKEN")), op_token=os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"))
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="argon-launcher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.config = self.home / ".config/argon-agents"
        self.cache = self.home / ".cache/argon-agents"
        self.tools = self.home / "tools"
        self.tmp = self.home / "tmp"
        self.shims = self.home / ".local/share/argon-agents/shims"
        for directory in (self.config, self.cache, self.tools, self.tmp, self.shims):
            directory.mkdir(parents=True)
        self.log = self.home / "events.jsonl"
        self.env = {key:value for key,value in os.environ.items() if not key.startswith(("ARGON_", "GIT_", "CODEX_", "OP_")) and key not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME="", XDG_CACHE_HOME="", TMPDIR=str(self.tmp), PATH=str(self.tools) + os.pathsep + os.environ["PATH"], FAKE_LOG=str(self.log), FAKE_SOCKET=str(self.tmp / "server.sock"))
        self.write_tool("codex", FAKE_CODEX)
        self.write_tool("websocat", FAKE_CODEX)
        self.write_tool("other-agent", FAKE_CODEX)
        self.write_tool("gh", "import sys\nsys.exit(0)\n")
        # Unexpected credential/network access fails immediately instead of contacting GitHub.
        for tool in ("curl", "op"):
            self.write_tool(tool, "import sys\nsys.exit(97)\n")
        (self.shims / "gh").symlink_to(REPO / "bin/argon-agent")
        self.configure()

    def write_tool(self, name, source):
        target = self.tools / name
        target.write_text("#!" + sys.executable + "\n" + source)
        target.chmod(0o755)

    def configure(self, remote=0, command="codex", arguments=()):
        words = " ".join(shlex.quote(word) for word in (command, *arguments))
        (self.config / "test.env").write_text('ARGON_APP_ID="test-app"\nARGON_KEY_REF="/does-not-exist"\nARGON_COMMAND=(' + words + ')\nARGON_CODEX_REMOTE=' + str(remote) + '\n')
        (self.cache / "test.meta").write_text('META_KEY="test-app/"\nAPP_SLUG="test-agent"\nINSTALLATION_ID="test-installation"\nBOT_EMAIL="123+test-agent[bot]@users.noreply.github.com"\n')
        token_directory = self.tmp / ("argon-agents-" + str(os.getuid()))
        token_directory.mkdir(mode=0o700, exist_ok=True)
        (token_directory / "test.token").write_text(str(int(time.time()) + 3600) + " ghs_synthetic_offline_test\n")

    def launch(self, *arguments, scenario="", version="0.162.1", extra_env=None, operation="run", cwd=REPO):
        environment = dict(self.env, FAKE_SCENARIO=scenario, FAKE_VERSION=version)
        environment.update(extra_env or {})
        completed = subprocess.run([BASH, str(REPO / "bin/argon-agent"), operation, "test", *arguments], env=environment, cwd=cwd, capture_output=True, text=True, timeout=30)
        self.assertNotIn("ghs_synthetic_offline_test", completed.stdout + completed.stderr)
        self.assertNotIn(OP_TOKEN, completed.stdout + completed.stderr)
        self.assertFalse(list(self.tmp.glob("argon-codex.*")), "connection pipes must be cleaned after success or failure")
        return completed

    def opt_in_session_token(self, mode=0o600, content=OP_TOKEN + "\n"):
        with (self.config / "test.env").open("a") as config:
            config.write("ARGON_OP_SESSION_TOKEN=1\n")
        token = self.config / "op.token"
        token.write_text(content)
        token.chmod(mode)

    def events(self, event=None):
        values = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [value for value in values if event is None or value["event"] == event]

    def assert_refused(self, completed, message):
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("argon-agent:", completed.stderr)
        self.assertIn(message, completed.stderr)
        self.assertEqual(self.events("attached"), [])

    def test_standalone_preserves_identity_and_bypasses_daemon(self):
        result = self.launch("resume", "thread-id", extra_env={"GH_TOKEN":"inherited", "GITHUB_TOKEN":"inherited"})
        self.assertEqual(result.returncode, 0, result.stderr)
        attachment = self.events("attached")[0]
        self.assertEqual(attachment["args"], ["--no-daemon", "resume", "thread-id"])
        self.assertEqual(attachment["identity"]["GIT_AUTHOR_NAME"], "test-agent[bot]")
        self.assertEqual(attachment["identity"]["GIT_COMMITTER_EMAIL"], "123+test-agent[bot]@users.noreply.github.com")
        self.assertFalse(attachment["tokens_present"])
        self.assertEqual(self.events("rpc"), [])

    def test_remote_resume_and_config_arguments_survive_attach(self):
        self.configure(remote=1, arguments=("--yolo", "--model", "test-model", "--no-alt-screen"))
        result = self.launch("resume", "thread-id", "prompt with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["args"], ["--remote", "unix://" + self.env["FAKE_SOCKET"], "--model", "test-model", "--no-alt-screen", "resume", "thread-id", "prompt with spaces"])
        methods = [event["method"] for event in self.events("rpc")]
        self.assertLess(methods.index("command/exec"), methods.index("remoteControl/enable"))
        enable = next(event for event in self.events("rpc") if event["method"] == "remoteControl/enable")
        self.assertEqual(enable["params"], {"ephemeral":True})

    def test_remote_resume_keeps_saved_permissions_and_explicit_caller_arguments(self):
        flags = (("--yolo",), ("--dangerously-bypass-approvals-and-sandbox",), ("--approve-for-me",), ("--ask-for-approval", "never"), ("-a", "never"), ("--ask-for-approval=never",), ("-anever",), ("--sandbox", "workspace-write"), ("-s", "workspace-write"), ("--sandbox=workspace-write",), ("-sworkspace-write",))
        for configured in flags:
            with self.subTest(configured=configured):
                self.log.unlink(missing_ok=True)
                self.configure(remote=1, arguments=configured)
                result = self.launch("--model", "override-model", "resume", "thread-id", "--sandbox", "read-only")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events("attached")[0]["args"][2:], ["--model", "override-model", "resume", "thread-id", "--sandbox", "read-only"])

    def test_remote_new_threads_keep_configured_permissions(self):
        self.configure(remote=1, arguments=("--yolo", "--model", "resume"))
        for arguments in (("new prompt",), ("--", "resume")):
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                result = self.launch(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events("attached")[0]["args"][2:], ["--cd", str(REPO), "--yolo", "--model", "resume", *arguments])

    def test_remote_new_threads_use_the_invoking_directory(self):
        self.configure(remote=1)
        result = self.launch("new prompt", cwd=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["args"][2:], ["--cd", str(self.home), "new prompt"])

    def test_remote_new_threads_preserve_explicit_directory_arguments(self):
        for flags in (("--cd", str(self.home)), ("-C", str(self.home)), ("--cd=" + str(self.home),), ("-C" + str(self.home),)):
            for configured in (False, True):
                with self.subTest(flags=flags, configured=configured):
                    self.log.unlink(missing_ok=True)
                    self.configure(remote=1, arguments=flags if configured else ())
                    result = self.launch(*(flags if not configured else ()), "new prompt")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.events("attached")[0]["args"][2:], [*flags, "new prompt"])

    def test_remote_resume_in_config_omits_permissions_and_preserves_option_values(self):
        self.configure(remote=1, arguments=("--yolo", "-c", "--yolo", "resume", "--last"))
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["args"][2:], ["-c", "--yolo", "resume", "--last"])

    def test_remote_uses_separate_home_and_leaves_personal_state_untouched(self):
        personal = self.home / ".codex"
        personal.mkdir()
        sentinel = personal / "auth.json"
        sentinel.write_text("synthetic personal state")
        self.configure(remote=1)
        result = self.launch(extra_env={"CODEX_HOME":str(personal), "CODEX_SQLITE_HOME":str(personal / "db")})
        self.assertEqual(result.returncode, 0, result.stderr)
        attachment = self.events("attached")[0]
        self.assertEqual(attachment["codex_home"], str(self.home / ".codex-argon/test"))
        self.assertIsNone(attachment["sqlite_home"])
        self.assertEqual((self.home / ".codex-argon/test").stat().st_mode & 0o777, 0o700)
        self.assertEqual(sentinel.read_text(), "synthetic personal state")
        self.assertEqual(list(personal.iterdir()), [sentinel])

    def test_explicit_bot_home_with_spaces_is_supported_and_protected(self):
        self.configure(remote=1)
        destination = self.home / "custom bot home"
        destination.mkdir(mode=0o755)
        with (self.config / "test.env").open("a") as config:
            config.write("ARGON_CODEX_HOME=" + shlex.quote(str(destination)) + "\n")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["codex_home"], str(destination))
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_codex_management_uses_bot_home_without_attaching_or_enabling(self):
        self.configure(remote=1, arguments=("--yolo",))
        result = self.launch("app-server", "daemon", "version", operation="codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        attachment = self.events("attached")[0]
        self.assertEqual(attachment["args"], ["app-server", "daemon", "version"])
        self.assertEqual(attachment["codex_home"], str(self.home / ".codex-argon/test"))
        self.assertEqual(self.events("rpc"), [])

    def test_codex_management_checks_identity_before_enabling_or_pairing(self):
        self.configure(remote=1)
        commands = (("remote-control",), ("remote-control", "start"), ("remote-control", "pair"), ("remote-control", "--json", "pair"), ("app-server", "daemon", "enable-remote-control"), ("app-server", "daemon", "start"))
        for arguments in commands:
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                result = self.launch(*arguments, operation="codex")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("command/exec", [event["method"] for event in self.events("rpc")])
                self.assertEqual("remoteControl/enable" in [event["method"] for event in self.events("rpc")], arguments in (("remote-control",), ("remote-control", "start")))
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.launch(*arguments, operation="codex", scenario="wrong_identity"), "different GitHub identity")
                self.assertNotIn("remoteControl/enable", [event["method"] for event in self.events("rpc")])

    def test_remote_control_enable_uses_the_verified_daemon_instead_of_a_foreground_server(self):
        self.configure(remote=1)
        for arguments in (("remote-control",), ("remote-control", "start"), ("remote-control", "--json"), ("remote-control", "start", "--json"), ("remote-control", "--json", "start")):
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                result = self.launch(*arguments, operation="codex")
                self.assertEqual(result.returncode, 0, result.stderr)
                methods = [event["method"] for event in self.events("rpc")]
                self.assertLess(methods.index("command/exec"), methods.index("remoteControl/enable"))
                self.assertEqual(self.events("attached"), [], "management must not execute the native foreground server")
                if "--json" in arguments:
                    self.assertEqual(json.loads(result.stdout), {"mode":"daemon", "status":"connected"})
                else:
                    self.assertIn("shared bot daemon", result.stdout)

    def test_remote_control_refuses_flags_that_would_escape_to_the_native_foreground_server(self):
        self.configure(remote=1)
        for arguments in (("--config", "test=true", "remote-control", "start"), ("remote-control", "-c", "test=true"), ("remote-control", "start", "pair")):
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.launch(*arguments, operation="codex"), "remote-control")
                self.assertEqual(self.events("rpc"), [])

    def test_codex_recovery_commands_remain_available_with_a_mismatched_server(self):
        self.configure(remote=1, arguments=("--yolo",))
        commands = (("login", "status"), ("logout",), ("app-server", "daemon", "version"), ("app-server", "daemon", "stop"), ("app-server", "daemon", "disable-remote-control"), ("remote-control", "stop"))
        for arguments in commands:
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                result = self.launch(*arguments, operation="codex", scenario="wrong_identity")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events("rpc"), [])
                self.assertEqual(self.events("attached")[0]["args"], list(arguments))

    def test_remote_refuses_unsafe_home_overrides(self):
        self.configure(remote=1)
        personal = self.home / ".codex"
        personal.mkdir()
        link = self.home / "linked-home"
        link.symlink_to(personal)
        for destination in ("relative", str(personal), str(link)):
            with self.subTest(destination=destination):
                self.log.unlink(missing_ok=True)
                with (self.config / "test.env").open("a") as config:
                    config.write("ARGON_CODEX_HOME=" + shlex.quote(destination) + "\n")
                self.assert_refused(self.launch(), "Codex home" if destination == str(link) else "ARGON_CODEX_HOME")

    def test_codex_management_requires_remote_opt_in(self):
        self.assert_refused(self.launch("login", operation="codex"), "requires ARGON_CODEX_REMOTE=1")
        self.assertEqual(self.events(), [])

    def test_check_refuses_mismatched_server_without_enabling_remote_access(self):
        self.configure(remote=1)
        self.assert_refused(self.launch(scenario="wrong_identity", operation="check"), "different GitHub identity")
        self.assertNotIn("remoteControl/enable", [event["method"] for event in self.events("rpc")])

    def test_remote_refuses_wrong_identity_and_stale_server_credentials(self):
        self.configure(remote=1)
        for scenario in ("wrong_identity", "stale_token", "stale_github_token", "wrong_gh"):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.launch(scenario=scenario), "different GitHub identity or credential environment")
                self.assertNotIn("remoteControl/enable", [event["method"] for event in self.events("rpc")])
                self.assertFalse(any("stop" in event.get("args", []) for event in self.events("call")))

    def test_remote_handles_unrelated_server_notifications(self):
        self.configure(remote=1)
        result = self.launch(scenario="notifications")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.events("attached")), 1)

    def test_remote_reports_server_failures_and_cleans_connection(self):
        self.configure(remote=1)
        scenarios = {"bad_lifecycle":"absolute server socket", "relative_socket":"absolute server socket", "start_failure":"could not start", "rpc_error":"rejected initialize", "disconnect":"disconnected", "invalid_json":"invalid JSON", "enable_denied":"could not enable"}
        for scenario, message in scenarios.items():
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.launch(scenario=scenario), message)

    def test_remote_refuses_conflicting_flags_before_server_start(self):
        self.configure(remote=1)
        for flag in ("--no-daemon", "--remote", "--remote=unix:///other.sock"):
            with self.subTest(flag=flag):
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.launch(flag), "cannot be combined")
                self.assertEqual(self.events(), [])
        self.configure(remote=1, arguments=("--no-daemon",))
        self.assert_refused(self.launch(), "cannot be combined")

    def test_remote_refuses_inherited_exec_server(self):
        self.configure(remote=1)
        self.assert_refused(self.launch(extra_env={"CODEX_EXEC_SERVER_URL":"http://example.invalid"}), "CODEX_EXEC_SERVER_URL")
        self.assertEqual(self.events(), [])

    def test_remote_version_support(self):
        self.configure(remote=1)
        for version in ("0.162.0", "0.161.9", "0.163.0-alpha.1", "unexpected"):
            with self.subTest(version=version):
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.launch(version=version), "requires stable Codex")
                self.assertFalse(self.events("rpc"))
        result = self.launch(version="0.163.0")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_codex_agent_arguments_are_unchanged(self):
        self.configure(command="other-agent", arguments=("--configured",))
        result = self.launch("--remote", "somewhere")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["args"], ["--configured", "--remote", "somewhere"])
        self.assertEqual(len(self.events("call")), 1)

    def test_remote_rejects_non_codex_configuration(self):
        self.configure(remote=1, command="other-agent")
        self.assert_refused(self.launch(), "requires ARGON_COMMAND")
        self.assertEqual(self.events(), [])

    def test_old_codex_without_daemon_flag_still_launches(self):
        result = self.launch("resume", "thread-id", scenario="old_help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["args"], ["resume", "thread-id"])

    def test_large_help_output_does_not_disable_standalone_identity_protection(self):
        result = self.launch(scenario="large_help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["args"], ["--no-daemon"])

    def test_explicit_standalone_flag_is_not_duplicated(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                self.log.unlink(missing_ok=True)
                self.configure(arguments=("--no-daemon",) if configured else ())
                result = self.launch(*(() if configured else ("--no-daemon",)))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events("attached")[0]["args"], ["--no-daemon"])

    def test_session_token_is_exported_only_when_opted_in(self):
        (self.config / "op.token").write_text(OP_TOKEN + "\n")
        (self.config / "op.token").chmod(0o600)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.events("attached")[0]["op_token"])
        self.log.unlink()
        self.opt_in_session_token()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["op_token"], OP_TOKEN)

    def test_session_token_reaches_the_remote_codex_server(self):
        self.configure(remote=1)
        self.opt_in_session_token()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["op_token"], OP_TOKEN)
        self.assertIn("remoteControl/enable", [event["method"] for event in self.events("rpc")])

    def test_remote_refuses_a_server_without_the_current_session_token(self):
        self.configure(remote=1)
        self.opt_in_session_token()
        for scenario in ("missing_op_token", "replaced_op_token"):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.launch(scenario=scenario), "different GitHub identity or credential environment")
                self.assertNotIn("remoteControl/enable", [event["method"] for event in self.events("rpc")])

    def test_session_token_opt_in_refuses_an_unusable_token_file(self):
        cases = (("missing", "needs a 1Password service account token"), ("empty", "is empty"), ("readable", "must be chmod 600"), ("directory", "is not a regular file"))
        for case, message in cases:
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                self.configure()
                token = self.config / "op.token"
                if token.is_dir():
                    token.rmdir()
                token.unlink(missing_ok=True)
                if case == "directory":
                    with (self.config / "test.env").open("a") as config:
                        config.write("ARGON_OP_SESSION_TOKEN=1\n")
                    token.mkdir()
                else:
                    self.opt_in_session_token(mode=0o644 if case == "readable" else 0o600, content="" if case == "empty" else OP_TOKEN + "\n")
                    if case == "missing":
                        token.unlink()
                self.assert_refused(self.launch(), message)
                self.assertEqual(self.events(), [])

    def test_session_token_setting_must_be_0_or_1(self):
        with (self.config / "test.env").open("a") as config:
            config.write("ARGON_OP_SESSION_TOKEN=yes\n")
        self.assert_refused(self.launch(), "ARGON_OP_SESSION_TOKEN must be 0 or 1")

    def test_install_is_repeatable_in_throwaway_home(self):
        original_config = (self.config / "test.env").read_bytes()
        for _ in range(2):
            result = subprocess.run([BASH, str(REPO / "install.sh"), "--prefix", "test-"], env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.config / "test.env").read_bytes(), original_config)
        launcher = self.home / ".local/bin/test-test"
        self.assertEqual(launcher.resolve(), (REPO / "bin/argon-agent").resolve())
        self.assertEqual((self.shims / "gh").resolve(), (REPO / "bin/argon-agent").resolve())
        result = subprocess.run([BASH, str(launcher), "resume", "thread-id"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("attached")[0]["identity"]["ARGON_AGENT"], "test")


if __name__ == "__main__":
    unittest.main()
