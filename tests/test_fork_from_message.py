from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fork_from_message as module
from agent_contracts import ContractError, READ_ONLY_POLICY, fork_launch, safe_launch_flags

SOURCE = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"
CHILD = "019fe92e-1111-7222-8333-444455556666"


class FakeClient:
    def __init__(self, cwd, agent="claude"):
        self.cwd, self.agent, self.calls = cwd, agent, []
        self.fail_start = self.blocked = self.same_id = self.missing_session = False
        self.changed_source = self.missing_root = False
        self.source_reads = self.busy_attempts = 0
        self.argv = [agent, "--model", "chosen-model"]
        self.child_argv = []
        self.width, self.height = 140, 50
        self.show_transcript = self.show_picker = True
        self.hook_review = False
        self.session = None

    def pane(self, child=False):
        sid = SOURCE if not child or self.same_id else CHILD
        if not child:
            self.source_reads += 1
            if self.changed_source and self.source_reads > 1:
                sid = CHILD
        value = {"pane_id": "w:p2" if child else "w:p1", "workspace_id": "w", "tab_id": "w:t1",
                 "terminal_id": "child-terminal" if child else "source-terminal", "agent": self.agent,
                 "agent_status": "blocked" if self.blocked and child else "idle", "cwd": self.cwd,
                 "foreground_cwd": self.cwd}
        if not self.missing_session:
            value["agent_session"] = {"agent": self.agent, "kind": "id", "source": "herdr:" + self.agent, "value": sid}
        return value

    def json(self, args, timeout=15):
        self.calls.append(tuple(args))
        op = tuple(args[:2])
        if op == ("pane", "get"):
            return {"pane": self.pane(args[2] == "w:p2")}
        if op == ("pane", "process-info"):
            child = args[-1] == "w:p2"
            argv = self.child_argv if child else self.argv
            if child and argv and argv[0] == "env":
                argv = argv[argv.index(self.agent):]
            proc = {"pid": 200 if child else 100, "name": self.agent, "argv": argv}
            if child and not self.child_argv:
                proc = {"pid": 2, "name": "zsh", "argv": ["zsh"]}
            return {"process_info": {"shell_pid": 2, "foreground_processes": [proc]}}
        if op == ("pane", "layout"):
            return {"layout": {"panes": [{"pane_id": "w:p1", "rect": {"width": self.width, "height": self.height}}]}}
        if op == ("pane", "split"):
            return {"pane": {"pane_id": "w:p2", "tab_id": "w:t1", "workspace_id": "w"}}
        if op in (("tab", "create"), ("workspace", "create")):
            return {"root_pane": {} if self.missing_root else {"pane_id": "w:p2"},
                    "tab": {"tab_id": "w:t2"}, "workspace": {"workspace_id": "new-w"}}
        if op == ("agent", "start"):
            if self.busy_attempts:
                self.busy_attempts -= 1
                raise module.ForkError("agent_pane_busy")
            if self.fail_start:
                raise module.ForkError("agent_not_ready")
            self.child_argv = [self.agent, *args[args.index("--") + 1:]]
            return {"type": "agent_started"}
        if op == ("pane", "run"):
            self.child_argv = [self.agent, "--tools", "read"]
            return {}
        if op in (("pane", "rename"), ("agent", "focus"), ("agent", "send-keys"),
                  ("tab", "focus"), ("workspace", "focus"), ("tab", "close"), ("pane", "close"), ("workspace", "close")):
            return {}
        raise AssertionError(args)

    def text(self, args, timeout=15):
        self.calls.append(tuple(args))
        if self.hook_review:
            return "Hooks need review"
        if self.agent == "claude":
            return "Restore the code and/or conversation" if self.show_picker else "Ready"
        if not self.show_transcript:
            return "Ready"
        if ("agent", "send-keys", "w:p2", "esc") in self.calls:
            return "edit next; edit message" if self.show_picker else "Ready"
        return "edit prev"


