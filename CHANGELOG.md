# Changelog

All notable changes to this project are written down here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] (2026-10-08)

The first stable release. What the plugin does as of this release is listed under Added and the two fixes since 0.1.4 under Fixed; the README, its banner, a catalog card and a short demo video are new.

### Added

- A policy for Hermes' built-in `memory` tool through one `pre_tool_call` hook, per store: the owner's profile (`USER.md`, setting `user_profile`) is append-only, and the agent's own notes (`MEMORY.md`, setting `agent_notes`) can be added to and updated but not deleted. Levels `off`, `no_delete`, `append_only` and `read_only`; a batch is checked operation by operation.
- Shared chats (groups, channels, forum topics, guild threads, webhook runs) are read-only (`group_chats`), except for the people in `trusted_users` (ids as `platform:id`) and the owner-only chats in `trusted_chats`, which are judged like a direct chat. `scheduled_jobs` adds an extra limit in cron runs; it is off by default.
- Modes: `block` refuses with a hint the model can act on, `approve` asks through Hermes' approval prompt in direct chats (with a key for that exact call) and blocks elsewhere, `observe` allows the call and logs it.
- Replacing an entry with empty or placeholder text ("n/a", "[deleted]", and placeholder words in Russian, Spanish, Portuguese, German and French) counts as removing it.
- Direct edits of `USER.md` and `MEMORY.md` through `write_file`, `patch`, `terminal` and `execute_code`, and shell commands that wipe or move the whole memory folder, are judged as an add, a replace and a remove at once.
- A snapshot of the memory file right before an edit or deletion goes through: 20 per file by default, at most 200, `snapshots: 0` turns them off. `/memory-shield snapshots` lists them, and `/memory-shield restore <n>` puts one back in a direct chat or a chat in `trusted_chats`, after saving the current file.
- An audit log of refused and flagged writes (the last 500, each with a 160-character excerpt; `audit_log: false` turns it off), shown by `/memory-shield log [n]` in a direct chat, the CLI or a chat in `trusted_chats`. `/memory-shield` shows the policy and how many events and snapshots are kept; `/memory-shield whoami` shows the user id and, in a shared chat, the chat key.
- A card for the Hermes plugin catalog, `docs/card.png` (1200x600): in a group, a stranger asks the agent to forget everything about its owner, the agent's notes stay, and the shield sits on the chat. `docs/banner.png` stays the README banner.
- The README shows a short demo (`docs/demo.webp`, also as `docs/demo.mp4`, 20 seconds), re-drawn from a local test run with fictional people, in two scenes: in the group "Friends", Sam asks the agent to remember "Alex owes me $100" and it refuses; in a direct chat, the owner, Alex, asks it to forget everything, and it refuses and offers a dated note. Under What you get, five screenshots from `docs/screenshots`.

### Changed

- The README follows the layout of Telegram Stickers: badges in one style, a one-line introduction to Hermes Agent, a Quick start (install and restart, `/memory-shield` to check, `trusted_users` for groups), What you get, the commands, Configuration, the comparison with `memory.write_approval`, Privacy and safety, Known limitations and Troubleshooting as a list. Known limitations is new: groups where the gateway does not pass the message author to plugins (`trusted_chats` covers an owner-only one), memory provider plugins, best-effort file and shell detection, facts about the owner that only the owner can fix by default, and `approve` depending on Hermes' approvals. Privacy and safety now also say who can run which subcommand and that the plugin holds the memory store's lock file while it reads or writes a memory file.
- The README banner is redrawn as a mini chat, like the Telegram Stickers banner: in a group, Sam tries to plant a claim about the owner, Alex, then to wipe what the agent knows, and the agent's notes stay. It drops its pills and uses larger text so it reads in small link previews.

### Fixed

- `/memory-shield log` answers only in a direct chat, the CLI or a chat in `trusted_chats`, like `/memory-shield restore`, and in any other chat says where to run it. Before, it worked in any chat, so whoever could run the agent's slash commands in a group could read up to 50 events from every chat, with excerpts from the owner's direct chats (for a deletion, words from the memory entry itself). `/memory-shield snapshots` still works in any chat: it shows times, stores and sizes, no memory text.
- A level set to `off` with `hermes config set` or as an unquoted `off` in `config.yaml` is stored as YAML `false`; the plugin now reads that as `off` instead of silently keeping the default. This applies to `user_profile`, `agent_notes`, `group_chats` and `scheduled_jobs`.

## [0.1.4] (2026-10-05)

### Fixed

- `scheduled_jobs` is listed in the plugin's settings, so Hermes Desktop shows it in the settings form.
- The `trusted_users` hint asks for ids as `platform:id`, like the README and `/memory-shield whoami`.

### Added

- README: how to update, switch off and remove the plugin.

## [0.1.3] (2026-10-05)

### Added

- `trusted_chats`: chats that only the owner and the agent are in, such as a private Telegram group with forum
  topics, are judged like a direct chat, and `/memory-shield restore` works there. In Telegram groups that Hermes
  observes, the sender is not passed to plugins, so `trusted_users` could not match there and the owner's own
  memory writes were blocked like a stranger's. `/memory-shield whoami` shows the key of the current chat.

### Fixed

- `platform:id` entries in `trusted_users` now match ids that contain a colon themselves, such as Matrix ids.

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

[Unreleased]: https://github.com/churnast/hermes-memory-shield/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/churnast/hermes-memory-shield/releases/tag/v1.0.0
[0.1.4]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/churnast/hermes-memory-shield/releases/tag/v0.1.0
