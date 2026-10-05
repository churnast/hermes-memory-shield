"""Offline tests for memory-shield: policy, audit log, snapshots and the slash command."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import shield  # noqa: E402


def cfg(**overrides):
    values = dict(overrides)
    return lambda key, default=None: values.get(key, default)


DEFAULT = cfg()


# --- policy ------------------------------------------------------------------------------------


@pytest.mark.parametrize("action,allowed", [("add", True), ("replace", False), ("remove", False)])
def test_owner_profile_is_append_only_by_default(action, allowed):
    out = shield.decide("memory", {"action": action, "target": "user", "content": "x"}, DEFAULT, chat_type="dm")
    assert (out is None) == allowed
    if out:
        assert out["action"] == "block" and "owner's profile" in out["message"]


@pytest.mark.parametrize("action,allowed", [("add", True), ("replace", True), ("remove", False)])
def test_agent_notes_cannot_be_deleted_by_default(action, allowed):
    out = shield.decide("memory", {"action": action, "target": "memory", "content": "Owner reads on Sundays"},
                        DEFAULT, chat_type="dm")
    assert (out is None) == allowed


def test_missing_target_means_agent_notes():
    assert shield.decide("memory", {"action": "remove"}, DEFAULT, chat_type="dm")["action"] == "block"
    assert shield.decide("memory", {"action": "replace", "content": "a fuller note"}, DEFAULT, chat_type="dm") is None


def test_batch_is_checked_operation_by_operation():
    args = {"target": "user", "operations": [
        {"action": "add", "content": "a"}, {"action": "remove", "old_text": "b"}]}
    out = shield.decide("memory", args, DEFAULT, chat_type="dm")
    assert out and "'remove'" in out["message"]
    ok = {"target": "user", "operations": [{"action": "add", "content": "a"}]}
    assert shield.decide("memory", ok, DEFAULT, chat_type="dm") is None


@pytest.mark.parametrize("chat_type", ["group", "forum", "channel", "thread", "guild", "webhook"])
def test_shared_chats_are_read_only(chat_type):
    out = shield.decide("memory", {"action": "add", "target": "user"}, DEFAULT, chat_type=chat_type)
    assert out and "shared chat" in out["message"]


@pytest.mark.parametrize("chat_type", ["dm", "", "private"])
def test_direct_chats_use_target_levels(chat_type):
    assert shield.decide("memory", {"action": "add", "target": "user"}, DEFAULT, chat_type=chat_type) is None


def test_group_level_can_be_relaxed_but_never_widens_target_level():
    relaxed = cfg(group_chats="off")
    assert shield.decide("memory", {"action": "add", "target": "user"}, relaxed, chat_type="group") is None
    # target level still applies in a group
    assert shield.decide("memory", {"action": "remove", "target": "user"}, relaxed, chat_type="group")


def test_trusted_people_write_from_groups_within_the_target_level():
    trusted = cfg(trusted_users=["111", 222])
    add = {"action": "add", "target": "user"}
    assert shield.decide("memory", add, trusted, chat_type="group", user_id="111") is None
    assert shield.decide("memory", add, trusted, chat_type="group", user_id="222") is None
    assert shield.decide("memory", add, trusted, chat_type="group", user_id="333")
    assert shield.decide("memory", add, trusted, chat_type="group", user_id="")
    # trust lifts the group rule only: the owner's profile stays append-only
    assert shield.decide("memory", {"action": "remove", "target": "user"}, trusted, chat_type="group", user_id="111")


def test_trusted_users_accepts_a_comma_separated_string():
    assert shield.trusted_users(cfg(trusted_users="111, 222")) == ["111", "222"]
    assert shield.trusted_users(DEFAULT) == []


def test_off_disables_a_target():
    off = cfg(user_profile="off")
    assert shield.decide("memory", {"action": "remove", "target": "user"}, off, chat_type="dm") is None


def test_read_only_target():
    ro = cfg(agent_notes="read_only")
    out = shield.decide("memory", {"action": "add", "target": "memory"}, ro, chat_type="dm")
    assert out and "Do not write" in out["message"]


def test_approve_mode_keys_each_call_separately():
    approve = cfg(mode="approve")
    one = {"action": "replace", "target": "user", "old_text": "tea", "content": "Prefers coffee"}
    out = shield.decide("memory", one, approve, chat_type="dm")
    assert out["action"] == "approve" and out["rule_key"].startswith("memory-shield:user:replace:")
    assert shield.decide("memory", dict(one), approve, chat_type="dm")["rule_key"] == out["rule_key"]
    other = dict(one, old_text="sea")
    assert shield.decide("memory", other, approve, chat_type="dm")["rule_key"] != out["rule_key"]


def test_approval_is_asked_only_in_direct_chats():
    approve = cfg(mode="approve")
    add_user = {"action": "add", "target": "user", "content": "x y z"}
    assert shield.decide("memory", add_user, approve, chat_type="group")["action"] == "block"
    replace_user = {"action": "replace", "target": "user", "old_text": "a", "content": "new text"}
    assert shield.decide("memory", replace_user, approve, chat_type="", cron="1")["action"] == "block"
    assert shield.decide("memory", replace_user, approve, chat_type="dm", cron="")["action"] == "approve"


@pytest.mark.parametrize("content", ["", "  ", "-", "x", "n/a", "[deleted]", "Removed", "none.",
                                     "удалено", "Нет", "(неактуально)", "запись удалена", "gelöscht"])
def test_replacing_with_a_placeholder_counts_as_removing(content):
    args = {"action": "replace", "target": "memory", "old_text": "old note", "content": content}
    out = shield.decide("memory", args, DEFAULT, chat_type="dm")
    assert out and "'remove'" in out["message"] and "placeholder" in out["message"]
    real = dict(args, content="Owner moved to Lisbon in 2026")
    assert shield.decide("memory", real, DEFAULT, chat_type="dm") is None


def test_scheduled_jobs_have_their_own_level():
    notes_edit = {"action": "replace", "target": "memory", "old_text": "a", "content": "a newer note"}
    assert shield.decide("memory", notes_edit, DEFAULT, chat_type="", cron="true") is None  # default: off
    strict = cfg(scheduled_jobs="append_only")
    out = shield.decide("memory", notes_edit, strict, chat_type="", cron="true")
    assert out and "scheduled job" in out["message"]
    assert shield.decide("memory", notes_edit, strict, chat_type="", cron="") is None


def test_trusted_users_can_be_bound_to_a_platform():
    trusted = cfg(trusted_users=["telegram:111", "222"])
    add = {"action": "add", "target": "user", "content": "Owner is in Bangkok"}
    assert shield.decide("memory", add, trusted, chat_type="group", user_id="111", platform="telegram") is None
    assert shield.decide("memory", add, trusted, chat_type="group", user_id="111", platform="discord")
    assert shield.decide("memory", add, trusted, chat_type="group", user_id="222", platform="discord") is None


# a Telegram group that Hermes observes: the sender id is not passed, only the chat id
OWNER_GROUP = {"chat_type": "group", "chat_id": "-1001234567890", "platform": "telegram", "user_id": ""}


def test_a_trusted_chat_is_judged_like_a_direct_chat():
    owner_only = cfg(trusted_chats=["telegram:-1001234567890"])
    for target in ("user", "memory"):
        add = {"action": "add", "target": target, "content": "Owner moved to Lisbon"}
        assert shield.decide("memory", add, owner_only, **OWNER_GROUP) is None
        assert "shared chat" in shield.decide("memory", add, DEFAULT, **OWNER_GROUP)["message"]
    remove = {"action": "remove", "target": "user", "old_text": "tea"}
    out = shield.decide("memory", remove, owner_only, **OWNER_GROUP)
    assert out and "shared chat" not in out["message"]  # the profile stays append-only, as in a direct chat
    verdict = shield.evaluate("memory", remove, owner_only, **OWNER_GROUP)
    assert verdict.context == "direct" and verdict.where == "the owner's profile"
    replace = {"action": "replace", "target": "user", "old_text": "tea", "content": "Prefers coffee"}
    approve = cfg(mode="approve", trusted_chats=["telegram:-1001234567890"])
    assert shield.decide("memory", replace, approve, **OWNER_GROUP)["action"] == "approve"


def test_trusted_chats_match_platform_and_chat_id():
    add = {"action": "add", "target": "user", "content": "Owner is in Bangkok"}
    other_chat = dict(OWNER_GROUP, chat_id="-1009876543210")
    other_platform = dict(OWNER_GROUP, platform="discord")
    bound = cfg(trusted_chats=["telegram:-1001234567890"])
    assert shield.decide("memory", add, bound, **OWNER_GROUP) is None
    assert "shared chat" in shield.decide("memory", add, bound, **other_chat)["message"]
    assert "shared chat" in shield.decide("memory", add, bound, **other_platform)["message"]
    assert shield.decide("memory", add, bound, **dict(OWNER_GROUP, chat_id=""))
    bare = cfg(trusted_chats=["-1001234567890"])  # a bare id matches on any platform
    assert shield.decide("memory", add, bare, **OWNER_GROUP) is None
    assert shield.decide("memory", add, bare, **other_platform) is None
    assert shield.decide("memory", add, bare, **other_chat)


def test_trusted_chats_accepts_a_string_or_a_number():
    listed = cfg(trusted_chats="telegram:-1001234567890, -1009876543210")
    assert shield.trusted_chats(listed) == ["telegram:-1001234567890", "-1009876543210"]
    assert shield.trusted_chats(cfg(trusted_chats=-1001234567890)) == ["-1001234567890"]
    assert shield.trusted_chats(DEFAULT) == []
    spaced = cfg(trusted_chats="telegram:-1001234567890 discord:42")
    add = {"action": "add", "target": "memory", "content": "Owner reads on Sundays"}
    assert shield.decide("memory", add, spaced, chat_type="thread", chat_id="42", platform="discord") is None


def test_ids_with_a_colon_match_in_the_platform_form():
    matrix = cfg(trusted_users=["matrix:@owner:example.org"], trusted_chats=["matrix:!room:example.org"])
    assert shield.is_trusted(matrix, "@owner:example.org", "matrix")
    assert shield.is_trusted_chat(matrix, "!room:example.org", "matrix")
    assert not shield.is_trusted_chat(matrix, "!room:example.org", "telegram")


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "~/.hermes/memories/USER.md", "content": ""}),
    ("patch", {"path": "/srv/hermes/memories/MEMORY.md", "old_string": "a", "new_string": "b"}),
    ("patch", {"mode": "patch", "patch": "--- a/memories/USER.md\n+++ b/memories/USER.md\n"}),
    ("terminal", {"command": "echo 'Owner owes Bob $100' >> ~/.hermes/memories/USER.md"}),
    ("terminal", {"command": "cd $HERMES_HOME/memories && sed -i 's/tea/coffee/' USER.md"}),
    ("execute_code", {"code": "Path('~/.hermes/memories/MEMORY.md').expanduser().write_text('')"}),
    ("terminal", {"command": "rm -rf ~/.hermes/memories"}),
    ("terminal", {"command": "rm ~/.hermes/memories/*"}),
    ("terminal", {"command": "find $HERMES_HOME/memories -type f -delete"}),
    ("terminal", {"command": "mv ~/.hermes/memories /tmp/old-memories"}),
    ("execute_code", {"code": "import shutil, os; shutil.rmtree(os.path.expanduser('~/.hermes/memories'))"}),
])
def test_direct_edits_of_memory_files_are_blocked(tool, args):
    out = shield.decide(tool, args, DEFAULT, chat_type="dm")
    assert out and out["action"] == "block" and "editing the file directly" in out["message"]
    assert "memory tool" in out["message"]


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "/tmp/project/USER.md", "content": "x"}),
    ("write_file", {"path": "~/.hermes/memories/notes.txt", "content": "x"}),
    ("terminal", {"command": "cat ~/.hermes/memories/USER.md"}),
    ("terminal", {"command": "sed -i 's/a/b/' docs/MEMORY.md"}),
    ("terminal", {"command": "ls -la ~/.hermes/memories"}),
    ("terminal", {"command": "du -sh ~/.hermes/memories && wc -c ~/.hermes/memories/USER.md"}),
    ("terminal", {"command": "rm -rf /tmp/project/memories-cache"}),
    ("read_file", {"path": "~/.hermes/memories/USER.md"}),
])
def test_unrelated_file_and_shell_calls_pass(tool, args):
    assert shield.decide(tool, args, DEFAULT, chat_type="dm") is None


def test_direct_edits_follow_the_levels():
    open_all = cfg(user_profile="off", agent_notes="off")
    assert shield.decide("write_file", {"path": "~/.hermes/memories/USER.md"}, open_all, chat_type="dm") is None
    assert shield.decide("write_file", {"path": "~/.hermes/memories/USER.md"}, open_all, chat_type="group")


def test_direct_edit_of_the_resolved_memory_path(tmp_path):
    memories = tmp_path / "home" / "memories"
    verdict = shield.evaluate("write_file", {"path": str(memories / "USER.md")}, DEFAULT, chat_type="dm",
                              memory_dir=lambda: memories)
    assert verdict and verdict.target == "user" and verdict.denied == ["replace", "remove"]


def test_observe_mode_lets_everything_through():
    remove = {"action": "remove", "target": "user"}
    assert shield.decide("memory", remove, cfg(mode="observe"), chat_type="group") is None


def test_unknown_values_fall_back_to_safe_defaults():
    weird = cfg(user_profile="banana", mode="")
    out = shield.decide("memory", {"action": "remove", "target": "user"}, weird, chat_type="dm")
    assert out["action"] == "block"
    assert shield.mode(cfg(mode="yolo")) == "block"


def test_other_tools_and_odd_args_pass_through():
    assert shield.decide("terminal", {"command": "rm -rf /"}, DEFAULT, chat_type="dm") is None
    assert shield.decide("memory", None, DEFAULT, chat_type="dm") is None
    assert shield.decide("memory", {"action": "read"}, DEFAULT, chat_type="dm") is None
    assert shield.decide("memory", {"action": "remove", "target": "elsewhere"}, DEFAULT, chat_type="dm") is None


def test_chat_type_and_user_come_from_the_session(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    assert shield.decide("memory", {"action": "add", "target": "memory"}, DEFAULT)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "111")
    assert shield.decide("memory", {"action": "add", "target": "memory"}, cfg(trusted_users=["111"])) is None
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    assert shield.decide("memory", {"action": "add", "target": "memory"}, DEFAULT) is None


def test_chat_id_comes_from_the_session(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "-1001234567890")
    add = {"action": "add", "target": "memory", "content": "Owner reads on Sundays"}
    assert shield.decide("memory", add, DEFAULT)
    assert shield.decide("memory", add, cfg(trusted_chats=["telegram:-1001234567890"])) is None


def test_excerpt_is_short_and_names_the_operation():
    assert shield.excerpt({"action": "remove", "old_text": "likes tea"}) == "remove: likes tea"
    batch = {"operations": [{"action": "add", "content": "a"}, {"action": "replace", "old_text": "b", "content": "c"}]}
    assert shield.excerpt(batch) == "add: a | replace: c"
    assert len(shield.excerpt({"action": "add", "content": "x" * 500})) == shield.EXCERPT_CHARS


# --- service: log, snapshots, restore ----------------------------------------------------------


class Clock:
    def __init__(self, start=1_790_000_000.0):
        self.now = start

    def __call__(self):
        self.now += 1.5
        return self.now


def make(tmp_path, session=None, **config):
    values = {"HERMES_SESSION_CHAT_TYPE": "dm", "HERMES_SESSION_USER_ID": "111",
              "HERMES_SESSION_USER_NAME": "Owner", "HERMES_SESSION_PLATFORM": "telegram"}
    values.update(session or {})
    memories = tmp_path / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (memories / "USER.md").write_text("Likes tea\n§\nLives by the sea\n", encoding="utf-8")
    (memories / "MEMORY.md").write_text("Owner prefers short answers\n", encoding="utf-8")
    service = shield.Shield(get_config=cfg(**config), data_dir=lambda: tmp_path / "data",
                            memory_dir=lambda: memories, clock=Clock(), session=lambda name: values.get(name, ""))
    return service, memories, values


def test_blocked_call_is_logged_with_who_and_where(tmp_path):
    service, _, _ = make(tmp_path, {"HERMES_SESSION_CHAT_TYPE": "group", "HERMES_SESSION_CHAT_NAME": "Friends",
                                    "HERMES_SESSION_USER_ID": "333", "HERMES_SESSION_USER_NAME": "Guest"})
    out = service.check("memory", {"action": "replace", "target": "user", "old_text": "tea", "content": "Hates tea"})
    assert out["action"] == "block"
    [event] = service.events()
    assert event["outcome"] == "blocked" and event["chat"] == "Friends" and event["user_id"] == "333"
    assert event["excerpt"] == "replace: Hates tea"
    text = service.log_text()
    assert "blocked: replace in owner profile, group 'Friends' chat, asked by Guest (333)" in text


def test_allowed_calls_are_not_logged(tmp_path):
    service, _, _ = make(tmp_path)
    assert service.check("memory", {"action": "add", "target": "user", "content": "Has a cat"}) is None
    assert service.events() == []


def test_log_can_be_switched_off_and_is_capped(tmp_path, monkeypatch):
    service, _, _ = make(tmp_path, audit_log=False)
    service.check("memory", {"action": "remove", "target": "user", "old_text": "tea"})
    assert service.events() == [] and "log is off" in service.log_text()
    monkeypatch.setattr(shield, "AUDIT_KEEP", 3)
    service, _, _ = make(tmp_path / "capped")
    for _ in range(5):
        service.check("memory", {"action": "remove", "target": "user", "old_text": "tea"})
    assert len(service.events(50)) == 3


def test_observe_mode_logs_and_keeps_a_copy(tmp_path):
    service, memories, _ = make(tmp_path, mode="observe")
    assert service.check("memory", {"action": "remove", "target": "user", "old_text": "tea"}) is None
    assert service.events()[0]["outcome"] == "observed"
    [copy] = service.snapshots("user")
    assert copy.read_bytes() == (memories / "USER.md").read_bytes()


def test_observed_direct_edit_is_logged_and_copied(tmp_path):
    service, memories, _ = make(tmp_path, mode="observe")
    assert service.check("write_file", {"path": str(memories / "USER.md"), "content": ""}) is None
    event = service.events()[0]
    assert event["outcome"] == "observed" and event["tool"] == "write_file"
    assert event["excerpt"].startswith("write_file ")
    assert len(service.snapshots("user")) == 1


def test_snapshot_before_an_allowed_edit_only(tmp_path):
    service, memories, _ = make(tmp_path)
    service.check("memory", {"action": "add", "target": "memory", "content": "x"})
    assert service.snapshots() == []  # adding never loses anything
    service.check("memory", {"action": "remove", "target": "user", "old_text": "tea"})
    assert service.snapshots() == []  # blocked, so nothing is about to change
    service.check("memory", {"action": "replace", "target": "memory", "old_text": "short", "content": "a newer note"})
    [copy] = service.snapshots("memory")
    assert copy.read_text(encoding="utf-8") == "Owner prefers short answers\n"


def test_approve_mode_keeps_a_copy_in_case_the_owner_approves(tmp_path):
    service, _, _ = make(tmp_path, mode="approve")
    out = service.check("memory", {"action": "remove", "target": "user", "old_text": "tea"})
    assert out["action"] == "approve" and len(service.snapshots("user")) == 1
    assert service.events()[0]["outcome"] == "sent for approval"


def test_snapshots_skip_duplicates_rotate_and_can_be_off(tmp_path):
    service, memories, _ = make(tmp_path, snapshots=2)
    service.snapshot("user")
    service.snapshot("user")
    assert len(service.snapshots("user")) == 1  # unchanged file, no second copy
    for text in ("one", "two", "three"):
        (memories / "USER.md").write_text(text, encoding="utf-8")
        service.snapshot("user")
    kept = service.snapshots("user")
    assert [p.read_text(encoding="utf-8") for p in kept] == ["three", "two"]
    off, _, _ = make(tmp_path / "off", snapshots=0)
    assert off.snapshot("user") is None and "snapshots are off" in off.snapshots_text()


def test_restore_brings_a_file_back_and_is_undoable(tmp_path):
    service, memories, _ = make(tmp_path)
    original = (memories / "USER.md").read_text(encoding="utf-8")
    service.snapshot("user")
    (memories / "USER.md").write_text("wiped", encoding="utf-8")
    message = service.command("restore 1")
    assert "Restored the owner profile from snapshot #1" in message and "/new" in message
    assert (memories / "USER.md").read_text(encoding="utf-8") == original
    # the wiped state was saved first, so the restore itself can be undone
    assert any(p.read_text(encoding="utf-8") == "wiped" for p in service.snapshots("user"))


def test_restore_of_the_oldest_copy_survives_rotation(tmp_path):
    service, memories, _ = make(tmp_path, snapshots=2)
    for text in ("old", "new"):
        (memories / "USER.md").write_text(text, encoding="utf-8")
        service.snapshot("user")
    (memories / "USER.md").write_text("current", encoding="utf-8")
    service.command("restore 2")
    assert (memories / "USER.md").read_text(encoding="utf-8") == "old"


def test_restore_is_refused_in_groups_for_strangers_and_for_bad_numbers(tmp_path):
    group, _, _ = make(tmp_path / "g", {"HERMES_SESSION_CHAT_TYPE": "group"})
    group.snapshot("user")
    assert "only in a direct chat" in group.command("restore 1")
    stranger, _, _ = make(tmp_path / "s", {"HERMES_SESSION_USER_ID": "999"}, trusted_users=["111"])
    stranger.snapshot("user")
    assert "trusted_users" in stranger.command("restore 1")
    owner, _, _ = make(tmp_path / "o")
    assert "no snapshot #4" in owner.command("restore 4")
    assert "Say which snapshot" in owner.command("restore")


TRUSTED_GROUP = {"HERMES_SESSION_CHAT_TYPE": "group", "HERMES_SESSION_CHAT_ID": "-1001234567890",
                 "HERMES_SESSION_USER_ID": "", "HERMES_SESSION_USER_NAME": ""}


def test_a_trusted_chat_writes_and_restores_like_a_direct_chat(tmp_path):
    settings = {"trusted_users": ["telegram:111"], "trusted_chats": ["telegram:-1001234567890"]}
    service, memories, _ = make(tmp_path, TRUSTED_GROUP, **settings)
    original = (memories / "USER.md").read_text(encoding="utf-8")
    assert service.check("memory", {"action": "add", "target": "user", "content": "Has a cat"}) is None
    service.snapshot("user")
    (memories / "USER.md").write_text("wiped", encoding="utf-8")
    assert "Restored the owner profile from snapshot #1" in service.command("restore 1")
    assert (memories / "USER.md").read_text(encoding="utf-8") == original
    other, _, _ = make(tmp_path / "other", dict(TRUSTED_GROUP, HERMES_SESSION_CHAT_ID="-1009876543210"), **settings)
    assert other.check("memory", {"action": "add", "target": "user", "content": "Has a cat"})["action"] == "block"
    assert "only in a direct chat" in other.command("restore 1")


def test_status_whoami_and_usage(tmp_path):
    service, _, _ = make(tmp_path, trusted_users=["111"])
    service.check("memory", {"action": "remove", "target": "user", "old_text": "tea"})
    status = service.command("")
    assert "owner profile: append_only" in status and "trusted in shared chats: 1" in status
    assert "trusted chats, judged like direct chats: none" in status
    assert "log: 1 event(s)" in status and "snapshots: 0 kept" in status
    whoami = service.command("whoami")
    assert "You are telegram:111 (Owner)" in whoami and "this id is in trusted_users" in whoami
    assert service.command("dance").startswith("Usage:")


def test_whoami_shows_the_chat_key_in_shared_chats(tmp_path):
    service, _, _ = make(tmp_path, TRUSTED_GROUP)
    text = service.command("whoami")
    assert "trusted_users cannot match here" in text
    assert "This chat is telegram:-1001234567890; it is not in trusted_chats" in text
    assert "only if nobody but you and the agent is in this chat" in text
    listed, _, _ = make(tmp_path / "listed", TRUSTED_GROUP, trusted_chats=["telegram:-1001234567890"])
    assert "it is in trusted_chats, so it is judged like a direct chat" in listed.command("whoami")
    assert "trusted chats, judged like direct chats: 1" in listed.command("")
    both, _, _ = make(tmp_path / "both", {"HERMES_SESSION_CHAT_TYPE": "group", "HERMES_SESSION_CHAT_ID": "-1009"})
    first, second = both.command("whoami").splitlines()
    assert first.startswith("You are telegram:111 (Owner) in a group chat") and "telegram:-1009" in second
    dm, _, _ = make(tmp_path / "dm", {"HERMES_SESSION_CHAT_ID": "111"})
    assert "trusted_chats" not in dm.command("whoami")  # a direct chat needs no entry


def test_a_broken_data_dir_never_changes_the_decision(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way", encoding="utf-8")
    service = shield.Shield(get_config=DEFAULT, data_dir=lambda: blocker, memory_dir=lambda: tmp_path,
                            session=lambda name: "dm" if name == "HERMES_SESSION_CHAT_TYPE" else "")
    assert service.check("memory", {"action": "remove", "target": "user", "old_text": "x"})["action"] == "block"
    (tmp_path / "MEMORY.md").write_text("note", encoding="utf-8")
    edit = {"action": "replace", "target": "memory", "old_text": "n", "content": "a newer note"}
    assert service.check("memory", edit) is None


# --- plugin entry point ------------------------------------------------------------------------


def test_register_wires_hook_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    spec = importlib.util.spec_from_file_location(
        "memory_shield_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_shield_plugin"] = module
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.hooks, self.commands = {}, {}

        def get_config(self, key, default=None):
            return default

        def register_hook(self, name, callback):
            self.hooks[name] = callback

        def register_command(self, name, handler, **kw):
            self.commands[name] = handler

    ctx = Ctx()
    module.register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="memory", args={"action": "remove", "target": "user"}, task_id="t")["action"] == "block"
    assert hook(tool_name="web_search", args={}, task_id="t") is None
    assert hook(tool_name="write_file", args={"path": "~/.hermes/memories/USER.md"}, task_id="t")["action"] == "block"
    assert hook(tool_name="write_file", args={"path": "/tmp/a.txt"}, task_id="t") is None
    assert "owner profile: append_only" in ctx.commands["memory-shield"]("")
    log = tmp_path / "plugin-data" / "memory-shield" / "audit.jsonl"
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["outcome"] == "blocked"
