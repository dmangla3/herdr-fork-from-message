# Contributing

Issues and focused pull requests are welcome. Keep the project name and plugin
ID `dmangla3.fork-from-message`, the original MIT attribution, and the historical
message-picker defaults intact.

## Development setup

Development requires Python 3.10 or newer, Bun, and Biome available on `PATH`.
Python tests use only the standard library; TypeScript tests use Bun's built-in
runner. Biome is a developer checker/formatter, not a runtime dependency. There
are no third-party runtime or test-library packages to install for this project.
Live verification additionally requires Herdr 0.9.5 or newer and only the agent
CLI being exercised.

```sh
git clone https://github.com/dmangla3/herdr-fork-from-message.git
cd herdr-fork-from-message
./scripts/check.sh
```

The full check runs Python tests and compilation, the Bun bridge tests, and Biome
checks. It may print installed Herdr, Codex, and Claude versions; it does not prove
live agent/provider behavior. Focused test commands are:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
bun test tests/runtime_bridge.test.ts
biome check scripts/runtime_bridge.ts tests/runtime_bridge.test.ts
```

For manual plugin development, linking is explicit:

```sh
herdr plugin link "$PWD"
```

Use the bridge from this actual checkout as described in
[README.md](README.md#runtime-bridge). Do not introduce a second installation,
a home-directory helper dependency, or automatically edited global configuration.

## Making changes

1. Add focused coverage for observable behavior, failures, races, and cleanup.
2. Keep exact native forking intact. Never replay the source prompt, resume it as
   a concurrent interactive writer, or rewrite/truncate/delete native sessions.
   A bounded read of the exact reported Pi-family header is permitted; arbitrary
   session scanning is not. Native fork bookkeeping can mutate stored metadata.
3. Preserve `--mode message --destination tab`, the three original action IDs,
   and adaptive `--direction auto`. Current-point and read-only modes remain
   explicit. Experimental agents require per-invocation CLI opt-in.
4. Bind owner metadata to the selected server and exact foreground process.
   Preserve only known selectors and roots; reject uncertain option arity or
   secret-bearing inline configuration instead of guessing or relaying it.
5. Fail before target creation for unavailable agents or unsupported modes. Close
   only empty action-created destinations after prelaunch failures. Preserve a
   destination once launch is attempted, and never delete an agent-owned child to
   undo a late failure.
6. Run `./scripts/check.sh` once over the final change set, and review the diff
   for unrelated work before opening a pull request. Stage explicit paths rather
   than the whole checkout when unrelated changes are present.

Do not add automatic dependency repair, hook/keybinding/service installation,
provider/auth changes, or silent fresh-session fallbacks. Normal native forks
retain native permissions and can resume restored goals or automation; read-only
mode must not claim complete filesystem isolation or universal OS sandboxing.

## Isolated live QA

Live QA is separate from unit/fixture checks. It must never use a user's active
conversation, provider account configuration, or unrelated working tree as a
disposable test resource. Prefer a named disposable Herdr server.

- Create a named disposable Herdr server, temporary checkout, plugin-state root,
  and agent-specific configuration/data/session roots. Do not copy real history,
  auth files, secrets, provider settings, or global extensions into fixtures.
- A bounded UI check may use an existing server only with one explicitly owned
  disposable tab, one source and at most one child. Record IDs before input,
  preserve focus, collect failures before cleanup, and compare all pre-existing
  pane IDs and selected workspace/tab afterward. Never restart or reconfigure
  that server. This is not equivalent to testing server isolation.
- Use synthetic source history and the minimum model-free native operation first.
  Provider-backed checks require an explicitly designated test configuration;
  record them separately from offline checks. Never send a prompt merely to turn
  an unverified child ID into an apparent success.
- Inventory every server/socket, pane, tab, workspace, process, state directory,
  and native session created by the test. Use explicit `--session <test-server>`
  and `--pane <source-id>` selection for automation; never fabricate `HERDR_ENV`
  or `HERDR_SOCKET_PATH` to make a test pass.
- Load the owner bridge from the actual checkout. Verify fresh owner-only records,
  expiry, process/session changes, GJC navigation invalidation, omitted-root
  handling, and the actual OmO launcher even when Herdr reports a Pi hint.
- Verify the exact native command, source process/session before and after,
  independent child history/identity where reported, and that picker keys go
  only to the child after marker detection. For Codex, report
  `started_identity_pending` honestly rather than claiming a verified child ID.
- Exercise missing identity, blocked/unsupported modes, stale registration,
  unexpected source options, root mismatches, experimental refusal, approval
  waits, launch failures, and picker timeouts. A fake adapter is useful unit
  coverage, not proof of a real CLI integration.
- Treat native metadata changes separately from interactive source changes.
  Copilot can append fork-info to the stored parent; check durable child history
  and that the acknowledged child ID matches the launched destination, without
  assuming the parent file is immutable.
- Check every read-only runtime separately. Test a denied write/tool and restored
  automation behavior without touching real files. A tool allowlist, policy text,
  native sandbox, and provider/network boundary are different claims.
- Stop and remove only resources explicitly inventoried as QA-owned. Never close
  unrelated panes, kill a shared service, repair dependencies, rewrite global
  settings, delete real session files, or merge test data into an active setup.
  Preserve a failing started destination for inspection before deliberate teardown.

Current local gates and the bounded Pi/OmO model-free UI checks are recorded in
[README.md](README.md#development-and-verification-status). Earlier Copilot
event-durability observations remain prior evidence. CI and provider checks are
separate results; do not infer them from fixtures or publish a broad compatibility
claim without current evidence.

## Pull requests

Describe the user-visible change, failure case, exact verification performed,
CLI versions, and remaining gaps. Avoid real conversation data, credentials,
private machine paths, or environment-specific provider setup assumptions.
Keep formatting and unrelated refactors out of behavior changes. A conventional
subject such as `fix: handle changed picker marker` is useful.

## Releasing

The published baseline remains `0.3.0`; native-fork additions belong under
**Unreleased** until a maintainer approves a release. Do not invent a released
version or reinterpret historical changelog claims as current verification.

For an approved release:

1. Update `version` in `herdr-plugin.toml` and retain the public plugin ID.
2. Move the relevant Unreleased entries under the approved dated version heading.
3. Run `./scripts/check.sh`, obtain CI results, and record isolated live checks for
   changed supported paths along with any unverified versions or providers.
4. Preserve the original MIT copyright/attribution and review the release diff.
5. Commit, tag `v<version>`, and publish only through the repository's authorized
   release workflow. Opening a pull request does not authorize a merge or release.
