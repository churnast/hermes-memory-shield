# Changelog

All notable changes to this project are written down here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] (2026-10-05)

### Fixed

- Placeholder words in Russian and a few other languages ("удалено", "нет", "неактуально", "gelöscht") now count
  as deleting an entry, like "deleted" and "n/a" already did. Found while testing with a Russian-speaking agent.

## [0.1.1] (2026-10-05)

### Fixed

- Shell commands that wipe or move the whole memory folder without naming a file (`rm -rf ~/.hermes/memories`,
  `find ... -delete`, `shutil.rmtree`) are now caught like direct edits of `USER.md` and `MEMORY.md`.

## [0.1.0] (2026-10-05)

First public release.

### Added

- Policy for the built-in `memory` tool through a `pre_tool_call` hook: owner profile append-only, agent notes without deletion, shared chats read-only; levels `off`, `no_delete`, `append_only`, `read_only`.
- Modes `block` (refuse with a hint the model can act on), `approve` (Hermes' approval prompt, direct chats only) and `observe` (log only).
- Replacing an entry with empty or placeholder text counts as removing it.
- Direct edits of `USER.md` and `MEMORY.md` through `write_file`, `patch`, `terminal` and `execute_code` are judged like the memory tool.
- `trusted_users` who may write memory from shared chats, and a `scheduled_jobs` level for cron runs.
- Snapshots of the memory file before every allowed edit or deletion, with `/memory-shield restore`.
- Audit log of refused and flagged writes, shown by `/memory-shield log`; `/memory-shield whoami`.

[Unreleased]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/churnast/hermes-memory-shield/releases/tag/v0.1.0
