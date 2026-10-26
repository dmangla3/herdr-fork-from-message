"""Bounded, private stdio RPC operations for native session forks (POSIX)."""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from typing import Any, Mapping, Sequence

RPC_TIMEOUT_SECONDS = 30.0
CLEANUP_TIMEOUT_SECONDS = 2.0
MAX_FRAME_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 8192
MAX_SESSION_ID_CHARS = 4096


class NativeSessionError(RuntimeError):
    """A native fork could not be completed safely."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


class _StdioJsonRpc:
    """One sequential JSON-RPC connection with an operation-wide deadline.

    The process has a private process group. Closing the connection terminates
    that group, escalates to SIGKILL, reaps the child, and closes both pipes.
    Server callbacks are rejected; no tool, authentication, or permission handler
    is installed. Neither stderr nor peer-provided error text is exposed.
    """

    def __init__(self, argv: Sequence[str], *, cwd: str, environment: Mapping[str, str],
                 timeout: float = RPC_TIMEOUT_SECONDS):
        self.argv = list(argv)
        self.cwd = cwd
        self.environment = environment
        self.timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._deadline = 0.0
        self._input_fd = -1
        self._output_fd = -1
        self._buffer = bytearray()
        self._next_id = 0

    def __enter__(self) -> _StdioJsonRpc:
        if os.name != "posix":
            raise NativeSessionError("Native session stdio forks require a POSIX process environment")
        if not 0 < self.timeout <= RPC_TIMEOUT_SECONDS:
            raise NativeSessionError("Native session RPC requires a finite, bounded timeout")
        self._deadline = time.monotonic() + self.timeout
        try:
            self._process = subprocess.Popen(
                self.argv, cwd=self.cwd, env=dict(self.environment),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, start_new_session=True,
            )
            if self._process.stdin is None or self._process.stdout is None:
                raise OSError("Missing RPC pipes")
            self._input_fd = self._process.stdin.fileno()
            self._output_fd = self._process.stdout.fileno()
            os.set_blocking(self._input_fd, False)
            os.set_blocking(self._output_fd, False)
        except (OSError, ValueError, TypeError):
            self.close()
            raise NativeSessionError(
                "Cannot start native session RPC; check the executable, working directory, and environment"
            ) from None
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        failed = False
        try:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                except OSError:
                    failed = True
                try:
                    process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    if sig == signal.SIGKILL:
                        failed = True
                except OSError:
                    failed = True
                # Kill the private group even if its leader has already exited:
                # descendants must not retain a headless server or either pipe.
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        failed = True
            self._buffer.clear()
        if failed:
            raise NativeSessionError(
                "Native session RPC cleanup failed; verify that the private headless process has stopped"
            ) from None

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise NativeSessionError(
                "Native session RPC timed out; check CLI authentication and native fork support before retrying"
            )
        return remaining

    def _wait(self, *, writing: bool) -> None:
        while True:
            remaining = self._remaining()
            try:
                readable, writable, _ = select.select(
                    [] if writing else [self._output_fd],
                    [self._input_fd] if writing else [], [], remaining,
                )
            except InterruptedError:
                continue
            if (writing and writable) or (not writing and readable):
                return

    def _send(self, message: Mapping[str, Any]) -> None:
        try:
            content = json.dumps(message, separators=(",", ":"), ensure_ascii=False,
                                 allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise NativeSessionError("Native session RPC cannot encode the request") from None
        if not 0 < len(content) <= MAX_FRAME_BYTES:
            raise NativeSessionError("Native session RPC request exceeds the frame size limit")
        frame = f"Content-Length: {len(content)}\r\n\r\n".encode("ascii") + content
        pending = memoryview(frame)
        while pending:
            self._wait(writing=True)
            try:
                written = os.write(self._input_fd, pending)
            except (BlockingIOError, InterruptedError):
                continue
            if written <= 0:
                raise NativeSessionError("Native session RPC input closed before the request was sent")
            pending = pending[written:]

    def _read_more(self, count: int) -> None:
        while True:
            self._wait(writing=False)
            try:
                data = os.read(self._output_fd, count)
            except (BlockingIOError, InterruptedError):
                continue
            if not data:
                raise NativeSessionError(
                    "Native session RPC output closed before a complete reply; check CLI compatibility and authentication"
                )
            self._buffer.extend(data)
            return

    def _receive(self) -> dict[str, Any]:
        while True:
            boundary = self._buffer.find(b"\r\n\r\n")
            if boundary >= 0:
                if boundary + 4 > MAX_HEADER_BYTES:
                    raise NativeSessionError("Native session RPC header exceeds the size limit")
                break
            if len(self._buffer) >= MAX_HEADER_BYTES:
                raise NativeSessionError("Native session RPC header is malformed or exceeds the size limit")
            self._read_more(min(4096, MAX_HEADER_BYTES - len(self._buffer)))
        try:
            header = bytes(self._buffer[:boundary]).decode("ascii")
            headers: dict[str, str] = {}
            for line in header.split("\r\n"):
                name, separator, value = line.partition(":")
                if not separator or not name or not all(
                    character.isalnum() or character == "-" for character in name
                ):
                    raise ValueError("Invalid header")
                key = name.lower()
                if key in headers or any(ord(character) < 32 for character in value):
                    raise ValueError("Invalid header")
                headers[key] = value.strip()
            length = headers.get("content-length", "")
            if not length or not length.isascii() or not length.isdecimal():
                raise ValueError("Invalid length")
            size = int(length)
        except (ValueError, UnicodeError):
            raise NativeSessionError("Native session RPC returned a malformed Content-Length header") from None
        if not 0 < size <= MAX_FRAME_BYTES:
            raise NativeSessionError("Native session RPC frame length is outside the permitted size limit")
        del self._buffer[:boundary + 4]
        while len(self._buffer) < size:
            self._read_more(min(65536, size - len(self._buffer)))
        content = bytes(self._buffer[:size])
        del self._buffer[:size]
        try:
            message = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                                 parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise NativeSessionError("Native session RPC returned malformed JSON") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise NativeSessionError("Native session RPC returned an invalid JSON-RPC 2.0 message")
        return message

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        if self._process is None:
            raise NativeSessionError("Native session RPC is not running")
        self._next_id += 1
        request_id = str(self._next_id)
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
            while True:
                self._remaining()
                message = self._receive()
                self._remaining()
                if "method" in message:
                    if (not isinstance(message["method"], str) or not message["method"] or
                            "result" in message or "error" in message or
                            ("params" in message and not isinstance(message["params"], (dict, list)))):
                        raise NativeSessionError("Native session RPC returned a malformed request or notification")
                    if "id" in message:
                        if type(message["id"]) not in (str, int):
                            raise NativeSessionError("Native session RPC returned a malformed server request ID")
                        self._send({"jsonrpc": "2.0", "id": message["id"], "error": {
                            "code": -32601, "message": "Client callbacks are disabled for native session forks",
                        }})
                    continue
                if type(message.get("id")) is not str or message["id"] != request_id:
                    raise NativeSessionError("Native session RPC returned an unexpected response ID")
                if ("result" in message) == ("error" in message):
                    raise NativeSessionError("Native session RPC returned a malformed response")
                if "error" in message:
                    error = message["error"]
                    if (not isinstance(error, dict) or type(error.get("code")) is not int or
                            not isinstance(error.get("message"), str)):
                        raise NativeSessionError("Native session RPC returned a malformed error response")
                    if error["code"] == -32601:
                        raise NativeSessionError(
                            "The CLI does not support the required native session RPC; upgrade the CLI before retrying"
                        )
                    raise NativeSessionError(
                        f"Native session RPC was rejected (code {error['code']}); "
                        "check CLI authentication, source session availability, and native fork support"
                    )
                return message["result"]
        except (OSError, ValueError, TypeError):
            raise NativeSessionError("Native session RPC transport failed; check CLI compatibility before retrying") from None


def _valid_session_id(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_SESSION_ID_CHARS and
            value == value.strip() and all(ord(character) >= 32 and not 127 <= ord(character) <= 159
                                          and not 0xD800 <= ord(character) <= 0xDFFF for character in value))


def copilot_fork(executable: str, source_id: str, *, cwd: str, environment: Mapping[str, str]) -> str:
    """Fork all source events through Copilot's native, experimental sessions.fork.

    Schema: github/copilot-sdk v0.2.2 nodejs/src/generated/rpc.ts. Omitting
    toEventId includes every event. No source resume, prompt, slash command,
    provider request, or client approval is sent. The returned sessionId is the
    server's fork acknowledgement; no undocumented persistence RPC is assumed.
    """
    if not isinstance(executable, str) or not executable.strip():
        raise NativeSessionError("A Copilot CLI executable is required for native session forks")
    if not _valid_session_id(source_id):
        raise NativeSessionError("A nonempty native source session ID without surrounding whitespace is required")
    with _StdioJsonRpc(
        [executable, "--headless", "--no-auto-update", "--log-level", "error", "--stdio"],
        cwd=cwd, environment=environment, timeout=RPC_TIMEOUT_SECONDS,
    ) as rpc:
        ping = rpc.request("ping", {})
        if (not isinstance(ping, dict) or type(ping.get("protocolVersion")) is not int or
                ping["protocolVersion"] < 2):
            raise NativeSessionError("Copilot native session forks require RPC protocol version 2 or newer; upgrade the CLI")
        result = rpc.request("sessions.fork", {"sessionId": source_id})
        child_id = result.get("sessionId") if isinstance(result, dict) else None
        if not _valid_session_id(child_id):
            raise NativeSessionError("Copilot native fork returned no valid child session ID; verify CLI native fork support")
        if child_id == source_id:
            raise NativeSessionError("Copilot native fork returned the source session ID; refusing to resume the source")
        return child_id
