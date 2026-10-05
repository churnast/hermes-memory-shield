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
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # the same advisory lock Hermes' memory store takes around MEMORY.md / USER.md
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLUGIN_NAME = "memory-shield"
TOOL = "memory"
DIRECT_EDIT_TOOLS = {"write_file": "path", "patch": "path"}
SHELL_TOOLS = {"terminal": "command", "execute_code": "code"}
WATCHED_TOOLS = frozenset({TOOL, *DIRECT_EDIT_TOOLS, *SHELL_TOOLS})
ACTIONS = ("add", "replace", "remove")
DESTRUCTIVE = frozenset({"replace", "remove"})
LEVELS: dict[str, frozenset] = {
    "off": frozenset(ACTIONS),
    "no_delete": frozenset({"add", "replace"}),
    "append_only": frozenset({"add"}),
    "read_only": frozenset(),
}
MODES = ("block", "approve", "observe")
DEFAULTS = {
    "user_profile": "append_only",   # target "user": what the agent knows about its owner
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
STUB_WORDS = frozenset({"none", "null", "nil", "empty", "blank", "deleted", "removed", "redacted",
                        "forgotten", "cleared", "tbd", "todo", "void"})
_SHELL_WRITE = re.compile(
    r">|\b(?:rm|mv|cp|tee|truncate|dd|unlink|shred|install|ln|rsync)\b|\bsed\s+-[a-zA-Z]*i|\bperl\s+-[a-zA-Z]*i"
    r"|-delete\b|write_text|write_bytes|\.write\(|open\([^)]*['\"][wax+]"
    r"|os\.(?:remove|replace|rename|unlink|rmdir)|shutil\.|rmtree|\.unlink\(")
_SHELL_MEMORY_DIR = re.compile(r"memories|\.hermes|HERMES_HOME")
# The memory folder itself, for commands that wipe or move it without naming a file.
_SHELL_MEMORY_FOLDER = re.compile(r"(?:\.hermes|HERMES_HOME|hermes_home\(\))[^\s'\"]*/memories\b|memories/\*")


class ShieldError(Exception):
    """A refusal that is shown to the person who typed the command."""


# --- Policy (pure) ----------------------------------------------------------------------------


def session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env  # type: ignore

        return get_session_env(name, "") or ""
    except Exception:
        return os.environ.get(name, "")


def level(get_config: Callable[[str, Any], Any], key: str) -> str:
    value = str(get_config(key, DEFAULTS[key]) or DEFAULTS[key]).strip().lower()
    return value if value in LEVELS else DEFAULTS[key]


def mode(get_config: Callable[[str, Any], Any]) -> str:
    value = str(get_config("mode", DEFAULTS["mode"]) or DEFAULTS["mode"]).strip().lower()
    return value if value in MODES else DEFAULTS["mode"]


def trusted_users(get_config: Callable[[str, Any], Any]) -> list[str]:
    """Platform user ids, as 'telegram:12345' or a bare '12345' (any platform). Display names are
    never trusted: anyone can copy one."""
    raw = get_config("trusted_users", []) or []
    if isinstance(raw, (str, int)):
        raw = str(raw).replace(",", " ").split()
    return [str(item).strip() for item in raw if str(item).strip()]


def is_trusted(get_config: Callable[[str, Any], Any], user_id: str | None, platform: str | None = "") -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    current = str(platform or "").strip().lower()
    for item in trusted_users(get_config):
        wanted_platform, sep, wanted_id = item.rpartition(":")
        if sep and wanted_id == uid and wanted_platform.lower() == current:
            return True
        if not sep and item == uid:
            return True
    return False


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
        if not _SHELL_WRITE.search(code):
            return None
        hits = [target for target, name in MEMORY_FILES.items() if name in code]
        if hits and _SHELL_MEMORY_DIR.search(code):
            return hits[0]  # "user" first: the stricter store when both are named
        if _SHELL_MEMORY_FOLDER.search(code):
            return "user"  # the whole folder: judged by the stricter store
    return None


def _hint(target: str, allowed: Iterable[str], direct_edit: bool = False) -> str:
    allowed = set(allowed)
    if direct_edit:
        return ("Memory files are changed only through the memory tool, which keeps this policy and a "
                "snapshot. Do not edit USER.md or MEMORY.md with file or shell tools.")
    if not allowed:
        return "Do not write to memory here. If something should be remembered, tell the owner in a direct chat."
    if allowed == {"add"}:
        if target == "user":
            return ("Existing facts about the owner cannot be changed or deleted by the agent. If something is "
                    "outdated, add a new dated entry with the correction and tell the owner what looks wrong.")
        return "Only new entries can be added here. Add a corrected entry instead of editing the old one."
    if allowed == {"add", "replace"}:
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
    context: str = "direct"      # direct | shared | scheduled
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
             cron: str | None = None, memory_dir: Callable[[], Path] | None = None) -> Verdict | None:
    """What a call asks to change in memory and which of it the policy denies; None when the call
    does not touch memory."""
    if tool_name not in WATCHED_TOOLS or not isinstance(args, dict):
        return None
    direct_edit = tool_name != TOOL
    if direct_edit:
        target = memory_file_target(tool_name, args, memory_dir)
        if target is None:
            return None
        actions = list(ACTIONS)  # rewriting the file can add, change and delete entries at once
    else:
        target = str(args.get("target") or "memory").lower()
        if target not in TARGET_SETTING:
            return None
        actions = requested_actions(args)
        if not actions:
            return None
    allowed = set(LEVELS[level(get_config, TARGET_SETTING[target])])
    where = TARGET_LABEL[target] + (" (editing the file directly)" if direct_edit else "")
    if chat_type is None:
        chat_type = session_value("HERMES_SESSION_CHAT_TYPE")
    if platform is None:
        platform = session_value("HERMES_SESSION_PLATFORM")
    if cron is None:
        cron = session_value("HERMES_CRON_SESSION")
    context = "direct"
    if is_shared_chat(chat_type):
        context = "shared"
        if user_id is None:
            user_id = session_value("HERMES_SESSION_USER_ID")
        if not is_trusted(get_config, user_id, platform):
            allowed &= LEVELS[level(get_config, "group_chats")]
            where += " (in a shared chat)"
    elif str(cron or "").strip().lower() in TRUTHY:
        context = "scheduled"
        allowed &= LEVELS[level(get_config, "scheduled_jobs")]
        where += " (in a scheduled job)"
    denied = sorted({a for a in actions if a not in allowed}, key=ACTIONS.index)
    return Verdict(target, actions, denied, where, _hint(target, allowed, direct_edit) if denied else "",
                   context, _fingerprint(tool_name, args))


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
           cron: str | None = None) -> dict[str, str] | None:
    """Return a pre_tool_call directive, or None to let the call through. No side effects."""
    verdict = evaluate(tool_name, args, get_config, chat_type, user_id, platform, cron)
    return directive(verdict, mode(get_config)) if verdict else None


