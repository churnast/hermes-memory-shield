"""Policy, audit log and snapshots for memory-shield.

No Hermes imports at module level, so the tests run without a Hermes checkout. Hermes lookups
(session values, the plugin data directory, the memory directory) are resolved lazily and fall
back to environment variables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # the same advisory lock Hermes' memory store takes around MEMORY.md / USER.md
    import fcntl
except ImportError:  # pragma: no cover (Windows)
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLUGIN_NAME = "memory-shield"
TOOL = "memory"
DIRECT_EDIT_TOOLS = {"write_file": "path", "patch": "path"}
SHELL_TOOLS = {"terminal": "command", "execute_code": "code"}
WATCHED_TOOLS = frozenset({TOOL, *DIRECT_EDIT_TOOLS, *SHELL_TOOLS})
ACTIONS = ("add", "replace", "remove")
DESTRUCTIVE = frozenset({"replace", "remove"})
OWNER_EDITS = "owner_edits"
LEVELS: dict[str, frozenset] = {
    "off": frozenset(ACTIONS),
    "no_delete": frozenset({"add", "replace"}),
    OWNER_EDITS: frozenset({"add"}),  # plus "replace" for the owner in a direct chat, see evaluate()
    "append_only": frozenset({"add"}),
    "read_only": frozenset(),
}
MODES = ("block", "approve", "observe")
DEFAULTS = {
    "user_profile": OWNER_EDITS,     # target "user": what the agent knows about its owner
    "agent_notes": "no_delete",      # target "memory": the agent's own notes
    "group_chats": "read_only",      # any shared chat: the stricter of this and the target level
    "scheduled_jobs": "off",         # cron runs: the stricter of this and the target level
    "mode": "block",                 # "approve": Hermes' approval gate; "observe": allow and log
}
DEFAULT_SNAPSHOTS = 20
MAX_SNAPSHOTS = 200
AUDIT_FILE = "audit.jsonl"
AUDIT_KEEP = 500
EXCERPT_CHARS = 160
SNAPSHOT_DIR = "snapshots"
MEMORY_FILES = {"user": "USER.md", "memory": "MEMORY.md"}
DIRECT_CHAT_TYPES = {"", "dm", "direct", "private", "im"}
TRUTHY = {"1", "true", "yes", "on"}
TARGET_SETTING = {"user": "user_profile", "memory": "agent_notes"}
TARGET_LABEL = {"user": "the owner's profile", "memory": "the agent's own notes"}
SHORT_LABEL = {"user": "owner profile", "memory": "agent notes"}
OUTCOME = {"block": "blocked", "approve": "sent for approval", "observe": "observed"}
TIME_FORMAT = "%Y-%m-%d %H:%M"
# A "replace" whose new text is one of these, or has fewer than three letters or digits, wipes the
# entry just like "remove" does, so it is judged as a removal.
STUB_WORDS = frozenset({
    # English
    "none", "null", "nil", "empty", "blank", "deleted", "removed", "redacted", "forgotten", "cleared", "tbd",
    "todo", "void", "n a", "not relevant", "no longer relevant", "obsolete", "outdated",
    # Russian
    "нет", "пусто", "пустая запись", "удалено", "удалена", "удалён", "удален", "стёрто", "стерто", "убрано",
    "очищено", "забыто", "неактуально", "не актуально", "устарело", "запись удалена",
    # Spanish, Portuguese, German, French
    "borrado", "eliminado", "apagado", "gelöscht", "entfernt", "supprimé", "vide",
})
# A terminal or execute_code payload is read up to this many characters. A longer one is not read at all:
# it is refused when it contains the word memories, USER.md or MEMORY.md in any case once quotes and
# backslashes are dropped, and let through when it does not.
MAX_SCAN_CHARS = 16 * 1024
_MEMORY_MENTION = re.compile(r"memories|user\.md|memory\.md", re.IGNORECASE)
_SHELL_QUOTING = re.compile(r"[\"'\\]")
_SHELL_MEMORY_DIR = re.compile(r"memories|\.hermes|HERMES_HOME")
# Where the memory folder lives, as written in a path: $HERMES_HOME, ~/.hermes or hermes_home() in Python.
_MEMORY_HOME = re.compile(r"\.hermes|HERMES_HOME|hermes_home\(\)")
# The memory folder itself, as a shell word, for commands that wipe or move it without naming a file.
_SHELL_MEMORY_FOLDER_END = re.compile(r"/memories/?$")
_SHELL_MEMORY_GLOB = "memories/*"
# The same two things inside a Python expression, where the path may be built from pieces.
_PY_MEMORY_FILE = re.compile(r"(?<![\w.])(USER|MEMORY)\.md(?![\w.])")
_PY_MEMORY_FOLDER_WORD = re.compile(r"memories(?![\w.\-/])")
# Shell commands judged by where their target is. "any": any memory path among the arguments counts
# (the file is removed, moved, truncated or edited in place); "last": only a memory path as the last
# argument, the destination, counts, so copying a memory file somewhere else passes.
_SHELL_ANY_ARG = frozenset({"rm", "rmdir", "unlink", "shred", "truncate", "trash", "mv", "tee"})
_SHELL_LAST_ARG = frozenset({"cp", "install", "rsync", "ln"})
_SHELL_IN_PLACE = frozenset({"sed", "perl"})
_SHELL_PREFIXES = frozenset({"sudo", "doas", "command", "builtin", "nohup", "env", "exec", "time", "nice"})
_SHELL_SPLIT = re.compile(r"\|\||&&|[;|\n]")
_SHELL_REDIRECT = re.compile(r"(?<![<\w])&?\d?>{1,2}\|?\s*(\"[^\"]*\"|'[^']*'|\S+)")
_SHELL_ALIAS = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=(\S+)")
_PY_ALIAS = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+)$")
_SHELL_VARIABLE = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")
_PY_NAME = re.compile(r"\b([A-Za-z_]\w*)\b")
_PY_STATEMENT = re.compile(r"[;\n]")
# Python calls that change or delete a file: methods of the path before the dot, and functions of the
# path in their arguments.
_PY_WRITE_METHOD = re.compile(r"\.(?:write_text|write_bytes|unlink|rmdir|rename|truncate)\(")
_PY_WRITE_FUNCTION = re.compile(r"os\.(?:remove|unlink|rmdir|rename|replace|truncate)\(|shutil\.(?:rmtree|move)\("
                                r"|send2trash\(")
_PY_ARG = r"((?:[^,()]|\([^()]*\))++)"
# Possessive, so a long run of spaces or of plain characters is read once instead of retried at every split.
_PY_OPEN = re.compile(r"\bopen\(\s*+" + _PY_ARG + r"\s*,\s*+(?:mode\s*+=\s*+)?['\"]([^'\"]*)['\"]")
_PY_COPY = re.compile(r"shutil\.copy\w*\(((?:[^()]|\([^()]*\))*)\)")
_PY_STRING = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
_SHELL_REDIRECT_WORD = re.compile(r"^(?:&?\d?>{1,2}\|?|<{1,3})")


class ShieldError(Exception):
    """A refusal that is shown to the person who typed the command."""


# --- Policy (pure) ----------------------------------------------------------------------------


def background_review() -> bool:
    """True inside Hermes' unattended self-improvement review: the fork that reads the finished
    conversation and tidies memory and skills with nobody watching. Hermes marks it with a context
    variable (tools/skill_provenance.py) that reaches pre_tool_call hooks; a review the owner asked for
    with /refine is attended and does not count. False without Hermes."""
    try:
        from tools.skill_provenance import is_unattended_review  # type: ignore

        return bool(is_unattended_review())
    except Exception:
        return False


def session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env  # type: ignore

        return get_session_env(name, "") or ""
    except Exception:
        return os.environ.get(name, "")


def level(get_config: Callable[[str, Any], Any], key: str) -> str:
    raw = get_config(key, DEFAULTS[key])
    if raw is False:  # YAML reads an unquoted off as false, and `hermes config set ... off` stores false
        return "off"
    value = str(raw or DEFAULTS[key]).strip().lower()
    return value if value in LEVELS else DEFAULTS[key]


def mode(get_config: Callable[[str, Any], Any]) -> str:
    value = str(get_config("mode", DEFAULTS["mode"]) or DEFAULTS["mode"]).strip().lower()
    return value if value in MODES else DEFAULTS["mode"]


def _id_list(get_config: Callable[[str, Any], Any], key: str) -> list[str]:
    raw = get_config(key, []) or []
    if isinstance(raw, (str, int)):
        raw = str(raw).replace(",", " ").split()
    return [str(item).strip() for item in raw if str(item).strip()]


def trusted_users(get_config: Callable[[str, Any], Any]) -> list[str]:
    """Platform user ids, as 'telegram:12345' or a bare '12345' (any platform). Display names are
    never trusted: anyone can copy one."""
    return _id_list(get_config, "trusted_users")


def trusted_chats(get_config: Callable[[str, Any], Any]) -> list[str]:
    """Chats that only the owner and the agent are in, as 'telegram:-100123' or a bare '-100123' (any
    platform). They are judged like a direct chat."""
    return _id_list(get_config, "trusted_chats")


def _listed(entries: Iterable[str], value: str | None, platform: str | None) -> bool:
    """'platform:id' matches on that platform only (the id itself may contain colons, as on Matrix);
    an entry without a colon matches that id on any platform."""
    wanted = str(value or "").strip()
    if not wanted:
        return False
    current = str(platform or "").strip().lower()
    for item in entries:
        prefix, sep, rest = item.partition(":")
        if (sep and prefix.lower() == current and rest == wanted) or (not sep and item == wanted):
            return True
    return False


def is_trusted(get_config: Callable[[str, Any], Any], user_id: str | None, platform: str | None = "") -> bool:
    return _listed(trusted_users(get_config), user_id, platform)


def is_trusted_chat(get_config: Callable[[str, Any], Any], chat_id: str | None, platform: str | None = "") -> bool:
    return _listed(trusted_chats(get_config), chat_id, platform)


def is_stub(text: Any) -> bool:
    plain = re.sub(r"[\W_]+", " ", str(text or "")).strip().lower()
    return len(plain.replace(" ", "")) < 3 or plain in STUB_WORDS


def requested_actions(args: dict[str, Any]) -> list[str]:
    """One action, or every action of a batch. A replace with empty or placeholder text counts as a
    removal. Unknown or missing actions are left to the tool."""
    operations = args.get("operations")
    ops = [op for op in operations if isinstance(op, dict)] if isinstance(operations, list) and operations else [args]
    found = []
    for op in ops:
        action = str(op.get("action") or "").lower()
        if action == "replace" and is_stub(op.get("content") or op.get("new_text")):
            action = "remove"
        found.append(action)
    return [a for a in found if a in ACTIONS]


def is_shared_chat(chat_type: str) -> bool:
    return (chat_type or "").strip().lower() not in DIRECT_CHAT_TYPES


def _is_memory_path(raw: str, name: str, memory_dir: Callable[[], Path] | None) -> bool:
    text = (raw or "").strip().replace("\\", "/")
    if not text or name not in text:
        return False
    if f"memories/{name}" in text:
        return True
    if memory_dir is None:
        return False
    try:
        path = Path(os.path.expanduser(text))
        return path.name == name and path.is_absolute() and path.resolve() == (memory_dir() / name).resolve()
    except (OSError, ValueError):
        return False


def memory_file_target(tool_name: str, args: dict[str, Any],
                       memory_dir: Callable[[], Path] | None = None) -> str | None:
    """'user' or 'memory' when a file or shell tool is about to change USER.md / MEMORY.md directly."""
    if tool_name in DIRECT_EDIT_TOOLS:
        texts = [str(args.get(DIRECT_EDIT_TOOLS[tool_name]) or "")]
        if tool_name == "patch":
            texts.append(str(args.get("patch") or ""))  # unified-diff mode names its files inside
        for target, name in MEMORY_FILES.items():
            if any(_is_memory_path(text, name, memory_dir) for text in texts):
                return target
        return None
    if tool_name in SHELL_TOOLS:
        code = str(args.get(SHELL_TOOLS[tool_name]) or "")
        if too_long(code):
            return _unread_target(code)
        hits = _shell_targets(code) | _python_targets(code)
        if hits:
            return "user" if "user" in hits else "memory"  # "user" first: the stricter store when both are named
    return None


def too_long(code: str) -> bool:
    """True when a shell or Python payload is past the cap and is judged without being read."""
    return len(code) > MAX_SCAN_CHARS


def _unread_target(code: str) -> str | None:
    """The store a payload too long to read is charged with: 'user' when it mentions the memory folder or
    USER.md, 'memory' when it mentions only MEMORY.md, None when it mentions none of them. Case, quotes
    and backslashes are ignored, so mem"ori"es or US\\ER.md still count."""
    mentions = {match.group(0).lower() for match in _MEMORY_MENTION.finditer(_SHELL_QUOTING.sub("", code))}
    if not mentions:
        return None
    return "memory" if mentions == {"memory.md"} else "user"


class _Aliases:
    """Names assigned a memory path earlier in the same call, for shell variables ($F, ${F}) or Python
    names. A value that refers to another alias is not kept, and values are substituted in one pass and
    never substituted again, so a chain of aliases cannot multiply the text; the substitution also stops
    once it has added more than the cap to the call's text, and a call that runs past that budget is
    judged as a write to the owner's profile rather than read on without its aliases."""

    def __init__(self, shell: bool) -> None:
        self.values: dict[str, str] = {}
        self._pattern = _SHELL_VARIABLE if shell else _PY_NAME
        self._room = MAX_SCAN_CHARS
        self.exhausted = False

    def refers_to_one(self, text: str) -> bool:
        return bool(self.values) and any(m.group(1) in self.values for m in self._pattern.finditer(text))

    def add(self, name: str, value: str) -> None:
        self.values[name] = value

    def expand(self, text: str) -> str:
        if not self.values:
            return text

        def value(match: re.Match[str]) -> str:
            replacement = self.values.get(match.group(1))
            if replacement is None:
                return match.group(0)
            if self._room < 0:
                self.exhausted = True
                return match.group(0)
            self._room -= len(replacement) - len(match.group(0))
            return replacement

        return self._pattern.sub(value, text)


