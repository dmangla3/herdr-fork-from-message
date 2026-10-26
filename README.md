# Fork from Message for Herdr

[![CI](https://github.com/dmangla3/herdr-fork-from-message/actions/workflows/ci.yml/badge.svg)](https://github.com/dmangla3/herdr-fork-from-message/actions/workflows/ci.yml)

Fork the exact native conversation in the focused [Herdr](https://herdr.dev) pane
into a new tab, split pane, or workspace. The original actions still open Codex or
Claude Code's native earlier-message picker. Explicit current-point and restricted
read-only modes are also available, subject to the compatibility table below.

The plugin does not resume the original source as a second interactive writer,
send it keys, or replace a failed fork with a fresh conversation. Native fork
bookkeeping can nevertheless update agent-owned metadata, including the stored
parent session. This is not an immutable snapshot or a separate filesystem.

## Requirements

| Dependency | Requirement |
| --- | --- |
| Herdr | 0.9.5 or newer; the foreground-process API is required |
| Python | 3.10 or newer; standard library only |
| Agent CLI | Only the agent you use, with the native capabilities below |
| Bun and Biome | Developers only; not required to use the plugin |

The historical picker controls below correspond to Codex CLI 0.146.1 and Claude
Code 2.1.220. Other versions must retain compatible native commands, integration
metadata, and UI markers. Authentication and provider access remain the agent's
responsibility; a startup or fork is not a promise of an offline agent session.

## Compatibility

| Agent | Earlier-message picker (`message`) | Current-point fork (`fork`) | Restricted `read-only` |
| --- | --- | --- | --- |
| Claude Code | Yes | Native `--resume ... --fork-session` | Built-in read tools; MCP and hooks disabled |
| Codex | Yes | Native `fork <session-id>` | Native read-only sandbox and additional tool restrictions |
| GJC | No | Owner bridge required; see setup qualification below | Owner bridge, read tools, and inherited-goal pause |
| OMP | No | Owner bridge required | Owner bridge and read-tool restrictions |
| Pi | No | Owner bridge required | Unavailable |
| OmO | No | Owner bridge required; actual OmO launcher retained | Unavailable |
| OpenCode | No | Experimental; explicit CLI opt-in only | Unavailable |
| GitHub Copilot | No | Experimental; explicit CLI opt-in only | Unavailable |
| Cursor | No | Blocked: only an interactive `/fork` with a concurrent source writer is verified | Unavailable |
| Antigravity | No | Blocked: only an interactive `/fork` with a concurrent source writer is verified | Unavailable |
| Hermes | No | Blocked: native branch drops tool history; storage import is not a safe, portable full-history fork | Unavailable |

These are bounded implementation contracts, not a claim of live verification for
every CLI version, provider, or configuration. Unsupported modes and blocked agents
fail before a destination is created. OpenCode and Copilot require
`--allow-experimental` on every invocation; no installed action enables it.

GJC's bridge-dependent paths require a bridge already loaded by the source owner.
GJC v0.19.0 uses explicit extension configuration, not a `--extension` launch flag.
See [Runtime bridge](#runtime-bridge) for the separate manual setup and verification
limits.

## Install

Install directly from GitHub:

```sh
herdr plugin install dmangla3/herdr-fork-from-message
```

Check that Herdr recognizes the plugin and your agent integration:

```sh
herdr plugin list --plugin dmangla3.fork-from-message
herdr integration status
```

For Codex or Claude, install only the matching integration when it is missing or
outdated, then restart that agent:

```sh
herdr integration install codex
herdr integration install claude
```

Other agents need matching Herdr session metadata or the owner bridge described
below. Installation does not repair dependencies, install hooks or services,
change provider configuration, or reserve a keyboard shortcut.

## Actions and modes

| Action | Qualified action ID | Mode | Destination |
| --- | --- | --- | --- |
| Fork agent from message | `dmangla3.fork-from-message.fork` | `message` | New tab |
| Fork agent beside current pane | `dmangla3.fork-from-message.fork-pane` | `message` | Split pane |
| Fork agent into new workspace | `dmangla3.fork-from-message.fork-workspace` | `message` | New workspace |
| Fork current conversation | `dmangla3.fork-from-message.fork-current` | `fork` | New tab |
| Fork current conversation beside pane | `dmangla3.fork-from-message.fork-current-pane` | `fork` | Split pane |
| Fork current conversation into workspace | `dmangla3.fork-from-message.fork-current-workspace` | `fork` | New workspace |
| Fork current conversation read-only | `dmangla3.fork-from-message.fork-read-only` | `read-only` | New tab |

Invoke an action from Herdr's action UI, or from the CLI:

```sh
herdr plugin action invoke fork --plugin dmangla3.fork-from-message
herdr plugin action invoke fork-pane --plugin dmangla3.fork-from-message
herdr plugin action invoke fork-workspace --plugin dmangla3.fork-from-message
herdr plugin action invoke fork-current --plugin dmangla3.fork-from-message
herdr plugin action invoke fork-read-only --plugin dmangla3.fork-from-message
```

The script defaults remain `--mode message --destination tab`. Use `--mode fork`
for a current-point native child without a message picker, or `--mode read-only`
for a current-point child with the supported agent's restrictions.

### Explicit server automation

For use outside a plugin action, run the script from an actual checkout and select
both the Herdr server and source pane. Use the server name `default` explicitly
when that is the intended server; replace `PANE_ID` with its pane ID:

```sh
checkout="$PWD" # Run from the plugin checkout.
python3 "$checkout/scripts/fork_from_message.py" \
  --session default --pane PANE_ID \
  --mode fork --destination pane --direction right --no-focus
```

Do not fabricate `HERDR_ENV`, `HERDR_SOCKET_PATH`, or plugin-context variables.
`--session <name>` pins all Herdr operations to that server; the script reads its
socket from `herdr --session <name> status` for owner-record lookup. Controllers
do not need to supply socket variables. `--direction auto|right|down` controls
splits; the default `auto` keeps the original adaptive behavior: right for wide
panes, down for narrow or tall panes. `--no-focus` leaves focus at the source.

### Optional keybindings

No shortcut is installed or reserved. After checking your Herdr configuration for
conflicts, an optional binding in `~/.config/herdr/config.toml` could be:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "dmangla3.fork-from-message.fork"
description = "Fork agent from message"
```

Here `prefix` means your configured Herdr prefix, not a required key combination.
Choose any free binding or use the action UI without one. After editing:

```sh
herdr config check
herdr server reload-config
```

## Historical message selection

1. Focus an idle Codex or Claude conversation.
2. Invoke one of the three original message actions.
3. Wait for the verified native picker in the new destination.
4. Select the earlier user message and confirm in the agent UI.

For Codex CLI 0.146.1, the plugin opens the transcript with `Ctrl+T`, verifies its
marker, then sends `Esc` and verifies the message picker. `Esc` or Left selects an
older user message, Right selects a newer one, and Enter confirms. Up and Down
scroll the transcript rather than changing the selection.

For Claude, the plugin opens Rewind with `Esc`, `Esc` and verifies the picker.
Use Up and Down, then Enter. **Restore conversation** rewinds the forked
conversation without restoring files. Claude's code-restoration choices can
modify the shared checkout; the plugin never selects them for you.

Only the child receives picker keys. No prompts, trust approvals, login input, or
permission answers are sent automatically.

## Runtime bridge

Pi, GJC, OMP, and OmO require `scripts/runtime_bridge.ts` loaded by the **source
session owner**. A Herdr agent label alone is insufficient. The bridge publishes
only allowlisted session, process, working-directory, model, and root metadata to
an owner-only local registration. It refreshes every 5 seconds; a registration
older than 20 seconds is rejected. PID, process start time, pane, terminal, socket,
and working directory must still match.

For Pi, OMP, or OmO, use a session-scoped extension from an actual checkout rather
than copying files into a global extension directory:

```sh
git clone https://github.com/dmangla3/herdr-fork-from-message.git
cd herdr-fork-from-message
checkout="$PWD"
herdr plugin link "$checkout"

# In the intended Herdr source pane, launch only the agent you use:
pi --extension "$checkout/scripts/runtime_bridge.ts"
# or: omp --extension "$checkout/scripts/runtime_bridge.ts"
# or: omo --extension "$checkout/scripts/runtime_bridge.ts"
```

Keep the checkout available while these sessions use its bridge. The helper is
resolved next to the bridge; no separate home-directory installation is searched.

**GJC is different:** v0.19.0 has no `--extension` flag. For a GJC source, inspect
its existing extension configuration and obtain the actual bridge path from the
checkout:

```sh
gjc config get extensions --json
printf '%s\n' "$checkout/scripts/runtime_bridge.ts"
```

Use GJC's supported configuration interface to add that absolute path to its
`extensions` array, preserving every existing entry. This is an explicit,
user-reviewed configuration change, not something the plugin installs or edits.
Restart the GJC source under the intended configuration root so the owner and
child load the same registered bridge. This configuration route has not been
live-revalidated on the public branch. A GJC registration is invalidated after
session switch, branch, or tree navigation; restart the source with its bridge
loaded before another fork.

OmO can appear as Pi in Herdr metadata. The foreground entrypoint and owner record
must identify OmO, and the child still uses `omo`, not a substituted Pi launcher.
Unknown configuration roots omitted by a Pi-family owner are explicitly unset
for the child instead of being filled from the caller's unrelated roots.

### Environment controls

Prefer CLI modes and server selection over setting bridge-control variables by
hand. All paths below must refer to the intended installation or state root.

| Variable | Purpose |
| --- | --- |
| `HERDR_FORK_AGENT` | Explicit verified Pi-family product when runtime detection needs it; cannot override a conflicting executable identity |
| `HERDR_FORK_READ_ONLY` | Child bridge restriction flag (`1` restricted, `0` normal); select the full mode with `--mode read-only`, not this flag alone |
| `HERDR_FORK_STATE_DIR` | Absolute plugin state root shared by the action and owner bridge; takes precedence over the native override |
| `HERDR_PLUGIN_STATE_DIR` | Herdr's native absolute plugin-state override when no `HERDR_FORK_STATE_DIR` is set |
| `HERDR_FORK_HELPER` | Optional absolute path to an existing regular helper file; default is the adjacent `fork_from_message.py`; this selects executable code, so use only a trusted file |
| `HERDR_FORK_SESSION` | Advanced selected-server pin propagated to the bridge/helper; normal automation should pass `--session <name>` |

Without an override, state uses the XDG state directory under
`herdr/plugins/dmangla3.fork-from-message`. The bridge does not collect arbitrary
process environment variables, credentials, or conversation bodies.

## Experimental native forks

OpenCode and Copilot require an idle source, the matching Herdr integration, and
operator verification that source and child use the same intended configuration,
data, and session roots. Herdr cannot reveal every variable set only inside the
source shell; matching session IDs alone do not prove arbitrary root equality.

From a plugin checkout, an explicit opt-in is:

```sh
python3 scripts/fork_from_message.py \
  --session default --pane PANE_ID --mode fork --allow-experimental
```

- **OpenCode:** uses `opencode mini --session <id> --fork`, preserving a source
  `--standalone` flag. Verify the same server/configuration and storage roots
  before opting in. `OPENCODE_CONFIG_CONTENT` is rejected whenever present,
  including an empty value: inline configuration may contain secrets. Use a
  trusted config file via `OPENCODE_CONFIG`, unset the inline variable in the
  relevant environments, and restart the source rather than relaying its content.
- **Copilot:** uses a private, bounded headless stdio `sessions.fork` operation
  covering all source events, then resumes only the acknowledged child. No parent
  resume or prompt replay is used. The returned ID must be distinct from the
  source, and the destination must report that exact child ID. Native Copilot
  writes a fork-info bookkeeping event to the stored parent; this is not an
  assistant reply in the child and does not mean the source transcript is
  byte-for-byte unchanged.

These paths do not bypass authentication or approve runtime requests. A native
child can remain stored if the fork RPC succeeds but launching its UI fails.

## Safety and limitations

- Source identity and foreground process are rechecked before launch and after
  child verification. Session navigation, a busy source, stale owner records,
  or mismatched child IDs cause a visible failure.
- The plugin never rewrites, truncates, or deletes agent-owned session files. It
  may read at most a bounded header of the exact reported Pi-family file to
  validate an ID; native agent commands read and write their own session data.
- Working files are shared. A normal fork retains native permissions and may
  restore goals or automation and resume inherited work. It is not automatically
  an independent read-only question-answer session.
- Read-only mode exists only for Claude, Codex, GJC, and OMP. It applies native
  tool/runtime restrictions, and GJC pauses inherited goals through its owner API.
  It is not a universal OS sandbox, a separate checkout, or a guarantee against
  all startup hooks, extensions, configuration effects, or provider networking.
- The plugin adds no telemetry or developer callback. Agents can contact their
  normal providers, load configured extensions, or perform normal startup work
  before any new prompt; the plugin does not provide privacy against that behavior.
- Only known safe model/profile/thinking selectors and supported roots are
  preserved. Prompts, credentials, broad permission flags, and arbitrary source
  options are not replayed. Unknown or ambiguous option arity fails closed.
- Codex may delay reporting the new ID until a later prompt. A successful native
  launch can return `started_identity_pending`; this is not a verified distinct
  child ID. Message mode additionally requires successful native picker markers.

See [SECURITY.md](SECURITY.md) for the trust boundaries and vulnerability reporting.

## Troubleshooting and cleanup

Inspect local action errors without publishing conversation contents or credentials:

```sh
herdr plugin log list --plugin dmangla3.fork-from-message --limit 5
```

- **Missing identity:** check the matching integration or fresh owner bridge; do
  not substitute a recent session, log entry, or fresh conversation.
- **Stale owner / GJC navigation:** restart the source with its bridge loaded.
- **Experimental refusal:** verify the matching integration and root provenance,
  then explicitly opt in; do not enable the flag globally.
- **Login, hook review, or permission prompt:** inspect the preserved child. No
  approval is answered automatically.
- **Picker timeout:** inspect the preserved child and agent version; native UI
  markers or controls may have changed.
- **Launch failure:** check the intended CLI is available in Herdr's environment
  and that its roots and configuration are suitable for this invocation.

Cleanup is deliberately conservative: only an empty destination created by this
action is closed after a prelaunch failure. Once launch has been attempted,
including approval waits and picker failures, the destination is preserved for
inspection. Native child records may also remain after a late failure. The plugin
never deletes agent-owned files or closes the original source.

## Development and verification status

Development uses Python's standard-library `unittest`, Bun's built-in test runner,
and Biome. With those developer tools installed:

```sh
git clone https://github.com/dmangla3/herdr-fork-from-message.git
cd herdr-fork-from-message
./scripts/check.sh
herdr plugin link "$PWD"
```

Public-branch local checks passed 107 Python tests, 57 Bun tests, Python compilation,
and Biome. Model-free live checks on Herdr 0.9.5 with Pi 1.3.0 and OmO 5.1.46 used
synthetic histories, isolated agent/session/state roots and the adjacent public
helper. Both produced new IDs with equal inherited entries and unchanged source
bytes measured after source startup. One owned tab was reused, with at most one
child; all 49 pre-existing pane IDs and the selected workspace/tab were preserved.

This was a bounded UI check on an existing Herdr server, not a test of server
creation or a full provider integration. New-provider prompts were not submitted.
Prior Copilot event-durability observations were not rerun on this branch.
OpenCode/Copilot full-panel E2E and GJC's public setup are not revalidated here.
CI results belong to the submitted commit, not every native/provider path.
See [CONTRIBUTING.md](CONTRIBUTING.md) for repeatable isolated QA boundaries.

## Uninstall

Remove only keybinding blocks, session-scoped bridge launch options, or the exact
GJC bridge configuration entry you added; preserve all unrelated entries. After
changing Herdr configuration, validate and reload it:

```sh
herdr config check
herdr server reload-config
```

For a GitHub installation:

```sh
herdr plugin uninstall dmangla3.fork-from-message
```

For a locally linked checkout:

```sh
herdr plugin unlink dmangla3.fork-from-message
```

Uninstalling the plugin does not remove any agent's session data.

## License

[MIT](LICENSE). Original copyright and attribution are retained.