def describe(get_config: Callable[[str, Any], Any]) -> str:
    def names(key: str) -> str:
        allowed = [a for a in ACTIONS if a in LEVELS[level(get_config, key)]]
        return ", ".join(allowed) if allowed else "nothing"

    trusted = trusted_users(get_config)
    on_violation = {"block": "block", "approve": "ask the owner to approve (direct chats; elsewhere block)",
                    "observe": "allow and log (observe mode)"}[mode(get_config)]
    return "\n".join([
        "memory-shield policy:",
        f"• owner profile: {level(get_config, 'user_profile')} (allowed: {names('user_profile')})",
        f"• agent notes: {level(get_config, 'agent_notes')} (allowed: {names('agent_notes')})",
        f"• shared chats: {level(get_config, 'group_chats')} (allowed: {names('group_chats')})",
        f"• scheduled jobs: {level(get_config, 'scheduled_jobs')} (allowed: {names('scheduled_jobs')})",
        f"• trusted in shared chats: {len(trusted) if trusted else 'nobody'}",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


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
                           memory_dir=self._memory_dir)
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
                return self.log_text(int(words[1]) if len(words) > 1 and words[1].isdigit() else 10)
            if sub == "snapshots":
                return self.snapshots_text()
            if sub == "restore":
                if len(words) < 2 or not words[1].lstrip("#").isdigit():
                    raise ShieldError("Say which snapshot: '/memory-shield restore 1' (numbers from "
                                      "'/memory-shield snapshots').")
                self._check_restore_allowed()
                return self.restore(int(words[1].lstrip("#")))
            if sub == "whoami":
                return self.whoami_text()
        except ShieldError as exc:
            return f"memory-shield: {exc}"
        return "Usage: /memory-shield [log [n] | snapshots | restore <n> | whoami]"

    def _check_restore_allowed(self) -> None:
        if is_shared_chat(self._session("HERMES_SESSION_CHAT_TYPE")):
            raise ShieldError("Restore works only in a direct chat with the agent.")
        user_id = self._session("HERMES_SESSION_USER_ID")
        if trusted_users(self._get_config) and user_id and not is_trusted(
                self._get_config, user_id, self._session("HERMES_SESSION_PLATFORM")):
            raise ShieldError("Only the people listed in trusted_users can restore memory.")

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
        lines.append("Restore one with '/memory-shield restore <n>' in a direct chat. "
                     "The current file is saved first, so a restore can be undone too.")
        return "\n".join(lines)

    def whoami_text(self) -> str:
        user_id = self._session("HERMES_SESSION_USER_ID")
        if not user_id:
            return "memory-shield: this session has no platform user id (CLI or a scheduled job)."
        name = self._session("HERMES_SESSION_USER_NAME")
        chat = self._session("HERMES_SESSION_CHAT_TYPE") or "direct"
        platform = self._session("HERMES_SESSION_PLATFORM")
        key = f"{platform}:{user_id}" if platform else user_id
        trusted = "is" if is_trusted(self._get_config, user_id, platform) else "is not"
        return (f"You are {key}{f' ({name})' if name else ''} in a {chat} chat; this id {trusted} in trusted_users. "
                f"Trusted people can let the agent write memory from their own messages in shared chats: "
                f"add '{key}' to plugins.entries.memory-shield.settings.trusted_users.")
