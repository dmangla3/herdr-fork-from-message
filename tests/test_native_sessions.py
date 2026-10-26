from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_sessions as module

SOURCE = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"
CHILD = "019fe92e-1111-7222-8333-444455556666"
SECRET = "must-not-appear-in-errors"


def frame(message):
    content = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(content)}\r\n\r\n".encode("ascii") + content


def reply(request, result):
    return frame({"jsonrpc": "2.0", "id": request["id"], "result": result})


class FakePipe:
    def __init__(self, fd):
        self.fd = fd
        self.closed = False

    def fileno(self):
        return self.fd

    def close(self):
        self.closed = True


class FakeProcess:
    pid = 701

    def __init__(self):
        self.stdin = FakePipe(101)
        self.stdout = FakePipe(102)
        self.wait_timeouts = []
        self.wait_calls = []

    def wait(self, *, timeout):
        self.wait_calls.append(timeout)
        if self.wait_timeouts and self.wait_timeouts.pop(0):
            raise subprocess.TimeoutExpired("private-copilot", timeout)
        return 0


class FakeRuntime:
    """In-memory nonblocking pipes: no real Copilot, subprocess, or clock waits."""

    def __init__(self, handler=None, *, read_chunk=65536, write_chunk=65536):
        self.process = FakeProcess()
        self.handler = handler or self.default_handler
        self.read_chunk = read_chunk
        self.write_chunk = write_chunk
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.messages = []
        self.launches = []
        self.signals = []
        self.blocking = []
        self.now = 0.0
        self.stall_reads = False
        self.stall_writes = False
        self.read_error = False
        self.write_error = False
        self.interrupt_read = False
        self.interrupt_write = False
        self.interrupt_select = False
        self.zero_write = False

    def default_handler(self, message):
        if message.get("method") == "ping":
            self.incoming.extend(reply(message, {"protocolVersion": 3}))
        elif message.get("method") == "sessions.fork":
            self.incoming.extend(reply(message, {"sessionId": CHILD}))

    def launch(self, argv, **options):
        self.launches.append((argv, options))
        return self.process

    def select(self, readable, writable, exceptional, timeout):
        if self.interrupt_select:
            self.interrupt_select = False
            raise InterruptedError()
        if (readable and self.stall_reads) or (writable and self.stall_writes):
            self.now += timeout
            return [], [], []
        return readable, writable, []

    def read(self, fd, size):
        if fd != self.process.stdout.fd:
            raise AssertionError(fd)
        if self.read_error:
            raise OSError(SECRET)
        if self.interrupt_read:
            self.interrupt_read = False
            raise BlockingIOError()
        count = min(size, self.read_chunk, len(self.incoming))
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data

    def write(self, fd, content):
        if fd != self.process.stdin.fd:
            raise AssertionError(fd)
        if self.write_error:
            raise OSError(SECRET)
        if self.interrupt_write:
            self.interrupt_write = False
            raise BlockingIOError()
        if self.zero_write:
            return 0
        count = min(len(content), self.write_chunk)
        self.outgoing.extend(content[:count])
        while True:
            boundary = self.outgoing.find(b"\r\n\r\n")
            if boundary < 0:
                break
            size = int(self.outgoing[:boundary].split(b":", 1)[1])
            end = boundary + 4 + size
            if len(self.outgoing) < end:
                break
            message = json.loads(self.outgoing[boundary + 4:end])
            del self.outgoing[:end]
            self.messages.append(message)
            self.handler(message)
        return count

    def __enter__(self):
        self.stack = ExitStack()
        patches = {
            "subprocess.Popen": self.launch,
            "select.select": self.select,
            "os.read": self.read,
            "os.write": self.write,
            "os.set_blocking": lambda fd, value: self.blocking.append((fd, value)),
            "os.killpg": lambda pid, sig: self.signals.append((pid, sig)),
            "time.monotonic": lambda: self.now,
        }
        for name, replacement in patches.items():
            self.stack.enter_context(patch.object(
                getattr(module, name.split(".")[0]), name.split(".")[1], replacement,
            ))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


