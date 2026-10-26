from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_contracts import ContractError
from harness_identity import entrypoint_argv, process_product, select_process, session_id, session_reference

SID = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"


class IdentityTests(unittest.TestCase):
    def test_only_entrypoints_identify_product(self):
        cases = [
            (["bun", "/opt/node_modules/omo-ai/bin/omo.js"], "omo"),
            (["bun", "/opt/node_modules/@code-yeongyu/senpi/dist/cli.js"], "omo"),
            (["node", "/opt/node_modules/omo-ai/node_modules/@code-yeongyu/senpi/dist/bundle/cli.js"], "omo"),
            (["bun", "/opt/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"], "pi"),
            (["node", "/opt/node_modules/@mariozechner/pi-coding-agent/dist/cli.js"], "pi"),
            (["python3.14", "/opt/hermes_cli/main.py"], "hermes"),
            (["cursor-agent", "--model", "test"], "cursor"),
            (["opencode", "mini"], "opencode"),
            (["agy"], "agy"),
            (["copilot"], "copilot"),
            (["zsh", "pi"], None),
            (["bun", "/tmp/unknown.js", "omo"], None),
            (["bun", "--eval", "omo"], None),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(process_product({"argv": argv}), expected)

    def test_omo_transport_hint_does_not_change_launcher(self):
        product, process = select_process([{"pid": 3, "argv": ["bun", "/opt/node_modules/@code-yeongyu/senpi/dist/cli.js"]}, {"argv": ["python3", "/plugin/fork_from_message.py"]}], "pi")
        self.assertEqual(product, "omo")
        self.assertEqual(process["pid"], 3)
        with self.assertRaises(ContractError):
            select_process([process], "omp")
        with self.assertRaises(ContractError):
            select_process([process, process], "pi")

    def test_interpreter_is_removed_before_flag_parsing(self):
        self.assertEqual(entrypoint_argv({"argv": ["bun", "/opt/cli.js", "--model", "chosen"]}), ["/opt/cli.js", "--model", "chosen"])
        self.assertEqual(entrypoint_argv({"argv": ["pi", "--model", "chosen"]}), ["pi", "--model", "chosen"])

    def test_exact_native_process_title_survives_node_argv_truncation(self):
        for extra in ({}, {"argv": None}):
            product, process = select_process([{"pid": 12, "name": "node", "argv0": "pi", **extra}], "pi")
            self.assertEqual(product, "pi")
            self.assertEqual(process["argv"], ["pi"])
        for invalid in ([], "pi", [None]):
            with self.assertRaises(ContractError):
                select_process([{"pid": 12, "name": "node", "argv0": "pi", "argv": invalid}], "pi")
        with self.assertRaises(ContractError):
            select_process([{"pid": 12, "name": "node", "argv0": "arbitrary pi prompt"}], "pi")

    def test_reference_namespaces_and_sources(self):
        for product, agent, source, value in [
            ("omo", "pi", "herdr:pi", SID),
            ("agy", "agy", "herdr:antigravity_cli", SID),
            ("opencode", "opencode", "herdr:opencode", "ses_test-123"),
            ("hermes", "hermes", "herdr:hermes", "cli-2026_123"),
        ]:
            pane = {"agent_session": {"agent": agent, "source": source, "kind": "id", "value": value}}
            self.assertEqual(session_reference(pane, product), value)
            pane["agent_session"]["source"] = "untrusted"
            with self.assertRaises(ContractError):
                session_reference(pane, product)
        for value in ("not-uuid", "019fe92d23967f408dfebdb1ae3d4e27", "--resume"):
            with self.assertRaises(ContractError):
                session_reference({"agent_session": {"agent": "pi", "source": "herdr:pi", "kind": "id", "value": value}}, "pi")

    def test_only_bounded_header_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            pane = {"agent_session": {"agent": "pi", "source": "herdr:pi", "kind": "path", "value": str(path)}}
            with self.assertRaises(ContractError):
                session_reference(pane, "pi")
            path.write_text(json.dumps({"type": "session", "id": SID}) + "\nnot json conversation body", encoding="utf-8")
            self.assertEqual(session_id(session_reference(pane, "pi")), SID)
            for header in ("x" * 32769, "{}", json.dumps({"type": "session", "id": SID.replace("-", "")})):
                path.write_text(header, encoding="utf-8")
                with self.assertRaises(ContractError):
                    session_id(str(path))


if __name__ == "__main__":
    unittest.main()
