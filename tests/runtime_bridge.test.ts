import { afterEach, describe, expect, test } from "bun:test";
import {
	existsSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import bridge, {
	readOnlyGuard,
	registrationHelper,
	runtimeAgent,
} from "../scripts/runtime_bridge";

const original = { ...process.env };
const directories: string[] = [];
const shutdowns: (() => unknown)[] = [];
type BridgeAPI = Parameters<typeof bridge>[0];
type Handler = Parameters<BridgeAPI["on"]>[1];
type Context = Parameters<Handler>[1];
const context = (): Context => ({
	cwd: "/fixture",
	sessionManager: {
		getSessionId: () => "fixture",
		getSessionFile: () => undefined,
	},
	ui: { notify: () => {} },
});
afterEach(() => {
	for (const shutdown of shutdowns.splice(0)) shutdown();
	for (const directory of directories.splice(0))
		rmSync(directory, { recursive: true, force: true });
	for (const key of Object.keys(process.env))
		if (!(key in original)) delete process.env[key];
	Object.assign(process.env, original);
});

function registrationFixture(agent: string, readOnly = false) {
	const directory = mkdtempSync(join(tmpdir(), "fork-from-message-bridge-"));
	directories.push(directory);
	const helper = join(directory, "register.py");
	writeFileSync(
		helper,
		'import json, os, sys\nfrom pathlib import Path\nPath(__file__).with_name("registration.json").write_text(sys.stdin.read(), encoding="utf-8")\nPath(__file__).with_name("environment.json").write_text(json.dumps({key: os.environ.get(key) for key in ("HERDR_PLUGIN_STATE_DIR", "HERDR_FORK_STATE_DIR")}), encoding="utf-8")\nPath(__file__).with_name("arguments.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")\n',
	);
	Object.assign(process.env, {
		HERDR_ENV: "1",
		HERDR_PANE_ID: "fixture",
		HERDR_FORK_AGENT: agent,
		HERDR_FORK_READ_ONLY: readOnly ? "1" : "0",
		HERDR_FORK_HELPER: helper,
	});
	delete process.env.HERDR_FORK_SESSION;
	const handlers = new Map<string, Handler>();
	const tools: string[][] = [];
	const pauses: string[] = [];
	const warnings: string[] = [];
	const ctx: Context = {
		...context(),
		model: { provider: "fixture-provider", id: "live-model" },
		sessionManager: {
			getSessionId: () => "child-session",
			getSessionFile: () => "/fixture/sessions/child-session.jsonl",
		},
		getActiveModelProfile: () => "live-profile",
		ui: { notify: (message) => warnings.push(message) },
		operateGoal: async (operation) => {
			pauses.push(operation);
		},
	};
	bridge({
		on: (name, handler) => {
			handlers.set(name, handler);
		},
		getThinkingLevel: () => "high",
		setActiveTools: async (names) => {
			tools.push(names);
		},
	});
	shutdowns.push(() => handlers.get("session_shutdown")?.({}, ctx));
	return {
		directory,
		helper,
		handlers,
		ctx,
		tools,
		pauses,
		warnings,
		payload: () =>
			JSON.parse(readFileSync(join(directory, "registration.json"), "utf8")),
		helperEnvironment: () =>
			JSON.parse(readFileSync(join(directory, "environment.json"), "utf8")),
		helperArguments: () =>
			JSON.parse(readFileSync(join(directory, "arguments.json"), "utf8")),
	};
}

describe("portable registration helper", () => {
	test("the default helper is adjacent to the bridge regardless of home/config roots", () => {
		const expected = fileURLToPath(
			new URL("../scripts/fork_from_message.py", import.meta.url),
		);
		expect(registrationHelper({})).toBe(expected);
		expect(
			registrationHelper({
				HOME: "/unrelated/home",
				USERPROFILE: "/unrelated/profile",
				XDG_CONFIG_HOME: "/unrelated/config",
				XDG_DATA_HOME: "/unrelated/data",
			}),
		).toBe(expected);
	});
	test("an explicit absolute regular helper is used without rewriting its path", () => {
		const fixture = registrationFixture("pi");
		expect(registrationHelper({ HERDR_FORK_HELPER: fixture.helper })).toBe(
			fixture.helper,
		);
	});
	test("invalid helper overrides never fall back to the adjacent helper", () => {
		const fixture = registrationFixture("pi");
		const missing = join(fixture.directory, "missing.py");
		const dangling = join(fixture.directory, "dangling.py");
		symlinkSync(missing, dangling);
		for (const helper of [
			"",
			" ",
			"register.py",
			"./register.py",
			missing,
			dangling,
			fixture.directory,
			`${fixture.helper}\n`,
			`${fixture.helper}\0`,
		])
			expect(() => registrationHelper({ HERDR_FORK_HELPER: helper })).toThrow(
				"HERDR_FORK_HELPER must be an absolute path to an existing regular file",
			);
	});
	test("a clone without its adjacent helper fails without looking in a home directory", async () => {
		const directory = mkdtempSync(join(tmpdir(), "fork-from-message-copy-"));
		directories.push(directory);
		const copy = join(directory, "runtime_bridge.ts");
		writeFileSync(
			copy,
			readFileSync(new URL("../scripts/runtime_bridge.ts", import.meta.url)),
		);
		const copied = (await import(pathToFileURL(copy).href)) as {
			registrationHelper: typeof registrationHelper;
		};
		expect(() =>
			copied.registrationHelper({ HOME: directory, USERPROFILE: directory }),
		).toThrow(
			"Fork from Message requires fork_from_message.py beside runtime_bridge.ts",
		);
	});
	test("an invalid override warns once without starting the registration helper", async () => {
		const fixture = registrationFixture("pi");
		process.env.HERDR_FORK_HELPER = fixture.directory;
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		await fixture.handlers.get("agent_end")?.({}, fixture.ctx);
		expect(fixture.warnings).toHaveLength(1);
		expect(fixture.warnings[0]).toStartWith(
			"Fork from Message registration unavailable:",
		);
		expect(fixture.warnings[0]).toContain("HERDR_FORK_HELPER");
		expect(existsSync(join(fixture.directory, "registration.json"))).toBe(
			false,
		);
	});
});

describe("Pi-family product identity", () => {
	test("a bare Bun runtime can use an explicit product identity", () => {
		for (const agent of ["gjc", "omp", "pi", "omo"] as const)
			expect(
				runtimeAgent(
					{ HERDR_FORK_AGENT: agent, OMO_NATIVE: "1" },
					"/usr/bin/bun",
					["bun"],
				),
			).toBe(agent);
	});
	test("a nested Pi executable or entrypoint outranks inherited OMO markers", () => {
		for (const environment of [
			{ OMO_NATIVE: "1" },
			{ SENPI_BRAND: '{"command":"omo"}' },
			{ HERDR_FORK_AGENT: "pi", OMO_NATIVE: "1" },
		]) {
			expect(runtimeAgent(environment, "/opt/bin/pi", ["pi"])).toBe("pi");
			expect(
				runtimeAgent(environment, "/usr/bin/bun", [
					"bun",
					"/opt/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
				]),
			).toBe("pi");
		}
	});
	test("an explicit unknown or conflicting product fails closed", () => {
		for (const [executable, argv, explicit] of [
			["/opt/bin/pi", ["pi"], "omo"],
			["/opt/bin/omo", ["omo"], "pi"],
			["/opt/bin/gjc", ["gjc"], "omp"],
			["/opt/bin/omp", ["omp"], "unknown"],
			[
				"/usr/bin/bun",
				[
					"bun",
					"/opt/node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
				],
				"omo",
			],
			["/usr/bin/bun", ["bun", "/opt/node_modules/omo-ai/bin/omo.js"], "pi"],
		] as const) {
			expect(
				runtimeAgent(
					{ HERDR_FORK_AGENT: explicit, OMO_NATIVE: "1" },
					executable,
					argv,
				),
			).toBeUndefined();
		}
	});
	test("OMO native and SENPI brand markers override the Pi transport identity", () => {
		for (const environment of [
			{ OMO_NATIVE: "1", HERDR_AGENT: "pi" },
			{ SENPI_BRAND: '{"command":"omo"}', HERDR_AGENT: "pi" },
		]) {
			expect(runtimeAgent(environment, "/usr/bin/bun", ["bun"])).toBe("omo");
		}
	});
	test("known native executable and engine paths identify the product", () => {
		for (const agent of ["gjc", "omp", "pi", "omo"] as const) {
			expect(runtimeAgent({}, `/opt/bin/${agent}`, [agent])).toBe(agent);
		}
		for (const [path, agent] of [
			[
				"/opt/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
				"pi",
			],
			["/opt/node_modules/@earendil-works/pi-coding-agent/dist/cli.js", "pi"],
			[
				"/opt/node_modules/@mariozechner/pi-coding-agent/dist/bundle/cli.js",
				"pi",
			],
			["/opt/node_modules/@mariozechner/pi-coding-agent/dist/cli.js", "pi"],
			["/opt/node_modules/omo-ai/bin/omo.js", "omo"],
			[
				"/opt/node_modules/omo-ai/node_modules/@code-yeongyu/senpi/dist/bundle/cli.js",
				"omo",
			],
			[
				"/opt/node_modules/omo-ai/node_modules/@code-yeongyu/senpi/dist/cli.js",
				"omo",
			],
			["/opt/node_modules/@code-yeongyu/senpi/dist/bundle/cli.js", "omo"],
			["/opt/node_modules/@code-yeongyu/senpi/dist/cli.js", "omo"],
		]) {
			expect(runtimeAgent({}, "/usr/bin/bun", ["bun", path])).toBe(agent);
		}
	});
	test("unknown or invalid identities never silently register as OMP", () => {
		for (const environment of [
			{},
			{ HERDR_AGENT: "pi" },
			{ HERDR_FORK_AGENT: "other" },
			{ SENPI_BRAND: "not json" },
			{ SENPI_BRAND: '{"command":"other"}' },
		]) {
			expect(
				runtimeAgent(environment, "/usr/bin/bun", ["bun", "/fixture/cli.js"]),
			).toBeUndefined();
		}
		expect(
			runtimeAgent({}, "/usr/bin/bun", ["bun", "/fixture/cli.js", "omo"]),
		).toBeUndefined();
		expect(runtimeAgent({}, "/usr/bin/bun", ["bun", "omo"])).toBeUndefined();
		expect(runtimeAgent({}, "/opt/bin/omp", ["omp", "omo"])).toBe("omp");
		expect(
			runtimeAgent(
				{ HERDR_FORK_AGENT: "other", OMO_NATIVE: "1" },
				"/opt/bin/omp",
				["omp"],
			),
		).toBeUndefined();
	});
	test("native-looking paths in prompts or extensions are not runtime entrypoints", () => {
		for (const path of [
			"/opt/node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
			"/opt/node_modules/@mariozechner/pi-coding-agent/dist/cli.js",
			"/opt/node_modules/omo-ai/bin/omo.js",
			"/opt/node_modules/@code-yeongyu/senpi/dist/bundle/cli.js",
		]) {
			for (const argv of [
				["bun", "/fixture/cli.js", "--prompt", path],
				["bun", "/fixture/cli.js", "--extension", path],
				["bun", "--eval", path],
				["bun", `${path}.backup`],
			])
				expect(runtimeAgent({}, "/usr/bin/bun", argv)).toBeUndefined();
		}
	});
	test("an unidentified runtime warns without registering or changing tools", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_FORK_AGENT: "unknown",
		});
		const handlers = new Map<string, Handler>();
		const warnings: string[] = [];
		let changedTools = false;
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {
				changedTools = true;
			},
		});
		expect([...handlers.keys()]).toEqual(["session_start"]);
		await handlers.get("session_start")?.(
			{},
			{
				...context(),
				ui: {
					notify: (message) => {
						warnings.push(message);
					},
				},
			},
		);
		expect(warnings[0]).toContain("product could not be identified");
		expect(warnings[0]).toStartWith(
			"Fork from Message registration unavailable:",
		);
		expect(warnings[0]).toContain("HERDR_FORK_AGENT");
		expect(changedTools).toBe(false);
	});
});

