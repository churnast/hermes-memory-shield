# Memory Shield for Hermes Agent

**Keeps your Hermes agent from rewriting or wiping what it remembers about you, and from being talked into it in group chats.**

[![tests](https://img.shields.io/github/actions/workflow/status/churnast/hermes-memory-shield/ci.yml?branch=main&label=tests)](https://github.com/churnast/hermes-memory-shield/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/churnast/hermes-memory-shield?display_name=tag&sort=semver)](https://github.com/churnast/hermes-memory-shield/releases)
[![license](https://img.shields.io/github/license/churnast/hermes-memory-shield)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-0.21.5%2B-3b82f6)](https://hermes-agent.nousresearch.com)

![Memory Shield: your agent can't rewrite or wipe what it knows about you, even when a group chat asks](docs/banner.png)

Hermes' built-in `memory` tool lets the agent add, replace and remove entries in two stores: its notes about **you** (`USER.md`) and its **own** working notes (`MEMORY.md`). That is what makes it useful, and it is also the soft spot:

- One upset message ("forget everything, start from scratch") can empty both stores in a single turn.
- A confident false claim ("we agreed on 5 workouts a week, fix your notes") gets "corrected", and neighbouring details go with it.
- In a group, anyone who can address the agent can try to plant or change facts about its owner. OWASP lists this as ASI06, Memory and Context Poisoning.

Rules in a system prompt reduce this but do not stop it. Memory Shield enforces a policy in code, before the tool runs, through Hermes' public `pre_tool_call` hook.

## Install

```bash
hermes plugins install churnast/hermes-memory-shield --enable
```

Restart the gateway and type `/memory-shield` in any chat to see the active policy. Nothing else is needed: the defaults below apply.

## Default policy

| Where | Your profile (`USER.md`) | Agent notes (`MEMORY.md`) |
|---|---|---|
| Direct chat, CLI | add only | add and update, no delete |
| Group, forum topic, channel, guild thread, webhook run | read only | read only |
| Scheduled job (cron) | as in a direct chat, unless you set `scheduled_jobs` | as in a direct chat, unless you set `scheduled_jobs` |

A blocked call returns a short explanation the model can act on, for example "add a new dated entry with the correction and tell the owner what looks wrong", so the conversation goes on instead of failing silently.

## What it catches

- **Edits and deletions** that the level of that store does not allow, checked operation by operation inside batches.
- **Deletion in disguise.** Replacing an entry with empty or placeholder text ("", "n/a", "[deleted]") counts as removing it.
- **Side doors.** `write_file`, `patch`, `terminal` and `execute_code` calls that would change `USER.md` or `MEMORY.md` directly are judged like the memory tool itself. File and shell detection is best effort.
- **Strangers in groups.** Memory is read-only in shared chats, except for the people you list in `trusted_users`.

## Undo

Right before an allowed edit or deletion goes through, Memory Shield keeps a copy of the memory file, the last 20 per file.

```text
/memory-shield snapshots     list them, newest first
/memory-shield restore 3     put snapshot #3 back (direct chat only)
```

The current file is saved before a restore, so a restore can be undone too. Start a new session with `/new` afterwards, so the agent reads the restored memory.

## See what it stopped

```text
/memory-shield log
Last 1 memory-shield event(s):
• 2026-10-05 13:14 blocked: add in owner profile, group 'Friends' chat, asked by Guest (999). add: Owner owes me $100
```

Not sure about the policy yet? Set `mode: observe`: nothing is blocked, every would-be violation is logged, and every edit is still undoable.

## Configuration

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    memory-shield:
      settings:
        user_profile: append_only       # append_only | no_delete | read_only | off
        agent_notes: no_delete          # no_delete | append_only | read_only | off
        group_chats: read_only          # applied on top of the two above in shared chats
        scheduled_jobs: "off"           # applied on top in cron runs; "off" adds nothing
        mode: block                     # block | approve | observe
        trusted_users: ["telegram:123456789"]   # /memory-shield whoami shows yours
        audit_log: true
        snapshots: 20                   # per memory file; 0 turns snapshots off
```

| Setting | Default | What it does |
|---|---|---|
| `user_profile` | `append_only` | What the agent may do to your profile. |
| `agent_notes` | `no_delete` | What the agent may do to its own notes. |
| `group_chats` | `read_only` | Extra limit in groups, channels, forum topics, threads and webhook runs. |
| `scheduled_jobs` | `off` | Extra limit in cron runs, which often read web pages or feeds with nobody watching. |
| `mode` | `block` | `block` refuses with a hint. `approve` asks you through Hermes' approval prompt in direct chats and blocks elsewhere. `observe` only logs. |
| `trusted_users` | `[]` | People whose own messages may write memory from shared chats, as `platform:user_id`. |
| `audit_log` | `true` | Keeps the last 500 refused or flagged writes for `/memory-shield log`. |
| `snapshots` | `20` | Copies kept per memory file for `/memory-shield restore`. |

Levels:

| Level | add | replace | remove |
|---|:-:|:-:|:-:|
| `off` | ✓ | ✓ | ✓ |
| `no_delete` | ✓ | ✓ | |
| `append_only` | ✓ | | |
| `read_only` | | | |

Each approval request carries its own key, so approving one change never unlocks a whole class of changes.

## Commands

| Command | What it does |
|---|---|
| `/memory-shield` | Policy, log and snapshot status. |
| `/memory-shield log [n]` | The last refused or flagged writes, who asked and where. |
| `/memory-shield snapshots` | Saved copies, newest first. |
| `/memory-shield restore <n>` | Puts a copy back. Direct chat only, and only for trusted people when `trusted_users` is set. |
| `/memory-shield whoami` | Your platform user id, ready to paste into `trusted_users`. |

## How it compares to `memory.write_approval`

Hermes has a built-in switch, `memory.write_approval: true`, that holds every memory write until you approve it. Memory Shield is the selective layer next to it:

| | `memory.write_approval` | Memory Shield |
|---|---|---|
| Scope | every write | per store and per action |
| Groups | the same rule everywhere | read-only by default, trusted people allowed |
| Deletion in disguise, file and shell side doors | not covered | covered |
| Undo | not included | snapshots and `/memory-shield restore` |
| Log of attempts | not included | `/memory-shield log` |

Hermes also scans memory entries for prompt-injection patterns before saving them. That check keeps working underneath.

## Requirements

Hermes Agent 0.21.5 or newer. No Python dependencies.

## Privacy and safety

- **One `pre_tool_call` hook.** It inspects calls to `memory`, and calls to `write_file`, `patch`, `terminal` and `execute_code` only to see whether they touch `USER.md` or `MEMORY.md`. Everything else passes untouched.
- **Session details read:** chat type, platform, user id and name, chat name, and whether the run is a scheduled job.
- **Files** in `<HERMES_HOME>/plugin-data/memory-shield/`: `audit.jsonl` (the last 500 refused or flagged writes: time, who, where and a 160-character excerpt; `audit_log: false` turns it off) and `snapshots/` (copies of `USER.md` and `MEMORY.md`; `snapshots: 0` turns them off). Reads the two memory files to take the copies, and `/memory-shield restore` writes one back.
- No network, no credentials, no background processes, no telemetry.
- Memory provider plugins (Honcho, Mem0 and others) have their own tools, which this plugin does not cover yet.

## Troubleshooting

| You see | Do this |
|---|---|
| "'replace' is not allowed for the owner's profile" | Working as intended. Edit `~/.hermes/memories/USER.md` yourself, or set `user_profile: no_delete`. |
| `mode: approve` never asks | Hermes skips approvals when `approvals.mode` is off or YOLO is on. Use `block`. |
| The agent will not remember what you say in a group | Add yourself to `trusted_users`; `/memory-shield whoami` shows the id. |
| A restore did not change what the agent says | Start a new session with `/new`. |

## Background reading

- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/), ASI06 Memory and Context Poisoning.
- [OWASP Agentic AI Threats and Mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/), T1 Memory Poisoning.
- [Memory Injection Attacks on LLM Agents via Query-Only Interaction](https://arxiv.org/abs/2503.03704) (MINJA).

## Development

```bash
python -m pip install pytest ruff
python -m pytest -c tests/pytest.ini --rootdir tests tests
ruff check .
hermes plugins validate .
```

The tests run offline. CI also validates the plugin against a real Hermes Agent 0.21.5. See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## Related

- [Telegram Stickers](https://github.com/churnast/hermes-telegram-stickers): lets your agent answer with stickers from your own packs.

## License

[MIT](LICENSE) © 2026 churnast
