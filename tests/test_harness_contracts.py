from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_contracts import (
    AGENTS,
    ContractError,
    ENVIRONMENT_KEYS,
    PI_FAMILY,
    UNAVAILABLE,
    fork_launch,
    safe_launch_flags,
)

REFERENCE = "019fe92d-2396-7f40-8dfe-bdb1ae3d4e27"
NEW_AGENTS = ("pi", "omo", "opencode", "copilot")
AVAILABLE_AGENTS = ("claude", "codex", *PI_FAMILY, "opencode", "copilot")
XDG_KEYS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
METADATA = {
    "HERDR_FORK_STATE_DIR": "/source/fork-state",
    "HERDR_FORK_HELPER": "/plugin/fork_from_message.py",
    "HERDR_FORK_SESSION": "selected-server",
}


class HarnessContractTests(unittest.TestCase):
    def launch(self, agent, *, argv=None, environment=None, read_only=False, runtime=None):
        return fork_launch(
            agent,
            REFERENCE,
            [agent] if argv is None else argv,
            {} if environment is None else environment,
            read_only=read_only,
            bridge=Path("/plugin/runtime_bridge.ts"),
            omp_overlay=Path("/plugin/read-only-omp.yml"),
            runtime=runtime,
        )

    def test_native_routes_keep_permissions_and_public_markers(self):
        expected = {
            "claude": ("claude", "--resume", REFERENCE, "--fork-session"),
            "codex": ("codex", "fork", REFERENCE),
            **{agent: (agent, "--fork", REFERENCE) for agent in PI_FAMILY},
            "opencode": ("opencode", "mini", "--session", REFERENCE, "--fork"),
            "copilot": ("copilot", "--resume", REFERENCE),
        }
        for agent, argv in expected.items():
            with self.subTest(agent=agent):
                launch = self.launch(agent, environment={"HERDR_FORK_READ_ONLY": "1", "HERDR_FORK_AGENT": "unrelated", **METADATA})
                self.assertEqual(launch.argv, argv)
                self.assertEqual(launch.environment["HERDR_FORK_READ_ONLY"], "0")
                self.assertEqual(launch.environment["HERDR_FORK_AGENT"], agent)
                for key, value in METADATA.items():
                    self.assertEqual(launch.environment[key], value)
                self.assertEqual(launch.restrictions, "native permissions; shared checkout")

    def test_new_read_only_launches_fail_closed_before_parsing_flags(self):
        for agent in NEW_AGENTS:
            with self.subTest(agent=agent), self.assertRaisesRegex(
                ContractError, "Read-only mode.*native guard support has not been verified"
            ):
                self.launch(agent, argv=[agent, "--model"], read_only=True)

    def test_unsafe_native_paths_fail_before_argument_construction(self):
        for agent in UNAVAILABLE:
            for read_only in (False, True):
                with self.subTest(agent=agent, read_only=read_only), self.assertRaisesRegex(ContractError, "Source was not resumed or changed"):
                    self.launch(agent, argv=[agent, "--model"], read_only=read_only)

    def test_hermes_diagnostic_describes_missing_safe_fork_contract(self):
        with self.assertRaisesRegex(ContractError, "no verified source-preserving native fork contract.*full conversation, tool history, and metadata"):
            self.launch("hermes")

    def test_excluded_values_cannot_be_reparsed_as_model_or_configuration_flags(self):
        options = (
            "--system-prompt", "--append-system-prompt", "--system-prompt-file",
            "--append-system-prompt-file", "--api-key", "--extension", "-e",
            "--goal", "--name", "-n", "--skill", "--prompt-template",
        )
        for agent in AVAILABLE_AGENTS:
            for option in options:
                for hidden_flag in ("--model", "--config", "--provider"):
                    with self.subTest(agent=agent, option=option, hidden_flag=hidden_flag):
                        self.assertEqual(safe_launch_flags(agent, [agent, option, hidden_flag, "secret", "--model", "real"]), ["--model", "real"])
                        self.assertEqual(safe_launch_flags(agent, [agent, f"{option}={hidden_flag}", "secret", "--model=real"]), ["--model", "real"])
                with self.subTest(agent=agent, option=option), self.assertRaisesRegex(ContractError, "Missing"):
                    safe_launch_flags(agent, [agent, option])
        for agent in PI_FAMILY:
            for option in ("--tools", "-t", "--fork"):
                with self.subTest(agent=agent, option=option):
                    self.assertEqual(safe_launch_flags(agent, [agent, option, "--model", "secret", "--model", "real"]), ["--model", "real"])

    def test_optional_variadic_prompt_and_mcp_options_fail_closed(self):
        options = (
            "--prompt", "-p", "--print", "--resume", "-r", "--continue",
            "--mcp-config", "--additional-mcp-config", "--mcp", "--allowedTools",
            "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--add-dir",
        )
        for agent in AVAILABLE_AGENTS:
            for option in options:
                if agent == "codex" and option == "-p":
                    continue
                for source in ([agent, option, "--model", "secret"], [agent, f"{option}=secret", "--model", "real"]):
                    with self.subTest(agent=agent, option=option, inline="=" in source[1]), self.assertRaises(ContractError) as caught:
                        safe_launch_flags(agent, source)
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertIn(option, str(caught.exception))
        for option in ("--tools", "--tools=secret"):
            with self.subTest(option=option), self.assertRaisesRegex(ContractError, "variable arity"):
                safe_launch_flags("claude", ["claude", option, "--model", "secret"])

    def test_generated_native_children_can_be_forked_again(self):
        for agent in ("claude", "copilot", *PI_FAMILY, "codex", "opencode"):
            first = self.launch(agent, argv=[agent, "--model", "chosen"])
            second = self.launch(agent, argv=list(first.argv))
            self.assertEqual(first.argv, second.argv)
        for agent in ("claude", "copilot"):
            for flag in ("--resume", "-r"):
                self.assertEqual(safe_launch_flags(agent, [agent, flag, REFERENCE, "--model", "chosen"]), ["--model", "chosen"])
                self.assertEqual(safe_launch_flags(agent, [agent, f"{flag}={REFERENCE}"]), [])
                for invalid in ("secret", "--model", ""):
                    with self.assertRaises(ContractError) as caught:
                        safe_launch_flags(agent, [agent, flag, invalid])
                    self.assertNotIn(invalid or "not-present", str(caught.exception))

    def test_omp_read_only_replaces_source_configuration_overlay(self):
        launch = self.launch("omp", argv=["omp", "--config", "/source/custom.yml"], read_only=True)
        self.assertNotIn("/source/custom.yml", launch.argv)
        self.assertEqual(launch.argv.count("--config"), 1)
        self.assertIn("/plugin/read-only-omp.yml", launch.argv)

    def test_restricted_children_can_fork_into_either_mode(self):
        for agent in ("claude", "codex", "gjc", "omp"):
            restricted = self.launch(agent, argv=[agent, "--model", "chosen"], read_only=True)
            repeated = self.launch(agent, argv=list(restricted.argv), read_only=True)
            self.assertEqual(repeated.argv, restricted.argv)
            normal = self.launch(agent, argv=list(restricted.argv), read_only=False)
            expected = self.launch(agent, argv=[agent, "--model", "chosen"], read_only=False)
            self.assertEqual(normal.argv, expected.argv)

    def test_only_exact_generated_guards_are_removed(self):
        for argv in (
            ["claude", "--mcp-config", '{"mcpServers":{"extra":{}}}'],
            ["claude", "--settings", '{"disableAllHooks":false}'],
            ["codex", "-c", "features.shell_tool=true"],
            ["codex", "-c", 'mcp_servers.name={command="other",enabled=false}'],
            ["codex", "-c", "developer_instructions=untrusted"],
        ):
            with self.subTest(argv=argv), self.assertRaises(ContractError):
                self.launch(argv[0], argv=argv)
        launch = self.launch("omp", argv=["omp", "--config", "/source/custom.yml"])
        self.assertIn("/source/custom.yml", launch.argv)

    def test_unknown_value_options_short_aliases_and_attached_values_fail_closed(self):
        options = ("--api-token", "--provider-options", "--unknown", "--unknown=secret", "-k", "-tsecret", "-msecret", "-eprompt")
        for agent in AVAILABLE_AGENTS:
            for option in options:
                with self.subTest(agent=agent, option=option), self.assertRaisesRegex(ContractError, "value arity is unknown") as caught:
                    safe_launch_flags(agent, [agent, option, "--model", "secret", "--model", "real"])
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("prompt", str(caught.exception))

    def test_unsupported_source_configuration_is_not_silently_lost(self):
        options = {
            "claude": ("--provider", "--settings", "--settings-file", "--settings-sources", "--config"),
            "codex": ("--provider", "--config", "-c"),
            "gjc": ("--provider", "--config", "--profile"),
            "omp": ("--provider", "--mpreset"),
            "pi": ("--settings", "--config", "--profile"),
            "omo": ("--settings", "--config", "--profile"),
            "opencode": ("--provider", "--config", "--profile"),
            "copilot": ("--provider", "--config", "--profile"),
        }
        for agent, flags in options.items():
            for flag in flags:
                for source in ([agent, flag, "--model", "secret"], [agent, f"{flag}=secret", "--model", "real"]):
                    with self.subTest(agent=agent, flag=flag), self.assertRaisesRegex(ContractError, "cannot be safely preserved") as caught:
                        safe_launch_flags(agent, source)
                    self.assertIn(flag, str(caught.exception))
                    self.assertNotIn("secret", str(caught.exception))

    def test_documented_configuration_selectors_and_short_aliases_are_preserved(self):
        flags = {
            "claude": ["--model", "chosen", "--effort", "high"],
            "codex": ["--model", "chosen", "--profile", "work"],
            "gjc": ["--model", "chosen", "--mpreset", "work", "--thinking", "high", "--session-dir", "/sessions"],
            "omp": ["--model", "chosen", "--thinking", "high", "--profile", "work", "--session-dir", "/sessions", "--config", "/source/settings.yml"],
            "pi": ["--model", "chosen", "--thinking", "high", "--session-dir", "/sessions", "--provider", "custom"],
            "omo": ["--model", "chosen", "--thinking", "high", "--session-dir", "/sessions", "--provider", "custom"],
            "opencode": ["--server", "http://127.0.0.1:4096", "--model", "chosen", "--agent", "build"],
            "copilot": ["--model", "chosen"],
        }
        for agent, expected in flags.items():
            with self.subTest(agent=agent):
                source = [agent, "--api-key", "secret", "--extension", "/untrusted.ts", *expected, "old prompt"]
                self.assertEqual(safe_launch_flags(agent, source), expected)
                inline = [f"{expected[index]}={expected[index + 1]}" for index in range(0, len(expected), 2)]
                self.assertEqual(safe_launch_flags(agent, [agent, *inline]), expected)
                launch = self.launch(agent, argv=source)
                self.assertEqual(launch.argv[-len(expected):], tuple(expected))
                self.assertNotIn("--extension", launch.argv)
                self.assertNotIn("secret", launch.argv)
        self.assertEqual(safe_launch_flags("codex", ["codex", "-m", "chosen", "-p", "work"]), ["-m", "chosen", "-p", "work"])
        self.assertEqual(safe_launch_flags("codex", ["codex", "-m=chosen", "-p=work"]), ["-m", "chosen", "-p", "work"])

    def test_native_boolean_options_do_not_consume_selectors(self):
        cases = {
            "claude": "--dangerously-skip-permissions",
            "codex": "--full-auto",
            "gjc": "--no-mcp",
            "omp": "--no-extensions",
            "opencode": "--fork",
            "copilot": "--allow-all-tools",
        }
        for agent, option in cases.items():
            with self.subTest(agent=agent):
                self.assertEqual(safe_launch_flags(agent, [agent, option, "--model", "real"]), ["--model", "real"])
            with self.subTest(agent=agent), self.assertRaises(ContractError):
                safe_launch_flags(agent, [agent, f"{option}=secret", "--model", "real"])

    def test_opencode_preserves_private_server_instead_of_using_default_service(self):
        launch = self.launch("opencode", argv=["opencode", "mini", "--standalone", "--session", "ses_source", "--fork", "--model", "old"], runtime={"model": "provider/live"})
        self.assertIn("--standalone", launch.argv)
        self.assertEqual(launch.argv.count("--fork"), 1)
        self.assertEqual(launch.argv.count("--model"), 1)
        self.assertIn("provider/live", launch.argv)
        self.assertNotIn("old", launch.argv)

    def test_entrypoints_subcommands_and_positional_prompt_text_are_not_scanned(self):
        cases = (
            ("opencode", ["/source/bin/opencode", "mini", "--model", "real"]),
            ("codex", ["/source/bin/codex", "fork", REFERENCE, "--model", "real"]),
            ("pi", ["/source/node_modules/pi/dist/cli.js", "prompt containing --model secret", "--model", "real"]),
        )
        for agent, argv in cases:
            with self.subTest(agent=agent):
                self.assertEqual(safe_launch_flags(agent, argv), ["--model", "real"])
        for agent in AVAILABLE_AGENTS:
            with self.subTest(agent=agent):
                self.assertEqual(safe_launch_flags(agent, ["--model=secret", "--model", "real", "--", "--model", "prompt", "--unknown"]), ["--model", "real"])
                self.assertEqual(safe_launch_flags(agent, [agent, "--system-prompt", "--", "--model", "secret"]), [])

    def test_selectors_reject_missing_empty_control_and_flag_values(self):
        invalid = ("", " ", "--allow-all-tools", "bad\nmodel", "bad\tmodel", "bad\x00model", "bad\x7fmodel", "bad\x85model", "bad\u2028model")
        for agent in AVAILABLE_AGENTS:
            with self.subTest(agent=agent), self.assertRaises(ContractError):
                safe_launch_flags(agent, [agent, "--model"])
            for value in invalid:
                for args in (["--model", value], [f"--model={value}"]):
                    with self.subTest(agent=agent, value=repr(value)), self.assertRaises(ContractError):
                        safe_launch_flags(agent, [agent, *args])
        for argv in (["pi", None], ["pi", "--model", 5]):
            with self.subTest(argv=argv), self.assertRaises(ContractError):
                safe_launch_flags("pi", argv)

    def test_source_configuration_roots_and_xdg_paths_are_preserved_without_credentials(self):
        roots = {
            "claude": ("CLAUDE_CONFIG_DIR",),
            "codex": ("CODEX_HOME",),
            "gjc": ("GJC_CONFIG_DIR", "GJC_CODING_AGENT_DIR"),
            "omp": ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"),
            "pi": ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"),
            "omo": ("OMO_CODING_AGENT_DIR", "SENPI_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "OMO_CODING_AGENT_SESSION_DIR"),
            "opencode": ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"),
            "copilot": ("COPILOT_HOME",),
        }
        for agent, keys in roots.items():
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as directory:
                environment = {key: directory for key in (*keys, *XDG_KEYS)}
                environment.update({"API_KEY": "secret", "GJC_MASTER_CAPABILITY": "secret", "CURSOR_TOKEN": "secret", "UNLISTED_FORK_METADATA": "secret", **METADATA})
                launch = self.launch(agent, environment=environment)
                for key in (*keys, *XDG_KEYS, *METADATA):
                    self.assertEqual(launch.environment[key], environment[key])
                for key in ("API_KEY", "GJC_MASTER_CAPABILITY", "CURSOR_TOKEN", "UNLISTED_FORK_METADATA"):
                    self.assertNotIn(key, launch.environment)
                self.assertEqual(set(ENVIRONMENT_KEYS[agent]) - set(XDG_KEYS), set(keys))

    def test_invalid_configuration_roots_fail_before_launch(self):
        for agent, key in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("pi", "PI_CODING_AGENT_DIR")):
            for value in ("", "relative", "/unsafe\npath"):
                with self.assertRaises(ContractError) as caught:
                    self.launch(agent, environment={key: value})
                self.assertIn(key, str(caught.exception))
        for agent, key in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")):
            with self.assertRaisesRegex(ContractError, "unavailable"):
                self.launch(agent, environment={key: "/unavailable-config-root"})

    def test_opencode_inline_configuration_is_rejected_without_disclosure(self):
        values = ("", "{}", '{"provider":{"custom":{"apiKey":"secret"}}}', "not-json")
        for value in values:
            with self.subTest(value=value), self.assertRaises(ContractError) as caught:
                self.launch("opencode", argv=["opencode", "--model"], environment={"OPENCODE_CONFIG_CONTENT": value, "OPENCODE_CONFIG": "/source/opencode.json"})
            error = str(caught.exception)
            self.assertIn("OPENCODE_CONFIG_CONTENT", error)
            self.assertIn("config file", error)
            self.assertIn("OPENCODE_CONFIG", error)
            self.assertIn("unset", error)
            self.assertNotIn("secret", error)
            self.assertNotIn("not-json", error)
        for agent, keys in ENVIRONMENT_KEYS.items():
            with self.subTest(agent=agent):
                self.assertNotIn("OPENCODE_CONFIG_CONTENT", keys)
        launch = self.launch("pi", environment={"OPENCODE_CONFIG_CONTENT": '{"apiKey":"secret"}'})
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", launch.environment)
        self.assertNotIn("secret", " ".join(launch.argv))

    def test_existing_gjc_and_omp_read_only_controls_remain(self):
        for agent, tools in (("gjc", "read,search,find,ast_grep"), ("omp", "read,grep,glob")):
            with self.subTest(agent=agent):
                launch = self.launch(agent, read_only=True)
                self.assertIn(tools, launch.argv)
                self.assertIn("--no-lsp", launch.argv)
                self.assertIn("--no-pty", launch.argv)
                self.assertEqual(launch.environment["HERDR_FORK_READ_ONLY"], "1")
                if agent == "gjc":
                    self.assertIn("--no-mcp", launch.argv)
                    self.assertEqual(launch.argv[-1], "/goal pause")
                    self.assertNotIn("--extension", launch.argv)
                else:
                    self.assertIn("--no-extensions", launch.argv)
                    self.assertIn("/plugin/runtime_bridge.ts", launch.argv)
                    self.assertIn("/plugin/read-only-omp.yml", launch.argv)

    def test_pi_family_live_model_thinking_and_session_directory_override_startup(self):
        for agent in PI_FAMILY:
            with self.subTest(agent=agent):
                launch = self.launch(
                    agent,
                    argv=[agent, "--model", "old/model", "--thinking", "low", "--session-dir", "/stale"],
                    runtime={"model": "new/live-model", "thinking": "high", "session_file": "/active sessions/current.jsonl"},
                )
                self.assertEqual(launch.argv.count("--model"), 1)
                self.assertEqual(launch.argv[launch.argv.index("--model") + 1], "new/live-model")
                self.assertEqual(launch.argv.count("--thinking"), 1)
                self.assertEqual(launch.argv[launch.argv.index("--thinking") + 1], "high")
                self.assertEqual(launch.argv.count("--session-dir"), 1)
                self.assertEqual(launch.argv[launch.argv.index("--session-dir") + 1], "/active sessions")
                self.assertNotIn("old/model", launch.argv)
                self.assertNotIn("/stale", launch.argv)

    def test_live_provider_qualified_model_replaces_stale_provider_selector(self):
        for agent in ("pi", "omo"):
            with self.subTest(agent=agent):
                launch = self.launch(
                    agent,
                    argv=[agent, "--provider", "old-provider", "--model", "old-model"],
                    runtime={"model": "current-provider/live-model"},
                )
                self.assertNotIn("--provider", launch.argv)
                self.assertNotIn("old-provider", launch.argv)
                self.assertIn("current-provider/live-model", launch.argv)

    def test_only_gjc_uses_live_model_profile(self):
        for agent in PI_FAMILY:
            with self.subTest(agent=agent):
                launch = self.launch(agent, runtime={"model": "provider/model", "profile": "work"})
                self.assertIn("--mpreset" if agent == "gjc" else "--model", launch.argv)
                self.assertEqual("work" in launch.argv, agent == "gjc")

    def test_runtime_selectors_fail_closed_before_argv_encoding(self):
        common = (None, 7, [], {}, "", " ", "-model", "--model", "bad\nvalue", "bad\tvalue", "bad\x00value", "bad\x7fvalue", "bad\x85value", "bad\u2028value")
        malformed = {
            "model": (*common, "provider/", "/model", "provider//model", "provider/../model", "provider/model with space"),
            "thinking": (*common, " high", "high ", "high effort"),
        }
        for agent in PI_FAMILY:
            for field, values in malformed.items():
                for value in values:
                    with self.subTest(agent=agent, field=field, value=repr(value)), self.assertRaisesRegex(ContractError, f"Invalid runtime {field}") as caught:
                        self.launch(agent, runtime={field: value})
                    self.assertNotIn("bad", str(caught.exception))
        for value in common:
            with self.subTest(profile=repr(value)), self.assertRaisesRegex(ContractError, "Invalid runtime profile"):
                self.launch("gjc", runtime={"model": "provider/model", "profile": value})

    def test_runtime_selectors_preserve_native_labels_and_nested_model_ids(self):
        for agent in PI_FAMILY:
            with self.subTest(agent=agent):
                launch = self.launch(agent, runtime={"model": "provider/team/model-v2:latest@rev", "thinking": "native-effort_2"})
                self.assertIn("provider/team/model-v2:latest@rev", launch.argv)
                self.assertIn("native-effort_2", launch.argv)
        launch = self.launch("gjc", runtime={"model": "provider/model", "profile": "work profile"})
        self.assertIn("work profile", launch.argv)

    def test_inherit_thinking_removes_stale_native_flag(self):
        for agent in PI_FAMILY:
            with self.subTest(agent=agent):
                launch = self.launch(agent, argv=[agent, "--thinking", "high"], runtime={"thinking": "inherit"})
                self.assertNotIn("--thinking", launch.argv)
                self.assertNotIn("high", launch.argv)

    def test_unknown_agent_is_a_contract_error(self):
        self.assertNotIn("unknown", AGENTS)
        with self.assertRaisesRegex(ContractError, "Unsupported agent"):
            self.launch("unknown")
        with self.assertRaisesRegex(ContractError, "Unsupported agent"):
            safe_launch_flags("unknown", ["unknown"])


if __name__ == "__main__":
    unittest.main()