class NativeSessionTests(unittest.TestCase):
    def fork(self, runtime, *, source=SOURCE):
        with runtime:
            return module.copilot_fork(
                "/private/bin/copilot", source, cwd="/source/project",
                environment={"PATH": "/private/bin", "COPILOT_HOME": "/private/state", "TOKEN": SECRET},
            )

    def assert_closed(self, runtime):
        self.assertTrue(runtime.process.stdin.closed)
        self.assertTrue(runtime.process.stdout.closed)
        self.assertEqual(runtime.signals, [
            (runtime.process.pid, signal.SIGTERM), (runtime.process.pid, signal.SIGKILL),
        ])
        self.assertEqual(runtime.process.wait_calls, [module.CLEANUP_TIMEOUT_SECONDS] * 2)

    def fork_reply(self, raw):
        runtime = FakeRuntime()

        def handle(message):
            if message.get("method") == "sessions.fork":
                runtime.incoming.extend(raw(message) if callable(raw) else raw)
            else:
                runtime.default_handler(message)

        runtime.handler = handle
        return runtime

    def test_native_fork_uses_exact_schema_and_private_process(self):
        runtime = FakeRuntime()
        self.assertEqual(self.fork(runtime), CHILD)
        self.assertEqual(runtime.messages, [
            {"jsonrpc": "2.0", "id": "1", "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": "2", "method": "sessions.fork", "params": {"sessionId": SOURCE}},
        ])
        self.assertEqual(runtime.launches, [(
            ["/private/bin/copilot", "--headless", "--no-auto-update", "--log-level", "error", "--stdio"],
            {"cwd": "/source/project", "env": {
                "PATH": "/private/bin", "COPILOT_HOME": "/private/state", "TOKEN": SECRET,
            }, "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                "bufsize": 0, "start_new_session": True},
        )])
        self.assertEqual(runtime.blocking, [(101, False), (102, False)])
        self.assert_closed(runtime)

    def test_notifications_unicode_and_short_reads_and_writes(self):
        child = "새로운-session"
        notice = {"jsonrpc": "2.0", "method": "session.event", "params": {"text": "알림"}}
        runtime = self.fork_reply(lambda message: frame(notice) + frame(notice) + reply(message, {"sessionId": child}))
        runtime.read_chunk = 1
        runtime.write_chunk = 3
        runtime.interrupt_read = runtime.interrupt_write = runtime.interrupt_select = True
        self.assertEqual(self.fork(runtime), child)
        self.assertEqual(len(runtime.messages), 2)
        self.assert_closed(runtime)

    def test_coalesced_frames_and_optional_header_are_supported(self):
        def response(message):
            notice = frame({"jsonrpc": "2.0", "method": "event", "params": []})
            valid = reply(message, {"sessionId": CHILD})
            return notice + valid.replace(b"\r\n\r\n", b"\r\nContent-Type: application/json\r\n\r\n", 1)

        runtime = self.fork_reply(response)
        self.assertEqual(self.fork(runtime), CHILD)
        self.assert_closed(runtime)

    def test_server_requests_are_denied_without_authentication_or_permissions(self):
        requests = [{"jsonrpc": "2.0", "id": index, "method": method, "params": {"secret": SECRET}}
                    for index, method in enumerate(("permission.request", "auth.request", "tool.call"))]
        runtime = self.fork_reply(lambda message: b"".join(frame(item) for item in requests) +
                                  reply(message, {"sessionId": CHILD}))
        self.assertEqual(self.fork(runtime), CHILD)
        responses = runtime.messages[2:]
        self.assertEqual([item["id"] for item in responses], [0, 1, 2])
        for response in responses:
            self.assertEqual(response["error"]["code"], -32601)
            self.assertNotIn("result", response)
            self.assertNotIn(SECRET, json.dumps(response))
        self.assert_closed(runtime)

    def test_rpc_rejection_is_actionable_and_does_not_disclose_peer_text(self):
        for code in (-32601, -32000):
            with self.subTest(code=code):
                runtime = self.fork_reply(lambda message: frame({
                    "jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": code, "message": SECRET, "data": {"environment": SECRET}},
                }))
                with self.assertRaises(module.NativeSessionError) as caught:
                    self.fork(runtime)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertIn("CLI", str(caught.exception))
                self.assert_closed(runtime)

    def test_malformed_frames_are_rejected_and_child_is_closed(self):
        invalid = [
            b"Length: 2\r\n\r\n{}",
            b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            b"Content-Length: -1\r\n\r\n",
            b"Content-Length: +2\r\n\r\n{}",
            b"Content-Length: 1.0\r\n\r\n{}",
            b"Content-Length: 0\r\n\r\n",
            f"Content-Length: {module.MAX_FRAME_BYTES + 1}\r\n\r\n".encode("ascii"),
            b"Content-Length: 2\r\nBad Header: value\r\n\r\n{}",
            b"Content-Length: 2\r\nX-Test: bad\x00value\r\n\r\n{}",
            b"Content-Length: 2\r\n\r\n\xff\xff",
            b"Content-Length: 1\r\n\r\n{",
            b"Content-Length: 2\r\n\r\n[]",
            b"Content-Length: 4\r\n\r\nnull",
            b"Content-Length: 25\r\n\r\n{\"jsonrpc\":\"2.0\",\"x\":NaN}",
            b"Content-Length: 33\r\n\r\n{\"jsonrpc\":\"2.0\",\"jsonrpc\":\"2.0\"}",
            b"X" * module.MAX_HEADER_BYTES,
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:80]):
                runtime = self.fork_reply(raw)
                with self.assertRaises(module.NativeSessionError):
                    self.fork(runtime)
                self.assert_closed(runtime)

    def test_eof_during_header_or_body_is_rejected(self):
        for raw in (b"", b"Content-Len", b"Content-Length: 99\r\n\r\n{}"):
            with self.subTest(raw=raw):
                runtime = self.fork_reply(raw)
                with self.assertRaisesRegex(module.NativeSessionError, "output closed"):
                    self.fork(runtime)
                self.assert_closed(runtime)

    def test_wrong_response_id_is_not_accepted_as_a_fork(self):
        for response_id in ("1", "other", 2, True, None):
            with self.subTest(response_id=response_id):
                runtime = self.fork_reply(lambda message: frame({
                    "jsonrpc": "2.0", "id": response_id, "result": {"sessionId": CHILD},
                }))
                with self.assertRaisesRegex(module.NativeSessionError, "response ID"):
                    self.fork(runtime)
                self.assert_closed(runtime)

    def test_invalid_rpc_envelopes_are_rejected(self):
        envelopes = [
            {"jsonrpc": "1.0", "id": "2", "result": {}},
            {"id": "2", "result": {}},
            {"jsonrpc": "2.0", "result": {}},
            {"jsonrpc": "2.0", "id": "2"},
            {"jsonrpc": "2.0", "id": "2", "result": {}, "error": {}},
            {"jsonrpc": "2.0", "id": "2", "error": SECRET},
            {"jsonrpc": "2.0", "id": "2", "error": {"code": "-1", "message": SECRET}},
            {"jsonrpc": "2.0", "id": "2", "error": {"code": True, "message": SECRET}},
            {"jsonrpc": "2.0", "id": "2", "error": {"code": -1, "message": 9}},
            {"jsonrpc": "2.0", "method": "notice", "result": {}},
            {"jsonrpc": "2.0", "method": "notice", "params": SECRET},
            {"jsonrpc": "2.0", "method": ""},
            {"jsonrpc": "2.0", "method": "callback", "id": True},
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                runtime = self.fork_reply(frame(envelope))
                with self.assertRaises(module.NativeSessionError) as caught:
                    self.fork(runtime)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assert_closed(runtime)

    def test_old_or_invalid_ping_protocol_never_issues_fork(self):
        for result in ({"protocolVersion": 1}, {"protocolVersion": True}, {"protocolVersion": "3"}, {}, [], None):
            with self.subTest(result=result):
                runtime = FakeRuntime()
                runtime.handler = lambda message: runtime.incoming.extend(reply(message, result))
                with self.assertRaisesRegex(module.NativeSessionError, "protocol version"):
                    self.fork(runtime)
                self.assertEqual([item["method"] for item in runtime.messages], ["ping"])
                self.assert_closed(runtime)

    def test_protocol_two_is_supported_without_version_specific_fallback(self):
        runtime = FakeRuntime()

        def handle(message):
            if message.get("method") == "ping":
                runtime.incoming.extend(reply(message, {"protocolVersion": 2}))
            else:
                runtime.default_handler(message)

        runtime.handler = handle
        self.assertEqual(self.fork(runtime), CHILD)
        self.assert_closed(runtime)

    def test_invalid_child_id_and_source_id_return_are_rejected(self):
        for child in (None, "", " ", " child ", "child\nvalue", "child\x7fvalue", "child\x9bvalue", 22, SOURCE,
                      "x" * (module.MAX_SESSION_ID_CHARS + 1)):
            with self.subTest(child=repr(child)[:80]):
                runtime = self.fork_reply(lambda message: reply(message, {"sessionId": child}))
                with self.assertRaises(module.NativeSessionError):
                    self.fork(runtime)
                self.assert_closed(runtime)
        for result in ([], None, "child", {}):
            with self.subTest(result=result):
                runtime = self.fork_reply(lambda message: reply(message, result))
                with self.assertRaises(module.NativeSessionError):
                    self.fork(runtime)
                self.assert_closed(runtime)

    def test_invalid_source_id_never_starts_a_process(self):
        for source in (None, "", " ", " source ", "source\nvalue", 22):
            with self.subTest(source=source):
                runtime = FakeRuntime()
                with self.assertRaises(module.NativeSessionError):
                    self.fork(runtime, source=source)
                self.assertEqual(runtime.launches, [])
                self.assertEqual(runtime.signals, [])

    def test_read_timeout_terminates_then_kills_and_reaps(self):
        runtime = FakeRuntime()
        runtime.stall_reads = True
        runtime.process.wait_timeouts = [True, False]
        with self.assertRaisesRegex(module.NativeSessionError, "timed out"):
            self.fork(runtime)
        self.assertEqual(runtime.now, module.RPC_TIMEOUT_SECONDS)
        self.assert_closed(runtime)

    def test_write_timeout_has_the_same_cleanup(self):
        runtime = FakeRuntime()
        runtime.stall_writes = True
        with self.assertRaisesRegex(module.NativeSessionError, "timed out"):
            self.fork(runtime)
        self.assertEqual(runtime.messages, [])
        self.assert_closed(runtime)

    def test_one_deadline_covers_ping_and_fork(self):
        runtime = FakeRuntime()

        def handle(message):
            if message.get("method") == "ping":
                runtime.now += module.RPC_TIMEOUT_SECONDS - 1
                runtime.default_handler(message)
            else:
                runtime.stall_reads = True

        runtime.handler = handle
        with self.assertRaisesRegex(module.NativeSessionError, "timed out"):
            self.fork(runtime)
        self.assertEqual(runtime.now, module.RPC_TIMEOUT_SECONDS)
        self.assertEqual([item["method"] for item in runtime.messages], ["ping", "sessions.fork"])
        self.assert_closed(runtime)

    def test_notification_flood_does_not_extend_deadline(self):
        runtime = FakeRuntime()
        notice = frame({"jsonrpc": "2.0", "method": "event"})

        def handle(message):
            runtime.incoming.extend(notice * 100)

        runtime.handler = handle
        original_read = runtime.read

        def advancing_read(fd, size):
            runtime.now += 1
            return original_read(fd, size)

        runtime.read = advancing_read
        runtime.read_chunk = len(notice)
        with self.assertRaisesRegex(module.NativeSessionError, "timed out"):
            self.fork(runtime)
        self.assertLessEqual(runtime.now, module.RPC_TIMEOUT_SECONDS)
        self.assert_closed(runtime)

    def test_transport_errors_do_not_disclose_os_error_text(self):
        for failure in ("read_error", "write_error", "zero_write"):
            with self.subTest(failure=failure):
                runtime = FakeRuntime()
                setattr(runtime, failure, True)
                with self.assertRaises(module.NativeSessionError) as caught:
                    self.fork(runtime)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assert_closed(runtime)

    def test_startup_failure_is_sanitized(self):
        with patch.object(module.subprocess, "Popen", side_effect=OSError(SECRET)):
            with self.assertRaisesRegex(module.NativeSessionError, "Cannot start") as caught:
                module.copilot_fork("copilot", SOURCE, cwd="/source/project", environment={"TOKEN": SECRET})
        self.assertNotIn(SECRET, str(caught.exception))

    def test_pipe_setup_failure_closes_the_started_process(self):
        runtime = FakeRuntime()
        with runtime, patch.object(module.os, "set_blocking", side_effect=OSError(SECRET)):
            with self.assertRaisesRegex(module.NativeSessionError, "Cannot start") as caught:
                module.copilot_fork("copilot", SOURCE, cwd="/source/project", environment={})
        self.assertNotIn(SECRET, str(caught.exception))
        self.assert_closed(runtime)

    def test_interrupted_pipe_setup_also_closes_the_started_process(self):
        runtime = FakeRuntime()
        with runtime, patch.object(module.os, "set_blocking", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                module.copilot_fork("copilot", SOURCE, cwd="/source/project", environment={})
        self.assert_closed(runtime)

    def test_missing_process_group_does_not_prevent_reaping_or_pipe_closure(self):
        runtime = FakeRuntime()
        with runtime, patch.object(module.os, "killpg", side_effect=ProcessLookupError()):
            self.assertEqual(module.copilot_fork("copilot", SOURCE, cwd="/source/project", environment={}), CHILD)
        self.assertTrue(runtime.process.stdin.closed)
        self.assertTrue(runtime.process.stdout.closed)
        self.assertEqual(runtime.process.wait_calls, [module.CLEANUP_TIMEOUT_SECONDS] * 2)

    def test_response_received_after_deadline_is_not_accepted(self):
        runtime = FakeRuntime()
        original_read = runtime.read

        def late_read(fd, size):
            runtime.now = module.RPC_TIMEOUT_SECONDS + 1
            return original_read(fd, size)

        runtime.read = late_read
        with self.assertRaisesRegex(module.NativeSessionError, "timed out"):
            self.fork(runtime)
        self.assertEqual([message["method"] for message in runtime.messages], ["ping"])
        self.assert_closed(runtime)

    def test_cleanup_failure_does_not_return_a_successful_fork(self):
        runtime = FakeRuntime()
        runtime.process.wait_timeouts = [True, True]
        with self.assertRaisesRegex(module.NativeSessionError, "cleanup failed"):
            self.fork(runtime)
        self.assert_closed(runtime)


if __name__ == "__main__":
    unittest.main()