def _memory_token(token: str, aliases: _Aliases, bare: bool = False) -> str | None:
    """'user' or 'memory' when a shell word names a memory file, 'user' when it names the memory folder."""
    text = aliases.expand(token.strip("'\""))
    for target, name in MEMORY_FILES.items():
        if text.rsplit("/", 1)[-1] == name and (_SHELL_MEMORY_DIR.search(text) or bare):
            return target
    return "user" if _shell_memory_folder(text) else None


def _shell_memory_folder(text: str) -> bool:
    """A shell word that is the memory folder: a path ending in /memories that starts from the Hermes home
    somewhere in the same unquoted run, or a glob of everything in it."""
    if text.endswith(_SHELL_MEMORY_GLOB):
        return True
    end = _SHELL_MEMORY_FOLDER_END.search(text)
    if not end:
        return False
    run = re.split(r"[\s'\"]", text[:end.start()])[-1]
    return _MEMORY_HOME.search(run) is not None


def _py_memory_folder(text: str) -> bool:
    """A Python expression that names the memory folder: the Hermes home followed by the word memories on
    the same line, read once per line so many mentions of the home cannot multiply the work."""
    for line in text.split("\n"):
        home = _MEMORY_HOME.search(line)
        if home and _PY_MEMORY_FOLDER_WORD.search(line, home.end()):
            return True
    return False


