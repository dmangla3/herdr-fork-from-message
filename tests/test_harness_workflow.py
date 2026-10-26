from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_fork_from_message import CHILD, SOURCE, FakeClient
import fork_from_message as module
from native_sessions import NativeSessionError
from runtime_registry import RegistryError


class HarnessWorkflowTests(unittest.TestCase):
    def workflow(self, client, **options):
        return module.ForkWorkflow(client, {"focused_pane_id": "w:p1", "workspace_id": "w"},
                                   {"HERDR_SOCKET_PATH": "/socket", "HERDR_FORK_STATE_DIR": "/state"},
                                   mode=options.pop("mode", "fork"), focus=False, **options)

    def assert_no_destination(self, client):
        self.assertFalse(any(c[:2] in (("pane", "split"), ("tab", "create"), ("workspace", "create"), ("pane", "run"), ("agent", "start")) for c in client.calls))

    def test_unavailable_harnesses_never_resume_source(self):
        for agent in ("cursor", "agy", "hermes"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd, agent)
                with self.assertRaisesRegex(module.ForkError, "Source was not resumed or changed"):
                    self.workflow(client).execute()
                self.assert_no_destination(client)

    def test_experimental_routes_require_explicit_opt_in(self):
        for agent in ("opencode", "copilot"):
            with tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd, agent)
                with self.assertRaisesRegex(module.ForkError, "--allow-experimental"):
                    self.workflow(client).execute()
                self.assert_no_destination(client)

    def test_pi_family_requires_authoritative_settings(self):
        for agent in ("pi", "omo", "gjc", "omp"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd, patch.object(module, "read_record", side_effect=RegistryError("missing owner")):
                client = FakeClient(cwd, agent)
                with self.assertRaisesRegex(module.ForkError, "missing owner"):
                    self.workflow(client).execute()
                self.assert_no_destination(client)

    def test_owner_file_header_must_match_registered_id(self):
        with tempfile.TemporaryDirectory() as cwd:
            path = Path(cwd) / "wrong-session.jsonl"
            path.write_text(json.dumps({"type": "session", "id": CHILD}) + "\n", encoding="utf-8")
            client = FakeClient(cwd, "pi")
            with patch.object(module, "read_record", return_value={"session_id": SOURCE, "session_file": str(path)}):
                with self.assertRaisesRegex(module.ForkError, "does not match"):
                    self.workflow(client).execute()
            self.assert_no_destination(client)

    def test_owner_file_path_cannot_switch_while_id_stays_same(self):
        with tempfile.TemporaryDirectory() as cwd:
            first, second = Path(cwd) / "one.jsonl", Path(cwd) / "two.jsonl"
            for path in (first, second):
                path.write_text(json.dumps({"type": "session", "id": SOURCE}) + "\n", encoding="utf-8")
            records = [{"session_id": SOURCE, "session_file": str(path)} for path in (first, second)]
            client = FakeClient(cwd, "pi")
            with patch.object(module, "read_record", side_effect=records):
                with self.assertRaisesRegex(module.ForkError, "identity changed"):
                    self.workflow(client).execute()
            self.assert_no_destination(client)

    def test_source_cwd_change_stops_before_destination(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as other:
            class MovingClient(FakeClient):
                def pane(self, child=False):
                    value = super().pane(child)
                    if not child and self.source_reads > 1:
                        value["foreground_cwd"] = other
                    return value
            client = MovingClient(cwd, "codex")
            with self.assertRaisesRegex(module.ForkError, "Source process changed"):
                self.workflow(client).execute()
            self.assert_no_destination(client)

    def test_native_uuid_inputs_are_normalized_consistently(self):
        self.assertEqual(module.native_id(SOURCE.upper()), SOURCE)
        pane = {"agent_session": {"agent": "codex", "source": "herdr:codex", "kind": "id", "value": SOURCE.upper()}}
        self.assertEqual(module.session_reference(pane, "codex"), SOURCE)

    def test_pi_family_keeps_launcher_roots_and_live_settings(self):
        for agent in ("pi", "omo", "gjc", "omp"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd:
                client = FakeClient(cwd, agent)
                if agent == "omo":
                    client.argv = ["bun", "/opt/node_modules/@code-yeongyu/senpi/dist/cli.js", "--model", "old"]
                def owner(root, socket, pane, pid):
                    return dict(session_id=CHILD if pane["pane_id"] == "w:p2" else SOURCE,
                                environment={"XDG_STATE_HOME": "/owner/state"}, model="provider/live")
                launched = []
                def start(workflow, pane, argv):
                    launched.extend(argv)
                    client.child_argv = [agent, "--fork", SOURCE]
                with patch.object(module, "read_record", side_effect=owner), patch.object(module.ForkWorkflow, "_start_shell", autospec=True, side_effect=start):
                    workflow = self.workflow(client)
                    workflow.env = {**workflow.env, "XDG_STATE_HOME": "/wrong-caller", "GJC_CODING_AGENT_DIR": "/wrong"}
                    result = workflow.execute()
                self.assertEqual(result.fork_session_id, CHILD)
                self.assertIn(agent, launched)
                self.assertIn("provider/live", launched)
                self.assertIn("XDG_STATE_HOME=/owner/state", launched)
                self.assertIn("HERDR_PLUGIN_STATE_DIR", launched)
                self.assertFalse(any("wrong" in x for x in launched))
                if agent in ("pi", "omo", "omp"):
                    self.assertIn("--extension", launched)
                    self.assertTrue(launched[-1].endswith("runtime_bridge.ts"))
                self.assertFalse(any(c[:2] == ("agent", "start") for c in client.calls))

    def test_new_read_only_modes_fail_before_destination(self):
        for agent in ("pi", "omo", "copilot", "opencode"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as cwd, patch.object(module, "read_record", return_value=dict(session_id=SOURCE, bridge_version=1)):
                client = FakeClient(cwd, agent)
                if agent == "opencode":
                    original = client.pane
                    def pane(child=False):
                        value = original(child)
                        value["agent_session"]["value"] = "ses_source"
                        return value
                    client.pane = pane
                with self.assertRaisesRegex(module.ForkError, "Read-only mode"):
                    self.workflow(client, mode="read-only", allow_experimental=True).execute()
                self.assert_no_destination(client)

    def test_copilot_resumes_only_native_new_id(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", return_value=CHILD) as clone:
            client = FakeClient(cwd, "copilot")
            result = self.workflow(client, allow_experimental=True).execute()
            self.assertEqual(clone.call_args.args[1], SOURCE)
            self.assertEqual(result.fork_session_id, CHILD)
            self.assertIn(CHILD, client.child_argv)
            self.assertNotIn(SOURCE, client.child_argv)
            self.assertFalse(any(c[:2] == ("pane", "run") for c in client.calls))

    def test_copilot_rpc_failure_never_creates_destination(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", side_effect=NativeSessionError("unsupported RPC")):
            client = FakeClient(cwd, "copilot")
            with self.assertRaisesRegex(module.ForkError, "unsupported RPC"):
                self.workflow(client, allow_experimental=True).execute()
            self.assert_no_destination(client)

    def test_copilot_invalid_acknowledged_id_never_enters_argv(self):
        for child_id in ("--flag", "not-a-uuid", ""):
            with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", return_value=child_id):
                client = FakeClient(cwd, "copilot")
                with self.assertRaises(module.ForkError):
                    self.workflow(client, allow_experimental=True).execute()
                self.assert_no_destination(client)

    def test_native_child_is_reported_on_later_launch_failure(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", return_value=CHILD):
            client = FakeClient(cwd, "copilot")
            client.fail_start = True
            with self.assertRaisesRegex(module.ForkError, "Destination preserved.*Native fork retained: " + CHILD):
                self.workflow(client, allow_experimental=True).execute()

    def test_expected_child_id_mismatch_is_not_success(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(module, "copilot_fork", return_value="019fe92e-1111-7222-8333-444455556667"):
            with self.assertRaisesRegex(module.ForkError, "differs.*Destination preserved"):
                self.workflow(FakeClient(cwd, "copilot"), allow_experimental=True).execute()

    def test_source_switch_during_rpc_stops_before_destination(self):
        with tempfile.TemporaryDirectory() as cwd:
            client = FakeClient(cwd, "copilot")
            def clone(*args, **kwargs):
                client.changed_source = True
                return CHILD
            with patch.object(module, "copilot_fork", side_effect=clone):
                with self.assertRaisesRegex(module.ForkError, "identity changed.*Native fork retained"):
                    self.workflow(client, allow_experimental=True).execute()
            self.assert_no_destination(client)

    def test_opencode_mini_uses_native_namespace_and_private_server_flag(self):
        class OpenCodeClient(FakeClient):
            def pane(self, child=False):
                value = super().pane(child)
                value["agent_session"]["value"] = "ses_child" if child else "ses_source"
                return value
        with tempfile.TemporaryDirectory() as cwd:
            client = OpenCodeClient(cwd, "opencode")
            client.argv = ["opencode", "mini", "--standalone"]
            result = self.workflow(client, allow_experimental=True).execute()
            self.assertEqual(result.fork_session_id, "ses_child")
            self.assertEqual(client.child_argv[:5], ["opencode", "mini", "--session", "ses_source", "--fork"])
            self.assertIn("--standalone", client.child_argv)

    def test_inline_opencode_config_is_rejected_without_value_disclosure(self):
        class OpenCodeClient(FakeClient):
            def pane(self, child=False):
                value = super().pane(child)
                value["agent_session"]["value"] = "ses_source"
                return value
        with tempfile.TemporaryDirectory() as cwd:
            client = OpenCodeClient(cwd, "opencode")
            workflow = self.workflow(client, allow_experimental=True)
            workflow.env = {**workflow.env, "OPENCODE_CONFIG_CONTENT": "secret-token"}
            with self.assertRaises(module.ForkError) as caught:
                workflow.execute()
            self.assertNotIn("secret-token", str(caught.exception))
            self.assert_no_destination(client)


if __name__ == "__main__":
    unittest.main()
