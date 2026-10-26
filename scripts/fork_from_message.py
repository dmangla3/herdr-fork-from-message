#!/usr/bin/env python3
"""Fork exact native conversations in Herdr without replaying a source prompt."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
import uuid

from agent_contracts import AGENTS, ENVIRONMENT_KEYS, PI_FAMILY, UNAVAILABLE, ContractError, fork_launch, safe_launch_flags
from harness_identity import entrypoint_argv, select_process, session_id, session_reference
from native_sessions import NativeSessionError, copilot_fork
from runtime_registry import PLUGIN_ID, RegistryError, descendant_of, process_start, read_record, state_root, write_record

ROOT = Path(__file__).resolve().parents[1]
DESTINATIONS = ("tab", "pane", "workspace")
MODES = ("message", "fork", "read-only")
EXPERIMENTAL = ("opencode", "copilot")
START_SECONDS = 45
SHELL_SECONDS = 8
PICKER_SECONDS = 8
POLL_SECONDS = 0.2
TRANSCRIPT_MARKERS = ("edit prev",)
PICKER_MARKERS = ("edit next", "edit message")
CLAUDE_PICKER_MARKERS = ("restore the code and/or conversation",)


class ForkError(RuntimeError):
    """A native fork could not be verified safely."""


class HerdrClient:
    def __init__(self, executable: str, session: str | None = None):
        self.session = session
        self.prefix = [executable, *(["--session", session] if session else [])]

    def _run(self, args: Sequence[str], timeout: float) -> str:
        try:
            return subprocess.run([*self.prefix, *args], capture_output=True, text=True,
                                  check=True, timeout=timeout).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            # CLI stderr can contain source arguments. Do not echo it into a notification.
            if "agent_pane_busy" in (getattr(exc, "stderr", "") or ""):
                raise ForkError("agent_pane_busy") from exc
            raise ForkError(f"Herdr {' '.join(args[:2])} failed ({type(exc).__name__}); inspect its local plugin log") from exc

    def json(self, args: Sequence[str], timeout: float = 15) -> dict[str, Any]:
        output = self._run(args, timeout)
        if not output.strip() and list(args[:2]) == ["pane", "run"]:
            return {}
        try:
            value = json.loads(output)
        except ValueError as exc:
            raise ForkError("Herdr returned invalid JSON") from exc
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            if value["error"].get("code") == "agent_pane_busy":
                raise ForkError("agent_pane_busy")
        if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
            raise ForkError(f"Herdr rejected {' '.join(args[:2])}; inspect its local plugin log")
        return value["result"]

    def text(self, args: Sequence[str], timeout: float = 15) -> str:
        return self._run(args, timeout)


def object_value(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForkError(f"Missing {name}")
    return value


def native_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ForkError("Missing native session ID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ForkError("Invalid native session ID") from exc
    if str(parsed) != value.lower():
        raise ForkError("Noncanonical native session ID")
    return str(parsed)


def foreground(client: HerdrClient, pane_id: str, agent: str | None = None) -> dict[str, Any]:
    info = object_value(client.json(["pane", "process-info", "--pane", pane_id]).get("process_info"), "process info")
    processes = info.get("foreground_processes")
    if not isinstance(processes, list):
        raise ForkError("Pane has no foreground process list")
    product, process = select_process([p for p in processes if isinstance(p, dict)], agent)
    process["product"] = product
    if type(process.get("pid")) is not int or process["pid"] <= 1:
        raise ForkError("Foreground process has no valid PID")
    return process


@dataclass(frozen=True)
class ForkResult:
    status: str
    source_pane_id: str
    source_session_id: str
    pane_id: str
    fork_session_id: str | None
    destination: str
    workspace_id: str
    tab_id: str
    cwd: str
    agent: str
    restrictions: str
    mode: str


class ForkWorkflow:
    def __init__(self, client: HerdrClient, context: Mapping[str, Any], env: Mapping[str, str],
                 destination: str = "tab", mode: str = "message", direction: str = "auto",
                 focus: bool = True, allow_experimental: bool = False):
        if destination not in DESTINATIONS or mode not in MODES or direction not in ("auto", "right", "down"):
            raise ForkError("Invalid fork placement or mode")
        self.client, self.context, self.env = client, context, env
        self.destination, self.mode, self.direction, self.focus = destination, mode, direction, focus
        self.allow_experimental = allow_experimental
        self.target: dict[str, str] | None = None
        self.resource: tuple[str, str] | None = None
        self.launch_attempted = False
        self.native_child_id: str | None = None

    def execute(self) -> ForkResult:
        try:
            return self._execute()
        except (ForkError, ContractError, RegistryError, NativeSessionError, OSError, ValueError) as exc:
            suffix = ""
            if self.resource:
                kind, resource_id = self.resource
                if self.launch_attempted:
                    suffix = f" Destination preserved: {kind} {resource_id}."
                else:
                    try:
                        self.client.json([kind, "close", resource_id])
                    except ForkError:
                        suffix = f" Could not close empty {kind} {resource_id}; inspect it manually."
            if self.native_child_id:
                suffix += f" Native fork retained: {self.native_child_id}."
            raise ForkError(str(exc) + suffix) from exc

    def _execute(self) -> ForkResult:
        source_id = self.context.get("focused_pane_id")
        if not isinstance(source_id, str) or not source_id:
            raise ForkError("No explicit source pane; invoke a plugin action or use --session and --pane")
        pane = object_value(self.client.json(["pane", "get", source_id]).get("pane"), "source pane")
        if pane.get("pane_id") != source_id or pane.get("workspace_id") != self.context.get("workspace_id"):
            raise ForkError("Source pane/workspace identity changed")
        reported = pane.get("agent")
        if reported and reported not in AGENTS and reported not in ("antigravity_cli", "antigravity-cli", "cursor-agent"):
            raise ForkError("Recognized harnesses: " + ", ".join(AGENTS))
        process = foreground(self.client, source_id, reported)
        agent = process["product"]
        pane = {**pane, "agent": agent}
        if agent in UNAVAILABLE:
            raise ForkError(f"Automatic {AGENTS[agent]} fork unavailable: {UNAVAILABLE[agent]}. Source was not resumed or changed")
        if self.mode == "message" and agent not in ("claude", "codex"):
            raise ForkError("Earlier-message picker supports Claude and Codex only; select --mode fork")
        if agent in EXPERIMENTAL and not self.allow_experimental:
            raise ForkError(f"{AGENTS[agent]} is experimental; verify matching source configuration roots and use --allow-experimental")
        if pane.get("agent_status") not in ("idle", "done"):
            raise ForkError("Source must be idle before forking; no input was sent")
        cwd = pane.get("foreground_cwd") or pane.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ForkError("Source working directory is unavailable")
        owner = None
        if agent in PI_FAMILY:
            owner = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), pane, process["pid"])
            reference = owner.get("session_file") or native_id(owner["session_id"])
            if self.mode == "read-only" and owner.get("bridge_version") != 1:
                raise ForkError("Read-only mode requires the runtime guard bridge")
        else:
            reference = session_reference(pane, agent)
        environment = dict(self.env)
        if owner:
            for key in ENVIRONMENT_KEYS[agent]:
                environment.pop(key, None)
            environment.update(owner.get("environment", {}))
        environment["HERDR_FORK_STATE_DIR"] = str(state_root(self.env))
        source_session_id = native_id(owner["session_id"]) if owner else session_id(reference)
        if session_id(reference) != source_session_id:
            raise ForkError("Owner session file does not match its registered session ID")
        mcp = self._codex_mcp(process, environment) if agent == "codex" and self.mode == "read-only" else []
        launch = fork_launch(agent, reference, entrypoint_argv(process), environment,
                             read_only=self.mode == "read-only", bridge=ROOT / "scripts/runtime_bridge.ts",
                             omp_overlay=ROOT / "scripts/read-only-omp.yml", mcp_servers=mcp, runtime=owner)
        expected_id = str(uuid.uuid4()) if agent == "claude" else None
        arguments = [*launch.argv]
        if expected_id:
            arguments.extend(("--session-id", expected_id))
        if agent in ("pi", "omo") or (agent == "omp" and self.mode != "read-only"):
            arguments.extend(("--extension", str(ROOT / "scripts/runtime_bridge.ts")))
        fingerprint = (pane.get("terminal_id"), process["pid"], process["argv"], str(Path(cwd).resolve()))
        self._check_source(source_id, agent, source_session_id, reference, owner, fingerprint)
        if agent == "copilot":
            executable = process["argv"][0] if Path(process["argv"][0]).name == "copilot" else "copilot"
            executable = shutil.which(executable) or executable
            expected_id = copilot_fork(executable, source_session_id, cwd=cwd, environment=environment)
            native_id(expected_id)
            self.native_child_id = expected_id
            arguments[2] = expected_id
            self._check_source(source_id, agent, source_session_id, reference, owner, fingerprint)
        target = self._create_target(pane, cwd, launch.environment)
        self.target = target
        self._wait_shell(target["pane_id"])
        self._check_source(source_id, agent, source_session_id, reference, owner, fingerprint)
        self.launch_attempted = True
        if agent in PI_FAMILY:
            unset = ["HERDR_PLUGIN_STATE_DIR"]
            if owner:
                unset.extend(key for key in ENVIRONMENT_KEYS[agent] if key not in owner.get("environment", {}))
            assignments = [f"{key}={value}" for key, value in launch.environment.items()]
            argv = ["env", *[part for key in unset for part in ("-u", key)], *assignments, *arguments]
            self._start_shell(target["pane_id"], argv)
        else:
            self._start_native(target["pane_id"], agent, arguments[1:])
        child_id = self._verify_child(target["pane_id"], agent, source_session_id, expected_id)
        self._check_source(source_id, agent, source_session_id, reference, owner, fingerprint)
        if self.mode == "message":
            self._picker(target["pane_id"], agent)
        label = "Read-only fork" if self.mode == "read-only" else "Fork"
        self.client.json(["pane", "rename", target["pane_id"], f"{label} - {AGENTS[agent]}"])
        if self.focus:
            if self.destination == "tab":
                self.client.json(["tab", "focus", target["tab_id"]])
            elif self.destination == "workspace":
                self.client.json(["workspace", "focus", target["workspace_id"]])
            else:
                self.client.json(["agent", "focus", target["pane_id"]])
        return ForkResult("ready" if child_id else "started_identity_pending", source_id, source_session_id,
                          target["pane_id"], child_id, self.destination, target["workspace_id"],
                          target["tab_id"], cwd, agent, launch.restrictions, self.mode)

    def _check_source(self, pane_id: str, agent: str, source_id: str, reference: str,
                      owner: Mapping[str, Any] | None, fingerprint: tuple[Any, ...]) -> None:
        pane = object_value(self.client.json(["pane", "get", pane_id]).get("pane"), "source recheck")
        current = foreground(self.client, pane_id, agent)
        cwd = pane.get("foreground_cwd") or pane.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ForkError("Source working directory changed or is unavailable")
        if (pane.get("terminal_id"), current["pid"], current["argv"], str(Path(cwd).resolve())) != fingerprint:
            raise ForkError("Source process changed during fork preparation")
        if owner:
            fresh = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), {**pane, "agent": agent}, current["pid"])
            fresh_reference = fresh.get("session_file") or native_id(fresh["session_id"])
            unchanged = native_id(fresh["session_id"]) == source_id and fresh_reference == reference and session_id(fresh_reference) == source_id
        else:
            unchanged = session_reference(pane, agent) == reference
        if not unchanged:
            raise ForkError("Source identity changed or session switched during fork")
        if pane.get("agent_status") not in ("idle", "done"):
            raise ForkError("Source is no longer idle; fork preparation stopped")

    def _codex_mcp(self, process: Mapping[str, Any], environment: Mapping[str, str]) -> list[tuple[str, str]]:
        try:
            flags = safe_launch_flags("codex", entrypoint_argv(process))
            profile = [part for index in range(0, len(flags), 2) if flags[index] in ("--profile", "-p") for part in flags[index:index + 2]]
            executable = process["argv"][0]
            result = subprocess.run([executable, *profile, "mcp", "list", "--json"], capture_output=True,
                                    text=True, check=True, timeout=15, env=dict(environment))
            entries = json.loads(result.stdout)
            if not isinstance(entries, list):
                raise ValueError("expected MCP list")
            return [(entry["name"], entry["transport"]["type"]) for entry in entries]
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            raise ForkError("Cannot enumerate Codex MCP servers; refusing read-only mode") from exc

    def _split_direction(self, source: str) -> str:
        if self.direction != "auto":
            return self.direction
        layout = object_value(self.client.json(["pane", "layout", "--pane", source]).get("layout"), "pane layout")
        for pane in layout.get("panes", []):
            if isinstance(pane, dict) and pane.get("pane_id") == source:
                rect = pane.get("rect", {})
                width, height = rect.get("width"), rect.get("height")
                if type(width) is int and type(height) is int and width > 0 and height > 0:
                    return "right" if width >= height * 2 else "down"
        raise ForkError("Source pane geometry is unavailable; specify --direction right or down")

    def _create_target(self, source: Mapping[str, Any], cwd: str, environment: Mapping[str, str]) -> dict[str, str]:
        env_args = [part for key, value in environment.items() for part in ("--env", f"{key}={value}")]
        if self.destination == "pane":
            result = self.client.json(["pane", "split", source["pane_id"], "--direction", self._split_direction(source["pane_id"]),
                                       "--cwd", cwd, *env_args, "--no-focus"])
            pane = object_value(result.get("pane"), "created pane")
            if isinstance(pane.get("pane_id"), str):
                self.resource = ("pane", pane["pane_id"])
            return self._target(pane, pane, pane)
        args = [self.destination, "create", "--cwd", cwd, "--label", "Conversation fork", *env_args, "--no-focus"]
        if self.destination == "tab":
            args.extend(("--workspace", source["workspace_id"]))
        result = self.client.json(args)
        resource = object_value(result.get(self.destination), "created " + self.destination)
        key = "tab_id" if self.destination == "tab" else "workspace_id"
        if isinstance(resource.get(key), str):
            self.resource = (self.destination, resource[key])
        return self._target(object_value(result.get("root_pane"), "created root pane"),
                            object_value(result.get("tab"), "created tab"),
                            resource if self.destination == "workspace" else source)

    @staticmethod
    def _target(pane: Mapping[str, Any], tab: Mapping[str, Any], workspace: Mapping[str, Any]) -> dict[str, str]:
        values = {"pane_id": pane.get("pane_id"), "tab_id": tab.get("tab_id"), "workspace_id": workspace.get("workspace_id")}
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ForkError("Herdr did not return every created destination ID")
        return values

    def _wait_shell(self, pane_id: str) -> None:
        deadline = time.monotonic() + SHELL_SECONDS
        while time.monotonic() < deadline:
            info = object_value(self.client.json(["pane", "process-info", "--pane", pane_id]).get("process_info"), "child process info")
            processes = info.get("foreground_processes", [])
            if len(processes) == 1 and processes[0].get("pid") == info.get("shell_pid"):
                return
            time.sleep(POLL_SECONDS)
        raise ForkError("New pane has not reached its shell prompt")

    def _start_native(self, pane: str, agent: str, arguments: Sequence[str]) -> None:
        args = ["agent", "start", f"fork-{uuid.uuid4().hex[:10]}", "--kind", agent, "--pane", pane,
                "--timeout", str(START_SECONDS * 1000), "--", *arguments]
        deadline = time.monotonic() + SHELL_SECONDS
        while True:
            try:
                self.client.json(args, timeout=START_SECONDS + 10)
                return
            except ForkError as exc:
                if "agent_pane_busy" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_SECONDS)

    def _start_shell(self, pane: str, argv: Sequence[str]) -> None:
        fd, filename = tempfile.mkstemp(prefix="herdr-fork-", suffix=".sh")
        path = Path(filename)
        try:
            if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-" for c in filename):
                os.close(fd)
                raise ForkError("Temporary launcher path is not shell-neutral")
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write("#!/bin/sh\nrm -f -- " + shlex.quote(filename) + "\nexec " + shlex.join(argv) + "\n")
            self.client.json(["pane", "run", pane, "/bin/sh", filename])
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _verify_child(self, pane_id: str, agent: str, parent_id: str, expected_id: str | None) -> str | None:
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            pane = object_value(self.client.json(["pane", "get", pane_id]).get("pane"), "child pane")
            if pane.get("agent_status") == "blocked":
                raise ForkError("Child requires login, trust, or permission input; no approval was answered")
            if pane.get("agent") != agent and not (agent == "omo" and pane.get("agent") == "pi"):
                time.sleep(POLL_SECONDS)
                continue
            try:
                process = foreground(self.client, pane_id, agent)
            except ContractError:
                time.sleep(POLL_SECONDS)
                continue
            if agent in PI_FAMILY:
                try:
                    record = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), {**pane, "agent": agent}, process["pid"])
                except RegistryError:
                    time.sleep(POLL_SECONDS)
                    continue
                child_id = native_id(record["session_id"])
                if self.mode == "read-only" and not record.get("read_only_ready"):
                    raise ForkError("Child read-only guard not ready")
            elif pane.get("agent_session") is not None:
                # Invalid or mismatched metadata is not the same as a delayed report.
                child_id = session_id(session_reference(pane, agent))
            else:
                argv = entrypoint_argv(process)
                if pane.get("agent_status") in ("idle", "done"):
                    if agent == "claude" and expected_id and "--session-id" in argv:
                        index = argv.index("--session-id")
                        if index + 1 < len(argv) and argv[index + 1] == expected_id:
                            return expected_id
                    if agent == "codex":
                        return None
                time.sleep(POLL_SECONDS)
                continue
            if child_id == parent_id:
                raise ForkError("Child reported the parent session ID instead of an independent fork")
            if expected_id and child_id != expected_id:
                raise ForkError("Child session ID differs from the native fork result")
            if pane.get("agent_status") in ("idle", "done"):
                return child_id
            time.sleep(POLL_SECONDS)
        raise ForkError("Child startup/identity could not be verified before timeout")

    def _wait_markers(self, pane: str, markers: tuple[str, ...], description: str) -> None:
        deadline = time.monotonic() + PICKER_SECONDS
        while time.monotonic() < deadline:
            screen = self.client.text(["agent", "read", pane, "--source", "detection", "--lines", "120"]).lower()
            if "hooks need review" in screen:
                raise ForkError("Child is waiting for hook trust review; no approval was answered")
            if any(marker in screen for marker in markers):
                return
            time.sleep(POLL_SECONDS)
        raise ForkError(f"Native {description} did not appear; inspect the preserved destination")

    def _picker(self, pane: str, agent: str) -> None:
        if agent == "codex":
            self.client.json(["agent", "send-keys", pane, "ctrl+t"])
            self._wait_markers(pane, TRANSCRIPT_MARKERS, "Codex transcript")
            self.client.json(["agent", "send-keys", pane, "esc"])
            self._wait_markers(pane, PICKER_MARKERS, "Codex message picker")
        else:
            self.client.json(["agent", "send-keys", pane, "esc", "esc"])
            self._wait_markers(pane, CLAUDE_PICKER_MARKERS, "Claude Rewind picker")


def register(client: HerdrClient, env: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
    pane_id = env.get("HERDR_PANE_ID", "")
    pane = object_value(client.json(["pane", "get", pane_id]).get("pane"), "owner pane")
    kind = payload.get("agent")
    if kind not in PI_FAMILY or not (pane.get("agent") == kind or (kind == "omo" and pane.get("agent") == "pi")):
        raise ForkError("Registration agent does not match pane owner")
    process = foreground(client, pane_id, kind)
    pid = process["pid"]
    if payload.get("pid") != pid or not descendant_of(os.getpid(), pid):
        raise ForkError("Registration must originate inside the exact foreground owner process")
    session = native_id(payload.get("session_id"))
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ForkError("Owner cwd must be an absolute path")
    if Path(cwd).resolve() != Path(pane.get("foreground_cwd") or pane.get("cwd", "")).resolve():
        raise ForkError("Registration cwd does not match owner cwd")
    fields = {"agent", "pid", "cwd", "session_id", "model", "profile", "thinking", "session_file",
              "bridge_version", "read_only_ready", "registration_valid", "environment"}
    if set(payload) - fields:
        raise ForkError("Owner registration contains unsupported fields")
    environment = payload.get("environment", {})
    if not isinstance(environment, dict) or any(key not in ENVIRONMENT_KEYS[kind] for key in environment):
        raise ForkError("Owner registration contains unsupported environment keys")
    safe = {key: value for key, value in payload.items() if key not in ("agent", "pid", "cwd", "session_id")}
    path = safe.get("session_file")
    if isinstance(path, str) and Path(path).is_absolute() and not Path(path).is_file():
        safe.pop("session_file")  # Native runtimes can allocate IDs before persisting a first turn.
    safe.update(agent=kind, pane_id=pane_id, terminal_id=pane["terminal_id"], pid=pid,
                process_start=process_start(pid), session_id=session, cwd=cwd)
    output = write_record(state_root(env), env.get("HERDR_SOCKET_PATH", ""), safe)
    return {"status": "registered", "path": str(output), "session_id": session}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fork from Message: native Herdr conversation forks")
    parser.add_argument("--destination", choices=DESTINATIONS, default="tab")
    parser.add_argument("--mode", choices=MODES, default="message")
    parser.add_argument("--direction", choices=("auto", "right", "down"), default="auto")
    parser.add_argument("--no-focus", action="store_true")
    parser.add_argument("--pane")
    parser.add_argument("--session")
    parser.add_argument("--allow-experimental", action="store_true")
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args(argv)
    try:
        env = dict(os.environ)
        selected = args.session or env.get("HERDR_FORK_SESSION")
        if args.pane and not selected:
            raise ForkError("Explicit --pane automation requires --session")
        if env.get("HERDR_ENV") != "1" and not (args.pane and selected):
            raise ForkError("Invoke an installed Herdr plugin action or select --session and --pane")
        if selected:
            if any(ord(c) < 32 for c in selected):
                raise ForkError("Invalid Herdr session selector")
            env["HERDR_FORK_SESSION"] = selected
        executable = env.get("HERDR_BIN_PATH") or shutil.which("herdr")
        if not executable:
            raise ForkError("Herdr is unavailable in PATH and HERDR_BIN_PATH was not provided")
        client = HerdrClient(executable, selected)
        if args.register:
            raw = sys.stdin.read(32769)
            if len(raw) > 32768:
                raise ForkError("Owner registration exceeds its size limit")
            output = register(client, env, object_value(json.loads(raw), "registration payload"))
        else:
            if args.pane:
                # External controllers do not inherit the source terminal's socket.
                # Resolve it from this selected server, never the focused pane or HOME.
                status = client.text(["status"])
                socket = next((line.strip().partition(":")[2].strip() for line in status.splitlines()
                               if line.strip().startswith("socket:")), None)
                if not socket or not Path(socket).is_absolute():
                    raise ForkError("Selected Herdr server did not report an absolute socket path")
                env["HERDR_SOCKET_PATH"] = socket
                pane = object_value(client.json(["pane", "get", args.pane]).get("pane"), "explicit source")
                context = {"focused_pane_id": args.pane, "workspace_id": pane["workspace_id"]}
            else:
                raw = env.get("HERDR_PLUGIN_CONTEXT_JSON")
                if not raw:
                    raise ForkError("Herdr did not provide plugin context")
                context = object_value(json.loads(raw), "invocation context")
            output = asdict(ForkWorkflow(client, context, env, args.destination, args.mode,
                                        args.direction, not args.no_focus, args.allow_experimental).execute())
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except (ForkError, RegistryError, ContractError, NativeSessionError, OSError, ValueError) as exc:
        print(f"Fork from Message failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
