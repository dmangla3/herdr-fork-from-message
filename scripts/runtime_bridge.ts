import { spawn } from "node:child_process";
import { statSync } from "node:fs";
import { basename, isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";

type Context = {
	cwd: string;
	model?: { provider: string; id: string };
	sessionManager: {
		getSessionId(): string;
		getSessionFile(): string | undefined;
	};
	getActiveModelProfile?: () => string | undefined;
	getGoalState?: () => unknown;
	operateGoal?: (op: "pause") => Promise<unknown>;
	ui: { notify(message: string, level?: string): void };
};
type Event = {
	toolName?: string;
	systemPrompt?: string | string[];
	type?: string;
};
type API = {
	on(name: string, handler: (event: Event, ctx: Context) => unknown): void;
	getThinkingLevel(): string | undefined;
	setActiveTools(names: string[]): Promise<void>;
};
type PiAgent = "gjc" | "omp" | "pi" | "omo";

const environmentKeys: Record<PiAgent, readonly string[]> = {
	gjc: ["GJC_CONFIG_DIR", "GJC_CODING_AGENT_DIR"],
	omp: ["PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"],
	pi: ["PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"],
	omo: [
		"OMO_CODING_AGENT_DIR",
		"SENPI_CODING_AGENT_DIR",
		"PI_CODING_AGENT_DIR",
		"OMO_CODING_AGENT_SESSION_DIR",
	],
};
const xdgKeys = [
	"XDG_CONFIG_HOME",
	"XDG_DATA_HOME",
	"XDG_STATE_HOME",
	"XDG_CACHE_HOME",
];

export function runtimeAgent(
	environment: Record<string, string | undefined>,
	executable: string,
	argv: readonly string[],
): PiAgent | undefined {
	let product: PiAgent | undefined;
	// Product executables take precedence over script paths and user arguments.
	for (const path of [executable, argv[0]]) {
		if (!path) continue;
		const name = basename(path).replace(/\.exe$/, "");
		if (Object.hasOwn(environmentKeys, name)) {
			product = name as PiAgent;
			break;
		}
	}
	// Only inspect the runtime entrypoint, never prompts or extension arguments.
	if (!product) {
		for (const path of argv.slice(0, 2)) {
			if (
				/\/node_modules\/(?:omo-ai\/bin\/omo\.js|@code-yeongyu\/senpi\/dist\/(?:bundle\/)?cli\.js)$/.test(
					path,
				)
			) {
				product = "omo";
				break;
			}
			if (
				/\/node_modules\/@(?:earendil-works|mariozechner)\/pi-coding-agent\/dist\/(?:bundle\/)?cli\.js$/.test(
					path,
				)
			) {
				product = "pi";
				break;
			}
		}
	}
	const explicit = environment.HERDR_FORK_AGENT;
	if (explicit !== undefined) {
		if (
			!Object.hasOwn(environmentKeys, explicit) ||
			(product && product !== explicit)
		)
			return undefined;
		return explicit as PiAgent;
	}
	if (product) return product;
	if (environment.OMO_NATIVE === "1") return "omo";
	try {
		if (JSON.parse(environment.SENPI_BRAND ?? "null")?.command === "omo")
			return "omo";
	} catch {
		// An invalid brand marker is not evidence of product identity.
	}
}

export function registrationHelper(
	environment: Record<string, string | undefined>,
): string {
	const override = environment.HERDR_FORK_HELPER;
	const helper =
		override ??
		fileURLToPath(new URL("./fork_from_message.py", import.meta.url));
	const message =
		override === undefined
			? "Fork from Message requires fork_from_message.py beside runtime_bridge.ts"
			: "HERDR_FORK_HELPER must be an absolute path to an existing regular file";
	if (!isAbsolute(helper) || hasControlCharacters(helper))
		throw new Error(message);
	try {
		if (statSync(helper).isFile()) return helper;
	} catch {
		// A missing or inaccessible helper never selects a different installation.
	}
	throw new Error(message);
}

function hasControlCharacters(value: string): boolean {
	return Array.from(value).some((character) => {
		const code = character.charCodeAt(0);
		return code < 32 || (code >= 127 && code <= 159);
	});
}

const reads = new Set(["read", "search", "find", "ast_grep", "grep", "glob"]);
const policy =
	"Independent read-only fork: inherited work is background. Do not resume prior tasks, change files/memory/settings, create agents/jobs, or contact the parent. The checkout is shared. Only read tools are permitted.";

export function readOnlyGuard(event: Event) {
	if (!reads.has(event.toolName ?? "")) {
		return {
			block: true,
			reason:
				"Read-only fork: this tool can mutate state or escape the read-only boundary.",
		};
	}
}

export default function runtimeBridge(api: API) {
	if (process.env.HERDR_ENV !== "1" || !process.env.HERDR_PANE_ID) return;
	const agent = runtimeAgent(process.env, process.execPath, process.argv);
	if (!agent) {
		api.on("session_start", (_event, ctx) => {
			ctx.ui.notify(
				"Fork from Message registration unavailable: the Pi-family product could not be identified. Set HERDR_FORK_AGENT to the verified product.",
				"warning",
			);
		});
		return;
	}
	const readOnly = process.env.HERDR_FORK_READ_ONLY === "1";
	let current: Context | undefined;
	let busy = false;
	let ready = !readOnly;
	let timer: ReturnType<typeof setInterval> | undefined;
	let warned = false;
	let invalidated = false;

	async function guard(ctx: Context) {
		if (!readOnly) return;
		if (agent !== "gjc" && agent !== "omp")
			throw new Error(
				`Read-only mode is not supported for ${agent}: native guard support has not been verified`,
			);
		// Pause through the owning session API, never rewrite its transcript.
		if (agent === "gjc") {
			if (!ctx.operateGoal)
				throw new Error(
					"GJC runtime does not expose the required goal pause gate",
				);
			await ctx.operateGoal("pause");
		}
		await api.setActiveTools(
			agent === "gjc"
				? ["read", "search", "find", "ast_grep"]
				: ["read", "grep", "glob"],
		);
		ready = true;
	}

	async function publish(invalidate = false) {
		if (busy || !current || (invalidated && !invalidate)) return;
		busy = true;
		try {
			const ctx = current;
			const environment: Record<string, string> = {};
			for (const key of [...environmentKeys[agent], ...xdgKeys]) {
				if (process.env[key]) environment[key] = process.env[key] as string;
			}
			const payload = {
				agent,
				pid: process.pid,
				cwd: ctx.cwd,
				session_id: ctx.sessionManager.getSessionId(),
				session_file: ctx.sessionManager.getSessionFile(),
				model:
					ctx.model &&
					ctx.model.provider !== "unknown" &&
					ctx.model.id !== "unknown"
						? `${ctx.model.provider}/${ctx.model.id}`
						: undefined,
				profile: ctx.getActiveModelProfile?.(),
				thinking: api.getThinkingLevel(),
				environment,
				bridge_version: 1,
				read_only_ready: ready,
				registration_valid: !invalidated,
			};
			const helper = registrationHelper(process.env);
			const session = process.env.HERDR_FORK_SESSION;
			if (
				session !== undefined &&
				(!session.trim() || hasControlCharacters(session))
			)
				throw new Error(
					"HERDR_FORK_SESSION must be a nonempty server selector without control characters",
				);
			const arguments_ = [
				helper,
				...(session === undefined ? [] : ["--session", session]),
				"--register",
			];
			const helperEnvironment = { ...process.env };
			if (helperEnvironment.HERDR_FORK_STATE_DIR !== undefined)
				helperEnvironment.HERDR_PLUGIN_STATE_DIR =
					helperEnvironment.HERDR_FORK_STATE_DIR;
			await new Promise<void>((resolve, reject) => {
				const child = spawn("python3", arguments_, {
					env: helperEnvironment,
					stdio: ["pipe", "ignore", "pipe"],
					timeout: 4000,
				});
				let detail = "";
				child.stderr.on("data", (data) => {
					detail += data.toString();
				});
				child.on("error", reject);
				child.on("close", (code) =>
					code === 0
						? resolve()
						: reject(new Error(detail || "owner registration failed")),
				);
				child.stdin.on("error", () => {});
				child.stdin.end(JSON.stringify(payload));
			});
		} catch (error) {
			if (!warned) {
				current?.ui.notify(
					`Fork from Message registration unavailable: ${String(error)}`,
					"warning",
				);
				warned = true;
			}
		} finally {
			busy = false;
		}
	}

	const update = async (_event: Event, ctx: Context) => {
		current = ctx;
		await guard(ctx);
		if (invalidated) return;
		await publish();
		if (!timer) {
			timer = setInterval(() => {
				void publish();
			}, 5000);
			timer.unref();
		}
	};
	api.on("session_start", update);
	api.on("agent_end", update);
	for (const event of [
		"session_before_switch",
		"session_before_branch",
		"session_before_tree",
	]) {
		api.on(event, () => (readOnly ? { cancel: true } : undefined));
	}
	for (const event of ["session_switch", "session_branch", "session_tree"])
		api.on(event, async (value, ctx) => {
			if (agent === "gjc") {
				// GJC 0.19 may retain the startup manager in extension contexts after a
				// cold-sidecar transition. Refuse stale identity rather than re-publish it.
				invalidated = true;
				ready = false;
				if (timer) clearInterval(timer);
				// Let any preceding heartbeat finish, then overwrite it with a tombstone.
				while (busy) await new Promise((resolve) => setTimeout(resolve, 10));
				await publish(true);
				ctx.ui.notify(
					"Fork from Message registration invalidated by session navigation. Restart GJC to register its current session safely.",
					"warning",
				);
				return;
			}
			await update(value, ctx);
		});
	api.on("session_shutdown", () => {
		if (timer) clearInterval(timer);
		current = undefined;
	});
	api.on("before_agent_start", async (event, ctx) => {
		await update(event, ctx);
		if (!readOnly) return;
		const original = event.systemPrompt ?? [];
		return {
			systemPrompt: Array.isArray(original)
				? [...original, policy]
				: `${original}\n\n${policy}`,
		};
	});
	api.on("tool_call", (event) => (readOnly ? readOnlyGuard(event) : undefined));
}
