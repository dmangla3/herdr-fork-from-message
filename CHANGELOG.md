# Changelog

All notable changes to this project will be documented in this file. The
project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Explicit current-point native forks with `--mode fork` and the
  `fork-current`, `fork-current-pane`, and `fork-current-workspace` actions.
- Optional restricted `--mode read-only` and the `fork-read-only` action for
  Claude Code, Codex, GJC, and OMP; these restrictions do not create a separate
  checkout or universal OS sandbox.
- Owner-bridge paths for Pi, GJC, OMP, and OmO, with owner-only registrations,
  5-second refresh, 20-second expiry, exact foreground-process checks, and GJC
  navigation invalidation. OmO keeps its actual launcher despite a Pi Herdr hint.
- Explicit `--session <name> --pane <id>` automation, `--no-focus`, and split
  `--direction auto|right|down` controls.
- CLI-only experimental OpenCode and Copilot paths requiring
  `--allow-experimental` and operator verification of configuration/session roots.
  OpenCode retains `--standalone` and rejects inline secret-bearing config;
  Copilot forks all events through bounded private headless `sessions.fork` and
  verifies the acknowledged distinct child ID.
- Public bridge/helper/state controls and developer-only Bun tests and Biome
  checks alongside standard-library Python tests.

### Changed

- Require Herdr 0.9.5 for its foreground-process API; Python remains 3.10 or newer.
- Retain plugin ID `dmangla3.fork-from-message`, version `0.3.0`, original MIT
  attribution, and the original `fork`, `fork-pane`, and `fork-workspace` actions
  with default `--mode message --destination tab` and adaptive splits.
- Recheck source process/session before launch and after child verification;
  preserve native picker-marker verification and send keys only to the child.
- Close only empty action-created destinations after prelaunch failures; preserve
  started or approval-waiting destinations for inspection. Native fork records
  can remain after a late failure, and agent-owned files are never deleted.
- Document shared files, native permissions and restored automation, bounded
  Pi-family header reads without session rewrites, and mutable native source
  bookkeeping such as Copilot's stored-parent fork-info event.
- Keep Cursor and Antigravity blocked without a verified source-preserving fork
  API, and Hermes blocked because native branching omits tool history and storage
  portability is unsafe. Unsupported read-only modes fail before target creation.
- Replace personal setup assumptions with explicit optional configuration,
  portable bridge paths, no reserved shortcuts, and isolated QA boundaries.

### Verification status

- Public-branch local Python/Bun tests, compile and Biome gates passed. Bounded
  Pi/OmO model-free live checks verified inherited entries and independent IDs
  using isolated agent roots and an owned tab; unrelated panes/focus were preserved.
- Copilot durability is prior evidence. Full OpenCode/Copilot panel E2E, GJC public
  setup and live-provider behavior are not revalidated by these checks.

## [0.3.0] - 2026-08-10

### Added

- Native Codex and Claude Code session forking from Herdr's trusted session ID.
- New-tab, split-pane, and new-workspace destinations.
- Automatic opening and verification of each agent's earlier-message picker.
- Visible failures and destination-specific cleanup without transcript mutation.
- A dependency-free test and verification harness.

[Unreleased]: https://github.com/dmangla3/herdr-fork-from-message/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/dmangla3/herdr-fork-from-message/releases/tag/v0.3.0
