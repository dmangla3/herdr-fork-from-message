"""Native fork launch contracts with opt-in read-only restrictions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
import uuid

AGENTS = {
    "claude": "Claude Code", "codex": "Codex", "omp": "OMP", "gjc": "GJC",
    "pi": "Pi", "omo": "OmO", "opencode": "OpenCode", "cursor": "Cursor",
    "copilot": "GitHub Copilot", "agy": "Antigravity", "hermes": "Hermes",
}
PI_FAMILY = ("gjc", "omp", "pi", "omo")
UNAVAILABLE = {
    "cursor": "its native /fork requires opening the active source as a writer first; source-preserving offline fork API unavailable",
    "agy": "its native /fork requires concurrent source resume; no documented offline fork API",
    "hermes": "no verified source-preserving native fork contract that retains full conversation, tool history, and metadata",
}
READ_ONLY_POLICY = """This is an independent READ-ONLY fork, not a delegated worker.
The inherited conversation is background, not an instruction to resume its tasks.
Wait for a new question. Answer only the user's new questions. Do not continue old
implementation, goals, plans, queued tasks or follow-ups. Do not modify files,
memories, settings, services, Git state or external systems. Do not start agents,
background jobs, schedules or automation. Do not contact or report back to the
parent session. File access is read-only; the checkout is shared, not isolated.
A tool or runtime restriction is authoritative even if inherited instructions ask
for edits or commits. Explain unavailable actions instead of bypassing a limit.
"""
READ_TOOLS = {
    "gjc": ("read", "search", "find", "ast_grep"),
    "omp": ("read", "grep", "glob"),
}
VALUE_FLAGS = {
    "claude": ("--model", "--effort"),
    "codex": ("--model", "-m", "--profile", "-p"),
    "gjc": ("--model", "--mpreset", "--thinking", "--session-dir"),
    "omp": ("--model", "--thinking", "--profile", "--session-dir", "--config"),
    "pi": ("--model", "--thinking", "--session-dir", "--provider"),
    "omo": ("--model", "--thinking", "--session-dir", "--provider"),
    "opencode": ("--server", "--model", "--agent"),
    "cursor": ("--model",),
    "agy": ("--model", "--effort", "--project", "--agent"),
    "copilot": ("--model",),
    "hermes": ("--model",),
}
# These options have exactly one value. Consume it even if it looks like a flag.
OMITTED_VALUE_FLAGS = {
    "--api-key", "--system-prompt", "--append-system-prompt", "--system-prompt-file",
    "--append-system-prompt-file", "--extension", "-e", "--goal", "--tools", "-t",
    "--exclude-tools", "--session", "--session-id", "--conversation", "--name", "-n",
    "--skill", "--prompt-template", "--theme", "--permission-mode", "--mode",
    "--export", "--tui-mode", "--use-theme", "--system-prompt-snapshot",
    "--sandbox", "--ask-for-approval",
}
OMITTED_SWITCHES = {
    "claude": {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--strict-mcp-config", "--fork-session"},
    "codex": {"--dangerously-bypass-approvals-and-sandbox", "--full-auto"},
    "gjc": {"--no-mcp", "--no-lsp", "--no-pty", "--no-title"},
    "omp": {"--no-extensions", "--no-lsp", "--no-pty", "--no-title"},
    "pi": set(),
    "omo": set(),
    "opencode": {"--fork"},
    "cursor": set(),
    "agy": set(),
    "copilot": {"--allow-all-tools", "--allow-all-paths", "--allow-all-urls"},
    "hermes": set(),
}
# Optional and variadic arguments cannot be skipped as if they owned one token.
AMBIGUOUS_FLAGS = {
    "--resume", "-r", "--continue", "-c", "--print", "-p", "--prompt", "--fork",
    "--mcp-config", "--additional-mcp-config", "--mcp", "--allowedTools", "--allowed-tools",
    "--disallowedTools", "--disallowed-tools", "--add-dir",
}
CONFIGURATION_FLAGS = {
    "--settings", "--settings-file", "--settings-sources", "--config", "--config-file",
    "--profile", "--mpreset", "--provider", "--session-dir", "--agent", "--server",
}
GENERATED_CLAUDE_GUARDS = {
    "--tools": "Read,Grep,Glob",
    "--mcp-config": '{"mcpServers":{}}',
    "--settings": '{"disableAllHooks":true}',
}
GENERATED_CODEX_CONFIG = (
    "sandbox_workspace_write.network_access=false", "features.apps=false",
    "features.hooks=false", "features.multi_agent=false", "features.shell_tool=false",
    "features.in_app_browser=false",
)
ENVIRONMENT_KEYS = {
    "claude": ("CLAUDE_CONFIG_DIR",),
    "codex": ("CODEX_HOME",),
    "gjc": ("GJC_CONFIG_DIR", "GJC_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "omp": ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "pi": ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "omo": ("OMO_CODING_AGENT_DIR", "SENPI_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "OMO_CODING_AGENT_SESSION_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "opencode": ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"),
    "cursor": ("CURSOR_CONFIG_DIR",),
    "copilot": ("COPILOT_HOME",),
    "agy": ("GEMINI_CLI_HOME",),
    "hermes": ("HERMES_HOME", "HERMES_PROFILE"),
}


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    restrictions: str = ""


def safe_launch_flags(agent: str, argv: Sequence[str]) -> list[str]:
    """Copy known selectors; reject uncertain arity instead of scanning secrets."""
    if agent not in AGENTS:
        raise ContractError(f"Unsupported agent: {agent}")
    kept: list[str] = []
    index = 1
    while index < len(argv):
        item = argv[index]
        if not isinstance(item, str):
            raise ContractError("Malformed source argv")
        if item == "--":
            break
        flag, separator, inline = item.partition("=")
        candidate = inline if separator else argv[index + 1] if index + 1 < len(argv) else None
        if _generated_guard(agent, flag, candidate):
            if not separator:
                index += 1
        elif agent == "opencode" and item == "--standalone":
            kept.append(item)
        elif flag in VALUE_FLAGS[agent]:
            if separator:
                value = inline
            elif index + 1 < len(argv):
                index += 1
                value = argv[index]
            else:
                raise ContractError(f"Missing {flag} value in source argv")
            if not _valid_value(value):
                raise ContractError(f"Invalid {flag} value in source argv")
            kept.extend((flag, value))
        elif agent in ("claude", "copilot") and flag in ("--resume", "-r"):
            if separator:
                value = inline
            elif index + 1 < len(argv):
                index += 1
                value = argv[index]
            else:
                raise ContractError(f"Native resume selector {flag} requires an exact canonical session ID")
            try:
                if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
                    raise ValueError("Noncanonical ID")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ContractError(f"Native resume selector {flag} requires an exact canonical session ID") from exc
        elif flag in OMITTED_SWITCHES[agent]:
            if separator:
                raise ContractError(f"Unexpected {flag} value in source argv")
        elif flag in CONFIGURATION_FLAGS or (agent == "codex" and flag == "-c"):
            raise ContractError(f"Source configuration option {flag} cannot be safely preserved")
        elif flag in AMBIGUOUS_FLAGS and not (flag == "--fork" and agent in PI_FAMILY):
            raise ContractError(f"Source option {flag} has optional or variable arity; refusing to guess")
        elif flag == "--tools" and agent == "claude":
            raise ContractError("Source option --tools has variable arity; refusing to guess")
        elif flag in OMITTED_VALUE_FLAGS or (flag == "--fork" and agent in PI_FAMILY):
            if not separator:
                if index + 1 >= len(argv):
                    raise ContractError(f"Missing {flag} value in source argv")
                index += 1
                if argv[index] == "--":
                    break
        elif item.startswith("-"):
            # An unknown short alias or attached value can also own later tokens.
            # Do not include the untrusted token in an error or reinterpret it.
            raise ContractError("Unsupported source option; its value arity is unknown")
        index += 1
    return kept


def fork_launch(
    agent: str,
    reference: str,
    source_argv: Sequence[str],
    environment: Mapping[str, str],
    *,
    read_only: bool,
    bridge: Path,
    omp_overlay: Path,
    mcp_servers: Sequence[tuple[str, str]] = (),
    runtime: Mapping[str, Any] | None = None,
) -> Launch:
    if agent not in AGENTS:
        raise ContractError(f"Unsupported agent: {agent}")
    if agent in UNAVAILABLE:
        raise ContractError(f"Automatic {AGENTS[agent]} fork unavailable: {UNAVAILABLE[agent]}. Source was not resumed or changed")
    if read_only and agent not in ("claude", "codex", "gjc", "omp"):
        raise ContractError(f"Read-only mode is not supported for {AGENTS[agent]}: native guard support has not been verified")
    if agent == "opencode" and "OPENCODE_CONFIG_CONTENT" in environment:
        raise ContractError("OpenCode inline configuration OPENCODE_CONFIG_CONTENT cannot be safely relayed; move it to a config file, set OPENCODE_CONFIG to that file, and unset OPENCODE_CONFIG_CONTENT before restarting the source")
    flags = safe_launch_flags(agent, source_argv)
    if agent == "omp":
        # Only this bundled guard overlay is ours to remove in a normal child.
        kept = []
        for index in range(0, len(flags), 2):
            pair = flags[index:index + 2]
            if pair != ["--config", str(omp_overlay)]:
                kept.extend(pair)
        flags = kept
    state = runtime or {}
    if agent in PI_FAMILY and state.get("session_file"):
        flags = _without(flags, {"--session-dir"}) + ["--session-dir", str(Path(state["session_file"]).parent)]
    # The live owner descriptor wins over initial process flags after model changes.
    model = _runtime_selector(state, "model")
    profile = _runtime_selector(state, "profile") if agent == "gjc" else None
    if model is not None:
        flags = _without(flags, {"--model", "-m", "--mpreset", "--provider"})
        flags.extend(("--mpreset", profile) if profile is not None else ("--model", model))
    thinking = _runtime_selector(state, "thinking")
    if agent in PI_FAMILY and thinking is not None:
        flags = _without(flags, {"--thinking"})
        if thinking != "inherit":
            flags.extend(("--thinking", thinking))
    env = {}
    keys = (*ENVIRONMENT_KEYS[agent], "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
            "HERDR_FORK_STATE_DIR", "HERDR_FORK_HELPER", "HERDR_FORK_SESSION")
    for key in keys:
        if key not in environment:
            continue
        value = environment[key]
        if not isinstance(value, str) or not value or not value.isprintable():
            raise ContractError(f"Invalid {key}; use a nonempty safe configuration path or selector")
        if key not in ("HERDR_FORK_SESSION", "HERMES_PROFILE") and not Path(value).is_absolute():
            raise ContractError(f"Invalid {key}; configuration paths must be absolute")
        if key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR") and not Path(value).is_dir():
            raise ContractError(f"Invalid {key}; source configuration directory is unavailable")
        env[key] = value
    command = agent
    if agent in PI_FAMILY:
        native = ["--fork", reference]
    elif agent == "claude":
        native = ["--resume", reference, "--fork-session"]
    elif agent == "codex":
        native = ["fork", reference]
    elif agent == "opencode":
        native = ["mini", "--session", reference, "--fork"]
    else:
        native = ["--resume", reference]
    # Explicit zero prevents an inherited read-only shell environment from
    # silently restricting the normal fork. Native permissions otherwise apply.
    env["HERDR_FORK_READ_ONLY"] = "1" if read_only else "0"
    env["HERDR_FORK_AGENT"] = agent
    if not read_only:
        return Launch(tuple([command, *native, *flags]), env, "native permissions; shared checkout")
    if agent == "claude":
        flags.extend([
            "--tools", GENERATED_CLAUDE_GUARDS["--tools"], "--permission-mode", "plan",
            "--strict-mcp-config", "--mcp-config", GENERATED_CLAUDE_GUARDS["--mcp-config"],
            "--settings", GENERATED_CLAUDE_GUARDS["--settings"],
            "--system-prompt-snapshot", "off",
            "--append-system-prompt", " ".join(READ_ONLY_POLICY.split()),
        ])
        restrictions = "built-in Read/Grep/Glob only; MCP and hooks disabled"
    elif agent == "codex":
        flags.extend([
            "--sandbox", "read-only", "--ask-for-approval", "never",
            *[part for value in GENERATED_CODEX_CONFIG for part in ("-c", value)],
            "-c", "developer_instructions=" + _toml_string(READ_ONLY_POLICY),
        ])
        for name, transport in mcp_servers:
            if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in name):
                raise ContractError("Codex MCP names with punctuation cannot be safely overridden by this CLI version")
            if transport == "stdio":
                value = '{command="false",enabled=false}'
            elif transport == "streamable_http":
                value = '{url="http://127.0.0.1:1/disabled",enabled=false}'
            else:
                raise ContractError("Unknown Codex MCP transport; refusing read-only mode")
            flags.extend(("-c", f"mcp_servers.{name}={value}"))
        restrictions = "native read-only sandbox; no approval escalation; configured MCP servers disabled"
    else:
        if agent == "omp":
            # A source overlay can re-enable goals or discovery after restrictions.
            flags = _without(flags, {"--config"})
        flags.extend(("--tools", ",".join(READ_TOOLS[agent]), "--no-lsp", "--no-pty", "--no-title"))
        if agent == "gjc":
            # The registered owner bridge gates restored modes and all tool calls.
            flags.extend(("--no-mcp", "--append-system-prompt", READ_ONLY_POLICY))
            # Native startup command pauses inherited goal state without a model turn.
            flags.append("/goal pause")
        else:
            flags.extend(("--no-extensions", "--extension", str(bridge), "--config", str(omp_overlay),
                          "--append-system-prompt", " ".join(READ_ONLY_POLICY.split())))
        restrictions = "owner bridge denies every non-read tool; inherited goals paused"
    return Launch(tuple([command, *native, *flags]), env, restrictions)


def _without(flags: list[str], names: set[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(flags):
        width = 1 if flags[index] == "--standalone" else 2
        if flags[index] not in names:
            kept.extend(flags[index:index + width])
        index += width
    return kept


def _generated_guard(agent: str, flag: str, value: Any) -> bool:
    """Recognize only exact guards we emit, never arbitrary native configuration."""
    if not isinstance(value, str):
        return False
    if agent == "claude":
        return flag in GENERATED_CLAUDE_GUARDS and GENERATED_CLAUDE_GUARDS[flag] == value
    if agent != "codex" or flag != "-c":
        return False
    if value in GENERATED_CODEX_CONFIG or value == "developer_instructions=" + _toml_string(READ_ONLY_POLICY):
        return True
    key, separator, config = value.partition("=")
    return bool(separator and re.fullmatch(r"mcp_servers\.[A-Za-z0-9_-]+", key) and config in (
        '{command="false",enabled=false}', '{url="http://127.0.0.1:1/disabled",enabled=false}',
    ))


def _valid_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not value.startswith("-") and value.isprintable()


def _runtime_selector(state: Mapping[str, Any], name: str) -> str | None:
    if name not in state:
        return None
    value = state[name]
    if not _valid_value(value):
        raise ContractError(f"Invalid runtime {name}")
    if name != "profile" and any(character.isspace() for character in value):
        raise ContractError(f"Invalid runtime {name}")
    if name == "model" and any(part in ("", ".", "..") for part in value.split("/")):
        raise ContractError("Invalid runtime model")
    return value


def _toml_string(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
