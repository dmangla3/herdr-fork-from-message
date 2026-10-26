"""Owner-written, private pane bindings. No process environment or log scanning."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping

from agent_contracts import ENVIRONMENT_KEYS, PI_FAMILY

PLUGIN_ID = "dmangla3.fork-from-message"
MAX_AGE_SECONDS = 20
MAX_RECORD_BYTES = 32768
MAX_STRING_LENGTH = 4096
RECORD_FIELDS = frozenset({
    "agent", "pane_id", "terminal_id", "pid", "process_start", "session_id", "cwd",
    "model", "profile", "thinking", "session_file", "bridge_version", "read_only_ready",
    "registration_valid", "environment",
})


class RegistryError(ValueError):
    pass


def _text(value: Any, name: str, *, absolute: bool = False, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > MAX_STRING_LENGTH
            or (not allow_empty and not value.strip())
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise RegistryError(f"Invalid owner registration {name}")
    if absolute and not Path(value).is_absolute():
        raise RegistryError(f"Owner registration {name} must be absolute")
    return value


def _record(value: Mapping[str, Any], *, stored: bool = False) -> dict[str, Any]:
    allowed = RECORD_FIELDS | {"socket", "updated_at"} if stored else RECORD_FIELDS
    if not isinstance(value, Mapping) or any(key not in allowed for key in value):
        raise RegistryError("Owner registration contains unsupported fields")
    agent = _text(value.get("agent"), "agent")
    if agent not in PI_FAMILY:
        raise RegistryError("Owner registration has no supported Pi-family product")
    safe: dict[str, Any] = {"agent": agent}
    for key in ("pane_id", "terminal_id", "process_start", "session_id", "cwd"):
        safe[key] = _text(value.get(key), key, absolute=key == "cwd")
    pid = value.get("pid")
    if type(pid) is not int or pid <= 0:
        raise RegistryError("Owner registration PID must be a positive integer")
    safe["pid"] = pid
    for key in ("model", "profile", "thinking", "session_file"):
        if key in value and value[key] is not None:
            safe[key] = _text(value[key], key, absolute=key == "session_file")
    if "bridge_version" in value:
        if type(value["bridge_version"]) is not int or value["bridge_version"] != 1:
            raise RegistryError("Unsupported owner registration bridge version")
        safe["bridge_version"] = value["bridge_version"]
    for key in ("read_only_ready", "registration_valid"):
        if key in value:
            if type(value[key]) is not bool:
                raise RegistryError(f"Owner registration {key} must be boolean")
            safe[key] = value[key]
    environment = value.get("environment", {})
    if not isinstance(environment, Mapping) or any(key not in ENVIRONMENT_KEYS[agent] for key in environment):
        raise RegistryError("Owner registration contains unsupported environment keys")
    safe["environment"] = {key: _text(item, key, absolute=True) for key, item in environment.items()}
    if stored:
        safe["socket"] = _text(value.get("socket"), "socket", allow_empty=True)
        updated = value.get("updated_at")
        if type(updated) not in (int, float):
            raise RegistryError("Invalid owner registration timestamp")
        try:
            updated = float(updated)
        except OverflowError as exc:
            raise RegistryError("Invalid owner registration timestamp") from exc
        if not math.isfinite(updated) or updated <= 0:
            raise RegistryError("Invalid owner registration timestamp")
        safe["updated_at"] = updated
    return safe


def state_root(env: Mapping[str, str]) -> Path:
    for key in ("HERDR_FORK_STATE_DIR", "HERDR_PLUGIN_STATE_DIR"):
        if key in env:
            return Path(_text(env[key], "Plugin state directory", absolute=True))
    base = env["XDG_STATE_HOME"] if "XDG_STATE_HOME" in env else str(Path.home() / ".local/state")
    return Path(_text(base, "Plugin state directory", absolute=True)) / "herdr/plugins" / PLUGIN_ID


def record_path(root: Path, socket: str, pane: str) -> Path:
    _text(str(root), "Plugin state directory", absolute=True)
    _text(socket, "socket", allow_empty=True)
    _text(pane, "pane_id")
    digest = hashlib.sha256((socket + "\0" + pane).encode()).hexdigest()
    return root / "owners" / (digest + ".json")


def process_start(pid: int) -> str:
    try:
        p = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RegistryError("Owner process start could not be verified") from exc
    value = p.stdout.strip()
    if not value:
        raise RegistryError("Owner process no longer exists")
    return value


def descendant_of(pid: int, owner: int) -> bool:
    for _ in range(64):
        if pid == owner:
            return True
        if pid <= 1:
            return False
        try:
            p = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return False
        try:
            pid = int(p.stdout.strip())
        except ValueError:
            return False
    return False


def write_record(root: Path, socket: str, data: Mapping[str, Any]) -> Path:
    safe = _record(data)
    destination = record_path(root, socket, safe["pane_id"])
    content = json.dumps({**safe, "socket": socket, "updated_at": time.time()}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(content) > MAX_RECORD_BYTES:
        raise RegistryError("Owner registration exceeds the private record size limit")
    directory = root / "owners"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RegistryError("Unsafe owner state directory")
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".owner-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def read_record(root: Path, socket: str, pane: Mapping[str, Any], pid: int) -> dict[str, Any]:
    path = record_path(root, socket, pane.get("pane_id"))
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            owner = os.fstat(directory)
            if not stat.S_ISDIR(owner.st_mode) or owner.st_uid != os.getuid() or owner.st_mode & 0o077:
                raise RegistryError("Unsafe owner state directory permissions")
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_RECORD_BYTES:
                raise RegistryError("Unsafe owner record permissions or size")
            content = stream.read(MAX_RECORD_BYTES + 1)
            if len(content.encode("utf-8")) > MAX_RECORD_BYTES:
                raise RegistryError("Owner registration exceeds the private record size limit")
            value = json.loads(content)
    except RegistryError:
        raise
    except (OSError, ValueError) as exc:
        raise RegistryError("No fresh owner registration. Enable the plugin runtime bridge and restart this agent.") from exc
    value = _record(value, stored=True)
    if value.get("registration_valid") is False:
        raise RegistryError("Owner session changed; restart the agent before forking")
    checks = {
        "socket": socket, "pane_id": pane["pane_id"], "terminal_id": pane.get("terminal_id"),
        "pid": pid, "process_start": process_start(pid), "agent": pane.get("agent"),
    }
    if any(value.get(key) != expected for key, expected in checks.items()):
        raise RegistryError("Owner registration no longer belongs to this exact pane/process")
    age = time.time() - value["updated_at"]
    if age < -5 or age > MAX_AGE_SECONDS:
        raise RegistryError("Owner registration expired; the runtime bridge is not reporting")
    cwd = pane.get("foreground_cwd") or pane.get("cwd")
    if Path(value["cwd"]).resolve() != Path(_text(cwd, "cwd", absolute=True)).resolve():
        raise RegistryError("Owner registration working directory changed")
    return value
