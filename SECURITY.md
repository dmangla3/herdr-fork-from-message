# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose local
conversation data, session identities, terminal contents, credentials, or arbitrary
command execution.

Report it privately through the repository's
[GitHub security advisory form](https://github.com/dmangla3/herdr-fork-from-message/security/advisories/new).
Include the affected version, agent and Herdr versions, reproduction steps using
disposable data, and the expected impact. Do not include real conversations,
credentials, inline provider configuration, or unredacted terminal captures.

For non-sensitive bugs, use the public issue tracker.

## Trust boundaries

This plugin controls local Herdr terminals under the current user's permissions.
It requires Herdr 0.9.5 or newer to identify the exact foreground process. A
matching Herdr integration supplies the native session reference, or a Pi-family
source owner publishes a fresh runtime-bridge registration. A label, recent log,
process argument containing a product name, or unrelated session file is not a
substitute for that identity.

Explicit automation selects both `--session <name>` and `--pane <id>`. The
selected server supplies its socket identity; a controller must not fabricate
`HERDR_ENV`, `HERDR_SOCKET_PATH`, or plugin-context metadata. Source process and
session identity are checked before launch and again after child verification.
Only the child receives picker keys. No source resume, prompt replay, login input,
trust approval, or permission answer is sent automatically.

The Pi/GJC/OMP/OmO owner bridge binds registrations to the server socket, pane,
terminal, process ID and start time, working directory, and session. Records are
owner-only, refresh every 5 seconds, and expire after 20 seconds. This protects
against stale or mismatched registrations, not a malicious process running as
the same OS user. GJC session navigation invalidates its registration. The
bridge must be loaded by the source owner. Pi/OMP/OmO use session-scoped extension
arguments; GJC v0.19.0 instead requires an explicit, user-reviewed entry in its
`extensions` configuration, preserving existing entries. The GJC setup has not
been live-revalidated on this branch. The bridge does not search for a separate
home-directory helper install.

`HERDR_FORK_HELPER` selects executable Python code. An override must name an
absolute, existing regular file; users must trust that file and its directory.
Without it, the helper must be adjacent to the bridge. State-root and server
selection variables likewise must point to the intended plugin instance. See
[README.md](README.md#environment-controls) for their precedence.

## Session data and native bookkeeping

The plugin never rewrites, truncates, or deletes agent-owned transcript/session
files. It can read at most a bounded header of the exact reported Pi-family file
to validate its session ID. The bridge reads runtime metadata, not the
conversation body; native agent commands still read and write their own data.

A native fork is not a promise that stored source data is byte-for-byte unchanged.
For example, Copilot's native `sessions.fork` writes a fork-info bookkeeping event
to the stored parent. This is not an assistant reply in the child. Copilot's
private headless RPC requests all source events without resuming the parent,
requires a distinct returned child ID, and verifies that the destination reports
that exact ID. A successful RPC can leave a stored child even if UI launch fails.

Cleanup closes only an action-created empty destination after a prelaunch
failure. Once launch has been attempted, approval waits, failed verification, and
picker timeouts preserve the destination for inspection. The plugin does not
close the source or delete native session files to undo a fork.

## Shared files, permissions, and networking

A fork uses the source working directory; it does not create a separate checkout,
container, or OS user. Normal mode retains native permissions. Restored goals,
automation, extensions, or queued work may resume under the agent's normal rules.
Source and child can therefore modify the same files.

Read-only mode is restricted to Claude Code, Codex, GJC, and OMP. It combines each
agent's native controls with read-tool policies; GJC also pauses inherited goal
state through its owner API. Other agents fail before destination creation.
These restrictions are not a universal OS sandbox or a guarantee against all
runtime startup effects. Native state writes, reads outside the checkout, loaded
configuration, and provider behavior must be considered separately. Codex's
native read-only sandbox does not extend that guarantee to other agents.

The plugin adds no telemetry, analytics, uploads, or developer callback. This does
not make the agent offline: normal startup, hooks, extensions, authentication,
and model-provider communication may occur before a new prompt. Do not use the
plugin as a privacy boundary against the agent or its configured provider.

Only allowlisted source selectors and supported root variables are relayed.
Prompts, credentials, broad permission flags, and arbitrary source options are not
replayed. Unknown or ambiguous option arity fails closed. Herdr does not expose
all variables set only inside a source shell, so an ID match cannot prove
arbitrary configuration/data-root equality. Owner-omitted Pi-family roots are
explicitly unset rather than borrowed from a controller's environment.

## Experimental and blocked paths

OpenCode and Copilot require explicit `--allow-experimental` for each CLI
invocation, a matching integration, and operator checks of source/child root
provenance. No installed action opts in automatically. OpenCode inline
`OPENCODE_CONFIG_CONTENT` is rejected, even when empty, because it can contain
secrets; use a trusted file via `OPENCODE_CONFIG` and unset inline content before
restarting the source.

Cursor and Antigravity are blocked because only concurrent-writer interactive
`/fork` behavior is verified. Hermes is blocked because its native branch omits
tool history and its storage import can lose metadata or repair storage. These
are native-contract limitations, not assumptions about any user's installation.

## Verification scope

Current local unit/compile/Biome gates and bounded public-branch Pi/OmO model-free
UI checks passed; see [README.md](README.md#development-and-verification-status).
Those UI checks used synthetic history and isolated agent roots in an explicitly
owned tab on an existing server. They are not server-isolation or provider checks.
Earlier Copilot durability results remain prior evidence. Compatibility claims
must distinguish fixtures, UI checks, CI and model/provider behavior; see
[CONTRIBUTING.md](CONTRIBUTING.md) for repeatable QA boundaries.
