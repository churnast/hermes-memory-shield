# Changelog

All notable changes to this project are written down here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.1] (2026-10-09)

### Fixed

- The `pre_tool_call` hook now has a bound on its time and memory use for `terminal` and `execute_code` calls, after a review of the plugin for the Hermes catalog. A call is read up to 16 KB; a longer one is judged without being read: it is refused when it mentions `USER.md`, `MEMORY.md` or the memories folder (with a hint to split it up or keep the memory files out of it) and let through when it mentions none of them. Before, a one-line call of `.hermes.unlink(` repeated 2000 times (30 KB) kept the hook busy for about two minutes, because the memory path was looked for once per `.unlink(` or `.write_text(` in a statement; it is now looked for once per statement.
- Shell variables and Python names assigned a memory path are substituted in one pass, a name whose value refers to another such name is not kept, and the substitution stops once it has added more than 16 KB to the call. Before, about thirty chained lines like `a1 = '.hermes/memories' + a2 + a2` doubled the text at every step until memory ran out.
- The two regular expressions that looked for the memories folder behind the Hermes home no longer rescan the rest of the line for every mention of the home.
- Snapshots, the audit log and a restored memory file are written through a temporary file with a unique name in the same folder (`tempfile.mkstemp`) instead of a fixed `.tmp` neighbour, so two writers can never share one; a file that already exists keeps its permissions.

## [1.1.0] (2026-10-08)

### Added

- A new level for the owner's profile, `owner_edits`, now the default: the agent may add facts wherever it may write and may replace one only when the owner asks in a direct chat, the CLI or a chat in `trusted_chats` (and, once `trusted_users` is set, only for a person listed there); nobody may delete one, and a replace with empty or placeholder text still counts as a delete. In a group, a scheduled job or the background review it behaves like `append_only`. `append_only` stays available for owners who want nothing changed by the agent at all.
- Hermes' unattended self-improvement review, the fork that tidies memory and skills after a conversation with nobody watching, may not add to, change or delete the owner's profile unless `user_profile` is `off`. The plugin reads Hermes' own marker for that fork (`tools.skill_provenance.is_unattended_review`), which reaches `pre_tool_call` hooks under the default `plugins.isolation: in_process`; a review the owner starts with `/refine` is attended and keeps the direct-chat rules. The agent's own notes keep their level there, where Hermes itself holds deletions for approval.

### Changed

- `trusted_users` now applies to direct chats too: once it is set, a direct chat with someone not listed is judged like a shared chat (read-only by default, `group_chats` decides), and `mode: approve` blocks there instead of asking. A session without a user id, such as the CLI, still counts as the owner. With `trusted_users` empty nothing changes. `/memory-shield` and `/memory-shield whoami` say so.
- The policy hints name the new rules: a replace outside the owner's direct chat says that facts about the owner are changed only when the owner asks in a direct chat; a refused delete of a fact about the owner says that facts are never deleted and that placeholder text counts as deleting.

### Fixed

- Shell and Python side-door detection no longer refuses commands that only read a memory file or write somewhere else. Before, any command that mentioned a memory file together with any writing word was refused, so `cat ~/.hermes/memories/USER.md > /tmp/copy.md`, `cp ~/.hermes/memories/USER.md /tmp/x` and `grep ... USER.md > elsewhere` were blocked. Now the plugin reads the call command by command and counts only a write whose target is `USER.md`, `MEMORY.md` or the memory folder: a redirection, `tee`, `cp`, `install`, `rsync`, `ln` or `dd of=` into it, `sed -i` or `perl -i` on it, `rm`, `mv`, `truncate`, `shred` or `find -delete` of it, and in Python `open(..., "w"/"a"/"x"/"+")`, `write_text`, `write_bytes`, `unlink`, `os.remove`, `shutil.move`, `shutil.rmtree` and `shutil.copy` with it as the destination. Simple shell variables, `cd` into the memory folder and Python names assigned a memory path in the same call are followed, and quoted strings are read as shell text once, for `python -c`, `subprocess` and `os.system`.

## [1.0.1] (2026-10-08)

### Fixed

- `/memory-shield log` follows the same rule as `/memory-shield restore`: it answers only in a direct chat, the CLI or a chat in `trusted_chats`, and when `trusted_users` is set, only the people listed there; a session without a user id, such as the CLI, counts as the owner. Before, anyone who could message the agent directly could read up to 50 events from every chat, with excerpts from the owner's own chats (for a deletion, words from the memory entry itself), even with `trusted_users` set. Someone not listed there gets a refusal that says where the command works and who may run it, and `restore` now gives the same one. `/memory-shield snapshots` is unchanged: it shows times, stores and sizes, no memory text.

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

[Unreleased]: https://github.com/churnast/hermes-memory-shield/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/churnast/hermes-memory-shield/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/churnast/hermes-memory-shield/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/churnast/hermes-memory-shield/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/churnast/hermes-memory-shield/releases/tag/v1.0.0
[0.1.4]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/churnast/hermes-memory-shield/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/churnast/hermes-memory-shield/releases/tag/v0.1.0