describe("Pi-family owner registration", () => {
	test("the selected server is passed as one explicit helper argument", async () => {
		const fixture = registrationFixture("pi");
		process.env.HERDR_FORK_SESSION = "selected server";
		process.env.HERDR_SESSION = "unrelated-focus";
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		expect(fixture.helperArguments()).toEqual([
			"--session",
			"selected server",
			"--register",
		]);
		expect(fixture.payload().environment.HERDR_FORK_SESSION).toBeUndefined();
		expect(process.env.HERDR_FORK_SESSION).toBe("selected server");
	});
	test("an absent selected server does not guess one from focus or other environment", async () => {
		const fixture = registrationFixture("omo");
		process.env.HERDR_SESSION = "unrelated-focus";
		process.env.HERDR_PLUGIN_CONTEXT_JSON = JSON.stringify({
			session: "unrelated-context",
			focused_pane_id: "other:pane",
		});
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		expect(fixture.helperArguments()).toEqual(["--register"]);
	});
	for (const session of ["", " ", "selected\nserver", "selected\0server"]) {
		test(`an invalid selected server ${JSON.stringify(session)} fails closed`, async () => {
			const fixture = registrationFixture("pi");
			process.env.HERDR_FORK_SESSION = session;
			await fixture.handlers.get("session_start")?.({}, fixture.ctx);
			expect(fixture.warnings[0]).toContain("HERDR_FORK_SESSION");
			expect(existsSync(join(fixture.directory, "registration.json"))).toBe(
				false,
			);
		});
	}
	test("an unauthenticated placeholder model is not relayed as a real selector", async () => {
		const fixture = registrationFixture("pi");
		await fixture.handlers.get("session_start")?.(
			{},
			{
				...fixture.ctx,
				model: { provider: "unknown", id: "unknown" },
			},
		);
		expect(fixture.payload().model).toBeUndefined();
	});
	test("the helper uses the parent's pinned root over an inherited plugin root", async () => {
		const fixture = registrationFixture("pi");
		process.env.HERDR_FORK_STATE_DIR = "/parent/pinned-state";
		process.env.HERDR_PLUGIN_STATE_DIR = "/shell/conflicting-state";
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		expect(fixture.helperEnvironment()).toEqual({
			HERDR_FORK_STATE_DIR: "/parent/pinned-state",
			HERDR_PLUGIN_STATE_DIR: "/parent/pinned-state",
		});
		expect(process.env.HERDR_PLUGIN_STATE_DIR).toBe("/shell/conflicting-state");
		expect(process.env.HERDR_FORK_STATE_DIR).toBe("/parent/pinned-state");
	});
	test("an explicitly empty pinned root is not replaced with an inherited plugin root", async () => {
		const fixture = registrationFixture("pi");
		process.env.HERDR_FORK_STATE_DIR = "";
		process.env.HERDR_PLUGIN_STATE_DIR = "/shell/conflicting-state";
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		expect(fixture.helperEnvironment()).toEqual({
			HERDR_FORK_STATE_DIR: "",
			HERDR_PLUGIN_STATE_DIR: "",
		});
		expect(process.env.HERDR_PLUGIN_STATE_DIR).toBe("/shell/conflicting-state");
	});
	test("the helper keeps the existing plugin root when no parent root is pinned", async () => {
		const fixture = registrationFixture("omo");
		delete process.env.HERDR_FORK_STATE_DIR;
		process.env.HERDR_PLUGIN_STATE_DIR = "/shell/existing-state";
		await fixture.handlers.get("session_start")?.({}, fixture.ctx);
		expect(fixture.helperEnvironment()).toEqual({
			HERDR_FORK_STATE_DIR: null,
			HERDR_PLUGIN_STATE_DIR: "/shell/existing-state",
		});
	});
	for (const event of ["session_switch", "session_branch", "session_tree"]) {
		test(`GJC ${event} writes a tombstone and never trusts later stale contexts`, async () => {
			const fixture = registrationFixture("gjc");
			await fixture.handlers.get("session_start")?.({}, fixture.ctx);
			expect(fixture.payload().registration_valid).toBe(true);
			await fixture.handlers.get(event)?.({}, fixture.ctx);
			expect(fixture.payload().registration_valid).toBe(false);
			expect(fixture.payload().read_only_ready).toBe(false);
			expect(fixture.warnings[0]).toContain("invalidated");
			await fixture.handlers.get("agent_end")?.(
				{},
				{
					...fixture.ctx,
					sessionManager: {
						getSessionId: () => "untrusted-session",
						getSessionFile: () => undefined,
					},
				},
			);
			expect(fixture.payload().session_id).toBe("child-session");
			expect(fixture.payload().registration_valid).toBe(false);
		});
	}
	for (const [agent, keys] of [
		["gjc", ["GJC_CONFIG_DIR", "GJC_CODING_AGENT_DIR"]],
		["omp", ["PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"]],
		["pi", ["PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"]],
		[
			"omo",
			[
				"OMO_CODING_AGENT_DIR",
				"SENPI_CODING_AGENT_DIR",
				"PI_CODING_AGENT_DIR",
				"OMO_CODING_AGENT_SESSION_DIR",
			],
		],
	] as const) {
		test(`${agent} registers live settings and configuration without restricting normal mode`, async () => {
			const fixture = registrationFixture(agent);
			for (const key of [
				"GJC_CONFIG_DIR",
				"GJC_CODING_AGENT_DIR",
				"PI_CONFIG_DIR",
				"PI_CODING_AGENT_DIR",
				"PI_CODING_AGENT_SESSION_DIR",
				"OMO_CODING_AGENT_DIR",
				"SENPI_CODING_AGENT_DIR",
				"OMO_CODING_AGENT_SESSION_DIR",
			])
				process.env[key] = `/unrelated/${key}`;
			for (const key of [
				...keys,
				"XDG_CONFIG_HOME",
				"XDG_DATA_HOME",
				"XDG_STATE_HOME",
				"XDG_CACHE_HOME",
			])
				process.env[key] = `/source/${key}`;
			process.env.FORK_TEST_API_KEY = "not-relayed";
			await fixture.handlers.get("session_start")?.({}, fixture.ctx);
			const payload = fixture.payload();
			expect(payload.agent).toBe(agent);
			expect(payload.session_id).toBe("child-session");
			expect(payload.session_file).toBe(
				"/fixture/sessions/child-session.jsonl",
			);
			expect(payload.model).toBe("fixture-provider/live-model");
			expect(payload.profile).toBe("live-profile");
			expect(payload.thinking).toBe("high");
			expect(payload.read_only_ready).toBe(true);
			expect(payload.registration_valid).toBe(true);
			expect(Object.keys(payload.environment).sort()).toEqual(
				[
					...keys,
					"XDG_CONFIG_HOME",
					"XDG_DATA_HOME",
					"XDG_STATE_HOME",
					"XDG_CACHE_HOME",
				].sort(),
			);
			for (const key of keys)
				expect(payload.environment[key]).toBe(`/source/${key}`);
			expect(payload.environment.XDG_STATE_HOME).toBe("/source/XDG_STATE_HOME");
			expect(payload.environment.FORK_TEST_API_KEY).toBeUndefined();
			expect(fixture.tools).toEqual([]);
			expect(fixture.pauses).toEqual([]);
			for (const toolName of ["write", "bash", "goal", "task"])
				expect(
					await fixture.handlers.get("tool_call")?.({ toolName }, fixture.ctx),
				).toBeUndefined();
			for (const name of [
				"session_before_switch",
				"session_before_branch",
				"session_before_tree",
			])
				expect(
					await fixture.handlers.get(name)?.({}, fixture.ctx),
				).toBeUndefined();
			expect(
				await fixture.handlers.get("before_agent_start")?.(
					{ systemPrompt: "Original prompt" },
					fixture.ctx,
				),
			).toBeUndefined();
			process.env[keys[0]] = "/updated/config-root";
			await fixture.handlers.get("agent_end")?.(
				{},
				{
					...fixture.ctx,
					model: { provider: "updated", id: "model" },
				},
			);
			expect(fixture.payload().environment[keys[0]]).toBe(
				"/updated/config-root",
			);
			expect(fixture.payload().model).toBe("updated/model");
		});
	}
	for (const agent of ["gjc", "omp"]) {
		test(`${agent} retains its verified read-only runtime gates`, async () => {
			const fixture = registrationFixture(agent, true);
			await fixture.handlers.get("session_start")?.({}, fixture.ctx);
			expect(fixture.payload().read_only_ready).toBe(true);
			expect(fixture.tools).toEqual([
				agent === "gjc"
					? ["read", "search", "find", "ast_grep"]
					: ["read", "grep", "glob"],
			]);
			expect(fixture.pauses).toEqual(agent === "gjc" ? ["pause"] : []);
			expect(
				await fixture.handlers.get("tool_call")?.(
					{ toolName: "bash" },
					fixture.ctx,
				),
			).toMatchObject({ block: true });
			expect(
				await fixture.handlers.get("tool_call")?.(
					{ toolName: "read" },
					fixture.ctx,
				),
			).toBeUndefined();
			expect(
				await fixture.handlers.get("session_before_switch")?.({}, fixture.ctx),
			).toEqual({ cancel: true });
			const result = (await fixture.handlers.get("before_agent_start")?.(
				{ systemPrompt: "Original prompt" },
				fixture.ctx,
			)) as { systemPrompt: string };
			expect(result.systemPrompt).toStartWith("Original prompt\n\n");
			expect(result.systemPrompt).toContain("Only read tools are permitted");
		});
	}
	for (const agent of ["pi", "omo"]) {
		test(`${agent} rejects unverified read-only registration`, async () => {
			Object.assign(process.env, {
				HERDR_ENV: "1",
				HERDR_PANE_ID: "fixture",
				HERDR_FORK_AGENT: agent,
				HERDR_FORK_READ_ONLY: "1",
			});
			const handlers = new Map<string, Handler>();
			let changedTools = false;
			bridge({
				on: (name, handler) => {
					handlers.set(name, handler);
				},
				getThinkingLevel: () => "high",
				setActiveTools: async () => {
					changedTools = true;
				},
			});
			await expect(
				handlers.get("session_start")?.({}, context()),
			).rejects.toThrow("native guard support has not been verified");
			expect(changedTools).toBe(false);
		});
	}
});