def _operands(words: list[str]) -> list[str]:
    """Arguments without options and without redirections and what they point at."""
    out = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if _SHELL_REDIRECT_WORD.match(word):
            skip = _SHELL_REDIRECT_WORD.fullmatch(word) is not None  # a bare operator takes the next word
            continue
        if not word.startswith("-"):
            out.append(word)
    return out


def _shell_targets(code: str, nested: bool = False) -> set[str]:
    """Memory files a shell command writes into, removes or moves: a redirection or tee into the file, an
    in-place edit, a removal or move of the file or the folder, or a copy whose destination is one of them.
    Reading a memory file, copying it elsewhere or redirecting elsewhere does not count. Quoted strings
    are read once as shell text too, for commands passed to python -c, subprocess or os.system."""
    hits: set[str] = set()
    aliases = _Aliases(shell=True)
    in_memory_dir = False
    xargs_removes = False  # memory paths among all words of the call count, read once after the loop
    for segment in _SHELL_SPLIT.split(code):
        segment = segment.strip()
        if not segment:
            continue
        for match in _SHELL_REDIRECT.finditer(segment):
            target = _memory_token(match.group(1), aliases, in_memory_dir)
            if target and not match.group(1).startswith("&"):
                hits.add(target)
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        alias = _SHELL_ALIAS.match(segment)
        if alias and not aliases.refers_to_one(alias.group(2)) and _memory_token(alias.group(2), aliases):
            aliases.add(alias.group(1), alias.group(2).strip("'\""))
        while words and (words[0] in _SHELL_PREFIXES or ("=" in words[0] and not words[0].startswith("-"))):
            words = words[1:]
        if not words:
            continue
        command = words[0].rsplit("/", 1)[-1]
        options = [w for w in words[1:] if w.startswith("-")]
        operands = _operands(words[1:])
        if command == "cd":
            in_memory_dir = bool(operands) and _memory_token(operands[0], aliases) == "user" and \
                MEMORY_FILES["user"] not in operands[0] and MEMORY_FILES["memory"] not in operands[0]
            continue
        found = [_memory_token(w, aliases, in_memory_dir) for w in operands]
        if command in _SHELL_ANY_ARG or (command in _SHELL_IN_PLACE and any(o.startswith("-") and "i" in o[1:3]
                                                                            for o in options)):
            hits.update(t for t in found if t)
        elif command in _SHELL_LAST_ARG and found and found[-1]:
            hits.add(found[-1])
        elif command == "dd":
            hits.update(t for t in (_memory_token(w[3:], aliases, in_memory_dir) for w in words if w.startswith("of="))
                        if t)
        elif command == "find" and any(found) and ("-delete" in words or any(w in ("rm", "mv") for w in words)):
            hits.update(t for t in found if t)
        elif command == "xargs" and any(w in _SHELL_ANY_ARG for w in words[1:]):
            xargs_removes = True
    if xargs_removes:
        hits.update(t for t in (_memory_token(w, aliases) for w in code.split()) if t)
    if aliases.exhausted:
        hits.add("user")  # the alias budget ran out: fail closed rather than stop following the aliases
    if not nested:
        for match in _PY_STRING.finditer(code):
            hits |= _shell_targets(match.group(1) or match.group(2) or "", nested=True)
    return hits