class ContractTests(unittest.TestCase):
    def launch(self, agent, **options):
        return fork_launch(agent, SOURCE, options.pop("argv", [agent]), options.pop("env", {}),
                           read_only=options.pop("read_only", True), bridge=Path("/plugin/runtime_bridge.ts"),
                           omp_overlay=Path("/plugin/read-only-omp.yml"), **options)

    def test_claude_read_only_disables_hooks_and_external_tools(self):
        args = self.launch("claude").argv
        self.assertEqual(args[1:4], ("--resume", SOURCE, "--fork-session"))
        self.assertIn("Read,Grep,Glob", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertIn('{"disableAllHooks":true}', args)
        self.assertIn(" ".join(READ_ONLY_POLICY.split()), args)

    def test_codex_read_only_uses_valid_transport_overrides(self):
        args = self.launch("codex", mcp_servers=[("normal", "stdio"), ("http-server", "streamable_http")]).argv
        self.assertIn("read-only", args)
        self.assertIn("never", args)
        self.assertIn('mcp_servers.normal={command="false",enabled=false}', args)
        for servers in ([("odd.name", "stdio")], [("normal", "unknown")]):
            with self.assertRaises(ContractError):
                self.launch("codex", mcp_servers=servers)

    def test_normal_mode_does_not_inherit_read_only_restrictions(self):
        for agent in ("claude", "codex", "gjc", "omp"):
            launch = self.launch(agent, read_only=False, env={"HERDR_FORK_READ_ONLY": "1"})
            self.assertNotIn("--tools", launch.argv)
            self.assertEqual(launch.environment["HERDR_FORK_READ_ONLY"], "0")

    def test_runtime_settings_replace_initial_values(self):
        launch = self.launch("gjc", argv=["gjc", "--model", "old", "--thinking", "low"],
                             runtime={"model": "provider/live", "profile": "work", "thinking": "high", "session_file": "/scope/current.jsonl"})
        self.assertNotIn("old", launch.argv)
        self.assertIn("work", launch.argv)
        self.assertIn("high", launch.argv)
        self.assertIn("/scope", launch.argv)
        self.assertEqual(launch.argv[-1], "/goal pause")

    def test_flag_separator_does_not_reinterpret_prompt(self):
        self.assertEqual(safe_launch_flags("omp", ["omp", "--", "--model", "prompt"]), [])


class WorkflowTests(unittest.TestCase):
    def workflow(self, client, **options):
        uuid_patch = patch.object(module.uuid, "uuid4", return_value=module.uuid.UUID(CHILD))
        uuid_patch.start()
        self.addCleanup(uuid_patch.stop)
        return module.ForkWorkflow(client, {"focused_pane_id": "w:p1", "workspace_id": "w"},
                                   {"HERDR_SOCKET_PATH": "/socket", "HERDR_FORK_STATE_DIR": "/state"}, **options)

    def test_upstream_default_is_message_picker_in_tab(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            result = self.workflow(client).execute()
        self.assertEqual(result.mode, "message")
        self.assertEqual(result.destination, "tab")
        self.assertIn(("agent", "send-keys", "w:p2", "esc", "esc"), client.calls)
        self.assertIn(("tab", "focus", "w:t2"), client.calls)
        self.assertFalse(any(c[2:3] == ("w:p1",) and c[:2] in (("agent", "send-keys"), ("pane", "run")) for c in client.calls))

    def test_current_point_is_explicit_and_preserves_native_permissions(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            result = self.workflow(client, mode="fork", destination="pane").execute()
        self.assertEqual(result.fork_session_id, CHILD)
        self.assertNotIn("--tools", client.child_argv)
        self.assertFalse(any(c[:2] == ("agent", "send-keys") for c in client.calls))
        split = next(c for c in client.calls if c[:2] == ("pane", "split"))
        self.assertIn("right", split)
        self.assertIn("--no-focus", split)

    def test_adaptive_split_and_explicit_direction(self):
        for width, direction, expected in ((80, "auto", "down"), (80, "right", "right"), (200, "auto", "right")):
            with tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd)
                client.width = width
                self.workflow(client, destination="pane", direction=direction).execute()
                split = next(c for c in client.calls if c[:2] == ("pane", "split"))
                self.assertEqual(split[split.index("--direction") + 1], expected)

    def test_focus_obeys_destination_and_no_focus(self):
        for destination, operation in (("tab", "tab"), ("workspace", "workspace"), ("pane", "agent")):
            with tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd)
                self.workflow(client, destination=destination).execute()
                self.assertTrue(any(c[:2] == (operation, "focus") for c in client.calls))
                client = FakeClient(cwd)
                self.workflow(client, destination=destination, focus=False).execute()
                self.assertFalse(any(c[1:2] == ("focus",) for c in client.calls))

    def test_source_identity_race_stops_before_creation(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.changed_source = True
            with self.assertRaisesRegex(module.ForkError, "identity changed"):
                self.workflow(client).execute()
            self.assertFalse(any(c[:2] == ("tab", "create") for c in client.calls))

    def test_invalid_or_missing_source_never_starts_fresh(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.missing_session = True
            with self.assertRaisesRegex(module.ForkError, "No trusted"):
                self.workflow(client).execute()
            self.assertEqual(client.child_argv, [])

    def test_failed_or_blocked_launch_preserves_only_new_destination(self):
        for setting in ("fail_start", "blocked", "same_id"):
            with tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd)
                setattr(client, setting, True)
                with self.assertRaisesRegex(module.ForkError, "Destination preserved: tab w:t2"):
                    self.workflow(client).execute()
                self.assertFalse(any(c[1:2] == ("close",) for c in client.calls))

    def test_incomplete_empty_destination_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.missing_root = True
            with self.assertRaisesRegex(module.ForkError, "destination ID"):
                self.workflow(client).execute()
            self.assertIn(("tab", "close", "w:t2"), client.calls)

    def test_native_busy_shell_is_retried(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module.time, "sleep"):
            client = FakeClient(cwd)
            client.busy_attempts = 1
            self.workflow(client).execute()
            self.assertEqual(sum(c[:2] == ("agent", "start") for c in client.calls), 2)

    def test_picker_markers_before_selection_and_focus(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, "codex")
            self.workflow(client).execute()
            keys = [c for c in client.calls if c[:2] == ("agent", "send-keys")]
            self.assertEqual(keys, [("agent", "send-keys", "w:p2", "ctrl+t"), ("agent", "send-keys", "w:p2", "esc")])
            self.assertGreater(client.calls.index(("tab", "focus", "w:t2")), client.calls.index(keys[-1]))

    def test_transcript_timeout_does_not_send_selection_key(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "PICKER_SECONDS", -1):
            client = FakeClient(cwd, "codex")
            client.show_transcript = False
            with self.assertRaisesRegex(module.ForkError, "Codex transcript.*Destination preserved"):
                self.workflow(client).execute()
            self.assertNotIn(("agent", "send-keys", "w:p2", "esc"), client.calls)

    def test_wrong_child_metadata_is_not_deferred_identity(self):
        class InvalidChild(FakeClient):
            def pane(self, child=False):
                pane = super().pane(child)
                if child:
                    pane["agent_session"]["source"] = "untrusted"
                return pane
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaisesRegex(module.ForkError, "No trusted"):
                self.workflow(InvalidChild(cwd, "codex")).execute()

    def test_missing_codex_child_report_is_explicitly_pending(self):
        class DelayedChild(FakeClient):
            def pane(self, child=False):
                pane = super().pane(child)
                if child:
                    pane.pop("agent_session")
                return pane
        with tempfile.TemporaryDirectory() as cwd:
            result = self.workflow(DelayedChild(cwd, "codex")).execute()
            self.assertIsNone(result.fork_session_id)
            self.assertEqual(result.status, "started_identity_pending")

    def test_hook_review_remains_unanswered(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd)
            client.hook_review = True
            with self.assertRaisesRegex(module.ForkError, "hook trust review"):
                self.workflow(client).execute()

    def test_read_only_mcp_inventory_uses_source_binary_root_and_profile(self):
        with tempfile.TemporaryDirectory() as cwd:
            workflow = self.workflow(FakeClient(cwd, "codex"))
            process = {"argv": ["/opt/tools/codex", "--profile", "work"]}
            environment = {"CODEX_HOME": cwd, "PATH": "/opt/tools"}
            reply = type("Result", (), {"stdout": '[{"name":"test","transport":{"type":"stdio"}}]'})()
            with patch.object(module.subprocess, "run", return_value=reply) as run:
                self.assertEqual(workflow._codex_mcp(process, environment), [("test", "stdio")])
                self.assertEqual(run.call_args.args[0], ["/opt/tools/codex", "--profile", "work", "mcp", "list", "--json"])
                self.assertEqual(run.call_args.kwargs["env"], environment)


class EntrypointTests(unittest.TestCase):
    def test_original_action_ids_keep_historical_defaults(self):
        manifest = (Path(__file__).resolve().parents[1] / "herdr-plugin.toml").read_text()
        self.assertIn('id = "dmangla3.fork-from-message"', manifest)
        for block in manifest.split("[[actions]]")[1:]:
            action = re.search(r'^id = "([^"]+)"', block, re.MULTILINE).group(1)
            command = json.loads(re.search(r'^command = (.+)$', block, re.MULTILINE).group(1))
            with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PLUGIN_CONTEXT_JSON": "{}", "HERDR_BIN_PATH": "herdr"}, clear=True), patch.object(module, "ForkWorkflow") as workflow, patch.object(module, "asdict", return_value={}), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(module.main(command[2:]), 0)
                self.assertEqual(workflow.call_args.args[4], "message" if action in ("fork", "fork-pane", "fork-workspace") else "read-only" if action == "fork-read-only" else "fork")

    def test_external_pane_requires_selected_server_not_fake_env(self):
        with patch.dict(os.environ, {"HERDR_BIN_PATH": "herdr"}, clear=True), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(module.main(["--pane", "w:p1"]), 1)

    def test_external_automation_resolves_selected_server_socket(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, "codex")
            client.text = lambda args: "server:\n  socket: /selected/server.sock\n"
            with patch.dict(os.environ, {"HERDR_BIN_PATH": "herdr", "HERDR_SOCKET_PATH": "/stale/socket"}, clear=True), patch.object(module, "HerdrClient", return_value=client) as factory, patch.object(module, "ForkWorkflow") as workflow, patch.object(module, "asdict", return_value={}), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(module.main(["--session", "test-server", "--pane", "w:p1", "--mode", "fork"]), 0)
                factory.assert_called_once_with("herdr", "test-server")
                self.assertEqual(workflow.call_args.args[2]["HERDR_SOCKET_PATH"], "/selected/server.sock")
                self.assertEqual(workflow.call_args.args[2]["HERDR_FORK_SESSION"], "test-server")
                self.assertNotIn("HERDR_ENV", workflow.call_args.args[2])

    def test_selected_server_is_retained_on_json_text_and_run(self):
        responses = [json.dumps({"result": {}}), "screen", ""]
        with patch.object(module.subprocess, "run") as run:
            run.side_effect = [type("Result", (), {"stdout": x})() for x in responses]
            client = module.HerdrClient("herdr", "named-server")
            client.json(["pane", "get", "w:p1"])
            client.text(["agent", "read", "w:p1"])
            client.json(["pane", "run", "w:p2", "/bin/sh", "/tmp/file with spaces.sh"])
            for call in run.call_args_list:
                self.assertEqual(call.args[0][:3], ["herdr", "--session", "named-server"])


if __name__ == "__main__":
    unittest.main()