describe("optional read-only tool boundary", () => {
	for (const name of ["read", "search", "find", "ast_grep", "grep", "glob"]) {
		test(`allows ${name}`, () =>
			expect(readOnlyGuard({ toolName: name })).toBeUndefined());
	}
	for (const name of [
		"goal",
		"write",
		"bash",
		"edit",
		"task",
		"irc",
		"mcp__service_send",
		"search_tool_bm25",
		"eval",
		"retain",
		"new_tool",
		"",
	]) {
		test(`blocks ${name || "missing tool name"}`, () =>
			expect(readOnlyGuard({ toolName: name })?.block).toBe(true));
	}
	test("does nothing outside Herdr", () => {
		delete process.env.HERDR_ENV;
		const handlers: string[] = [];
		bridge({
			on: (name) => {
				handlers.push(name);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		expect(handlers).toEqual([]);
	});
	test("GJC refuses a read-only runtime without authoritative pause control", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_FORK_AGENT: "gjc",
			HERDR_FORK_READ_ONLY: "1",
		});
		const handlers = new Map<string, Handler>();
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		await expect(
			handlers.get("session_start")?.({}, context()),
		).rejects.toThrow("goal pause gate");
	});
	test("in-process GJC navigation stops future registration instead of trusting stale context", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_FORK_AGENT: "gjc",
		});
		delete process.env.HERDR_FORK_READ_ONLY;
		const handlers = new Map<string, Handler>();
		const warnings: string[] = [];
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		await handlers.get("session_switch")?.(
			{},
			{
				...context(),
				ui: { notify: (message: string) => warnings.push(message) },
			},
		);
		expect(warnings[0]).toContain("invalidated");
		await handlers.get("session_shutdown")?.({}, context());
	});
	test("read-only forks cancel all in-process identity navigation", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_FORK_AGENT: "gjc",
			HERDR_FORK_READ_ONLY: "1",
		});
		const handlers = new Map<string, Handler>();
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {},
		});
		for (const name of [
			"session_before_switch",
			"session_before_branch",
			"session_before_tree",
		]) {
			expect(await handlers.get(name)?.({}, context())).toEqual({
				cancel: true,
			});
		}
	});
	test("normal Fork from Message allows mutations and session navigation", async () => {
		Object.assign(process.env, {
			HERDR_ENV: "1",
			HERDR_PANE_ID: "fixture",
			HERDR_FORK_AGENT: "gjc",
			HERDR_FORK_READ_ONLY: "0",
		});
		const handlers = new Map<string, Handler>();
		let changedTools = false;
		bridge({
			on: (name, handler) => {
				handlers.set(name, handler);
			},
			getThinkingLevel: () => "high",
			setActiveTools: async () => {
				changedTools = true;
			},
		});
		for (const toolName of ["write", "edit", "bash", "goal", "task"]) {
			expect(
				await handlers.get("tool_call")?.({ toolName }, context()),
			).toBeUndefined();
		}
		for (const name of [
			"session_before_switch",
			"session_before_branch",
			"session_before_tree",
		]) {
			expect(await handlers.get(name)?.({}, context())).toBeUndefined();
		}
		expect(changedTools).toBe(false);
	});
});