def _python_targets(code: str) -> set[str]:
    """Memory files that Python code opens for writing, rewrites, deletes, moves or copies over, statement by
    statement; a name assigned a memory path earlier in the code counts as that path."""
    hits: set[str] = set()
    aliases = _Aliases(shell=False)

    def target_in(text: str) -> str | None:
        text = aliases.expand(text)
        found = _PY_MEMORY_FILE.search(text)
        if found and _SHELL_MEMORY_DIR.search(text):
            return "user" if found.group(1) == "USER" else "memory"
        return "user" if _py_memory_folder(text) else None

    for statement in _PY_STATEMENT.split(code):
        statement = statement.strip()
        if not statement:
            continue
        alias = _PY_ALIAS.match(statement)
        if alias and not aliases.refers_to_one(alias.group(2)) and target_in(alias.group(2)):
            aliases.add(alias.group(1), alias.group(2))
        for match in _PY_OPEN.finditer(statement):
            target = target_in(match.group(1))
            if target and any(flag in match.group(2) for flag in "wax+"):
                hits.add(target)
        for match in _PY_COPY.finditer(statement):
            destination = ",".join(match.group(1).split(",")[1:])
            target = target_in(destination)
            if target:
                hits.add(target)
        # The path is read once per statement, not once per call: the text before the last method call
        # holds the paths before every dot, the text after the first function call holds every argument.
        methods = list(_PY_WRITE_METHOD.finditer(statement))
        if methods:
            target = target_in(statement[:methods[-1].start()])
            if target:
                hits.add(target)
        function = _PY_WRITE_FUNCTION.search(statement)
        if function:
            target = target_in(statement[function.end():])
            if target:
                hits.add(target)
    if aliases.exhausted:
        hits.add("user")
    return hits


def _hint(target: str, allowed: Iterable[str], direct_edit: bool = False, context: str = "direct",
          unread: bool = False) -> str:
    allowed = set(allowed)
    if unread:
        return (f"The command is longer than {MAX_SCAN_CHARS // 1024} KB and names a memory file or the memory "
                "folder, so it is refused without being read. Split it into shorter calls, or keep USER.md, "
                "MEMORY.md and the memories folder out of it and use the memory tool for them.")
    if direct_edit:
        return ("Memory files are changed only through the memory tool, which keeps this policy and a "
                "snapshot. Do not edit USER.md or MEMORY.md with file or shell tools.")
    if context == "review":
        return ("The background review does not change what the agent knows about the owner. Leave the "
                "owner's profile as it is.")
    if not allowed:
        return "Do not write to memory here. If something should be remembered, tell the owner in a direct chat."
    if allowed == {"add"}:
        if target == "user":
            if context == "direct":
                return ("Existing facts about the owner cannot be changed or deleted by the agent. If something "
                        "is outdated, add a new dated entry with the correction and tell the owner what looks "
                        "wrong.")
            return ("Existing facts about the owner are changed only when the owner asks in a direct chat. "
                    "Add a new dated entry with the correction instead.")
        return "Only new entries can be added here. Add a corrected entry instead of editing the old one."
    if allowed == {"add", "replace"}:
        if target == "user":
            return ("Facts about the owner cannot be deleted, and replacing one with empty or placeholder text "
                    "counts as deleting. Replace an outdated fact with the corrected one instead.")
        return ("Entries cannot be deleted, and replacing one with empty or placeholder text counts as deleting. "
                "Replace an outdated entry with an updated version instead.")
    return ""


