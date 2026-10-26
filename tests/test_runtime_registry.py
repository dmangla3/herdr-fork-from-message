from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import runtime_registry as registry

SOURCE = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"


class RegistryTests(unittest.TestCase):
    def data(self, cwd):
        return {"agent": "gjc", "pane_id": "w:p1", "terminal_id": "t", "cwd": cwd,
                "pid": os.getpid(), "process_start": "2026년 10월 26일 월요일 01시 00분", "session_id": SOURCE}

    def read(self, root, data):
        pane = {key: data[key] for key in ("pane_id", "terminal_id", "agent", "cwd")}
        with patch.object(registry, "process_start", return_value=data["process_start"]):
            return registry.read_record(root, "/socket", pane, data["pid"])

    def test_public_default_and_explicit_root_precedence(self):
        with patch.object(registry.Path, "home", return_value=Path("/home/developer")):
            self.assertEqual(registry.state_root({}), Path("/home/developer/.local/state/herdr/plugins/dmangla3.fork-from-message"))
        self.assertEqual(registry.state_root({"HERDR_FORK_STATE_DIR": "/pinned", "HERDR_PLUGIN_STATE_DIR": "/other"}), Path("/pinned"))
        for key in ("HERDR_FORK_STATE_DIR", "HERDR_PLUGIN_STATE_DIR", "XDG_STATE_HOME"):
            for value in ("", "relative", "/root\nother"):
                with self.assertRaises(registry.RegistryError):
                    registry.state_root({key: value})

    def test_private_record_exact_binding_and_locale_text(self):
        with tempfile.TemporaryDirectory() as cwd:
            root, data = Path(cwd), self.data(cwd)
            path = registry.write_record(root, "/socket", data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            value = self.read(root, data)
            self.assertEqual(value["environment"], {})
            self.assertEqual(value["process_start"], data["process_start"])
            for field, wrong in (("terminal_id", "other"), ("pid", data["pid"] + 1), ("agent", "omp"), ("cwd", "/")):
                with self.subTest(field=field), self.assertRaises(registry.RegistryError):
                    self.read(root, {**data, field: wrong})

    def test_invalid_payload_never_creates_owner_file(self):
        invalid = [
            {"unexpected": "secret"}, {"model": "x\n--option"}, {"profile": 3}, {"thinking": ""},
            {"session_file": "relative.jsonl"}, {"cwd": "relative"}, {"pid": True}, {"pid": -1},
            {"environment": {"TOKEN": "private"}}, {"environment": {"XDG_STATE_HOME": "relative"}},
            {"environment": []}, {"read_only_ready": "true"}, {"bridge_version": True},
            {"registration_valid": 1}, {"model": "x" * 4097}, {"model": "bad\u007fvalue"},
        ]
        for update in invalid:
            with self.subTest(update=update), tempfile.TemporaryDirectory() as cwd:
                root = Path(cwd)
                with self.assertRaises(registry.RegistryError):
                    registry.write_record(root, "/socket", {**self.data(cwd), **update})
                self.assertFalse((root / "owners").exists())

    def test_tampered_timestamp_and_schema_are_rejected(self):
        invalid = [{"updated_at": x} for x in (float("nan"), float("inf"), -1, True, "123")]
        invalid.extend(({"environment": {"TOKEN": "secret"}}, {"unexpected": "secret"}, {"registration_valid": False}))
        for update in invalid:
            with self.subTest(update=update), tempfile.TemporaryDirectory() as cwd:
                root, data = Path(cwd), self.data(cwd)
                path = registry.write_record(root, "/socket", data)
                value = json.loads(path.read_text())
                path.write_text(json.dumps({**value, **update}), encoding="utf-8")
                with self.assertRaises(registry.RegistryError):
                    self.read(root, data)

    def test_expired_and_future_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as cwd:
            root, data = Path(cwd), self.data(cwd)
            value = json.loads(registry.write_record(root, "/socket", data).read_text())
            for offset in (-6, 21):
                with patch.object(registry.time, "time", return_value=value["updated_at"] + offset), self.assertRaisesRegex(registry.RegistryError, "expired"):
                    self.read(root, data)

    def test_symlink_or_public_record_or_owner_directory_is_rejected(self):
        for kind in ("symlink-file", "symlink-dir", "file-mode", "directory-mode"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as cwd:
                root, data = Path(cwd), self.data(cwd)
                path = registry.write_record(root, "/socket", data)
                if kind == "symlink-file":
                    target = root / "foreign"
                    path.rename(target)
                    path.symlink_to(target)
                elif kind == "symlink-dir":
                    target = root / "foreign"
                    path.parent.rename(target)
                    path.parent.symlink_to(target, target_is_directory=True)
                elif kind == "file-mode":
                    path.chmod(0o644)
                else:
                    path.parent.chmod(0o755)
                with self.assertRaises(registry.RegistryError):
                    self.read(root, data)

    def test_record_size_is_bounded_before_parsing(self):
        with tempfile.TemporaryDirectory() as cwd:
            root, data = Path(cwd), self.data(cwd)
            path = registry.write_record(root, "/socket", data)
            path.write_bytes(b" " * (registry.MAX_RECORD_BYTES + 1))
            with self.assertRaises(registry.RegistryError):
                self.read(root, data)

    def test_process_exit_and_timeout_are_controlled_registry_failures(self):
        for error in (OSError("private details"), subprocess.CalledProcessError(1, "ps"), subprocess.TimeoutExpired("ps", 3)):
            with patch.object(registry.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(registry.RegistryError, "could not be verified"):
                    registry.process_start(1234)
                self.assertFalse(registry.descendant_of(1234, 4567))


if __name__ == "__main__":
    unittest.main()
