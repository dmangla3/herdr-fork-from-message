"""Resolve products from executable entrypoints, never prompt text or recent logs."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
import re
import uuid

from agent_contracts import AGENTS, ContractError

ALIASES = {"cursor-agent": "cursor", "agent": "cursor", "antigravity-cli": "agy", "antigravity_cli": "agy"}


def entrypoint_argv(process: Mapping[str, Any]) -> list[str]:
    argv = process.get("argv")
    if argv is None and process.get("argv0") in AGENTS:
        # Node can replace its OS process title, hiding the original argv.
        # Only exact known titles are usable; owner registration supplies flags.
        return [process["argv0"]]
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return []
    executable = Path(argv[0]).name
    if executable in ("node", "bun") or re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        if len(argv) > 1 and not argv[1].startswith("-"):
            return argv[1:]
    return argv


def process_product(process: Mapping[str, Any]) -> str | None:
    argv = entrypoint_argv(process)
    if not argv:
        return None
    executable = Path(argv[0]).name
    paths = argv[:1]
    for value in paths:
        if re.search(r"/node_modules/omo-ai/(?:bin/omo\.js|node_modules/@code-yeongyu/senpi/dist/(?:bundle/)?cli\.js)$", value):
            return "omo"
        if re.search(r"/node_modules/@code-yeongyu/senpi/dist/(?:bundle/)?cli\.js$", value):
            return "omo"
        if re.search(r"/node_modules/@(?:earendil-works|mariozechner)/pi-coding-agent/dist/(?:bundle/)?cli\.js$", value):
            return "pi"
    for value in paths:
        name = Path(value).name
        if name in AGENTS:
            return name
        if name in ALIASES:
            return ALIASES[name]
        if value.endswith(("/hermes_cli/main.py", "/hermes_cli/__main__.py")):
            return "hermes"
    name = process.get("name")
    if name in AGENTS and executable == name:
        return name
    return None


def select_process(processes: Sequence[Mapping[str, Any]], reported: str | None = None) -> tuple[str, dict[str, Any]]:
    matches = [(kind, dict(p)) for p in processes if (kind := process_product(p))]
    if reported:
        reported = ALIASES.get(reported, reported)
        # OMO's launcher deliberately reports the pi-family hint to Herdr.
        matches = [(kind, p) for kind, p in matches if kind == reported or (reported == "pi" and kind == "omo")]
    if len(matches) != 1:
        raise ContractError("Pane must have exactly one identifiable foreground harness process")
    kind, process = matches[0]
    if process.get("argv") is None:
        process["argv"] = entrypoint_argv(process)
    return kind, process


def session_reference(pane: Mapping[str, Any], product: str) -> str:
    ref = pane.get("agent_session")
    reported = ref.get("agent") if product == "omo" and isinstance(ref, dict) and ref.get("agent") in ("pi", "omo") else product
    source = "herdr:antigravity_cli" if product == "agy" else f"herdr:{reported}"
    if not isinstance(ref, dict) or ref.get("agent") != reported or ref.get("source") != source:
        raise ContractError(f"No trusted {product} session reference. Enable its Herdr integration or runtime bridge and restart that agent.")
    value = ref.get("value")
    if not isinstance(value, str) or not value:
        raise ContractError("Missing native session reference")
    if ref.get("kind") == "path" and product in ("pi", "omo"):
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise ContractError("Reported session file is unavailable")
        return value
    if ref.get("kind") != "id":
        raise ContractError("Unexpected native session reference type")
    if product == "opencode":
        if not re.fullmatch(r"ses_[A-Za-z0-9_-]+", value):
            raise ContractError("Invalid OpenCode session ID")
    elif product == "hermes":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
            raise ContractError("Invalid Hermes session ID")
    else:
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ContractError("Invalid native session ID") from exc
        if str(parsed) != value.lower():
            raise ContractError("Noncanonical native session ID")
        return str(parsed)
    return value


def session_id(reference: str) -> str:
    """Read only the exact reported Pi-family header, not its conversation body."""
    if not Path(reference).is_absolute():
        return reference
    import json
    try:
        with Path(reference).open("rb") as stream:
            raw = stream.readline(32769)
        line = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError("Reported session header is unavailable") from exc
    if len(raw) > 32768:
        raise ContractError("Session header exceeds the size limit")
    try:
        header = json.loads(line)
    except ValueError as exc:
        raise ContractError("Invalid reported session header") from exc
    value = header.get("id") if isinstance(header, dict) and header.get("type") == "session" else None
    try:
        parsed = str(uuid.UUID(value))
        if parsed != value.lower():
            raise ValueError("Noncanonical ID")
        return parsed
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError("Reported session header has no canonical ID") from exc