@dataclass
class Verdict:
    target: str
    actions: list[str]
    denied: list[str]
    where: str
    hint: str
    context: str = "direct"      # direct | shared | stranger | scheduled | review
    fingerprint: str = ""

    @property
    def destructive(self) -> bool:
        return any(action in DESTRUCTIVE for action in self.actions)

    @property
    def message(self) -> str:
        return f"memory-shield: '{'/'.join(self.denied)}' is not allowed for {self.where}. {self.hint}".strip()


def _fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    raw = json.dumps([tool_name, args], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def evaluate(tool_name: str, args: dict[str, Any] | None, get_config: Callable[[str, Any], Any],
             chat_type: str | None = None, user_id: str | None = None, platform: str | None = None,
             cron: str | None = None, memory_dir: Callable[[], Path] | None = None,
             chat_id: str | None = None, review: bool | None = None) -> Verdict | None:
    """What a call asks to change in memory and which of it the policy denies; None when the call
    does not touch memory. A chat listed in trusted_chats is judged like a direct chat. Once
    trusted_users is set, a direct chat with someone not listed there is judged like a shared chat;
    a session without a user id (the CLI) still counts as the owner."""
    if tool_name not in WATCHED_TOOLS or not isinstance(args, dict):
        return None
    direct_edit = tool_name != TOOL
    unread = False
    if direct_edit:
        target = memory_file_target(tool_name, args, memory_dir)
        if target is None:
            return None
        actions = list(ACTIONS)  # rewriting the file can add, change and delete entries at once
        unread = tool_name in SHELL_TOOLS and too_long(str(args.get(SHELL_TOOLS[tool_name]) or ""))
    else:
        target = str(args.get("target") or "memory").lower()
        if target not in TARGET_SETTING:
            return None
        actions = requested_actions(args)
        if not actions:
            return None
    target_level = level(get_config, TARGET_SETTING[target])
    allowed = set(LEVELS[target_level])
    where = TARGET_LABEL[target]
    if unread:
        where += " (in a command too long to read)"
    elif direct_edit:
        where += " (editing the file directly)"
    if chat_type is None:
        chat_type = session_value("HERMES_SESSION_CHAT_TYPE")
    if platform is None:
        platform = session_value("HERMES_SESSION_PLATFORM")
    if cron is None:
        cron = session_value("HERMES_CRON_SESSION")
    if chat_id is None:
        chat_id = session_value("HERMES_SESSION_CHAT_ID")
    if user_id is None:
        user_id = session_value("HERMES_SESSION_USER_ID")
    if review is None:
        review = background_review()
    trusted = trusted_users(get_config)
    context = "direct"
    if review and target == "user" and target_level != "off":
        context = "review"
        allowed = set()
        where += " (in the background review)"
    elif is_shared_chat(chat_type) and not is_trusted_chat(get_config, chat_id, platform):
        context = "shared"
        if not is_trusted(get_config, user_id, platform):
            allowed &= LEVELS[level(get_config, "group_chats")]
            where += " (in a shared chat)"
    elif trusted and user_id and not is_trusted(get_config, user_id, platform):
        context = "stranger"
        allowed &= LEVELS[level(get_config, "group_chats")]
        where += " (in a direct chat with someone not in trusted_users)"
    elif str(cron or "").strip().lower() in TRUTHY:
        context = "scheduled"
        allowed &= LEVELS[level(get_config, "scheduled_jobs")]
        where += " (in a scheduled job)"
    if target_level == OWNER_EDITS and context == "direct":
        allowed.add("replace")  # the owner, in a direct chat, the CLI or a trusted chat, may correct entries
    denied = sorted({a for a in actions if a not in allowed}, key=ACTIONS.index)
    hint = _hint(target, allowed, direct_edit, context, unread) if denied else ""
    return Verdict(target, actions, denied, where, hint, context, _fingerprint(tool_name, args))


def directive(verdict: Verdict, current_mode: str) -> dict[str, str] | None:
    """The pre_tool_call answer for a denied call. Observe mode lets it through. Approval is asked
    only in a direct chat: in a group it is unclear who answers, and a scheduled job has nobody."""
    if not verdict.denied or current_mode == "observe":
        return None
    if current_mode == "approve" and verdict.context == "direct":
        # one key per exact call, so an "always allow" answer cannot switch off a whole rule
        return {"action": "approve", "message": verdict.message,
                "rule_key": f"memory-shield:{verdict.target}:{'-'.join(verdict.denied)}:{verdict.fingerprint}"}
    return {"action": "block", "message": verdict.message}


def effective_mode(verdict: Verdict, current_mode: str) -> str:
    return "block" if current_mode == "approve" and verdict.context != "direct" else current_mode


def decide(tool_name: str, args: dict[str, Any] | None, get_config: Callable[[str, Any], Any],
           chat_type: str | None = None, user_id: str | None = None, platform: str | None = None,
           cron: str | None = None, chat_id: str | None = None, review: bool | None = None) -> dict[str, str] | None:
    """Return a pre_tool_call directive, or None to let the call through. No side effects."""
    verdict = evaluate(tool_name, args, get_config, chat_type, user_id, platform, cron, chat_id=chat_id,
                       review=review)
    return directive(verdict, mode(get_config)) if verdict else None


def describe(get_config: Callable[[str, Any], Any]) -> str:
    def names(key: str) -> str:
        current = level(get_config, key)
        allowed = [a for a in ACTIONS if a in LEVELS[current]]
        if current == OWNER_EDITS:
            return "add; replace too for the owner in a direct chat"
        return ", ".join(allowed) if allowed else "nothing"

    trusted = trusted_users(get_config)
    chats = trusted_chats(get_config)
    on_violation = {"block": "block", "approve": "ask the owner to approve (direct chats; elsewhere block)",
                    "observe": "allow and log (observe mode)"}[mode(get_config)]
    return "\n".join([
        "memory-shield policy:",
        f"• owner profile: {level(get_config, 'user_profile')} (allowed: {names('user_profile')})",
        f"• agent notes: {level(get_config, 'agent_notes')} (allowed: {names('agent_notes')})",
        f"• shared chats: {level(get_config, 'group_chats')} (allowed: {names('group_chats')})",
        f"• scheduled jobs: {level(get_config, 'scheduled_jobs')} (allowed: {names('scheduled_jobs')})",
        f"• trusted people: {len(trusted) if trusted else 'nobody listed'}"
        + (" (direct chats with anyone else are judged like a shared chat)" if trusted else ""),
        f"• trusted chats, judged like direct chats: {len(chats) if chats else 'none'}",
        "• editing USER.md / MEMORY.md with file or shell tools: judged as add + replace + remove",
        f"• on a violation: {on_violation}",
    ])


def excerpt(args: dict[str, Any], tool_name: str = TOOL) -> str:
    """What the call tried to write or delete, shortened for the audit log."""
    if tool_name in DIRECT_EDIT_TOOLS:
        joined = f"{tool_name} {args.get(DIRECT_EDIT_TOOLS[tool_name]) or ''}".strip()
    elif tool_name in SHELL_TOOLS:
        joined = f"{tool_name}: {args.get(SHELL_TOOLS[tool_name]) or ''}".strip()
    else:
        operations = args.get("operations")
        ops = operations if isinstance(operations, list) and operations else [args]
        parts = []
        for op in ops:
            if not isinstance(op, dict):
                continue
            action = str(op.get("action") or "")
            text = op.get("old_text") if action == "remove" else (op.get("content") or op.get("new_text")
                                                                   or op.get("old_text"))
            if text:
                parts.append(f"{action}: {text}")
        joined = " | ".join(parts)
    joined = " ".join(joined.split())
    return joined if len(joined) <= EXCERPT_CHARS else joined[:EXCERPT_CHARS - 1] + "…"


# --- Files ------------------------------------------------------------------------------------


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def default_data_dir() -> Path:
    """<HERMES_HOME>/plugin-data/memory-shield/, via Hermes when available."""
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        return plugin_data_dir(PLUGIN_NAME)
    except Exception:
        path = _hermes_home() / "plugin-data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path


def default_memory_dir() -> Path:
    """Where Hermes keeps MEMORY.md and USER.md for the active profile."""
    try:
        from tools.memory_tool import get_memory_dir  # type: ignore

        return get_memory_dir()
    except Exception:
        return _hermes_home() / "memories"


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold the memory store's own lock file (USER.md.lock) while reading or writing."""
    if fcntl is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(path.suffix + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write through a temporary file of its own in the same folder, so two writers never share one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)  # mkstemp makes the file private; keep the old mode
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


# --- Plugin service ---------------------------------------------------------------------------


class Shield:
    """The policy plus the two side effects the owner can switch off: the audit log and snapshots."""

    def __init__(self, get_config: Callable[[str, Any], Any],
                 data_dir: Callable[[], Path] | None = None,
                 memory_dir: Callable[[], Path] | None = None,
                 clock: Callable[[], float] = time.time,
                 session: Callable[[str], str] = session_value) -> None:
        self._get_config = get_config
        self._data_dir = data_dir or default_data_dir
        self._memory_dir = memory_dir or default_memory_dir
        self._clock = clock
        self._session = session
        self._lock = threading.Lock()

    # settings
    def audit_enabled(self) -> bool:
        return self._get_config("audit_log", True) is not False

    def snapshot_limit(self) -> int:
        try:
            return max(0, min(MAX_SNAPSHOTS, int(self._get_config("snapshots", DEFAULT_SNAPSHOTS))))
        except (TypeError, ValueError):
            return DEFAULT_SNAPSHOTS

    # the hook
    def check(self, tool_name: str, args: dict[str, Any] | None) -> dict[str, str] | None:
        verdict = evaluate(tool_name, args, self._get_config,
                           chat_type=self._session("HERMES_SESSION_CHAT_TYPE"),
                           user_id=self._session("HERMES_SESSION_USER_ID"),
                           platform=self._session("HERMES_SESSION_PLATFORM"),
                           cron=self._session("HERMES_CRON_SESSION"),
                           memory_dir=self._memory_dir,
                           chat_id=self._session("HERMES_SESSION_CHAT_ID"), review=background_review())
        if verdict is None:
            return None
        current = mode(self._get_config)
        answer = directive(verdict, current)
        if verdict.denied:
            self._safely(self._record, OUTCOME[effective_mode(verdict, current)], verdict, args or {}, tool_name)
        if verdict.destructive and (answer is None or answer["action"] == "approve"):
            # the change may go through: keep a copy of the file it is about to change
            self._safely(self.snapshot, verdict.target)
        return answer

    @staticmethod
    def _safely(func: Callable[..., Any], *args: Any) -> Any:
        try:
            return func(*args)
        except Exception as exc:  # a log or copy failure must never change the decision
            logger.warning("memory-shield: %s failed: %s", getattr(func, "__name__", "step"), exc)
            return None

    # audit log
    def _record(self, outcome: str, verdict: Verdict, args: dict[str, Any], tool_name: str = TOOL) -> None:
        if not self.audit_enabled():
            return
        entry = {
            "ts": round(self._clock(), 3),
            "outcome": outcome,
            "target": verdict.target,
            "denied": verdict.denied,
            "tool": tool_name,
            "chat_type": self._session("HERMES_SESSION_CHAT_TYPE") or verdict.context,
            "platform": self._session("HERMES_SESSION_PLATFORM"),
            "chat": self._session("HERMES_SESSION_CHAT_NAME"),
            "user_id": self._session("HERMES_SESSION_USER_ID"),
            "user": self._session("HERMES_SESSION_USER_NAME"),
            "excerpt": excerpt(args, tool_name),
        }
        path = self._data_dir() / AUDIT_FILE
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > AUDIT_KEEP:
                _write_atomic(path, ("\n".join(lines[-AUDIT_KEEP:]) + "\n").encode("utf-8"))

    def events(self, count: int = 10) -> list[dict[str, Any]]:
        try:
            lines = (self._data_dir() / AUDIT_FILE).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-count:] if count > 0 else []:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # snapshots
    def _snapshot_dir(self) -> Path:
        return self._data_dir() / SNAPSHOT_DIR

    def snapshots(self, target: str = "") -> list[Path]:
        """Newest first. File names start with a sortable timestamp and end with the target."""
        folder = self._snapshot_dir()
        if not folder.is_dir():
            return []
        pattern = f"*-{target}.md" if target else "*.md"
        return sorted((p for p in folder.glob(pattern) if p.is_file()), reverse=True)

    def snapshot(self, target: str) -> Path | None:
        limit = self.snapshot_limit()
        if not limit or target not in MEMORY_FILES:
            return None
        source = self._memory_dir() / MEMORY_FILES[target]
        if not source.exists():
            return None
        with _file_lock(source):
            data = source.read_bytes()
        with self._lock:
            existing = self.snapshots(target)
            if existing and existing[0].read_bytes() == data:
                return existing[0]  # nothing changed since the last copy
            now = self._clock()
            name = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{int(now * 1000) % 1000:03d}-{target}.md"
            path = self._snapshot_dir() / name
            _write_atomic(path, data)
            for old in self.snapshots(target)[limit:]:
                old.unlink(missing_ok=True)
            return path

    def restore(self, number: int) -> str:
        files = self.snapshots()
        if not 1 <= number <= len(files):
            raise ShieldError(f"There is no snapshot #{number}. '/memory-shield snapshots' lists them.")
        chosen = files[number - 1]
        target = chosen.stem.rsplit("-", 1)[-1]
        if target not in MEMORY_FILES:
            raise ShieldError(f"Snapshot #{number} does not belong to a memory file.")
        data = chosen.read_bytes()  # read first: the copy below may rotate the chosen file out
        before = self.snapshot(target)
        destination = self._memory_dir() / MEMORY_FILES[target]
        with _file_lock(destination):
            _write_atomic(destination, data)
        saved = " The state before the restore was saved as a new snapshot." if before else ""
        return (f"Restored the {SHORT_LABEL[target]} from snapshot #{number} "
                f"({self._when(chosen)}).{saved} Start a new session (/new) so the agent reads it.")

    def _when(self, path: Path) -> str:
        stamp = "-".join(path.stem.split("-")[:2])
        try:
            return time.strftime(TIME_FORMAT, time.strptime(stamp, "%Y%m%d-%H%M%S"))
        except ValueError:
            return stamp

    # slash command
    def command(self, raw_args: str = "") -> str:
        words = (raw_args or "").split()
        sub = words[0].lower() if words else ""
        try:
            if sub in ("", "policy", "status"):
                return self.status_text()
            if sub == "log":
                self._check_owner_only("The log")
                return self.log_text(int(words[1]) if len(words) > 1 and words[1].isdigit() else 10)
            if sub == "snapshots":
                return self.snapshots_text()
            if sub == "restore":
                if len(words) < 2 or not words[1].lstrip("#").isdigit():
                    raise ShieldError("Say which snapshot: '/memory-shield restore 1' (numbers from "
                                      "'/memory-shield snapshots').")
                self._check_owner_only("Restore")
                return self.restore(int(words[1].lstrip("#")))
            if sub == "whoami":
                return self.whoami_text()
        except ShieldError as exc:
            return f"memory-shield: {exc}"
        return "Usage: /memory-shield [log [n] | snapshots | restore <n> | whoami]"

    def _check_private_chat(self, what: str) -> None:
        """Refuse in shared chats other than those in trusted_chats. Restore needs this, and so does the
        log: it holds what people tried to write in every chat, the owner's direct chats included."""
        if is_shared_chat(self._session("HERMES_SESSION_CHAT_TYPE")) and not is_trusted_chat(
                self._get_config, self._session("HERMES_SESSION_CHAT_ID"), self._session("HERMES_SESSION_PLATFORM")):
            raise ShieldError(f"{what} works only in a direct chat with the agent or in a chat listed in "
                              "trusted_chats.")

    def _check_owner_only(self, what: str) -> None:
        """Restore and the log: refuse where _check_private_chat does and, when trusted_users is set, refuse
        anyone not listed there, in a direct chat too. A session without a user id, such as the CLI or a
        trusted chat that passes none, counts as the owner."""
        self._check_private_chat(what)
        platform = self._session("HERMES_SESSION_PLATFORM")
        user_id = self._session("HERMES_SESSION_USER_ID")
        if trusted_users(self._get_config) and user_id and not is_trusted(self._get_config, user_id, platform):
            raise ShieldError(f"{what} works only for the people listed in trusted_users, in a direct chat with "
                              "the agent or in a chat listed in trusted_chats.")

    def status_text(self) -> str:
        lines = [describe(self._get_config)]
        if self.audit_enabled():
            events = self.events(AUDIT_KEEP)
            latest = (f", latest {time.strftime(TIME_FORMAT, time.localtime(events[-1]['ts']))} "
                      f"{events[-1].get('outcome', '')}") if events else ""
            lines.append(f"• log: {len(events)} event(s){latest}")
        else:
            lines.append("• log: off")
        limit = self.snapshot_limit()
        if limit:
            kept = self.snapshots()
            latest = f", latest {self._when(kept[0])}" if kept else ""
            lines.append(f"• snapshots: {len(kept)} kept{latest}, up to {limit} per file")
        else:
            lines.append("• snapshots: off")
        lines.append("Commands: /memory-shield log · snapshots · restore <n> · whoami")
        return "\n".join(lines)

    def log_text(self, count: int = 10) -> str:
        events = self.events(max(1, min(count, 50)))
        if not events:
            return "memory-shield: nothing was blocked or flagged yet." if self.audit_enabled() else \
                "memory-shield: the log is off (audit_log: false)."
        lines = [f"Last {len(events)} memory-shield event(s):"]
        for event in reversed(events):
            when = time.strftime(TIME_FORMAT, time.localtime(event.get("ts", 0)))
            where = event.get("chat_type") or "direct"
            if event.get("chat"):
                where += f" '{event['chat']}'"
            who = event.get("user") or event.get("user_id") or "unknown"
            if event.get("user_id") and event.get("user") and event["user_id"] != event["user"]:
                who += f" ({event['user_id']})"
            denied = "/".join(event.get("denied") or [])
            label = SHORT_LABEL.get(event.get("target", ""), event.get("target", ""))
            lines.append(f"• {when} {event.get('outcome', '?')}: {denied} in {label}, {where} chat, "
                         f"asked by {who}. {event.get('excerpt', '')}".rstrip())
        return "\n".join(lines)

    def snapshots_text(self) -> str:
        if not self.snapshot_limit():
            return "memory-shield: snapshots are off (snapshots: 0)."
        files = self.snapshots()
        if not files:
            return "memory-shield: no snapshots yet. One is taken right before the agent edits or deletes entries."
        lines = ["Snapshots, newest first:"]
        for number, path in enumerate(files, 1):
            target = path.stem.rsplit("-", 1)[-1]
            size = path.stat().st_size
            lines.append(f"#{number} {self._when(path)} {SHORT_LABEL.get(target, target)}, {size} bytes")
        lines.append("Restore one with '/memory-shield restore <n>' in a direct chat or a trusted chat. "
                     "The current file is saved first, so a restore can be undone too.")
        return "\n".join(lines)

    def whoami_text(self) -> str:
        user_id = self._session("HERMES_SESSION_USER_ID")
        chat = self._session("HERMES_SESSION_CHAT_TYPE") or "direct"
        chat_id = self._session("HERMES_SESSION_CHAT_ID") if is_shared_chat(chat) else ""
        if not user_id and not chat_id:
            return "memory-shield: this session has no platform user id (CLI or a scheduled job)."
        platform = self._session("HERMES_SESSION_PLATFORM")
        if user_id:
            name = self._session("HERMES_SESSION_USER_NAME")
            key = f"{platform}:{user_id}" if platform else user_id
            trusted = "is" if is_trusted(self._get_config, user_id, platform) else "is not"
            lines = [f"You are {key}{f' ({name})' if name else ''} in a {chat} chat; this id {trusted} in "
                     f"trusted_users. Trusted people may write memory from their own messages in shared chats and "
                     f"correct the owner's profile in direct chats; once the list is set, direct chats with anyone "
                     f"else are judged like a shared chat. Add '{key}' to "
                     f"plugins.entries.memory-shield.settings.trusted_users."]
        else:  # e.g. a Telegram group that Hermes observes: the sender is not passed to plugins
            lines = [f"This {chat} chat passes no user id to plugins, so trusted_users cannot match here."]
        if chat_id:
            key = f"{platform}:{chat_id}" if platform else chat_id
            if is_trusted_chat(self._get_config, chat_id, platform):
                lines.append(f"This chat is {key}; it is in trusted_chats, so it is judged like a direct chat. "
                             f"Remove it from there as soon as anyone else joins.")
            else:
                lines.append(f"This chat is {key}; it is not in trusted_chats. Add '{key}' to "
                             f"plugins.entries.memory-shield.settings.trusted_chats only if nobody but you and the "
                             f"agent is in this chat: it is then judged like a direct chat.")
        return "\n".join(lines)
