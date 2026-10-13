#!/usr/bin/env python3
"""Jazi's Agent Board 🦎: a keyboard-driven TUI over your Claude Code background sessions.

Reads `claude agents --json --all` plus each job's state.json, and adds what the
stock view lacks: the project each session belongs to, auto-derived and manual
tags, board-local renames, pinning and a group-by-project view.

Run `agent-board --help` for the options, `agent-board --doctor` to check your setup.
"""
from __future__ import annotations

import argparse
import base64
import curses
import fcntl
import hashlib
import json
import locale
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pullrequests as prs
import slash

__version__ = "0.15.0"

CLAUDE = os.environ.get("AGENT_BOARD_CLAUDE", "claude")
JOBS_DIR = Path(os.environ.get("AGENT_BOARD_JOBS_DIR", Path.home() / ".claude" / "jobs"))
PROJECTS_DIR = Path(os.environ.get("AGENT_BOARD_PROJECTS_DIR", Path.home() / ".claude" / "projects"))
LIMITS_PATH = Path(os.environ.get("AGENT_BOARD_LIMITS", Path.home() / ".cache" / "agent-board" / "limits.json"))
META_PATH = Path(
    os.environ.get("AGENT_BOARD_META", Path.home() / ".config" / "agent-board" / "meta.json")
)
REFRESH_SECONDS = 5.0  # each refresh starts Claude Code once (~0.3 s of CPU); every 2 s cost a tenth of a core
SPINNER = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"]
WAITING_MARK = "⧗"  # a working session whose turn is over, waiting on a background task it started
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]")

# ───────────────────────────── tags ─────────────────────────────

TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
NOT_A_TICKET = {"UTF", "SHA", "ISO", "PRD", "ADR", "DR", "HTTP", "TLS", "AES", "RFC", "CVE", "PSR", "WI", "MD"}
PR_RE = re.compile(
    r"(?:\b(?:PR|MR)\s*#?|pull request\s*#?|merge request\s*#?)(\d+)"
    r"|/(?:pull|merge_requests)/(\d+)"
    r"|(?<![\w/])!(\d{2,5})\b",
    re.IGNORECASE,
)
REVIEW_RE = re.compile(
    r"\bcode[- ]review\b|\breview(?:ing)?\s+(?:the\s+|this\s+|my\s+)?(?:pr|mr|pull|merge|code|changes|diff)\b"
    r"|/code-review",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Tag:
    label: str
    kind: str  # review | ticket | pr | custom


TAG_LIMITS = {"review": 1, "ticket": 2, "adr": 2, "dr": 2, "pr": 2}
TAG_ORDER = {"review": 0, "ticket": 1, "adr": 2, "dr": 3, "pr": 4}
ADR_RE = re.compile(r"\b(ADR-\d{3,6})\b")  # padded, as ADR series are: ADR-001, ADR-0020
# a product Decision Record: DR #1, DR#1, "RM DR#1" (RM's first) or "DR: LRT#2"
DR_RE = re.compile(r"(?:\b([A-Z]{2,5})\s+)?\bDR(?::\s*|\s*)([A-Z]{2,5})?\s?#(\d+)\b")
TAG_PREFIX = {"Ticket: ": "ticket", "ADR: ": "adr", "DR: ": "dr", "PR #": "pr"}


def is_adr_key(key: str) -> bool:
    """BE-0012 is an Architecture Decision Record: Jira never pads its numbers with zeros, ADR series do."""
    number = key.rsplit("-", 1)[-1]
    return len(number) >= 3 and number.startswith("0")


def tag_kind(label: str) -> str:
    """The kind of a tag from its label, as the board writes them."""
    if label == "Code Review":
        return "review"
    return next((kind for prefix, kind in TAG_PREFIX.items() if label.startswith(prefix)), "custom")


def remembered_tag(label: str, kind: str) -> Tag:
    """A tag saved in the metadata; one saved as a ticket before ADRs were told apart comes back as an ADR."""
    if kind == "ticket" and is_adr_key(label.removeprefix("Ticket: ")):
        return Tag(f"ADR: {label.removeprefix('Ticket: ')}", "adr")
    return Tag(label, kind)


def merge_tags(kept: list[Tag], found: list[Tag]) -> list[Tag]:
    """Tags seen before stay; newly found ones join while there is room (up to two tickets and two PRs)."""
    out: list[Tag] = []
    for t in kept + found:
        if t.kind not in TAG_LIMITS or any(o.label == t.label for o in out):
            continue
        if sum(1 for o in out if o.kind == t.kind) < TAG_LIMITS[t.kind]:
            out.append(t)
    return sorted(out, key=lambda t: TAG_ORDER[t.kind])


def derive_tags(*sources: str) -> list[Tag]:
    text = "\n".join(s for s in sources if s)
    tags: list[Tag] = []
    if REVIEW_RE.search(text):
        tags.append(Tag("Code Review", "review"))
    tickets: list[str] = []
    adrs: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in ADR_RE.finditer(text)]
    for m in TICKET_RE.finditer(text):
        key = m.group(1)
        if key.split("-")[0] in NOT_A_TICKET or key in tickets:
            continue
        if is_adr_key(key):
            adrs.append((m.start(), key))
        elif len(tickets) < 2:
            tickets.append(key)
    tags.extend(Tag(f"Ticket: {k}", "ticket") for k in tickets)
    adr_keys: list[str] = []
    for _, key in sorted(adrs):
        if key not in adr_keys and len(adr_keys) < 2:
            adr_keys.append(key)
    tags.extend(Tag(f"ADR: {k}", "adr") for k in adr_keys)
    drs: list[str] = []
    for m in DR_RE.finditer(text):
        label = f"{m.group(1) or m.group(2) or ''}#{m.group(3)}"
        if label not in drs and len(drs) < 2:
            drs.append(label)
    tags.extend(Tag(f"DR: {d}", "dr") for d in drs)
    prs: list[str] = []
    for m in PR_RE.finditer(text):
        number = next(g for g in m.groups() if g)
        if number not in prs:
            prs.append(number)
        if len(prs) == 2:
            break
    tags.extend(Tag(f"PR #{n}", "pr") for n in prs)
    return tags


# ───────────────────────────── model ─────────────────────────────

WORKING = {"working", "running", "busy"}
AWAITING = {"waiting", "awaiting", "needs_input", "needs-input", "blocked", "input"}
FAILED = {"failed", "error", "crashed"}
GROUP_ORDER = ["awaiting", "working", "idle", "done"]
GROUP_TITLE = {"awaiting": "Awaiting input", "working": "Working", "idle": "Idle", "done": "Completed"}


@dataclass
class Session:
    id: str
    name: str = ""
    state: str = "done"
    cwd: str = ""
    project: str = "?"
    project_path: str = ""
    branch: str = ""
    worktree: str = ""
    detail: str = ""
    intent: str = ""
    tokens: int = 0
    started_at: float = 0.0  # epoch seconds
    updated_at: float = 0.0
    auto_tags: list[Tag] = field(default_factory=list)
    idle: bool = False  # alive and finished its turn, though its last written state still says "working"
    waiting_on: list[str] = field(default_factory=list)  # background tasks it is waiting on after its turn ended

    @property
    def group(self) -> str:
        if self.state in WORKING:
            return "idle" if self.idle else "working"
        if self.state in AWAITING:
            return "awaiting"
        return "done"


def project_of(origin_cwd: str, cwd: str, worktree_path: str = "") -> tuple[str, str]:
    root = origin_cwd or cwd
    marker = "/.claude/worktrees/"
    if marker in root:
        root = root.split(marker)[0]
    if worktree_path and marker in worktree_path and not origin_cwd:
        root = worktree_path.split(marker)[0]
    root = root.rstrip("/")
    name = os.path.basename(root) or "/"
    if root == str(Path.home()):
        name = "~"
    return name, root


def parse_iso(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def fmt_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def read_state(jobs_dir: Path, job_id: str) -> dict:
    try:
        return json.loads((jobs_dir / job_id / "state.json").read_text())
    except (OSError, ValueError):
        return {}


WORKTREE_MARK = "/.claude/worktrees/"


def worktree_folder(path: str) -> str:
    """The worktree's folder name when the path is inside one (often the ticket: task+BI-5008-…), else ""."""
    return path.split(WORKTREE_MARK, 1)[1].split("/", 1)[0] if WORKTREE_MARK in path else ""


class BranchLookup:
    """The branch checked out in a session's worktree, asked of git once per folder (and again after a few minutes)."""

    TTL = 300

    def __init__(self, run: Callable = subprocess.run, now: Callable[[], float] = time.time):
        self.run, self.now = run, now
        self.cache: dict[str, tuple[str, float]] = {}

    def __call__(self, path: str) -> str:
        if not worktree_folder(path):
            return ""  # the main checkout's branch says nothing about one session
        hit = self.cache.get(path)
        if hit and self.now() - hit[1] < self.TTL:
            return hit[0]
        try:
            proc = self.run(["git", "-C", path, "branch", "--show-current"], capture_output=True, text=True, timeout=5)
            branch = proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            branch = ""
        self.cache[path] = (branch, self.now())
        return branch


BRANCHES = BranchLookup()


def build_session(row: dict, state: dict, live_branch: str = "") -> Session:
    job_id = str(row.get("id") or state.get("daemonShort") or "")
    cwd = str(row.get("cwd") or state.get("cwd") or "")
    origin = str(state.get("originCwd") or "")
    worktree = str(state.get("worktreePath") or "")
    project, project_path = project_of(origin, cwd, worktree)
    name = str(row.get("name") or state.get("name") or "")
    detail = str(state.get("detail") or "")
    intent = str(state.get("intent") or "")
    branch = str(state.get("worktreeBranch") or "")
    in_flight = state.get("inFlight") if isinstance(state.get("inFlight"), dict) else {}
    still_busy = any(in_flight.get(k) for k in ("tasks", "queued", "drainableMonitors"))
    turn_ended = row.get("status") == "idle" or state.get("tempo") == "idle"
    idle = turn_ended and not still_busy
    started = float(row.get("startedAt") or 0) / 1000 or parse_iso(state.get("createdAt"))
    updated = parse_iso(state.get("updatedAt")) or started
    return Session(
        id=job_id,
        name=name,
        state=str(row.get("state") or state.get("state") or "done"),
        cwd=cwd,
        project=project,
        project_path=project_path,
        branch=branch,
        worktree=os.path.basename(worktree.rstrip("/")) if worktree else "",
        detail=" ".join(detail.split()),
        intent=" ".join(intent.split()),
        tokens=int(state.get("tokens") or 0),
        started_at=started,
        updated_at=updated,
        auto_tags=derive_tags(name, intent, detail, branch, worktree_folder(cwd), live_branch),
        idle=idle,
        waiting_on=background_tasks(state, in_flight) if turn_ended and still_busy else [],
    )


TASK_KINDS = {"local_bash": "a shell command", "shell": "a shell command", "monitor": "a monitor"}


def background_tasks(state: dict, in_flight: dict) -> list[str]:
    """What a session whose turn has ended is still waiting on: the labels Claude Code lists under "fan", else the task kinds."""
    fan = state.get("fan") if isinstance(state.get("fan"), list) else []
    labels = [" ".join(str(t.get("label") or "").split()) for t in fan if isinstance(t, dict)]
    labels = [l for l in labels if l]
    if labels:
        return labels
    kinds = [str(k) for k in in_flight.get("kinds") or []]
    return [TASK_KINDS.get(k, f"a {k.replace('_', ' ')}") for k in kinds] or ["a background task"]


def scan_jobs(jobs_dir: Path) -> list[dict]:
    rows = []
    for state_file in sorted(jobs_dir.glob("*/state.json")):
        state = read_state(jobs_dir, state_file.parent.name)
        rows.append({"id": state_file.parent.name, "state": state.get("state"), "name": state.get("name"),
                     "cwd": state.get("cwd")})
    return rows


def load_sessions(
    run: Callable = subprocess.run, jobs_dir: Path = JOBS_DIR, claude: str = CLAUDE
) -> list[Session]:
    try:
        proc = run([claude, "agents", "--json", "--all"], capture_output=True, text=True, timeout=20)
        rows = json.loads(proc.stdout)
        if not isinstance(rows, list):
            raise ValueError("unexpected output")
    except (OSError, ValueError, subprocess.SubprocessError):
        rows = scan_jobs(jobs_dir)
    return [build_session(row, read_state(jobs_dir, str(row.get("id", ""))), BRANCHES(str(row.get("cwd") or ""))) for row in rows if row.get("id")]


# ───────────────────────────── metadata (renames, tags, pins) ─────────────────────────────


MODEL_PRESETS = [
    ("Default", "", "no --model flag: whatever Claude Code would use"),
    ("Fable", "fable", "alias · the latest Fable"),
    ("Opus", "opus", "alias · the latest Opus"),
    ("Sonnet", "sonnet", "alias · the latest Sonnet"),
    ("Haiku", "haiku", "alias · the latest Haiku"),
    ("Fable 5.1", "claude-fable-5-1", "full model id"),
    ("Opus 5.5", "claude-opus-5-5", "full model id"),
    ("Sonnet 5.5", "claude-sonnet-5-5", "full model id"),
    ("Haiku 5.5", "claude-haiku-5-5", "full model id"),
]
REFRESHING = "refreshing pull requests…"
CONFIRM_KEY = "x"  # ctrl+x asks; x (or ctrl+x again) confirms: stopping, deleting and removing a tag all work this way


def is_confirmation(key: str, ch: str) -> bool:
    return key == "ctrl+x" or (key == "char" and ch.lower() == CONFIRM_KEY)
PASTE_CHIP_MIN_CHARS = 800  # Claude Code collapses a longer paste into a chip: [Pasted text #1 +97 lines]
PASTE_CHIP_RE = re.compile(r"\[Pasted text #(\d+)(?: \+(\d+) lines)?\]")
PR_REFRESH_CHOICES = [0, 5, 10, 15, 30, 60]  # minutes between pull-request refreshes; 0 = only when asked
DEFAULT_PR_REFRESH = 15
REVIEW_AGE_CHOICES = [0, 7, 14, 30, 60, 90, 180, 365]  # the review queue hides PRs opened longer ago than this many days; 0 = keep all
DEFAULT_REVIEW_AGE = 30
DEFAULT_REVIEW_COMMAND = "/code-review:code-review {urls}"
CONFLICT_PROMPT = (
    "Resolve the merge conflicts in {kind} {ref} ({url}): branch `{head}` against its base `{base}`. "
    "Work in a new git worktree so the main checkout stays untouched. Fetch origin, check out `{head}`, merge `origin/{base}` into it "
    "and resolve every conflict, keeping what both sides meant. Run the tests that cover the files you touched, commit the merge "
    "and push the branch: a normal push, never a force push. If a conflict is ambiguous, stop and ask me instead of guessing."
)
DEFAULT_REVIEW_COMMAND_GITLAB = "Review these GitLab merge requests, reading each one and its diff with glab: {urls}"
VIEW_ORDER = ("status", "project", "review", "mine")
PR_VIEWS = ("review", "mine")
VIEW_LABEL = {"status": "by status", "project": "by project", "review": "review queue", "mine": "my pull requests"}
TABS = (("sessions", "Sessions"), ("review", "Review queue"), ("mine", "My PRs"))  # the title row's tabs; Sessions is grouped by status or project
GROUPINGS = ("status", "project")
MINE_GROUPS = [("attention", "Needs attention"), ("ready", "Ready to merge"), ("waiting", "Waiting"), ("draft", "Draft")]
DEFAULT_LIBRARY = ["Bug", "Feature", "Docs", "Plan", "Tests", "Code Review", "Pull Request", "Urgent", "Blocked"]
DEFAULT_RULES = [
    {"tag": "Bug", "pattern": r"\b(bug|fix(es|ed)?|broken|crash(es|ed)?|errors?|exceptions?|regression|fail(s|ed|ing)?)\b"},
    {"tag": "Feature", "pattern": r"\b(add|implement|build|create|introduce|support)\b"},
    {"tag": "Docs", "pattern": r"\b(docs?|documentation|readme|changelog|adr)\b"},
    {"tag": "Plan", "pattern": r"\b(plan|design|architecture|brainstorm|propose|rfc|prd)\b"},
    {"tag": "Tests", "pattern": r"\b(tests?|spec|coverage|phpunit|jest|pytest)\b"},
    {"tag": "Pull Request", "pattern": r"\b(pull requests?|merge requests?|open a pr|create a pr)\b"},
]
MAX_SUGGESTED_TAGS = 3


class Meta:
    """Board-local state: per-session renames/tags/pins, the tag library and the auto-tag rules.

    On disk: {"sessions": {id: {...}}, "library": [labels], "rules": [{"tag", "pattern"}]}.
    A flat {id: {...}} file from an earlier version is read as the sessions part."""

    def __init__(self, path: Path = META_PATH):
        self.path = path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        structured = any(k in data for k in ("sessions", "library", "rules", "settings"))
        sessions = data.get("sessions", {}) if structured else data
        self.sessions: dict[str, dict] = sessions if isinstance(sessions, dict) else {}
        library = data.get("library")
        self.library: list[str] = [str(t) for t in library] if isinstance(library, list) else list(DEFAULT_LIBRARY)
        rules = data.get("rules")
        self.rules: list[dict] = rules if isinstance(rules, list) else [dict(r) for r in DEFAULT_RULES]
        settings = data.get("settings")
        self.settings: dict = settings if isinstance(settings, dict) else {}

    @property
    def data(self) -> dict[str, dict]:
        return self.sessions

    def get(self, job_id: str) -> dict:
        return self.sessions.get(job_id, {})

    def update(self, job_id: str, **changes: object) -> None:
        entry = dict(self.sessions.get(job_id, {}))
        for key, value in changes.items():
            if value in (None, "", [], False):
                entry.pop(key, None)
            else:
                entry[key] = value
        if entry:
            self.sessions[job_id] = entry
        else:
            self.sessions.pop(job_id, None)
        self._save()

    def set_setting(self, key: str, value: object) -> None:
        if value in (None, ""):
            self.settings.pop(key, None)
        else:
            self.settings[key] = value
        self._save()

    # ── tag library ──
    def find_tag(self, label: str) -> Optional[str]:
        return next((t for t in self.library if t.lower() == label.strip().lower()), None)

    def add_tag(self, label: str) -> str:
        label = " ".join(label.split())
        existing = self.find_tag(label)
        if existing:
            return existing
        self.library.append(label)
        self._save()
        return label

    def toggle_tag(self, job_id: str, label: str) -> bool:
        tags = list(self.get(job_id).get("tags", []))
        on = label not in tags
        tags = tags + [label] if on else [t for t in tags if t != label]
        self.update(job_id, tags=tags)
        return on

    def rename_tag(self, old: str, new: str) -> None:
        new = " ".join(new.split())
        if not new or new == old:
            return
        self.library = [new if t == old else t for t in self.library]
        self.library = list(dict.fromkeys(self.library))
        for entry in self.sessions.values():
            if old in entry.get("tags", []):
                entry["tags"] = list(dict.fromkeys(new if t == old else t for t in entry["tags"]))
        for rule in self.rules:
            if rule.get("tag") == old:
                rule["tag"] = new
        self._save()

    def delete_tag(self, label: str) -> None:
        self.library = [t for t in self.library if t != label]
        for job_id in list(self.sessions):
            entry = self.sessions[job_id]
            entry["tags"] = [t for t in entry.get("tags", []) if t != label]
            if not entry["tags"]:
                del entry["tags"]
            if not entry:
                del self.sessions[job_id]
        self.rules = [r for r in self.rules if r.get("tag") != label]
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = {"sessions": self.sessions, "library": self.library, "rules": self.rules, "settings": self.settings}
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            tmp.replace(self.path)
        except OSError:
            pass


def suggest_tags(prompt: str, meta: Meta) -> list[str]:
    """Library tags whose rule matches the prompt, earliest match first; the built-in Code Review /
    Ticket / PR tags are derived from the session's intent anyway and are not repeated here."""
    hits: list[tuple[int, str]] = []
    for rule in meta.rules:
        try:
            m = re.search(str(rule.get("pattern", "")), prompt, re.IGNORECASE)
        except re.error:
            continue
        label = str(rule.get("tag", "")).strip()
        if m and label and label not in [h[1] for h in hits]:
            hits.append((m.start(), label))
    return [label for _, label in sorted(hits)[:MAX_SUGGESTED_TAGS]]


# ───────────────────────────── usage (from local transcripts) ─────────────────────────────

USAGE_DAYS = 14
RIGHT_MARGIN = 3
DOUBLE_CLICK_SECONDS = 0.4  # a second click on the same session row this soon after the first opens it
HEADER_ROWS = 4  # a padding row, the title bar, the pull-request strip and a spacer under it
LIST_ROWS_BESIDE_LIMITS = 6  # a long list keeps at least this many rows when the LIMITS block is pinned under it
LIST_ROWS_BESIDE_USAGE = 12  # ... and this many before the USAGE panel is pinned too
APP_NAME = "Jazi's Agent Board"
APP_MARK = "🦎"


@dataclass(frozen=True)
class Turn:
    ts: float
    model: str
    project: str
    out: int
    inp: int  # input tokens that were not served from cache (fresh + cache writes)
    cache_read: int
    session: str


@dataclass
class UsageSummary:
    periods: dict[str, dict[str, int]]  # today | 7d | 30d | all -> turns, out, inp, cache_read, sessions
    daily: list[int]  # turns per day for the last USAGE_DAYS days, oldest first
    models: list[tuple[str, int]]  # 30 days, output tokens, descending
    projects: list[tuple[str, int]]  # 30 days, turns, descending
    first_day: str = ""


def pretty_model(model: str) -> str:
    return re.sub(r"-\d{8}$", "", model.removeprefix("claude-"))


def parse_turn(line: bytes) -> Optional[tuple[str, Turn]]:
    try:
        d = json.loads(line)
    except ValueError:
        return None
    msg = d.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    model = msg.get("model") if isinstance(msg, dict) else None
    if not isinstance(usage, dict) or not model or model.startswith("<"):
        return None
    key = str(d.get("requestId") or d.get("uuid") or "")
    ts = parse_iso(d.get("timestamp"))
    if not key or not ts:
        return None
    return key, Turn(
        ts=ts,
        model=pretty_model(model),
        project=project_of("", str(d.get("cwd") or ""))[0],
        out=int(usage.get("output_tokens") or 0),
        inp=int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        session=str(d.get("sessionId") or ""),
    )


_CWD_RE = re.compile(r'"cwd":\s*"((?:[^"\\]|\\.)*)"')
_known_cache: dict[str, tuple[float, str]] = {}  # transcript folder -> (newest transcript mtime, directory it was run in)


def _transcript_cwd(folder: Path) -> Optional[tuple[float, str]]:
    """The directory Claude Code was last run in for this transcript folder, read from its newest transcripts."""
    try:
        files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    if not files:
        return None
    mtime = files[0].stat().st_mtime
    cached = _known_cache.get(str(folder))
    if cached and cached[0] == mtime:
        return cached
    for path in files[:3]:
        try:
            with open(path, "rb") as f:
                for _ in range(300):
                    line = f.readline()
                    if not line:
                        break
                    found = _CWD_RE.search(line.decode(errors="ignore"))
                    if found:
                        try:
                            cwd = json.loads(f'"{found.group(1)}"')
                        except ValueError:
                            continue
                        _known_cache[str(folder)] = (mtime, cwd)
                        return mtime, cwd
        except OSError:
            continue
    return None


MENTION_RE = re.compile(r"(?<!\S)@(\S+)")


def mention_name(name: str) -> str:
    """A project's name as it is written after an @: no spaces."""
    return "-".join(name.split())


def match_projects(projects: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    """Projects whose name starts with the query first, then those containing it, then those whose path does."""
    q = query.lower()
    starts = [p for p in projects if mention_name(p[0]).lower().startswith(q)]
    inside = [p for p in projects if p not in starts and q in p[0].lower()]
    by_path = [p for p in projects if p not in starts and p not in inside and q in p[1].lower()]
    return starts + inside + by_path


def _folder_path(folder: Path) -> Optional[str]:
    """The directory a transcript folder is named after, for folders that hold no transcripts (only memory, say).

    The name replaces every / with -, so it is decoded against the disk: the first reading in which each step is a real directory."""
    tokens = [t for t in folder.name.split("-") if t]

    def walk(prefix: str, rest: list[str]) -> Optional[str]:
        if not rest:
            return prefix
        for k in range(1, len(rest) + 1):
            candidate = f"{prefix}/{'-'.join(rest[:k])}"
            if os.path.isdir(candidate):
                found = walk(candidate, rest[k:])
                if found:
                    return found
        return None

    return walk("", tokens) if tokens and folder.name.startswith("-") else None


def known_projects(root: Path = PROJECTS_DIR) -> list[tuple[str, str]]:
    """(name, path) of every directory Claude Code has been used in on this machine, most recently used first.

    Worktrees fold into their repository; directories that no longer exist and hidden ones (.claude/jobs, …) are left out."""
    found: dict[str, float] = {}
    try:
        folders = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []
    for folder in folders:
        hit = _transcript_cwd(folder)
        if not hit:
            decoded = _folder_path(folder)
            try:
                hit = (folder.stat().st_mtime, decoded) if decoded else None
            except OSError:
                hit = None
        if not hit:
            continue
        mtime, cwd = hit
        _, path = project_of("", cwd)
        if any(part.startswith(".") for part in path.split("/") if part) or not os.path.isdir(path):
            continue
        found[path] = max(found.get(path, 0.0), mtime)
    return [(project_of("", path)[0], path) for path, _ in sorted(found.items(), key=lambda kv: -kv[1])]


class UsageScanner:
    """Reads Claude Code transcripts incrementally: only bytes appended since the last scan are parsed."""

    def __init__(self, root: Path = PROJECTS_DIR):
        self.root = root
        self.files: dict[Path, dict] = {}

    def scan(self) -> None:
        for path in self.root.glob("*/*.jsonl"):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            state = self.files.get(path)
            if state is None or state["offset"] > size:
                state = {"offset": 0, "turns": {}}
            if size > state["offset"]:
                try:
                    with open(path, "rb") as f:
                        f.seek(state["offset"])
                        for line in f:
                            if not line.endswith(b"\n"):
                                break  # a record still being written; pick it up next scan
                            state["offset"] += len(line)
                            if b'"usage"' in line and b'"type":"assistant"' in line:
                                parsed = parse_turn(line)
                                if parsed:
                                    state["turns"][parsed[0]] = parsed[1]  # streamed duplicates: the last one wins
                except OSError:
                    continue
            self.files[path] = state

    def turns(self) -> list[Turn]:
        return [t for state in self.files.values() for t in state["turns"].values()]


def summarize(turns: list[Turn], now: float) -> UsageSummary:
    today = datetime.fromtimestamp(now).date()
    spans = {"today": 1, "7d": 7, "30d": 30, "all": 10**6}
    acc = {k: {"turns": 0, "out": 0, "inp": 0, "cache_read": 0} for k in spans}
    sessions: dict[str, set[str]] = {k: set() for k in spans}
    daily = [0] * USAGE_DAYS
    models: dict[str, int] = {}
    projects: dict[str, int] = {}
    first = ""
    for t in turns:
        day = datetime.fromtimestamp(t.ts).date()
        age = (today - day).days
        if age < 0:
            continue
        first = min(first, day.isoformat()) if first else day.isoformat()
        for key, span in spans.items():
            if age < span:
                a = acc[key]
                a["turns"] += 1
                a["out"] += t.out
                a["inp"] += t.inp
                a["cache_read"] += t.cache_read
                sessions[key].add(t.session)
        if age < USAGE_DAYS:
            daily[USAGE_DAYS - 1 - age] += 1
        if age < 30:
            models[t.model] = models.get(t.model, 0) + t.out
            projects[t.project] = projects.get(t.project, 0) + 1
    periods = {k: {**acc[k], "sessions": len(sessions[k])} for k in spans}
    return UsageSummary(
        periods=periods,
        daily=daily,
        models=sorted(models.items(), key=lambda kv: -kv[1]),
        projects=sorted(projects.items(), key=lambda kv: -kv[1]),
        first_day=first,
    )


def fmt_num(n: int) -> str:
    for limit, unit in ((10**9, "B"), (10**6, "M"), (10**3, "k")):
        if n >= limit:
            v = n / limit
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return str(n)


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    top = max(values) if values else 0
    if top <= 0:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(len(SPARK) - 1, round(v / top * (len(SPARK) - 1)))] if v else SPARK[0] for v in values)


def bar(share: float, width: int = 10) -> str:
    filled = max(0, min(width, round(share * width)))
    return "█" * filled + "░" * (width - filled)


def _pad(segs: list[Seg], width: int) -> list[Seg]:
    return segs + [(" " * max(0, width - seg_width(segs)), "normal")]


def usage_panel(u: UsageSummary, width: int, height: int) -> list[Line]:
    """A titled block of up to `height` lines: totals, 14-day activity, models and projects."""
    title: list[Seg] = [("  ", "normal"), ("USAGE", "head"), ("  this machine · from local transcripts ", "faint")]
    title.append(("─" * max(0, width - seg_width(title)), "rule"))

    totals: list[list[Seg]] = [[("", "normal")]]
    head = f"{'':<9}{'turns':>7}{'output':>8}{'sessions':>10}"
    totals = [[(head, "faint")]]
    for label, key in (("Today", "today"), ("7 days", "7d"), ("30 days", "30d"), ("All time", "all")):
        p = u.periods[key]
        totals.append([
            (f"{label:<9}", "dim"), (f"{fmt_num(p['turns']):>7}", "bold"), (f"{fmt_num(p['out']):>8}", "bold"),
            (f"{p['sessions']:>10}", "bold"),
        ])
    totals.append([(f"{'14 days':<9}", "faint"), (sparkline(u.daily), "accent")])
    totals.append([(f"{'cache reads':<12}", "faint"), (fmt_num(u.periods["30d"]["cache_read"]), "dim"), (" tokens, 30 days", "faint")])

    def ranked(title_text: str, items: list[tuple[str, int]], colour: bool, limit: int) -> list[list[Seg]]:
        rows: list[list[Seg]] = [[(title_text, "faint")]]
        total = sum(v for _, v in items) or 1
        for name, value in items[:limit]:
            rows.append([
                (f"{fit(name, 16):<17}", project_style(name) if colour else "name"), (bar(value / total), "work"),
                (f" {'<1' if 0 < value / total < 0.005 else round(value / total * 100):>3}%", "dim"),
            ])
        if not items:
            rows.append([("no activity", "faint")])
        return rows

    columns = [totals, ranked("Models · 30d output", u.models, False, 5), ranked("Projects · 30d turns", u.projects, True, 6)]
    widths = [38, 35, 35]
    if width < 112:
        columns, widths = columns[:2], widths[:2]
    if width < 76:
        columns, widths = columns[:1], widths[:1]
    rows = max(len(c) for c in columns)
    body: list[Line] = []
    for i in range(rows):
        segs: list[Seg] = [("  ", "normal")]
        for column, w in zip(columns, widths, strict=False):
            segs += _pad(column[i] if i < len(column) else [], w)
        body.append(Line(segs))
    return ([Line(title)] + body)[:height]


# ───────────────────────────── account limits (fed by the agent-board-limits mod) ─────────────────────────────

LIMIT_NAMES = {"five_hour": "5-hour", "seven_day": "Weekly", "spend_limit": "Spend"}
STALE_AFTER_SECONDS = 30 * 60


@dataclass(frozen=True)
class LimitWindow:
    kind: str
    percent: float
    resets_at: float  # epoch seconds; 0 when unknown


@dataclass(frozen=True)
class Limits:
    windows: list[LimitWindow]
    updated_at: float


def read_limits(path: Path = LIMITS_PATH) -> Optional[Limits]:
    """The last reading a Claude Code session wrote, or None when there is none yet."""
    try:
        data = json.loads(path.read_text())
        windows = [
            LimitWindow(str(w["kind"]), float(w["percentUsed"]), parse_iso(w.get("resetsAt")))
            for w in data["windows"]
        ]
        return Limits(windows, float(data["updatedAt"]) / 1000) if windows else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def fmt_until(seconds: float) -> str:
    s = int(seconds)
    if s >= 86400:
        return f"{s // 86400}d {s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{max(1, s // 60)}m"


def limit_style(percent: float) -> str:
    return "done" if percent < 60 else "wait" if percent < 85 else "fail"


def limits_panel(limits: Optional[Limits], width: int, now: float, disk: Optional[DiskInfo] = None) -> list[Line]:
    title: list[Seg] = [("  ", "normal"), ("LIMITS", "head")]
    disk_lines = disk_rows(disk, width) if disk and disk.disks else []
    if limits is None:
        title.append(("─" * max(0, width - seg_width(title) - 1), "rule"))
        note = "no reading yet: it appears after your next turn in a session that loads the agent-board-limits mod (see README)"
        return [Line(title), Line([("  ", "normal"), (fit(note, max(10, width - 4)), "faint")])] + disk_lines
    age = now - limits.updated_at
    stale = age > STALE_AFTER_SECONDS
    title.append((f"  as of {fmt_until(age)} ago ", "wait" if stale else "faint"))
    title.append(("─" * max(0, width - seg_width(title)), "rule"))
    rows = [Line(title)]
    bar_width = 20 if width >= 70 else 10
    for w in limits.windows:
        expired = 0 < w.resets_at <= now
        pct = f"{w.percent:>3.0f}%" if w.percent >= 1 or w.percent == 0 else " <1%"
        segs: list[Seg] = [
            ("  ", "normal"), (f"{LIMIT_NAMES.get(w.kind, w.kind.replace('_', ' ').title()):<9}", "dim"),
            (bar(min(1.0, w.percent / 100), bar_width), "faint" if expired else limit_style(w.percent)),
            (f" {pct}  ", "faint" if expired else "bold"),
        ]
        if expired:
            segs.append(("window has reset since — waiting for a newer reading", "faint"))
        elif w.resets_at:
            segs.append((f"resets in {fmt_until(w.resets_at - now)}", "dim"))
            segs.append((f"  ({datetime.fromtimestamp(w.resets_at).strftime('%a %H:%M')})", "faint"))
        rows.append(Line(segs))
    return rows + disk_lines


# ───────────────────────────── disk (a row in the LIMITS block) ─────────────────────────────

DISK_REFRESH_SECONDS = 30
DISK_SIZES_REFRESH_SECONDS = 300  # measuring Claude Code's data and the worktrees walks many files


@dataclass(frozen=True)
class DiskUsage:
    label: str
    total: int
    used: int
    free: int

    @property
    def percent(self) -> float:
        in_use = self.used + self.free
        return self.used / in_use * 100 if in_use else 0.0


@dataclass(frozen=True)
class DiskInfo:
    disks: list[DiskUsage]
    claude_bytes: Optional[int] = None  # ~/.claude: jobs, transcripts, plugins; None until measured
    worktree_bytes: Optional[int] = None  # the git worktrees sessions work in


def fmt_bytes(n: int) -> str:
    for unit, size in (("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= size:
            v = n / size
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return f"{n}B"


def disk_style(percent: float) -> str:
    """Green until 15% is free, yellow below that, red below 5%."""
    return "done" if percent < 85 else "wait" if percent < 95 else "fail"


def mount_point(path: str) -> str:
    p = os.path.realpath(path)
    while not os.path.ismount(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)
    return p


def read_disks(paths: list[str], usage: Callable = shutil.disk_usage, mount_of: Callable[[str], str] = mount_point) -> list[DiskUsage]:
    """One entry per distinct filesystem the paths live on, the first path's (your home) first."""
    mounts: dict[str, None] = {}
    for p in paths:
        try:
            mounts.setdefault(mount_of(p))
        except OSError:
            continue
    out = []
    for mount in mounts:
        try:
            u = usage(mount)
        except OSError:
            continue
        out.append(DiskUsage("Disk" if len(mounts) == 1 else f"Disk {mount}", u.total, u.used, u.free))
    return out


def dir_size(path: str) -> int:
    """Disk space a folder takes, counted like `du`: allocated blocks, hard links once, symlinks not followed."""
    total, seen, stack = 0, set(), [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_blocks * 512
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif st.st_nlink > 1:
                        if (st.st_dev, st.st_ino) in seen:
                            total -= st.st_blocks * 512
                        seen.add((st.st_dev, st.st_ino))
        except OSError:
            continue
    return total


def disk_rows(disk: DiskInfo, width: int) -> list[Line]:
    bar_width = 20 if width >= 70 else 10 if width >= 46 else 5
    rows = []
    for i, d in enumerate(disk.disks):
        pct = d.percent
        head: list[Seg] = [
            ("  ", "normal"), (f"{fit(d.label, 9):<9}", "dim"), (bar(pct / 100, bar_width), disk_style(pct)), (f" {pct:>3.0f}%  ", "bold"),
        ]
        room = width - seg_width(head) - 1
        free = f"{fmt_bytes(d.free)} free of {fmt_bytes(d.total)}"
        segs = head + ([(fit(free, room), "dim")] if room >= 6 else [])
        if pct >= 95 and width - seg_width(segs) > 16:
            segs.append(("  ⚠ nearly full", "fail"))
        if i == 0 and disk.claude_bytes is not None:
            extra = f"  ·  Claude data {fmt_bytes(disk.claude_bytes)}" + (f"  ·  worktrees {fmt_bytes(disk.worktree_bytes)}" if disk.worktree_bytes is not None else "")
            left = width - seg_width(segs) - 1
            if left > 8:
                segs.append((fit(extra, left), "faint"))
        rows.append(Line(segs))
    return rows


# ───────────────────────────── text helpers ─────────────────────────────


def cell_width(text: str) -> int:
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = cell_width(ch)
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


Seg = tuple[str, str]  # (text, style)


@dataclass
class Line:
    segs: list[Seg]
    selected: bool = False
    caret: Optional[int] = None  # column of the text cursor on this row, when it is being edited in place


def seg_width(segs: list[Seg]) -> int:
    return sum(cell_width(t) for t, _ in segs)


def plain(line: Line) -> str:
    return "".join(t for t, _ in line.segs)


def tag_style(tag: Tag) -> str:
    if tag.kind == "custom":
        return f"tag_c{int(hashlib.md5(tag.label.lower().encode()).hexdigest(), 16) % 6}"
    return f"tag_{tag.kind}"


def project_style(name: str) -> str:
    return f"proj{int(hashlib.md5(name.encode()).hexdigest(), 16) % 6}"


# ───────────────────────────── board state ─────────────────────────────


@dataclass
class Effect:
    kind: str  # attach | quit | dispatch | stop | remove | peek
    session_id: str = ""
    prompt: str = ""
    cwd: str = ""
    tags: list[str] = field(default_factory=list)
    url: str = ""
    items: list = field(default_factory=list)
    text: str = ""
    cut: bool = False
    model: str = ""


HELP_COMMON_END = [
    ("alt+← / alt+→", "the tab to the left / right: Sessions, Review queue, My PRs (a click on a tab works too)"),
    ("ctrl+s", "next tab, round and round"),
    ("ctrl+l", "refresh pull requests now"),
    ("ctrl+o", "settings: PR refresh interval, drafts, review queue age, review command"),
]
# What the "?" overlay lists, per view: only keys that do something in the view you are looking at.
HELP_BY_VIEW: dict[str, list[tuple[str, str]]] = {
    "sessions": [
        ("↑ / ↓", "select a session (stops at the first and last; esc returns to the prompt)"),
        ("home / end", "jump to the first / last session"),
        ("alt+↑ / alt+↓", "jump between groups"),
        ("alt+1 … alt+6", "open the Nth visible session"),
        ("enter / → / double-click", "open the selected session (a draft in the prompt is kept)"),
        ("space", "peek at the session's recent output"),
        ("ctrl+r", "rename the session right in its row, typing replaces the selected old name (kept by the board only)"),
        ("ctrl+g", "tags: pick, create, rename, delete (ctrl+r / ctrl+x inside)"),
        ("ctrl+t", "pin the session to the top"),
        ("ctrl+x", "stop a working or idle session / delete a finished one (asks first: x or ctrl+x again confirms)"),
        ("type, then enter", "start a new background session; it is selected, so → opens it"),
        ("@", "type @ for your projects: tab or enter completes @name, and the session starts in that project"),
        ("/", "slash commands: type / for the list, ↑/↓ choose, tab or enter completes it"),
        ("← / → / click", "move the caret in the prompt; ctrl+← / ctrl+→ by word, home / end to the ends"),
        ("delete / ctrl+w", "delete the character after the caret / the word before it (alt+backspace too)"),
        ("ctrl+v / ctrl+j", "paste from the clipboard / new line; long pastes become [Pasted text] chips"),
        ("ctrl+x / ctrl+y", "with a prompt typed and no session selected: cut / copy the prompt"),
        ("ctrl+p", "choose the project a new session starts in: every project you have used Claude Code in is listed; ctrl+t inside pins one"),
        ("ctrl+e", "choose the model new sessions use (alias or any model id)"),
        ("ctrl+f", "find a session"),
        ("ctrl+b", "group the sessions by status or by project (remembered)"),
        *HELP_COMMON_END,
        ("ctrl+c", "clear the typed prompt, or close a popup; never quits"),
        ("esc", "leave the list, then clear the prompt or search, then quit"),
        ("mouse", "click selects a session, double-click opens it; drag selects text and copies it"),
    ],
    "review": [
        ("↑ / ↓", "move between pull requests"),
        ("alt+↑ / alt+↓", "jump between projects"),
        ("space", "mark / unmark the pull request, then move on"),
        ("ctrl+a", "mark or unmark every pull request in the view"),
        ("r", "review the marked PRs (or the current one): one session per project"),
        ("a", "approve the pull request on GitHub / GitLab (asks first: a or enter); approved, it leaves the queue"),
        ("enter / click", "jump to a session already on that PR (⟳), else open it in the browser"),
        ("o", "open the pull request in the browser"),
        ("ctrl+f", "filter pull requests"),
        *HELP_COMMON_END,
        ("esc", "clear the marks or the filter, then back to the session list"),
        ("mouse", "click opens a pull request; drag selects text and copies it"),
    ],
    "mine": [
        ("↑ / ↓", "move between your pull requests"),
        ("alt+↑ / alt+↓", "jump between groups"),
        ("enter / click", "jump to a session already on that PR (⟳), else open it in the browser"),
        ("o", "open the pull request in the browser"),
        ("f", "fix conflicts: a background session merges the base into the branch, resolves them and pushes"),
        ("ctrl+f", "filter pull requests"),
        *HELP_COMMON_END,
        ("esc", "clear the filter, then back to the session list"),
        ("mouse", "click opens a pull request; drag selects text and copies it"),
    ],
}


def help_rows(mode: str) -> list[tuple[str, str]]:
    return HELP_BY_VIEW["sessions" if mode in ("status", "project") else mode]


def model_label(value: str) -> str:
    """Short name for the prompt line: an alias as it is, a full id without its claude- prefix."""
    return value.removeprefix("claude-") if value else "default model"


def bg_command(prompt: str, model: str = "") -> list[str]:
    """Arguments for `claude` that start a background session, on the chosen model when there is one."""
    return ["--bg", *(["--model", model] if model else []), prompt]


def order_stack(items: list) -> list[tuple[object, int]]:
    """Parents first, each followed by the PRs stacked on it: [(pr, depth within this list)]."""
    present = {(p.repo, p.number) for p in items}
    roots = sorted((p for p in items if p.parent is None or (p.repo, p.parent) not in present), key=lambda p: (p.repo, -p.updated_at))
    out: list[tuple[object, int]] = []
    seen: set[str] = set()

    def add(p, depth: int) -> None:
        out.append((p, depth))
        seen.add(p.key)
        for c in sorted((c for c in items if c.repo == p.repo and c.parent == p.number and c.key not in seen), key=lambda c: c.number):
            add(c, depth + 1)

    for r in roots:
        add(r, 0)
    return out


class Board:
    def __init__(self, meta: Meta, launch_cwd: str, now: Callable[[], float] = time.time):
        self.meta = meta
        self.now = now
        self.sessions: list[Session] = []
        self.launch_cwd = launch_cwd
        self.mode = self.grouping  # status | project (the Sessions tab, grouped that way) | review | mine
        self.input = ""
        self.prompt_hit: Optional[tuple[int, int, list[tuple[int, int, int]]]] = None  # (first row, first column, [(row, column, index)]) of the drawn prompt, for clicks
        self.pastes: dict[int, str] = {}  # the full text behind each [Pasted text #N] chip in the prompt
        self.paste_seq = 0
        self.query = ""
        self.edit: Optional[dict] = None  # {'kind': find|rename|tags, 'text': str, 'sid': str}
        self.confirm: Optional[dict] = None  # {'kind': stop|remove, 'sid': str}
        self.sel: Optional[str] = None
        self.scroll = 0
        self.msg = ""
        self.help = False
        self.peek: Optional[dict] = None  # {'title': str, 'lines': [str], 'top': int}
        self.target_path: Optional[str] = None
        self.picker: Optional[dict] = None  # {'text': str, 'idx': int}
        self.modelpicker: Optional[dict] = None  # {'text': str, 'idx': int}
        self.tagpicker: Optional[dict] = None  # {'sid', 'text', 'idx', 'rename': old label|None, 'delete': label|None}
        self.pending: list[dict] = []  # new sessions whose suggested tags wait for the session to show up
        self.hit: dict[int, str] = {}  # screen row -> session id, filled by compose
        self.tab_hit: list[tuple[int, int, int, str]] = []  # (row, first column, end column, tab) of each drawn tab
        self.tick = 0
        self.usage: Optional[UsageSummary] = None
        self.limits: Optional[Limits] = None
        self.disk: Optional[DiskInfo] = None
        self.prs: Optional[prs.PrData] = None
        self.pr_busy = False
        self.pr_sel: Optional[str] = None
        self.marked: set[str] = set()
        self.settingsui: Optional[dict] = None  # {'idx': int, 'edit': {'field','text'} | None}
        self.refresh_requested = threading.Event()
        self.attached = False  # a session has the terminal; the session list waits until the board is back
        self.sessions_due = threading.Event()  # set to refresh the session list now instead of on the next tick
        self.sync_open = False  # a synchronized repaint was begun after a session and must be ended
        self.queued_effect: Optional[Effect] = None  # an action a mouse click asked for, run by the main loop
        self.last_click: Optional[tuple[str, float]] = None  # (session id, when) of the last click on a session row
        self.busy: dict[str, str] = {}  # session id -> "removing…" / "stopping…" while that runs in the background
        self.pr_busy_keys: dict[str, str] = {}  # pull request key -> "approving…" while that runs in the background
        self.approved: set[str] = set()  # pull requests approved from the board: kept out of the queue even if a refresh still lists them
        self.slash_catalog = slash.Catalog()
        self.slash_state: dict = {"query": None, "idx": 0}
        self.workers: list[threading.Thread] = []
        self.select_when_seen: Optional[tuple[str, float]] = None  # (id, when asked): a session just started from the prompt
        self.known_projects_fn: Optional[Callable[[], list[tuple[str, str]]]] = None  # set by the app: directories Claude Code has been used in
        self.known: list[tuple[str, str]] = []
        self.known_at = -1e12
        self.mention_state: dict = {"query": None, "idx": 0}
        self.successors: dict[str, Optional[str]] = {}  # session being deleted -> the row to highlight once it is gone
        self.placeholders: list[Session] = []  # rows for sessions still being started, until the real ones show up
        self.placeholder_seq = 0

    # ── data ──
    def choose_successor(self, sid: str) -> Optional[str]:
        """The row that follows `sid` in the list (else the one before it), skipping rows that are going away too."""
        ids = [s.id for s in self.flat()]
        if sid not in ids:
            return None
        i = ids.index(sid)
        for other in ids[i + 1 :] + ids[:i][::-1]:
            if other not in self.busy and other not in self.successors:
                return other
        return None

    def start_placeholder(self, prompt: str, cwd: str) -> str:
        """Puts a "starting…" row on the list at once, selected, so the prompt feels answered while `claude --bg` runs."""
        self.placeholder_seq += 1
        sid = f"starting-{self.placeholder_seq}"
        project, path = project_of("", cwd)
        now = self.now()
        self.placeholders.append(Session(id=sid, name=" ".join(prompt.split())[:60], state="working", cwd=cwd, project=project, project_path=path, started_at=now, updated_at=now))
        self.busy[sid] = "starting…"
        self.sessions = self.sessions + self.placeholders[-1:]
        self.sel = sid
        return sid

    def finish_placeholder(self, sid: str, real_id: Optional[str] = None) -> None:
        """Swaps the "starting…" row for the real session: the selection moves over if it was still on the row."""
        self.placeholders = [p for p in self.placeholders if p.id != sid]
        self.busy.pop(sid, None)
        self.sessions = [x for x in self.sessions if x.id != sid]
        if self.sel == sid:
            self.sel = real_id if real_id and any(x.id == real_id for x in self.sessions) else None

    def set_sessions(self, sessions: list[Session]) -> None:
        self._keep_tags(sessions)
        self.sessions = sessions + self.placeholders
        ids = {s.id for s in self.sessions}
        if self.sel and self.sel not in ids:
            heir = self.successors.get(self.sel)  # a deleted session hands the highlight to the row after it
            self.sel = heir if heir in ids else None
        for gone in [sid for sid in self.successors if sid not in ids]:
            del self.successors[gone]
        if self.select_when_seen:
            wanted, asked_at = self.select_when_seen
            new = next((s for s in sessions if s.id == wanted), None)
            if new or self.now() - asked_at > 60:
                if new and self.sel is None and self.matches(new):  # only while you are still on the prompt
                    self.sel = wanted
                self.select_when_seen = None
        self._resolve_pending()

    def _keep_tags(self, sessions: list[Session]) -> None:
        """A ticket or PR a session once showed stays on it, even after its summary line moves on (remembered in meta)."""
        changed = False
        for s in sessions:
            entry = self.meta.sessions.get(s.id, {})
            seen = [remembered_tag(str(label), str(kind)) for label, kind in entry.get("seen", []) if kind in TAG_LIMITS]
            s.auto_tags = merge_tags(seen, s.auto_tags)
            labels = [[t.label, t.kind] for t in s.auto_tags]
            if labels and labels != entry.get("seen"):
                self.meta.sessions[s.id] = {**entry, "seen": labels}
                changed = True
        if changed:
            self.meta._save()

    def expect_session(self, prompt: str, cwd: str, tags: list[str]) -> None:
        if tags:
            self.pending.append({"prompt": " ".join(prompt.split())[:50], "path": project_of("", cwd)[1], "tags": tags, "at": self.now()})

    def _resolve_pending(self) -> None:
        keep = []
        for p in self.pending:
            match = next(
                (s for s in self.sessions if s.project_path == p["path"] and s.intent.startswith(p["prompt"]) and s.started_at >= p["at"] - 10),
                None,
            )
            if match:
                self.meta.update(match.id, tags=list(dict.fromkeys(self.meta.get(match.id).get("tags", []) + p["tags"])))
                if self.sel is None and self.matches(match):
                    self.sel = match.id
            elif self.now() - p["at"] < 120:
                keep.append(p)
        self.pending = keep

    def name_of(self, s: Session) -> str:
        return self.meta.get(s.id).get("name") or s.name or s.intent[:48] or s.id

    def tags_of(self, s: Session) -> list[Tag]:
        tags = list(s.auto_tags)
        have = {t.label for t in tags}
        for label in self.meta.get(s.id).get("tags", []):
            if label not in have:
                tags.append(Tag(label, "custom"))
        return tags

    def tag_on(self, s: Session, label: str) -> Optional[Tag]:
        """The tag this session shows under that name (derived or manual), compared case-insensitively."""
        return next((t for t in self.tags_of(s) if t.label.lower() == label.lower()), None)

    def pinned(self, s: Session) -> bool:
        return bool(self.meta.get(s.id).get("pinned"))

    def projects(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        launch = project_of("", self.launch_cwd)
        seen[launch[1]] = launch[0]
        for s in sorted(self.sessions, key=lambda x: -x.updated_at):
            seen.setdefault(s.project_path, s.project)
        return [(name, path) for path, name in seen.items()]

    def target(self) -> tuple[str, str]:
        if self.target_path:
            return os.path.basename(self.target_path.rstrip("/")) or "/", self.target_path
        return project_of("", self.launch_cwd)

    def picker_items(self) -> list[tuple[str, str, int, int]]:
        """(name, path, sessions, working) for each project matching the picker's filter text."""
        counts: dict[str, list[int]] = {}
        for s in self.sessions:
            c = counts.setdefault(s.project_path, [0, 0])
            c[0] += 1
            c[1] += s.group == "working"
        text = self.picker["text"].strip() if self.picker else ""
        listed = self.all_projects()
        items = [
            (name, path, *counts.get(path, [0, 0]))
            for name, path in listed
            if text.lower() in name.lower() or text.lower() in path.lower()
        ]
        if text[:1] in ("/", "~"):
            custom = os.path.expanduser(text).rstrip("/") or "/"
            if os.path.isdir(custom) and custom not in {i[1] for i in items}:
                items.append((os.path.basename(custom) or "/", custom, *counts.get(custom, [0, 0])))
        return items

    def model(self) -> str:
        return str(self.meta.settings.get("model", ""))

    def model_items(self) -> list[tuple[str, str, str]]:
        """(label, value, note): the presets, models seen in recent usage, and the typed text as a custom id."""
        items = list(MODEL_PRESETS)
        known = {v for _, v, _ in items}
        for name, _ in (self.usage.models if self.usage else []):
            value = f"claude-{name}"
            if value not in known and name not in known:
                items.append((name, value, "used recently on this machine"))
                known.add(value)
        text = self.modelpicker["text"].strip() if self.modelpicker else ""
        shown = [i for i in items if text.lower() in i[0].lower() or text.lower() in i[1].lower()]
        if text and text not in {v for _, v, _ in items}:
            shown.append((text, text, "use this id as typed"))
        return shown

    def open_modelpicker(self) -> None:
        values = [v for _, v, _ in self.model_items()]
        self.modelpicker = {"text": "", "idx": values.index(self.model()) if self.model() in values else 0}

    def _handle_modelpicker(self, key: str, ch: str) -> None:
        p = self.modelpicker
        items = self.model_items()
        if key in ("esc", "ctrl+e"):
            self.modelpicker = None
        elif key == "up":
            p["idx"] = max(0, p["idx"] - 1)
        elif key == "down":
            p["idx"] = min(max(0, len(items) - 1), p["idx"] + 1)
        elif key == "enter":
            if items:
                label, value, _ = items[min(p["idx"], len(items) - 1)]
                self.meta.set_setting("model", value or None)
                self.msg = f"new sessions will use {model_label(value)}"
            self.modelpicker = None
        elif key == "backspace":
            p["text"], p["idx"] = p["text"][:-1], 0
        elif key == "ctrl+u":
            p["text"], p["idx"] = "", 0
        elif key in ("char", "space"):
            p["text"], p["idx"] = p["text"] + (ch or " "), 0
        return None

    def pinned_projects(self) -> list[str]:
        value = self.meta.settings.get("projects")
        return [str(p) for p in value] if isinstance(value, list) else []

    def toggle_pinned_project(self, path: str, name: str) -> None:
        pins = self.pinned_projects()
        if path in pins:
            pins.remove(path)
            self.msg = f"unpinned {name}"
        else:
            pins.append(path)
            self.msg = f"pinned {name}: it stays in this list"
        self.meta.set_setting("projects", pins or None)

    def open_picker(self) -> None:
        self.refresh_known(0)
        paths = [i[1] for i in self.picker_items()]
        current = self.target()[1]
        self.picker = {"text": "", "idx": paths.index(current) if current in paths else 0}

    def tagpicker_items(self) -> list[tuple[str, str]]:
        """('tag', label) for each matching library tag, plus ('create', text) when the filter is new."""
        tp = self.tagpicker
        if not tp or tp["rename"] is not None:
            return []
        text = " ".join(tp["text"].split())
        items = [("tag", t) for t in self.meta.library if text.lower() in t.lower()]
        session = next((s for s in self.sessions if s.id == tp["sid"]), None)
        derived = {t.label.lower() for t in session.auto_tags} if session else set()
        if text and not self.meta.find_tag(text) and text.lower() not in derived:
            items.append(("create", text))
        return items

    def _handle_tagpicker(self, key: str, ch: str) -> None:
        tp = self.tagpicker
        if tp["delete"] is not None:
            label, tp["delete"] = tp["delete"], None
            if is_confirmation(key, ch):
                self.meta.delete_tag(label)
                tp["idx"] = 0
                self.msg = f"deleted tag “{label}”"
            else:
                self.msg = "cancelled"
            return None
        typing = key in ("char", "space") or key in ("backspace", "ctrl+u")
        if tp["rename"] is not None:
            if key == "esc":
                tp["rename"], tp["text"] = None, ""
            elif key == "enter":
                old, new = tp["rename"], tp["text"].strip()
                tp["rename"], tp["text"] = None, ""
                if new and new != old:
                    self.meta.rename_tag(old, new)
                    self.msg = f"renamed “{old}” → “{new}”"
        elif key in ("esc", "ctrl+g"):
            self.tagpicker = None
        elif key == "up":
            tp["idx"] = max(0, tp["idx"] - 1)
        elif key == "down":
            tp["idx"] = min(max(0, len(self.tagpicker_items()) - 1), tp["idx"] + 1)
        elif key == "enter":
            items = self.tagpicker_items()
            if items:
                kind, label = items[min(tp["idx"], len(items) - 1)]
                session = next((x for x in self.sessions if x.id == tp["sid"]), None)
                shown = self.tag_on(session, label) if session and kind == "tag" else None
                if shown and shown.kind != "custom":
                    self.msg = f"“{shown.label}” comes from the session itself, so it can't be toggled"
                    return None
                if kind == "create":
                    label = self.meta.add_tag(label)
                    tp["text"] = ""
                self.meta.toggle_tag(tp["sid"], label)
                tp["idx"] = self.meta.library.index(label) if kind == "create" else tp["idx"]
        elif key in ("ctrl+r", "ctrl+x"):
            items = self.tagpicker_items()
            if items and items[min(tp["idx"], len(items) - 1)][0] == "tag":
                label = items[min(tp["idx"], len(items) - 1)][1]
                if key == "ctrl+r":
                    tp["rename"], tp["text"] = label, label
                else:
                    tp["delete"] = label
        if typing and (tp["rename"] is not None or self.tagpicker):
            if key == "backspace":
                tp["text"] = tp["text"][:-1]
            elif key == "ctrl+u":
                tp["text"] = ""
            elif key in ("char", "space"):
                tp["text"] += ch or " "
            if tp["rename"] is None:
                tp["idx"] = 0
        return None

    def _handle_picker(self, key: str, ch: str) -> None:
        p = self.picker
        items = self.picker_items()
        if key in ("esc", "ctrl+p"):
            self.picker = None
        elif key == "up":
            p["idx"] = max(0, p["idx"] - 1)
        elif key == "down":
            p["idx"] = min(max(0, len(items) - 1), p["idx"] + 1)
        elif key == "enter":
            if items:
                name, path, _, _ = items[min(p["idx"], len(items) - 1)]
                self.target_path = path
                self.msg = f"new sessions start in {name}"
            self.picker = None
        elif key == "backspace":
            p["text"], p["idx"] = p["text"][:-1], 0
        elif key == "ctrl+t":
            if items:
                name, path, _, _ = items[min(p["idx"], len(items) - 1)]
                self.toggle_pinned_project(path, name)
        elif key == "ctrl+u":
            p["text"], p["idx"] = "", 0
        elif key in ("char", "space"):
            p["text"], p["idx"] = p["text"] + (ch or " "), 0
        return None

    def matches(self, s: Session) -> bool:
        q = self.query.strip().lower()
        if not q:
            return True
        hay = " ".join(
            [self.name_of(s), s.project, s.branch, s.detail, s.id] + [t.label for t in self.tags_of(s)]
        ).lower()
        return all(part in hay for part in q.split())

    def groups(self) -> list[tuple[str, str, list[Session]]]:
        visible = [s for s in self.sessions if self.matches(s)]

        def order(s: Session):
            return (not self.pinned(s), -s.updated_at)

        if self.mode == "project":
            by_project: dict[str, list[Session]] = {}
            for s in visible:
                by_project.setdefault(s.project_path, []).append(s)
            result = []
            for path, items in by_project.items():
                items.sort(key=lambda s: (GROUP_ORDER.index(s.group) if not self.pinned(s) else -1, -s.updated_at))
                result.append((items[0].project, path, items))
            result.sort(key=lambda g: -max(s.updated_at for s in g[2]))
            return result
        result = []
        for key in GROUP_ORDER:
            items = sorted((s for s in visible if s.group == key), key=order)
            if items:
                result.append((GROUP_TITLE[key], "finished its turn · still open, waiting for you" if key == "idle" else "", items))
        return result

    def flat(self) -> list[Session]:
        return [s for _, _, items in self.groups() for s in items]

    def counts(self) -> dict[str, int]:
        c = {k: 0 for k in GROUP_ORDER}
        for s in self.sessions:
            c[s.group] += 1
        return c

    # ── slash commands ──
    def slash_query(self) -> Optional[str]:
        """What follows the slash while the first word of the prompt is being typed; None when no list should show."""
        if self._lists_blocked():
            return None
        token = self.slash_token()
        return None if token is None else token[1:]

    def _lists_blocked(self) -> bool:
        """The slash and @ lists belong to the prompt: they stay shut while anything else has the focus."""
        blocked = self.sel is not None or self.mode in PR_VIEWS or self.edit or self.picker or self.modelpicker or self.tagpicker
        return bool(blocked or self.settingsui or self.confirm or self.help or self.peek)

    def _trailing_word(self, sign: str) -> Optional[str]:
        """The word being typed at the caret when it starts with `sign`: `/cod` in "Create a mod /cod". None once a space follows it."""
        text = self.input
        if not text or text[-1].isspace() or self.cursor != len(text):
            return None
        word = text.split()[-1]
        return word if word.startswith(sign) else None

    def slash_token(self) -> Optional[str]:
        return self._trailing_word("/")

    def slash_view(self) -> tuple[list, int]:
        """The commands matching what is typed, and the highlighted one (reset when the text changes)."""
        query = self.slash_query()
        matches = [] if query is None else slash.match(self.slash_catalog.entries(self.target()[1]), query)
        if self.slash_state["query"] != query:
            self.slash_state = {"query": query, "idx": 0}
        self.slash_state["idx"] = max(0, min(self.slash_state["idx"], len(matches) - 1))
        return matches, self.slash_state["idx"]

    def _slash_key(self, key: str) -> tuple[bool, Optional[Effect]]:
        """(consumed, effect): arrows move through the list, tab or enter put the highlighted command in the prompt."""
        matches, idx = self.slash_view()
        if not matches or key not in ("up", "down", "tab", "enter"):
            return False, None
        token = self.slash_token() or ""
        before = self.input[: len(self.input) - len(token)]
        if key == "up":
            self.slash_state["idx"] = max(0, idx - 1)
        elif key == "down":
            self.slash_state["idx"] = min(len(matches) - 1, idx + 1)
        else:
            chosen = matches[idx]
            if key == "enter" and token.lower() == f"/{chosen.name.lower()}":
                return False, None  # it is already typed out in full: enter starts the session
            self.input = f"{before}/{chosen.name} "
        return True, None

    # ── @project mentions ──
    def all_projects(self) -> list[tuple[str, str]]:
        """Projects with sessions, then your pins, then everywhere you have used Claude Code: (name, path)."""
        listed = self.projects()
        have = {p for _, p in listed}
        for path in self.pinned_projects() + [p for _, p in self.known]:
            if path not in have and os.path.isdir(path):
                listed.append((project_of("", path)[0], path))
                have.add(path)
        return listed

    def refresh_known(self, max_age: float = 30.0) -> None:
        if not self.known_projects_fn or self.now() - self.known_at < max_age:
            return
        self.known_at = self.now()
        try:
            self.known = self.known_projects_fn()
        except Exception:  # the extra projects are a convenience; everything works without them
            self.known = []

    def mention_query(self) -> Optional[str]:
        """What follows the @ while a mention is being typed; None when no list should show."""
        if self._lists_blocked():
            return None
        token = self._trailing_word("@")
        return None if token is None else token[1:]

    def mention_view(self) -> tuple[list[tuple[str, str, int, int]], int]:
        """The projects matching what follows the @ ((name, path, sessions, working)), and the highlighted one."""
        query = self.mention_query()
        matches: list[tuple[str, str, int, int]] = []
        if query is not None:
            self.refresh_known()
            counts: dict[str, list[int]] = {}
            for s in self.sessions:
                c = counts.setdefault(s.project_path, [0, 0])
                c[0] += 1
                c[1] += s.group == "working"
            matches = [(n, p, *counts.get(p, [0, 0])) for n, p in match_projects(self.all_projects(), query)]
        if self.mention_state["query"] != query:
            self.mention_state = {"query": query, "idx": 0}
        self.mention_state["idx"] = max(0, min(self.mention_state["idx"], len(matches) - 1))
        return matches, self.mention_state["idx"]

    def _mention_key(self, key: str) -> bool:
        """Arrows move through the project list, tab or enter put the highlighted project in the prompt as @name."""
        matches, idx = self.mention_view()
        if not matches or key not in ("up", "down", "tab", "enter"):
            return False
        token = self._trailing_word("@") or ""
        if key == "up":
            self.mention_state["idx"] = max(0, idx - 1)
        elif key == "down":
            self.mention_state["idx"] = min(len(matches) - 1, idx + 1)
        else:
            name = mention_name(matches[idx][0])
            if key == "enter" and token.lower() == f"@{name.lower()}":
                return False  # it is already typed out in full: enter starts the session
            self.input = f"{self.input[: len(self.input) - len(token)]}@{name} "
        return True

    def resolve_mention(self, text: str) -> tuple[str, Optional[tuple[str, str]]]:
        """(text to send, (name, path)) for the first @word naming a known project; others stay as typed.

        A mention that opens the prompt is dropped, it only chose the project. Anywhere else it is part of the
        sentence, so its name stays in the text and only the @ is removed."""
        known = {mention_name(n).lower(): (n, p) for n, p in reversed(self.all_projects())}
        for m in MENTION_RE.finditer(text):
            hit = known.get(m.group(1).lower())
            if hit:
                if text[: m.start()].strip():  # a word in the sentence ("add it to my @web-app"): the name stays, the @ goes
                    return text[: m.start()] + m.group(1) + text[m.end() :], hit
                end = m.end() + (1 if text[m.end() : m.end() + 1] in (" ", "\t") else 0)
                return text[: m.start()] + text[end:], hit  # leading: it only picked the project
        return text, None

    def start_target(self) -> tuple[str, str]:
        """Where the session being typed would start: the project it @mentions, else the chosen one."""
        mentioned = self.resolve_mention(self.input)[1] if "@" in self.input else None
        return mentioned or self.target()

    # ── pull requests ──
    def pr_interval(self) -> int:
        """Seconds between refreshes; 0 means only when asked (ctrl+l)."""
        minutes = self.meta.settings.get("pr_refresh_minutes", DEFAULT_PR_REFRESH)
        return int(minutes) * 60 if minutes in PR_REFRESH_CHOICES else DEFAULT_PR_REFRESH * 60

    def pr_project(self, pr) -> tuple[str, str]:
        path = (self.prs.repos.get(pr.repo_key) if self.prs else None) or ""
        return (os.path.basename(path.rstrip("/")) or pr.repo.split("/")[-1], path)

    def linked_sessions(self, pr) -> list[Session]:
        path = self.pr_project(pr)[1]
        label = f"PR #{pr.number}"
        return [s for s in self.sessions if path and s.project_path == path and any(t.kind == "pr" and t.label == label for t in s.auto_tags)]

    def _matches_pr(self, pr) -> bool:
        q = self.query.strip().lower()
        if not q:
            return True
        hay = " ".join([pr.title, pr.repo, getattr(pr, "author", ""), getattr(pr, "ticket", ""), self.pr_project(pr)[0], f"#{pr.number}"]).lower()
        return all(part in hay for part in q.split())

    def pr_groups(self) -> list[tuple[str, str, list[tuple[object, int]]]]:
        """(title, subtitle, [(pr, nesting depth)]) for the current pull-request view."""
        if not self.prs:
            return []
        if self.mode == "review":
            by_project: dict[str, list] = {}
            for path in self.prs.repos.values():
                by_project.setdefault(path, [])
            for p in self.review_prs():
                if self._matches_pr(p):
                    by_project.setdefault(self.pr_project(p)[1], []).append(p)
            groups = []
            for path, items in by_project.items():
                remote = next((k for k, v in self.prs.repos.items() if v == path), "")
                provider, _, repo = remote.partition(":")
                groups.append((os.path.basename(path.rstrip("/")) or path, f"{provider} · {repo}" if remote else "", [(p, 0) for p in sorted(items, key=lambda p: p.created_at)]))
            return sorted(groups, key=lambda g: (-len(g[2]), g[0]))
        groups = []
        mine = [p for p in self.prs.mine if self._matches_pr(p)]
        for key, title in MINE_GROUPS:
            items = [p for p in mine if p.group == key]
            if items:
                groups.append((title, "", order_stack(items)))
        return groups

    def review_age_days(self) -> int:
        days = self.meta.settings.get("review_max_age_days", DEFAULT_REVIEW_AGE)
        return days if days in REVIEW_AGE_CHOICES else DEFAULT_REVIEW_AGE

    def review_prs(self) -> list:
        """The review queue without PRs opened longer ago than the setting allows (ctrl+o)."""
        if not self.prs:
            return []
        days = self.review_age_days()
        if not days:
            return list(self.prs.review)
        cutoff = self.now() - days * 86400
        return [p for p in self.prs.review if not p.created_at or p.created_at >= cutoff]

    def hidden_by_age(self) -> int:
        return len(self.prs.review) - len(self.review_prs()) if self.prs else 0

    def without_approved(self, data: "prs.PrData") -> "prs.PrData":
        data.review = [p for p in data.review if p.key not in self.approved]
        return data

    def pr_flat(self) -> list:
        return [p for _, _, items in self.pr_groups() for p, _ in items]

    def pr_counts(self) -> dict:
        data = self.prs
        if not data:
            return {"review": 0, "attention": 0, "ready": 0, "reasons": {}}
        reasons: dict[str, int] = {}
        for p in data.mine:
            for r in p.attention if p.group == "attention" else []:
                reasons[r] = reasons.get(r, 0) + 1
        return {
            "review": len(self.review_prs()),
            "attention": sum(1 for p in data.mine if p.group == "attention"),
            "ready": sum(1 for p in data.mine if p.group == "ready"),
            "reasons": reasons,
        }

    def _move_pr(self, delta: int) -> None:
        flat = self.pr_flat()
        keys = [p.key for p in flat]
        if not keys:
            self.pr_sel = None
            return
        if self.pr_sel not in keys:
            self.pr_sel = keys[0] if delta > 0 else keys[-1]
            return
        self.pr_sel = keys[max(0, min(len(keys) - 1, keys.index(self.pr_sel) + delta))]

    def _jump_pr_group(self, delta: int) -> None:
        groups = [g for g in self.pr_groups() if g[2]]
        if not groups:
            return
        firsts = [g[2][0][0].key for g in groups]
        cur = next((i for i, g in enumerate(groups) if any(p.key == self.pr_sel for p, _ in g[2])), -1)
        self.pr_sel = firsts[max(0, min(len(firsts) - 1, cur + delta))]

    def selected_pr(self):
        return next((p for p in self.pr_flat() if p.key == self.pr_sel), None)

    @property
    def grouping(self) -> str:
        """How the Sessions tab groups its sessions: "status" or "project", remembered between runs."""
        value = self.meta.settings.get("grouping")
        return value if value in GROUPINGS else "status"

    @property
    def tab(self) -> str:
        return self.mode if self.mode in PR_VIEWS else "sessions"

    def set_tab(self, tab: str) -> None:
        self.mode = self.grouping if tab == "sessions" else tab
        self.scroll = 0

    def move_tab(self, delta: int) -> None:
        """alt+← / alt+→: the tab beside this one; stops at the first and the last."""
        names = [t for t, _ in TABS]
        self.set_tab(names[max(0, min(len(names) - 1, names.index(self.tab) + delta))])

    def cycle_view(self) -> None:
        names = [t for t, _ in TABS]
        self.set_tab(names[(names.index(self.tab) + 1) % len(names)])

    def toggle_grouping(self) -> None:
        if self.mode in PR_VIEWS:
            return
        self.meta.set_setting("grouping", "project" if self.grouping == "status" else None)
        self.mode = self.grouping
        self.scroll = 0

    def settings_fields(self) -> list[tuple[str, str]]:
        return [("refresh", "PR refresh"), ("drafts", "Include drafts"), ("age", "Review queue age"), ("review_cmd", "Review command"), ("review_cmd_gl", "GitLab review prompt")]

    def setting_text(self, name: str) -> str:
        s = self.meta.settings
        if name == "review_cmd":
            return s.get("review_command", DEFAULT_REVIEW_COMMAND)
        if name == "review_cmd_gl":
            return s.get("review_command_gitlab", DEFAULT_REVIEW_COMMAND_GITLAB)
        return ""

    def _commit_setting(self, name: str, text: str) -> None:
        text = text.strip()
        if name == "review_cmd":
            self.meta.set_setting("review_command", None if text in ("", DEFAULT_REVIEW_COMMAND) else text)
        elif name == "review_cmd_gl":
            self.meta.set_setting("review_command_gitlab", None if text in ("", DEFAULT_REVIEW_COMMAND_GITLAB) else text)

    def _handle_settings(self, key: str, ch: str) -> None:
        ui = self.settingsui
        fields = self.settings_fields()
        ui["idx"] = min(ui["idx"], len(fields) - 1)
        name = fields[ui["idx"]][0]
        edit = ui["edit"]
        if edit is not None:
            if key == "esc":
                ui["edit"] = None
            elif key == "enter":
                self._commit_setting(edit["field"], edit["text"])
                ui["edit"] = None
            elif key == "backspace":
                edit["text"] = edit["text"][:-1]
            elif key == "ctrl+u":
                edit["text"] = ""
            elif key in ("char", "space"):
                edit["text"] += ch or " "
            return None
        if key in ("esc", "ctrl+o"):
            self.settingsui = None
        elif key == "up":
            ui["idx"] = max(0, ui["idx"] - 1)
        elif key == "down":
            ui["idx"] = min(len(fields) - 1, ui["idx"] + 1)
        elif name == "refresh" and key in ("left", "right", "enter", "space"):
            cur = self.meta.settings.get("pr_refresh_minutes", DEFAULT_PR_REFRESH)
            i = PR_REFRESH_CHOICES.index(cur) if cur in PR_REFRESH_CHOICES else PR_REFRESH_CHOICES.index(DEFAULT_PR_REFRESH)
            step = -1 if key == "left" else 1
            self.meta.set_setting("pr_refresh_minutes", PR_REFRESH_CHOICES[(i + step) % len(PR_REFRESH_CHOICES)])
        elif name == "drafts" and key in ("enter", "space", "left", "right"):
            self.meta.set_setting("include_drafts", not self.meta.settings.get("include_drafts", False) or None)
            self.refresh_requested.set()
        elif name == "age" and key in ("left", "right", "enter", "space"):
            i = REVIEW_AGE_CHOICES.index(self.review_age_days())
            days = REVIEW_AGE_CHOICES[(i + (-1 if key == "left" else 1)) % len(REVIEW_AGE_CHOICES)]
            self.meta.set_setting("review_max_age_days", None if days == DEFAULT_REVIEW_AGE else days)
        elif key == "enter" and name not in ("refresh", "drafts", "age"):
            ui["edit"] = {"field": name, "text": self.setting_text(name)}
        return None

    def _handle_pr_view(self, key: str, ch: str) -> Optional[Effect]:
        p = self.selected_pr()
        if key == "esc":
            if self.marked:
                self.marked.clear()
            elif self.query:
                self.query = ""
            else:
                self.set_tab("sessions")
        elif key == "up":
            self._move_pr(-1)
        elif key == "down":
            self._move_pr(1)
        elif key == "alt+up":
            self._jump_pr_group(-1)
        elif key == "alt+down":
            self._jump_pr_group(1)
        elif key == "space" and self.mode == "review" and p:
            self.marked ^= {p.key}
            self._move_pr(1)
        elif key == "ctrl+a" and self.mode == "review":
            keys = {x.key for x in self.pr_flat()}
            self.marked = set() if keys and keys <= self.marked else keys
        elif key == "char" and ch == "a" and self.mode == "review" and p:
            if p.key in self.pr_busy_keys:
                self.msg = f"#{p.number} is {self.pr_busy_keys[p.key]}"
            else:
                self.confirm = {"kind": "approve", "pr": p.key}
        elif key == "char" and ch == "r" and self.mode == "review":
            chosen = [x for x in self.pr_flat() if x.key in self.marked] or ([p] if p else [])
            if chosen:
                return Effect("review", items=chosen)
            self.msg = "nothing selected: move onto a PR or mark some with space"
        elif key == "char" and ch == "f" and self.mode == "mine" and p:
            if p.merge == "conflict":
                return Effect("fix_conflicts", items=[p])
            self.msg = f"{pr_ref(p)} has no conflicts to fix"
        elif key == "char" and ch == "o" and p:
            return Effect("browse", url=p.url)
        elif key in ("enter", "right") and p:
            effect = self._enter_pr(p)
            if key == "enter" or effect.kind == "attach":
                return effect
        elif key == "ctrl+l":
            return Effect("refresh_prs")
        elif key == "ctrl+o":
            self.settingsui = {"idx": 0, "edit": None}
        elif key == "ctrl+f":
            self.edit = {"kind": "find", "text": self.query, "sid": "", "cursor": len(self.query)}
        elif key == "char" and ch == "?":
            self.help = True
        elif key == "char" and ch == "r":
            self.msg = "r reviews marked PRs in the review queue (ctrl+s)"
        return None

    # ── input ──
    def selected(self) -> Optional[Session]:
        return next((s for s in self.sessions if s.id == self.sel), None)

    def _move(self, delta: int) -> None:
        flat = self.flat()
        if not flat:
            self.sel = None
            return
        ids = [s.id for s in flat]
        if self.sel not in ids:
            self.sel = ids[0] if delta > 0 else ids[-1]
            return
        i = ids.index(self.sel) + delta
        self.sel = ids[max(0, min(i, len(ids) - 1))]  # stops at either end; esc goes back to the prompt

    def _enter_pr(self, pr) -> Effect:
        """Enter on a pull request: jump to a session already working on it, else open it in the browser."""
        linked = self.linked_sessions(pr)
        if linked:
            linked.sort(key=lambda s: (s.group != "working", -s.updated_at))
            return Effect("attach", session_id=linked[0].id)
        return Effect("browse", url=pr.url)

    def click(self, y: int, x: int = -1) -> None:
        """A click on a pull request opens it, like Enter would; one on a session only selects it, a double-click opens it.
        A click in the prompt moves its caret, one on a tab opens it."""
        prev, self.last_click = self.last_click, None
        tab = next((t for ty, x0, x1, t in self.tab_hit if y == ty and x0 <= x < x1), None)
        if tab:
            self.set_tab(tab)
            return
        owner = self.hit.get(y)
        if not owner:
            if x >= 0:
                self.click_prompt(y, x)
            return
        if owner in self.busy:
            self.msg = f"{self.busy[owner]}"
            return
        if self.mode in PR_VIEWS:
            self.pr_sel = owner
            pr = self.selected_pr()
            if pr:
                self.queued_effect = self._enter_pr(pr)
        else:
            self.sel = owner
            now = self.now()
            if prev and prev[0] == owner and now - prev[1] <= DOUBLE_CLICK_SECONDS:
                self.queued_effect = Effect("attach", session_id=owner)
            else:
                self.last_click = (owner, now)

    def _jump_group(self, delta: int) -> None:
        groups = self.groups()
        if not groups:
            return
        firsts = [items[0].id for _, _, items in groups]
        current = next((gi for gi, (_, _, items) in enumerate(groups) if any(s.id == self.sel for s in items)), -1)
        self.sel = firsts[max(0, min(len(firsts) - 1, current + delta))]

    def handle(self, key: str, ch: str = "") -> Optional[Effect]:
        self.msg = "" if key != "tick" else self.msg
        if key == "tick":
            self.tick += 1
            return None
        if key == "ctrl+c":
            # never leaves the board (esc does): it cancels whatever is open, else clears the prompt
            modal = self.confirm or self.help or self.peek or self.picker or self.modelpicker or self.tagpicker or self.settingsui or self.edit
            if modal:
                return self.handle("esc")
            if self.input and self.mode not in PR_VIEWS:
                self.reset_prompt()
                self.msg = "prompt cleared"
            else:
                self.msg = "ctrl+c does not close the board · esc quits"
            return None
        if key == "paste":
            return self._handle_paste(ch)
        if self.confirm:
            return self._handle_confirm(key, ch)
        if self.help:
            self.help = False
            return None
        if self.settingsui:
            return self._handle_settings(key, ch)
        if self.picker:
            return self._handle_picker(key, ch)
        if self.modelpicker:
            return self._handle_modelpicker(key, ch)
        if self.tagpicker:
            return self._handle_tagpicker(key, ch)
        if self.peek:
            return self._handle_peek(key)
        if self.edit:
            return self._handle_edit(key, ch)
        return self._handle_main(key, ch)

    def add_paste(self, text: str) -> str:
        """Stores a long paste and returns the chip that stands for it in the prompt, as Claude Code shows it."""
        self.paste_seq += 1
        newlines = len(re.findall(r"\r\n|\r|\n", text))
        self.pastes[self.paste_seq] = text
        return f"[Pasted text #{self.paste_seq}" + (f" +{newlines} lines" if newlines else "") + "]"

    @property
    def input(self) -> str:
        return self._input

    @input.setter
    def input(self, value: str) -> None:
        self._input = value
        self.cursor = len(value)  # replacing the prompt wholesale leaves the caret at its end

    def _put(self, value: str, cursor: int) -> None:
        self._input, self.cursor = value, cursor

    def _insert(self, text: str) -> None:
        c = self.cursor
        self._put((self._input[:c] + text + self._input[c:])[:20000], c + len(text))

    def _forget_chips(self, removed: str) -> None:
        for m in PASTE_CHIP_RE.finditer(removed):
            self.pastes.pop(int(m.group(1)), None)

    def _delete_range(self, start: int, end: int) -> None:
        self._forget_chips(self._input[start:end])
        self._put(self._input[:start] + self._input[end:], start)

    def _prompt_key(self, key: str) -> bool:
        """Caret movement and deletion in the prompt, as in Claude Code. A pasted-text chip counts as one character."""
        toks = prompt_tokens(self._input)
        c = self.cursor
        before = [t for t in toks if t[1] <= c]
        after = [t for t in toks if t[0] >= c]
        if key in ("left", "right", "home", "end"):
            if key == "left" and before:
                self.cursor = before[-1][0]
            elif key == "right" and after:
                self.cursor = after[0][1]
            elif key == "home":
                self.cursor = 0
            elif key == "end":
                self.cursor = len(self._input)
        elif key in ("ctrl+left", "alt+left"):
            self.cursor = self._word_start(before)
        elif key in ("ctrl+right", "alt+right"):
            self.cursor = self._word_end(after)
        elif key == "delete" and after:
            self._delete_range(*after[0])
        elif key in ("ctrl+w", "alt+backspace"):
            self._delete_range(self._word_start(before), c)
        elif key == "ctrl+delete":
            self._delete_range(c, self._word_end(after))
        else:
            return False
        return True

    def _is_space(self, tok: tuple[int, int]) -> bool:
        return self._input[tok[0] : tok[1]].isspace()

    def _word_start(self, before: list[tuple[int, int]]) -> int:
        i = len(before)
        while i > 0 and self._is_space(before[i - 1]):
            i -= 1
        while i > 0 and not self._is_space(before[i - 1]):
            i -= 1
        return before[i][0] if i < len(before) else self.cursor

    def _word_end(self, after: list[tuple[int, int]]) -> int:
        i = 0
        while i < len(after) and self._is_space(after[i]):
            i += 1
        while i < len(after) and not self._is_space(after[i]):
            i += 1
        return after[i - 1][1] if i else self.cursor

    def click_prompt(self, y: int, x: int) -> None:
        """A click in the prompt puts the caret at the nearest character boundary of the row clicked."""
        hit = self.prompt_hit
        row = [b for b in hit[2] if b[0] == y - hit[0]] if hit else []
        if not row or self.mode in PR_VIEWS:
            return
        self.sel = None
        self.cursor = min(row, key=lambda b: abs(hit[1] + b[1] - x))[2]

    def prompt_text(self, text: Optional[str] = None) -> str:
        """The prompt as it will be sent: every chip replaced by the text it stands for."""
        return PASTE_CHIP_RE.sub(lambda m: self.pastes.get(int(m.group(1)), m.group(0)), self.input if text is None else text)

    def reset_prompt(self) -> None:
        self.input = ""
        self.pastes.clear()
        self.paste_seq = 0

    def _handle_paste(self, text: str) -> None:
        """Pasted text goes wherever typing would: single-line fields flatten it, the prompt keeps its lines."""
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        text = "".join(c for c in text if c == "\n" or c >= " ")[:500000]
        flat = " ".join(text.split())
        if self.confirm or self.help or self.peek:
            return None
        if self.settingsui:
            if self.settingsui["edit"] is not None:
                self.settingsui["edit"]["text"] += flat
        elif self.picker:
            self.picker["text"] += flat
            self.picker["idx"] = 0
        elif self.modelpicker:
            self.modelpicker["text"] += flat
            self.modelpicker["idx"] = 0
        elif self.tagpicker:
            self.tagpicker["text"] += flat
            if self.tagpicker["rename"] is None:
                self.tagpicker["idx"] = 0
        elif self.edit:
            e = self.edit
            if e.pop("all", False):  # a paste replaces the selected old name, as typing does
                e["text"], e["cursor"] = "", 0
            c = e.get("cursor", len(e["text"]))
            e["text"], e["cursor"] = e["text"][:c] + flat + e["text"][c:], c + len(flat)
            if self.edit["kind"] == "find":
                self.query = self.edit["text"]
        elif self.mode in PR_VIEWS:
            self.msg = "paste goes into the prompt: switch to the status or project view (ctrl+s)"
        elif text:
            self.sel = None
            if len(text) > PASTE_CHIP_MIN_CHARS:
                self._insert(self.add_paste(text))
            else:
                self._insert(text)
        return None

    def _handle_confirm(self, key: str, ch: str) -> Optional[Effect]:
        c, self.confirm = self.confirm, None
        if c["kind"] == "approve":
            pr = next((x for x in self.pr_flat() if x.key == c["pr"]), None)
            if pr and (key == "enter" or (key == "char" and ch.lower() == "a")):
                return Effect("approve", items=[pr])
            self.msg = "cancelled"
            return None
        if is_confirmation(key, ch):
            return Effect(c["kind"], session_id=c["sid"])
        self.msg = "cancelled"
        return None

    def _handle_peek(self, key: str) -> None:
        p = self.peek
        if key == "up":
            p["top"] = max(0, p["top"] - 1)
        elif key == "down":
            p["top"] += 1
        elif key in ("esc", "space", "enter", "left") or key == "char":
            self.peek = None
        return None

    def _handle_edit(self, key: str, ch: str) -> Optional[Effect]:
        e = self.edit
        whole = e.pop("all", False)  # a rename opens with the old name selected; the first key replaces it or lets go of it
        if key == "esc":
            if e["kind"] == "find":
                self.query = ""
            self.edit = None
        elif key == "enter":
            self._commit_edit()
        elif whole and key in ("left", "home", "ctrl+left", "alt+left"):
            e["cursor"] = 0
        elif whole and key in ("right", "end", "ctrl+right", "alt+right"):
            e["cursor"] = len(e["text"])
        elif whole and key in ("backspace", "delete", "ctrl+w", "alt+backspace", "ctrl+delete", "ctrl+u"):
            e["text"], e["cursor"] = "", 0
        else:
            if whole and key in ("char", "space"):
                e["text"], e["cursor"] = "", 0
            edited = edit_line(e["text"], e.get("cursor", len(e["text"])), key, ch)
            if edited:
                e["text"], e["cursor"] = edited
                if e["kind"] == "find":
                    self.query = e["text"]
        return None

    def _commit_edit(self) -> None:
        e, self.edit = self.edit, None
        text = e["text"].strip()
        if e["kind"] == "rename":
            self.meta.update(e["sid"], name=text or None)
            self.msg = "renamed" if text else "name reset"
        elif e["kind"] == "find":
            self.query = text

    def _handle_main(self, key: str, ch: str) -> Optional[Effect]:
        if key == "ctrl+s":
            self.cycle_view()
            return None
        if key in ("alt+left", "alt+right"):  # in the prompt too: ctrl+← / ctrl+→ still move the caret by word
            self.move_tab(-1 if key == "alt+left" else 1)
            return None
        if key == "ctrl+b":
            self.toggle_grouping()
            return None
        if self.mode in PR_VIEWS:
            return self._handle_pr_view(key, ch)
        consumed, effect = self._slash_key(key)
        if consumed:
            return effect
        if self._mention_key(key):
            return None
        s = self.selected()
        if s is None and self._prompt_key(key):
            return None
        if key == "esc":
            if s:
                self.sel = None  # back to the prompt; a draft there is kept
            elif self.input:
                self.reset_prompt()
            elif self.query:
                self.query = ""
            else:
                return Effect("quit")
        elif key == "up":
            self._move(-1)
        elif key == "down":
            self._move(1)
        elif key in ("home", "end") and s and self.flat():  # in the prompt they move the caret instead
            self.sel = self.flat()[0 if key == "home" else -1].id
        elif key == "alt+up":
            self._jump_group(-1)
        elif key == "alt+down":
            self._jump_group(1)
        elif key.startswith("alt+") and key[4:].isdigit():
            flat = self.flat()
            n = int(key[4:]) - 1
            if 0 <= n < len(flat) and flat[n].id not in self.busy:
                return Effect("attach", session_id=flat[n].id)
        elif key in ("enter", "right", "space", "ctrl+x") and s and s.id in self.busy:
            self.msg = f"“{self.name_of(s)}” is {self.busy[s.id]}"
        elif key == "enter":
            if s:  # a selected session has the focus, whatever draft is waiting in the prompt
                return Effect("attach", session_id=s.id)
            if self.input.strip():
                typed, mentioned = self.resolve_mention(self.input)
                if mentioned and not typed.strip():
                    self.msg = f"write the task after @{mention_name(mentioned[0])}"
                    return None
                prompt = self.prompt_text(typed).strip()
                cwd = mentioned[1] if mentioned else self.target()[1]
                self.reset_prompt()
                tags = [self.meta.add_tag(t) for t in suggest_tags(prompt, self.meta)]
                return Effect("dispatch", prompt=prompt, cwd=cwd, tags=tags, model=self.model())
        elif key == "right" and s:
            return Effect("attach", session_id=s.id)
        elif key == "space" and s:
            return Effect("peek", session_id=s.id)
        elif key in ("ctrl+x", "ctrl+y") and not s and self.input:
            text = self.prompt_text()
            if key == "ctrl+x":
                self.reset_prompt()
            return Effect("copy", text=text, cut=key == "ctrl+x")
        elif key in ("ctrl+r", "ctrl+g", "ctrl+t", "ctrl+x") and not s:
            self.msg = "select a session first (↓)"
        elif key == "ctrl+r" and s:
            self.edit = {"kind": "rename", "text": self.name_of(s), "sid": s.id, "cursor": len(self.name_of(s)), "all": True}
        elif key == "ctrl+g" and s:
            self.tagpicker = {"sid": s.id, "text": "", "idx": 0, "rename": None, "delete": None}
        elif key == "ctrl+t" and s:
            self.meta.update(s.id, pinned=not self.pinned(s))
        elif key == "ctrl+x" and s:
            self.confirm = {"kind": "stop" if s.group in ("working", "idle") else "remove", "sid": s.id}
        elif key == "ctrl+l":
            return Effect("refresh_prs")
        elif key == "ctrl+o":
            self.settingsui = {"idx": 0, "edit": None}
        elif key == "ctrl+f":
            self.edit = {"kind": "find", "text": self.query, "sid": "", "cursor": len(self.query)}
        elif key == "ctrl+p":
            self.open_picker()
        elif key == "ctrl+e":
            self.open_modelpicker()
        elif key == "ctrl+u":
            self.sel = None
            self.reset_prompt()
        elif key == "ctrl+j":
            self.sel = None
            self._insert("\n")
        elif key == "backspace":
            self.sel = None
            before = [t for t in prompt_tokens(self._input) if t[1] <= self.cursor]
            if before:  # a pasted block goes in one backspace, as in Claude Code
                self._delete_range(before[-1][0], self.cursor)
        elif key == "char" and ch == "?" and not self.input:
            self.help = True
        elif key in ("char", "space"):
            self.sel = None
            self._insert(ch or " ")
        return None


# ───────────────────────────── rendering ─────────────────────────────


def row_lines(board: Board, s: Session, width: int, selected: bool) -> list[Line]:
    now = board.now()
    icon_style, icon = {
        "working": ("work", SPINNER[board.tick % len(SPINNER)]),
        "awaiting": ("wait", "◆"),
        "idle": ("dim", "○"),
        "done": ("fail", "✗") if s.state in FAILED else ("done", "●"),
    }[s.group]
    if s.group == "working" and s.waiting_on:  # its turn is over; only a background task keeps it going, so nothing spins
        icon_style, icon = "work", WAITING_MARK
    busy = board.busy.get(s.id)
    if busy:  # being stopped or deleted right now: a spinner and a label, so it is clear that something is happening
        icon_style, icon = "wait", SPINNER[board.tick % len(SPINNER)]
    pin = [("★ ", "accent")] if board.pinned(s) else []
    age = fmt_age(now - (s.started_at if s.group == "working" else s.updated_at)) if (s.started_at or s.updated_at) else ""
    project_chip: list[Seg] = [("● ", project_style(s.project)), (s.project, "dim")]
    right: list[Seg] = project_chip + [("  ", "normal"), (f"{age:>4}", "dim")]
    prefix: list[Seg] = [("▌" if selected else " ", "accent"), (" ", "normal"), (icon, icon_style), ("  ", "normal")] + pin
    chips: list[list[Seg]] = [[(f" {t.label} ", tag_style(t))] for t in board.tags_of(s)]
    if busy:
        chips = [[(f" {busy} ", "chip_warn")]]
    name = board.name_of(s)
    renaming = board.edit if (board.edit and board.edit["kind"] == "rename" and board.edit["sid"] == s.id) else None
    caret_x: Optional[int] = None

    def total(n: str, cs: list[list[Seg]]) -> int:
        return seg_width(prefix) + cell_width(n) + sum(1 + seg_width(c) for c in cs) + 2 + seg_width(right)

    if renaming:  # the name is edited right here in the list, so your eyes stay on the row
        room = max(8, width - seg_width(prefix) - 2 - seg_width(right) - 1)
        shown, col, _ = prompt_window(renaming["text"], renaming.get("cursor", len(renaming["text"])), room, "edit_sel" if renaming.get("all") else "edit")
        left: list[Seg] = prefix + shown
        caret_x = seg_width(prefix) + col
    else:
        while chips and total(name, chips) > width:
            chips.pop()
        if total(name, chips) > width:
            name = fit(name, max(8, width - seg_width(prefix) - 2 - seg_width(right)))
        left = prefix + [(name, "dim" if busy else "name_sel" if selected else "name")]
        for c in chips:
            left += [(" ", "normal")] + c
    gap = max(1, width - seg_width(left) - seg_width(right))
    first = Line(left + [(" " * gap, "normal")] + right, selected, caret_x)

    branch = [(f"⎇ {s.branch}", "faint")] if s.branch else []
    indent = [("▌" if selected else " ", "accent"), ("    ", "normal")]
    avail = width - seg_width(indent) - (cell_width(branch[0][0]) + 2 if branch else 0) - 1
    text = s.detail or "—"
    if s.group == "working" and s.waiting_on:
        tasks = s.waiting_on
        text = (f"waiting on {tasks[0]}" if len(tasks) == 1 else f"waiting on {len(tasks)} background tasks: {', '.join(tasks)}") + (f" · {s.detail}" if s.detail else "")
    detail = fit(f"{busy} this can take a few seconds" if busy else text, max(10, avail))
    second_left = indent + [(detail, "dim")]
    gap = max(1, width - seg_width(second_left) - (cell_width(branch[0][0]) if branch else 0) - 1)
    second = Line(second_left + ([(" " * gap, "normal")] + branch if branch else []), selected)
    return [first, second]


def group_header(title: str, subtitle: str, count: int, width: int) -> Line:
    segs: list[Seg] = [("  ", "normal"), (title, "head"), (f"  {count}", "faint")]
    if subtitle:
        segs.append((f"   {fit(subtitle, max(10, width - cell_width(title) - 12))}", "faint"))
    return Line(segs)


PR_FLAG_STYLE = {
    "conflicts": "chip_bad", "changes requested": "chip_bad", "checks failing": "chip_bad", "unresolved threads": "chip_warn",
    "draft": "chip_info", "nobody asked": "chip_warn", "behind base": "chip_warn", "checks running": "chip_info",
    "re-requested": "chip_warn", "teammate-changes-requested": "chip_bad", "teammate-approved": "chip_good",
}


def pr_ref(pr) -> str:
    return f"{'!' if pr.provider == 'gitlab' else '#'}{pr.number}"


def pr_row(board: Board, pr, depth: int, width: int, selected: bool) -> Line:
    now = board.now()
    review = isinstance(pr, prs.ReviewPr)
    linked = board.linked_sessions(pr)
    marked = pr.key in board.marked
    if review:
        lead: list[Seg] = [("▌" if selected else " ", "accent"), (" ", "normal"), ("[x]" if marked else "[ ]", "done" if marked else "faint"), ("  ", "normal")]
        flags = [f for f in pr.flags if f != "new"] + (["draft"] if pr.draft else [])
        age = fmt_age(now - pr.created_at) if pr.created_at else ""
        right: list[Seg] = [(f"@{pr.author}" if pr.author else "", "dim"), (f"  {age:>4}", "dim")]
    else:
        icon, style = {"attention": ("✗", "fail"), "ready": ("✓", "done"), "waiting": ("○", "dim"), "draft": ("◌", "faint")}[pr.group]
        nest = "└ " * min(depth, 1) if depth else ""
        lead = [("▌" if selected else " ", "accent"), (" ", "normal"), (icon, style), ("  ", "normal"), ("  " * max(0, depth - 1) + nest, "faint")]
        flags = pr.attention + pr.notes
        project, _ = board.pr_project(pr)
        age = fmt_age(now - pr.updated_at) if pr.updated_at else ""
        right = [("● ", project_style(project)), (project, "dim"), (f"  {age:>4}", "dim")]
    if linked:
        right.append((f"  ⟳{len(linked)}" if len(linked) > 1 else "  ⟳", "accent"))
    chips: list[list[Seg]] = [[(f" {f} ", PR_FLAG_STYLE.get(f, "chip_info"))] for f in flags]
    if pr.key in board.pr_busy_keys:
        chips.insert(0, [(f" {board.pr_busy_keys[pr.key]} ", "chip_warn")])
    ref = pr_ref(pr)
    base = lead + [(ref, "accent"), ("  ", "normal")]
    title = pr.title

    def total(t: str, cs: list[list[Seg]]) -> int:
        return seg_width(base) + cell_width(t) + sum(1 + seg_width(c) for c in cs) + 2 + seg_width(right)

    while chips and total(title, chips) > width:
        chips.pop()
    if total(title, chips) > width:
        title = fit(title, max(8, width - seg_width(base) - 2 - seg_width(right)))
    left = base + [(title, "name_sel" if selected else "name")]
    for c in chips:
        left += [(" ", "normal")] + c
    gap = max(1, width - seg_width(left) - seg_width(right))
    return Line(left + [(" " * gap, "normal")] + right, selected)


def pr_body(board: Board, width: int) -> tuple[list[Line], list[Optional[str]], Optional[tuple[int, int]]]:
    data = board.prs
    body: list[Line] = []
    owners: list[Optional[str]] = []
    sel_range: Optional[tuple[int, int]] = None
    if data is None:
        text = "   Loading pull requests…" if board.pr_busy else "   Pull requests have not been loaded yet — ctrl+l fetches them"
        return [Line([]), Line([(text, "faint")])], [None, None], None
    for gi, (title, subtitle, items) in enumerate(board.pr_groups()):
        if gi:
            body.append(Line([]))
            owners.append(None)
        head: list[Seg] = [("  ", "normal")]
        if board.mode == "review":
            head += [("● ", project_style(title)), (title, "head"), (f"  {len(items)}", "faint"), (f"   {subtitle}", "faint")]
        else:
            head += [(title, "head"), (f"  {len(items)}", "faint")]
        body.append(Line(head))
        owners.append(None)
        if not items:
            body.append(Line([("     ", "normal"), ("nothing waiting on you ✓", "faint")]))
            owners.append(None)
        for pr, depth in items:
            selected = pr.key == board.pr_sel
            if selected:
                sel_range = (len(body), len(body) + 1)
            body.append(pr_row(board, pr, depth, width, selected))
            owners.append(pr.key)
    if not body:
        what = "to review" if board.mode == "review" else "open"
        body = [Line([]), Line([(f"   No pull requests {what} in these projects" + (" match your search" if board.query else ""), "faint")])]
        owners = [None, None]
    problems = [Line([("  ", "normal"), ("⚠ " + fit(e, max(10, width - 6)), "wait")]) for e in data.errors[:3]]
    if problems:
        body += [Line([])] + problems
        owners += [None] * (len(problems) + 1)
    return body, owners, sel_range


def strip_segs(board: Board, width: int) -> list[Seg]:
    """The always-visible pull-request line: brighter than the faint chrome, since it carries the numbers you act on."""
    data = board.prs
    if data is None:
        text = "PRs: loading…" if board.pr_busy else "PRs: not loaded"
        return [(" ", "normal"), (text, "strip"), (" ", "normal")]
    c = board.pr_counts()
    segs: list[Seg] = [(" ", "normal")]
    segs += [("⇣ ", "accent" if c["review"] else "strip"), (str(c["review"]), "accent" if c["review"] else "strip_bold"), (" to review", "accent" if c["review"] else "strip"), ("    ", "normal")]
    if c["attention"]:
        short = {"conflicts": "conflict", "changes requested": "changes", "checks failing": "CI failing", "unresolved threads": "threads"}
        why = " · ".join(f"{n} {short.get(r, r)}" for r, n in c["reasons"].items())
        segs += [("⚑ ", "wait"), (str(c["attention"]), "wait"), (" of yours need you", "wait"), (f" ({why})" if why else "", "strip")]
    else:
        segs += [("⚑ ", "strip"), ("yours are all quiet", "strip")]
    if c["ready"]:
        segs += [("    ", "normal"), (f"✓ {c['ready']} ready to merge", "done")]
    age = board.now() - data.fetched_at if data.fetched_at else 0
    interval = board.pr_interval()
    stale = interval and age > 2 * interval
    suffix = "  ·  refreshing…" if board.pr_busy else f"  ·  {fmt_until(age)} ago" if data.fetched_at else ""
    if data.errors:
        suffix += f"  ·  ⚠ {len(data.errors)}"
    segs.append((suffix + " ", "wait" if stale or data.errors else "dim"))
    return segs


def _overlay_mention(lines: list[Line], width: int, matches: list, chosen: int) -> None:
    """The projects above the prompt while an @mention is typed: @name, then sessions and path."""
    rows_available = max(1, len(lines) - HEADER_ROWS - 1)
    count = min(len(matches), SLASH_ROWS, rows_available)
    start = max(0, min(chosen - count // 2, len(matches) - count))
    shown: list[Line] = []
    for i in range(start, start + count):
        (name, path, total, working), on = matches[i], i == chosen
        label = fit_tail(f"@{mention_name(name)}", SLASH_NAME_WIDTH)
        stats = (f"{total} session{'s' if total != 1 else ''}" + (f" · {working} working" if working else "")) if total else "no sessions"
        head: list[Seg] = [("❯ " if on else "  ", "accent"), (f"{label:<{SLASH_NAME_WIDTH}}", "name_sel" if on else "name"), ("  ", "normal")]
        room = max(0, width - seg_width(head) - 1)
        shown.append(Line(head + [(fit(f"{stats} · {path}", room), "dim")], on))
    lines[len(lines) - count :] = shown


SLASH_ROWS = 10
SLASH_NAME_WIDTH = 34


def _overlay_slash(lines: list[Line], width: int, matches: list, chosen: int) -> None:
    """The list of commands above the prompt, as `claude agents` shows it: /name, then kind · description."""
    rows_available = max(1, len(lines) - HEADER_ROWS - 1)
    count = min(len(matches), SLASH_ROWS, rows_available)
    start = max(0, min(chosen - count // 2, len(matches) - count))
    shown: list[Line] = []
    for i in range(start, start + count):
        entry, on = matches[i], i == chosen
        name = fit_tail(f"/{entry.name}", SLASH_NAME_WIDTH)  # a long name keeps its end, which tells them apart
        head: list[Seg] = [("❯ " if on else "  ", "accent"), (f"{name:<{SLASH_NAME_WIDTH}}", "name_sel" if on else "name"), ("  ", "normal")]
        room = max(0, width - seg_width(head) - 1)
        shown.append(Line(head + [(fit(f"{entry.kind} · {entry.description}", room), "dim")], on))
    lines[len(lines) - count :] = shown


def _overlay_settings(board: Board, lines: list[Line], width: int) -> None:
    ui = board.settingsui
    fields = board.settings_fields()
    ui["idx"] = min(ui["idx"], len(fields) - 1)
    s = board.meta.settings
    rows: list[list[Seg]] = []
    for i, (name, label) in enumerate(fields):
        on = i == ui["idx"]
        arrow = (" ▸ " if on else "   ", "accent")
        if name == "refresh":
            minutes = s.get("pr_refresh_minutes", DEFAULT_PR_REFRESH)
            value = "only when I press ctrl+l" if minutes == 0 else f"every {minutes} min"
            shown: list[Seg] = [("◂ ", "faint"), (value, "bold"), (" ▸", "faint")]
        elif name == "drafts":
            shown = [("[x] yes" if s.get("include_drafts") else "[ ] no", "done" if s.get("include_drafts") else "faint")]
        elif name == "age":
            days = board.review_age_days()
            shown = [("◂ ", "faint"), (f"hide PRs older than {days} days" if days else "show every PR", "bold"), (" ▸", "faint")]
        else:
            text = board.setting_text(name)
            if ui["edit"] and ui["edit"]["field"] == name:
                shown = [(ui["edit"]["text"], "bold"), ("▏", "accent")]
            else:
                shown = [(fit(text, max(10, width - 44)) if text else "not set (enter to add)", "dim" if text else "faint")]
        rows.append([arrow, (f"{label:<24}", "name_sel" if on else "name")] + shown + [(" ", "normal")])
    _paste(lines, _box(rows, width, len(lines) - HEADER_ROWS, "settings"), width)


def compose(board: Board, width: int, height: int, demo: bool = False) -> tuple[list[Line], Optional[tuple[int, int]]]:
    """Returns the screen as styled lines and the caret position (y, x) if one should be shown."""
    lines: list[Line] = []
    inner = max(20, width - RIGHT_MARGIN)  # right-aligned content stops short of the terminal edge
    counts = board.counts()
    summary = f"{counts['awaiting']} awaiting input · {counts['working']} working · " + (f"{counts['idle']} idle · " if counts["idle"] else "") + f"{counts['done']} completed"
    brand: list[Seg] = [(f" {APP_MARK} ", "accent"), (APP_NAME, "bold"), ("   ", "normal")]
    tabs: list[Seg] = []
    for i, (tab, label) in enumerate(TABS):
        tabs += ([(" ", "normal")] if i else []) + [(f" {label} ", "tab_on" if tab == board.tab else "tab")]
    if demo:
        tabs.append(("  demo data", "wait"))
    if seg_width(brand) + seg_width(tabs) > inner:
        brand = [(f" {APP_MARK} ", "accent")]  # a narrow screen keeps the tabs and drops the name
    if seg_width(brand) + seg_width(tabs) + 1 + cell_width(summary) > inner:
        summary = ""
    board.tab_hit, col = [], seg_width(brand)
    for text, style in tabs:
        if style in ("tab", "tab_on"):
            board.tab_hit.append((1, col, col + cell_width(text), next(t for t, l in TABS if f" {l} " == text)))
        col += cell_width(text)
    left = brand + tabs
    gap = max(1, inner - seg_width(left) - cell_width(summary))
    lines.append(Line([]))  # breathing room above the title
    lines.append(Line(left + [(" " * gap, "normal"), (summary, "dim")]))
    strip = _ellipsize(strip_segs(board, inner), inner)
    lines.append(Line(strip + [("─" * max(0, width - seg_width(strip)), "rule")]))
    lines.append(Line([]))  # air between the strip and the first group

    prompt_lines, prompt_caret, prompt_hit = prompt_area(board, width, inner, max(1, (height - HEADER_ROWS - 3) // 2))
    body_h = max(1, height - HEADER_ROWS - 2 - len(prompt_lines))
    body: list[Line] = []
    owners: list[Optional[str]] = []
    sel_range: Optional[tuple[int, int]] = None
    rename_at: Optional[tuple[int, int]] = None  # (body row, column) of the in-place rename cursor
    groups = board.groups() if board.mode not in PR_VIEWS else []
    if board.mode in PR_VIEWS:
        body, owners, sel_range = pr_body(board, inner)
    for gi, (title, subtitle, items) in enumerate(groups):
        if gi:
            body.append(Line([]))
            owners.append(None)
        header = group_header(title, subtitle, len(items), inner)
        if gi == 0:  # the grouping is named once, beside the first group: ctrl+b switches it
            note = f"grouped by {board.mode} · ctrl+b"
            room = inner - seg_width(header.segs) - cell_width(note)
            if room >= 2:
                header = Line(header.segs + [(" " * room, "normal"), (note, "faint")])
        body.append(header)
        owners.append(None)
        for s in items:
            selected = s.id == board.sel
            rows = row_lines(board, s, inner, selected)
            if rows[0].caret is not None:
                rename_at = (len(body), rows[0].caret)
            if selected:
                sel_range = (len(body), len(body) + len(rows))
            body.extend(rows)
            owners.extend([s.id] * len(rows))
    if not body and board.mode not in PR_VIEWS:
        owners = [None, None]
        body = [Line([]), Line([("   No sessions" + (" match your search" if board.query else " yet — type a task below"), "faint")])]
    # The LIMITS block, and the USAGE panel above it, are pinned under the list; a long list scrolls in the rows left above them
    panels: list[Line] = []
    if not (board.help or board.peek or board.picker or board.modelpicker or board.tagpicker or board.settingsui):
        limits_block = limits_panel(board.limits, inner, board.now(), board.disk)
        if body_h - len(limits_block) - 1 >= min(len(body), LIST_ROWS_BESIDE_LIMITS):
            panels = limits_block
            room = body_h - len(panels) - 2 - min(len(body), LIST_ROWS_BESIDE_USAGE)
            if board.usage and room >= 4:
                panels = usage_panel(board.usage, inner, min(9, room)) + [Line([])] + panels
    if panels:
        body_h -= len(panels) + 1  # the list's rows, with a blank line between it and the panels
    if sel_range:
        if sel_range[0] < board.scroll:
            top = sel_range[0]
            while top > 0 and owners[top - 1] is None and sel_range[1] - (top - 1) <= body_h:
                top -= 1  # bring the group heading, and the gap above it, back with the group's first row
            board.scroll = top
        if sel_range[1] > board.scroll + body_h:
            board.scroll = sel_range[1] - body_h
    elif board.mode not in PR_VIEWS and board.sel is None:
        board.scroll = 0  # back in the prompt: the list returns to its top
    board.scroll = max(0, min(board.scroll, max(0, len(body) - body_h)))
    view = body[board.scroll : board.scroll + body_h]
    inline_caret: Optional[tuple[int, int]] = None
    if rename_at and 0 <= rename_at[0] - board.scroll < body_h:
        inline_caret = (HEADER_ROWS + rename_at[0] - board.scroll, rename_at[1])
    board.hit = {HEADER_ROWS + i: owner for i, owner in enumerate(owners[board.scroll : board.scroll + body_h]) if owner}
    view += [Line([]) for _ in range(body_h - len(view))]
    if panels:
        view += [Line([])] + panels
    lines.extend(view)

    if board.help:
        _overlay_help(board, lines, width)
    if board.picker:
        _overlay_picker(board, lines, width)
    if board.modelpicker:
        _overlay_modelpicker(board, lines, width)
    if board.tagpicker:
        _overlay_tagpicker(board, lines, width)
    if board.settingsui:
        _overlay_settings(board, lines, width)
    if board.peek:
        _overlay_peek(board, lines, width)

    if board.slash_query() is not None:
        matches, chosen = board.slash_view()
        if matches:
            _overlay_slash(lines, width, matches, chosen)
    elif board.mention_query() is not None:
        matches, chosen = board.mention_view()
        if matches:
            _overlay_mention(lines, width, matches, chosen)

    lines.append(Line([("─" * width, "rule")]))
    input_y = len(lines)
    lines.extend(prompt_lines)
    board.prompt_hit = (input_y, prompt_hit[0], prompt_hit[1]) if prompt_hit else None
    caret = (input_y + prompt_caret[0], prompt_caret[1]) if prompt_caret else None
    lines.append(Line(_ellipsize(_hint_segs(board), inner)))
    if inline_caret:
        caret = inline_caret
    return [Line(_clip(l.segs, width), l.selected) for l in lines[:height]], caret


def prompt_area(board: Board, width: int, inner: int, max_rows: int) -> tuple[list[Line], Optional[tuple[int, int]], Optional[tuple[int, list[tuple[int, int, int]]]]]:
    """The rows under the list: the prompt (as tall as its text, up to `max_rows`), else a field being edited or a PR note.

    Returns (lines, caret (row, column) within them, (first text column, [(row, column, text index)]) for clicks)."""
    caret: Optional[tuple[int, int]] = None
    editing_setting = board.settingsui["edit"] if board.settingsui else None
    if editing_setting:
        name = dict(board.settings_fields()).get(editing_setting["field"], "setting")
        pre = [(" ", "normal"), (f"✎ {name} ", "accent")]
        line = Line(pre + [(fit(editing_setting["text"], max(5, width - seg_width(pre) - 1)), "bold")])
        return [line], (0, min(width - 1, seg_width(pre) + cell_width(editing_setting["text"]))), None
    if board.edit and board.edit["kind"] == "find":  # a rename is edited in the session's own row instead
        label = "⌕ find "
        pre = [(" ", "normal"), (label, "accent")]
        text = board.edit["text"]
        shown, col, _ = prompt_window(text, board.edit.get("cursor", len(text)), width - seg_width(pre) - 1, "bold")
        return [Line(pre + shown)], (0, min(width - 1, seg_width(pre) + col)), None
    if board.mode in PR_VIEWS:
        n = len(board.marked)
        text = f"{n} marked for review" if n else ("space marks a PR for review, r starts a review session" if board.mode == "review" else "enter opens a PR; a ⟳ marks a PR a session is already working on")
        old = board.hidden_by_age() if board.mode == "review" and not n else 0
        if old:
            text += f" · {old} older than {board.review_age_days()} days hidden (ctrl+o)"
        return [Line([(" ", "normal"), ("◆ " if n else "  ", "accent"), (text, "bold" if n else "faint")])], None, None
    pre = [(" ❯ ", "accent")]
    target_name, _ = board.start_target()
    chosen = board.model()
    model_seg = (model_label(chosen), "accent" if chosen else "faint")
    options = [
        [(f"in {target_name}", "faint"), ("  ·  ", "faint"), model_seg, ("  ctrl+p ctrl+e ", "faint")],
        [(f"in {target_name}", "faint"), ("  ·  ", "faint"), model_seg, (" ", "faint")],
        [model_seg, (" ", "faint")],
    ]
    placeholder = "describe a task for a new session"
    room = inner - seg_width(pre) - (seg_width(prompt_segs(board.input, "bold")) if board.input else cell_width(placeholder)) - 2
    right = next((o for o in options if seg_width(o) <= room), options[-1])
    hit, top = None, 0
    if board.input:
        rows, (caret_row, caret_col), bounds = prompt_wrap(
            board.input, board.cursor, inner - seg_width(pre) - seg_width(right) - 2, inner - seg_width(pre) - 1, "bold" if board.sel is None else "dim"
        )
        top = max(0, min(caret_row - max_rows + 1, len(rows) - max_rows))  # a prompt taller than the room scrolls with its caret
        shown = rows[top : top + max_rows]
        hit = (seg_width(pre), [(r - top, c, i) for r, c, i in bounds if top <= r < top + len(shown)])
        caret_row -= top
    else:
        rows = shown = [[(placeholder, "faint")]]
        caret_row = caret_col = 0
    lines = []
    for r, body_segs in enumerate(shown):
        if (r == 0 and top) or (r == len(shown) - 1 and top + len(shown) < len(rows)):
            lead = [(" ⋮ ", "faint")]  # more of the prompt above or below
        else:
            lead = pre if r == 0 else [("   ", "normal")]
        tail = [(" " * max(1, inner - seg_width(lead) - seg_width(body_segs) - seg_width(right)), "normal")] + right if r == 0 else []
        lines.append(Line(lead + body_segs + tail))
    if board.sel is None and not (board.help or board.peek or board.confirm or board.picker or board.modelpicker or board.tagpicker or board.settingsui):
        caret = (caret_row, seg_width(pre) + caret_col)
    return lines, caret, hit


def _ellipsize(segs: list[Seg], width: int) -> list[Seg]:
    out, used = [], 0
    for text, style in segs:
        room = width - used
        if cell_width(text) > room:
            out.append((fit(text, room), style))
            break
        out.append((text, style))
        used += cell_width(text)
    return out


def prompt_segs(text: str, style: str) -> list[Seg]:
    """The prompt as styled pieces: typed text as it is (line breaks as ⏎), each pasted-text chip as a chip."""
    segs: list[Seg] = []
    pos = 0
    for m in PASTE_CHIP_RE.finditer(text):
        if m.start() > pos:
            segs.append((text[pos : m.start()].replace("\n", " ⏎ "), style))
        segs.append((f" {m.group(0)} ", "chip_info"))
        pos = m.end()
    if pos < len(text):
        segs.append((text[pos:].replace("\n", " ⏎ "), style))
    return segs


def edit_line(text: str, cursor: int, key: str, ch: str = "") -> Optional[tuple[str, int]]:
    """One key in a single-line field (a rename, the find bar): the new (text, caret), or None for a key it does not use.

    The same moves as the prompt: ←/→, home/end, ctrl or alt+←/→ by word, and deleting either side of the caret."""
    c = max(0, min(cursor, len(text)))

    def word_start(i: int) -> int:
        while i > 0 and text[i - 1].isspace():
            i -= 1
        while i > 0 and not text[i - 1].isspace():
            i -= 1
        return i

    def word_end(i: int) -> int:
        while i < len(text) and text[i].isspace():
            i += 1
        while i < len(text) and not text[i].isspace():
            i += 1
        return i

    if key == "left":
        return text, max(0, c - 1)
    if key == "right":
        return text, min(len(text), c + 1)
    if key == "home":
        return text, 0
    if key == "end":
        return text, len(text)
    if key in ("ctrl+left", "alt+left"):
        return text, word_start(c)
    if key in ("ctrl+right", "alt+right"):
        return text, word_end(c)
    if key == "backspace":
        return text[: max(0, c - 1)] + text[c:], max(0, c - 1)
    if key == "delete":
        return text[:c] + text[c + 1 :], c
    if key in ("ctrl+w", "alt+backspace"):
        return text[: word_start(c)] + text[c:], word_start(c)
    if key == "ctrl+delete":
        return text[:c] + text[word_end(c) :], c
    if key == "ctrl+u":
        return "", 0
    if key in ("char", "space"):
        typed = ch or " "
        return text[:c] + typed + text[c:], c + len(typed)
    return None


def prompt_tokens(text: str) -> list[tuple[int, int]]:
    """The prompt's editable units as (start, end): each character, except a pasted-text chip, which is one unit."""
    toks: list[tuple[int, int]] = []
    pos = 0
    for m in PASTE_CHIP_RE.finditer(text):
        toks.extend((i, i + 1) for i in range(pos, m.start()))
        toks.append((m.start(), m.end()))
        pos = m.end()
    toks.extend((i, i + 1) for i in range(pos, len(text)))
    return toks


def prompt_window(text: str, cursor: int, width: int, style: str) -> tuple[list[Seg], int, list[tuple[int, int]]]:
    """The part of the prompt that fits `width` around the caret: (pieces, caret column, [(column, text index)]).

    The caret always stays visible; what is cut off at either side is shown as …"""
    toks = prompt_tokens(text)
    items: list[tuple[str, str]] = []
    for a, b in toks:
        piece = text[a:b]
        items.append((f" {piece} ", "chip_info") if PASTE_CHIP_RE.fullmatch(piece) else (piece.replace("\n", " ⏎ "), style))
    wid = [cell_width(d) for d, _ in items]
    n = len(items)
    ci = sum(1 for t in toks if t[1] <= cursor)
    lo, hi = 0, n
    if sum(wid) > width:
        after_room = min(sum(wid[ci:]), max(width // 3, 1))
        before_room = width - after_room
        if sum(wid[:ci]) < before_room:
            before_room, after_room = sum(wid[:ci]), width - sum(wid[:ci])
        used, lo = 0, ci
        while lo > 0 and used + wid[lo - 1] <= before_room:
            lo -= 1
            used += wid[lo]
        if lo > 0:
            while lo < ci and used > before_room - 1:
                used -= wid[lo]
                lo += 1
        used, hi = 0, ci
        while hi < n and used + wid[hi] <= after_room:
            used += wid[hi]
            hi += 1
        if hi < n:
            while hi > ci and used > after_room - 1:
                hi -= 1
                used -= wid[hi]
    segs: list[Seg] = []
    bounds: list[tuple[int, int]] = []
    col = 0
    if lo > 0:
        segs.append(("…", "faint"))
        col = 1
    caret_col = col
    for i in range(lo, hi):
        if i == ci:
            caret_col = col
        bounds.append((col, toks[i][0]))
        text_i, style_i = items[i]
        if segs and segs[-1][1] == style_i and style_i != "chip_info":
            segs[-1] = (segs[-1][0] + text_i, style_i)
        else:
            segs.append((text_i, style_i))
        col += wid[i]
    if ci >= hi:
        caret_col = col
    bounds.append((col, toks[hi - 1][1] if hi > lo else (toks[lo][0] if lo < n else len(text))))
    if hi < n:
        segs.append(("…", "faint"))
    return segs, caret_col, bounds


def prompt_wrap(text: str, cursor: int, first_width: int, width: int, style: str) -> tuple[list[list[Seg]], tuple[int, int], list[tuple[int, int, int]]]:
    """The whole prompt wrapped into rows: (rows of pieces, caret (row, column), [(row, column, text index)]).

    Rows break between words where they can and at every line break; the first row is `first_width` wide, the rest `width`."""
    toks = prompt_tokens(text)
    items: list[tuple[str, str]] = []
    for a, b in toks:
        piece = text[a:b]
        items.append((f" {piece} ", "chip_info") if PASTE_CHIP_RE.fullmatch(piece) else ("" if piece == "\n" else piece, style))
    wid = [cell_width(d) for d, _ in items]
    is_space = [text[a:b] == " " for a, b in toks]
    rows: list[list[int]] = [[]]
    used = 0
    for i, (a, b) in enumerate(toks):
        if text[a:b] == "\n":
            rows[-1].append(i)
            rows.append([])
            used = 0
            continue
        room = max(1, first_width if len(rows) == 1 else width)
        if used + wid[i] > room and rows[-1] and not is_space[i]:  # a space may hang past the edge, a break never starts a row
            row = rows[-1]
            cut = next((k + 1 for k in range(len(row) - 1, -1, -1) if is_space[row[k]]), 0)
            carry = row[cut:] if cut else []
            if sum(wid[j] for j in carry) + wid[i] > width:
                carry = []  # one word longer than a row is cut where the row ends
            del row[len(row) - len(carry) :]
            rows.append(carry)
            used = sum(wid[j] for j in carry)
        rows[-1].append(i)
        used += wid[i]
    ci = sum(1 for t in toks if t[1] <= cursor)
    out: list[list[Seg]] = []
    bounds: list[tuple[int, int, int]] = []
    caret: Optional[tuple[int, int]] = None
    col = 0
    for r, row in enumerate(rows):
        segs: list[Seg] = []
        col = 0
        for j in row:
            if j == ci:
                caret = (r, col)
            bounds.append((r, col, toks[j][0]))
            piece, piece_style = items[j]
            if segs and segs[-1][1] == piece_style and piece_style != "chip_info":
                segs[-1] = (segs[-1][0] + piece, piece_style)
            elif piece:
                segs.append((piece, piece_style))
            col += wid[j]
        if not row:
            bounds.append((r, 0, len(text)))
        elif text[toks[row[-1]][0] : toks[row[-1]][1]] != "\n":
            bounds.append((r, col, toks[row[-1]][1]))
        out.append(segs)
    return out, caret or (len(rows) - 1, col), bounds


def tail_segs(segs: list[Seg], width: int) -> list[Seg]:
    """The end of the pieces that fits `width`: a chip is kept whole or dropped, plain text is cut with an ellipsis."""
    if seg_width(segs) <= width:
        return segs
    out: list[Seg] = []
    used = 0
    for text, style in reversed(segs):
        w = cell_width(text)
        if used + w <= width:
            out.insert(0, (text, style))
            used += w
        else:
            room = width - used
            if style != "chip_info" and room > 1:
                out.insert(0, (fit_tail(text, room), style))
            break
    return out


def fit_tail(text: str, width: int) -> str:
    """The end of the text: what you are typing or pasting is at the right-hand side."""
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    out, used = "", 0
    for ch in reversed(text):
        w = cell_width(ch)
        if used + w > width - 1:
            break
        out = ch + out
        used += w
    return "…" + out


def _clip(segs: list[Seg], width: int) -> list[Seg]:
    out, used = [], 0
    for text, style in segs:
        room = width - used
        if room <= 0:
            break
        if cell_width(text) > room:
            text = fit(text, room) if room > 1 else ""
        out.append((text, style))
        used += cell_width(text)
    return out


def _hint_segs(board: Board) -> list[Seg]:
    if board.confirm and board.confirm["kind"] == "approve":
        pr = next((x for x in board.pr_flat() if x.key == board.confirm["pr"]), None)
        where = "GitLab" if pr and pr.provider == "gitlab" else "GitHub"
        what = f"Approve #{pr.number} “{pr.title}” on {where}?" if pr else "Approve?"
        return [(" ", "normal"), (what, "warn"), ("  a or enter approves · any other key cancels", "faint")]
    if board.confirm:
        s = next((x for x in board.sessions if x.id == board.confirm["sid"]), None)
        verb = "Stop" if board.confirm["kind"] == "stop" else "Delete"
        return [(" ", "normal"), (f"{verb} “{board.name_of(s) if s else '?'}”?", "warn"), (f"  {CONFIRM_KEY} or ctrl+x to confirm · any other key cancels", "faint")]
    if board.msg:
        return [(" ", "normal"), (board.msg, "fail" if board.msg.startswith("✗") else "accent")]
    if board.edit:
        hint = {
            "find": "type to filter · enter keep · esc clear",
            "rename": "enter save · esc cancel · empty resets the name",
        }[board.edit["kind"]]
    elif board.peek:
        hint = "↑/↓ scroll · esc close"
    elif board.settingsui:
        hint = "enter save · esc cancel" if board.settingsui["edit"] else "↑/↓ move · ←/→ change · enter edit/toggle · esc close"
    elif board.picker:
        hint = "↑/↓ choose · type to filter, or a /path · enter select · esc cancel"
    elif board.modelpicker:
        hint = "↑/↓ choose · type to filter, or any model id · enter select · esc cancel"
    elif board.tagpicker:
        tp = board.tagpicker
        if tp["delete"] is not None:
            return [(" ", "normal"), (f"Delete tag “{tp['delete']}” from the library and every session?", "warn"), (f"  {CONFIRM_KEY} or ctrl+x to confirm · any other key cancels", "faint")]
        hint = (
            "enter save · esc cancel" if tp["rename"] is not None
            else "↑/↓ move · enter toggle · type to filter or create · ctrl+r rename · ctrl+x delete · esc done"
        )
    elif board.mode == "review":
        hint = "space mark · ctrl+a all · r review · a approve · enter open · o browser · ctrl+l refresh · ctrl+o settings · esc back"
    elif board.mode == "mine":
        hint = "enter open · f fix conflicts · o browser · ctrl+l refresh · ctrl+o settings · ctrl+s next view · esc back"
    elif board.sel:
        s = board.selected()
        stop = "stop" if s and s.group in ("working", "idle") else "delete"
        hint = ("draft kept · " if board.input else "") + f"enter open · space peek · ctrl+r rename · ctrl+g tags · ctrl+t pin · ctrl+x {stop} · ? shortcuts"
    elif board.slash_query() is not None and board.slash_view()[0]:
        n = len(board.slash_view()[0])
        hint = f"↑/↓ choose · tab or enter completes · esc clears · {n} command{'s' if n != 1 else ''}"
    elif board.mention_query() is not None and board.mention_view()[0]:
        n = len(board.mention_view()[0])
        hint = f"↑/↓ choose · tab or enter completes · esc clears · {n} project{'s' if n != 1 else ''} · the session starts there"
    elif board.input:
        full = board.prompt_text()
        previews = [t.label for t in derive_tags(full)] + suggest_tags(full, board.meta)
        if previews:
            segs: list[Seg] = [(" ", "normal"), (f"enter start in {board.start_target()[0]} on {model_label(board.model())} · will tag ", "faint")]
            for label in previews:
                kind = tag_kind(label)
                segs += [(f" {label} ", tag_style(Tag(label, kind))), (" ", "normal")]
            return segs + [("· backspace removes a pasted block" if board.pastes else "· ctrl+x cut", "faint")]
        hint = f"enter start in {board.start_target()[0]} on {model_label(board.model())} · ctrl+x cut to clipboard · ctrl+y copy · ctrl+c clears · esc"
        if board.pastes:
            hint = f"enter start in {board.start_target()[0]} on {model_label(board.model())} · backspace removes a pasted block · ctrl+x cut · ctrl+c clears"
    else:
        hint = "↑/↓ select · ctrl+f find · ctrl+s view · ctrl+p project · ? shortcuts · esc quit"
    return [(" ", "normal"), (hint, "faint")]


def _box(rows: list[list[Seg]], width: int, height: int, title: str) -> list[Line]:
    inner = min(width - 4, max([seg_width(r) for r in rows] + [cell_width(title) + 4]) + 2)
    top = f"╭─ {title} " + "─" * max(0, inner - cell_width(title) - 3) + "╮"
    out = [Line([(top, "rule")])]
    for r in rows[: max(0, height - 2)]:
        pad = max(0, inner - seg_width(r))
        out.append(Line([("│", "rule")] + r + [(" " * pad, "normal"), ("│", "rule")]))
    out.append(Line([("╰" + "─" * inner + "╯", "rule")]))
    return out


def _paste(lines: list[Line], box: list[Line], width: int) -> None:
    top = HEADER_ROWS + max(0, (len(lines) - HEADER_ROWS - len(box)) // 2)
    for i, bl in enumerate(box):
        y = top + i
        if HEADER_ROWS <= y < len(lines):
            w = seg_width(bl.segs)
            left = max(0, (width - w) // 2)
            lines[y] = Line([(" " * left, "normal")] + bl.segs)


def _overlay_help(board: Board, lines: list[Line], width: int) -> None:
    room = max(12, width - 4 - 22)
    rows = [[(f" {k:<18}", "key"), (fit(v, room), "dim")] for k, v in help_rows(board.mode)]
    rows += [[("", "normal")], [(" ctrl+s switches view; every view has its own shortcuts", "faint")]]
    _paste(lines, _box(rows, width, len(lines) - HEADER_ROWS, f"shortcuts · {VIEW_LABEL[board.mode]}"), width)


def _overlay_picker(board: Board, lines: list[Line], width: int) -> None:
    p = board.picker
    items = board.picker_items()
    p["idx"] = min(p["idx"], max(0, len(items) - 1))
    current = board.target()[1]
    pins = set(board.pinned_projects())
    rows: list[list[Seg]] = [[(" ⌕ ", "accent"), (p["text"] or "filter, or type a /path", "bold" if p["text"] else "faint"), (" ", "normal")], [("", "normal")]]
    for i, (name, path, total, working) in enumerate(items):
        on = i == p["idx"]
        mark = "✓" if path == current else " "
        stats = f"{total} session{'s' if total != 1 else ''}" + (f" · {working} working" if working else "") if total else "no sessions"
        star = "★" if path in pins else " "
        rows.append([
            (" ▸ " if on else "   ", "accent"), (f"{mark} ", "done"), ("● ", project_style(name)),
            (f"{fit(name, 20):<20}", "name_sel" if on else "name"), (f"{star} ", "accent"), (f"{stats:<24}", "dim" if total else "faint"), (fit(path, 40), "faint"), (" ", "normal"),
        ])
    if not items:
        rows.append([("   no match — start with / or ~ to use any directory ", "faint")])
    rows += [[("", "normal")], [("   ★ pinned · ctrl+t pins or unpins the highlighted project (type a /path first to pin any directory)", "faint")]]
    _paste(lines, _box(rows, width, len(lines) - HEADER_ROWS, "start new sessions in"), width)


def _overlay_modelpicker(board: Board, lines: list[Line], width: int) -> None:
    p = board.modelpicker
    items = board.model_items()
    p["idx"] = min(p["idx"], max(0, len(items) - 1))
    current = board.model()
    rows: list[list[Seg]] = [[(" ⌕ ", "accent"), (p["text"] or "filter, or type any model id", "bold" if p["text"] else "faint"), (" ", "normal")], [("", "normal")]]
    visible = max(3, len(lines) - HEADER_ROWS - 8)
    start = max(0, min(p["idx"] - visible // 2, len(items) - visible))
    for i in range(start, min(len(items), start + visible)):
        label, value, note = items[i]
        on = i == p["idx"]
        mark = "✓" if value == current else " "
        rows.append([
            (" ▸ " if on else "   ", "accent"), (f"{mark} ", "done"), (f"{fit(label, 14):<15}", "name_sel" if on else "name"),
            (f"{fit(value or '—', 20):<21}", "dim"), (fit(note, 44), "faint"), (" ", "normal"),
        ])
    rows += [[("", "normal")], [("   used for sessions started from the prompt and for review sessions", "faint")]]
    _paste(lines, _box(rows, width, len(lines) - HEADER_ROWS, "model for new sessions"), width)


def _overlay_tagpicker(board: Board, lines: list[Line], width: int) -> None:
    tp = board.tagpicker
    session = next((s for s in board.sessions if s.id == tp["sid"]), None)
    items = board.tagpicker_items()
    tp["idx"] = min(tp["idx"], max(0, len(items) - 1))
    if tp["rename"] is not None:
        head = [(" ✎ ", "accent"), (f"rename “{tp['rename']}” → ", "dim"), (tp["text"], "bold"), ("▏ ", "accent")]
    else:
        head = [(" ⌕ ", "accent"), (tp["text"] or "filter, or type a new tag", "bold" if tp["text"] else "faint"), (" ", "normal")]
    rows: list[list[Seg]] = [head, [("", "normal")]]
    visible = max(3, len(lines) - HEADER_ROWS - 8)
    start = max(0, min(tp["idx"] - visible // 2, len(items) - visible))
    for i in range(start, min(len(items), start + visible)):
        kind, label = items[i]
        on = i == tp["idx"]
        arrow = (" ▸ " if on else "   ", "accent")
        if kind == "create":
            rows.append([arrow, ("+ create ", "accent"), (f"“{label}”", "bold"), (" ", "normal")])
        else:
            shown = board.tag_on(session, label) if session else None
            row: list[Seg] = [arrow, ("[x] " if shown else "[ ] ", "done" if shown else "faint")]
            row.append((f" {label} ", tag_style(shown or Tag(label, "custom"))))
            if shown and shown.kind != "custom":
                row.append(("  derived", "faint"))
            rows.append(row + [(" ", "normal")])
    if not items:
        rows.append([("   no tags yet — type a name to create one ", "faint")])
    if session and session.auto_tags:
        rows += [[("", "normal")], [("   derived: ", "faint"), (", ".join(t.label for t in session.auto_tags), "dim"), (" ", "normal")]]
    name = board.name_of(session) if session else tp["sid"]
    _paste(lines, _box(rows, width, len(lines) - HEADER_ROWS, f"tags · {fit(name, 40)}"), width)


def wrap_lines(lines: list[str], width: int) -> list[str]:
    """Word-wraps each line to `width`, keeping blank lines."""
    import textwrap

    out: list[str] = []
    for line in lines:
        out.extend(textwrap.wrap(line, max(10, width), break_long_words=True, break_on_hyphens=False) or [""])
    return out


def _overlay_peek(board: Board, lines: list[Line], width: int) -> None:
    p = board.peek
    body_h = len(lines) - HEADER_ROWS - 2
    inner = width - 6
    source = wrap_lines(p["lines"], inner) if p.get("wrap") else p["lines"]  # error text wraps; logs keep their own layout
    p["top"] = max(0, min(p["top"], max(0, len(source) - body_h)))
    shown = source[p["top"] : p["top"] + body_h]
    rows = [[(" " + fit(l, inner), "normal")] for l in shown]
    rows += [[(" ", "normal")] for _ in range(body_h - len(rows))]
    box = _box(rows, width, len(lines) - HEADER_ROWS, fit(p["title"], inner - 6))
    for i, bl in enumerate(box):
        if HEADER_ROWS + i < len(lines):
            lines[HEADER_ROWS + i] = bl


# ───────────────────────────── selection, clipboard, attach ─────────────────────────────

Cell = tuple[int, int]  # (row, column)


def slice_cells(text: str, c0: int, c1: int) -> str:
    out, col = "", 0
    for ch in text:
        w = cell_width(ch)
        if col >= c1:
            break
        if col + w > c0:
            out += ch
        col += w
    return out


def selection_text(lines: list[str], a: Cell, b: Cell) -> str:
    (y0, x0), (y1, x1) = sorted([a, b])
    rows = []
    for y in range(y0, y1 + 1):
        text = lines[y] if y < len(lines) else ""
        rows.append(slice_cells(text, x0 if y == y0 else 0, x1 + 1 if y == y1 else 10**6).rstrip())
    return "\n".join(rows).strip("\n")


class Selection:
    def __init__(self) -> None:
        self.start: Optional[Cell] = None
        self.end: Optional[Cell] = None
        self.active = False

    def clear(self) -> None:
        self.start = self.end = None
        self.active = False

    def region(self) -> Optional[tuple[Cell, Cell]]:
        if self.start is None or self.end is None or self.start == self.end:
            return None
        return tuple(sorted([self.start, self.end]))  # type: ignore[return-value]


def _bit(name: str, fallback: int = 0) -> int:
    return getattr(curses, name, fallback)


def mouse_event(sel: Selection, board: Board, y: int, x: int, bstate: int, lines: list[str]) -> Optional[str]:
    """Applies one mouse event; returns text that should be copied to the clipboard, if any."""
    modal = bool(board.settingsui or board.picker or board.modelpicker or board.tagpicker or board.help or board.peek or board.edit or board.confirm)
    if bstate & _bit("BUTTON4_PRESSED", 0x10000):
        if not modal:
            board.handle("up")
    elif bstate & _bit("BUTTON5_PRESSED", 0x200000):
        if not modal:
            board.handle("down")
    elif bstate & _bit("BUTTON1_PRESSED"):
        sel.start = sel.end = (y, x)
        sel.active = True
    elif bstate & _bit("REPORT_MOUSE_POSITION") and sel.active:
        sel.end = (y, x)
    elif bstate & _bit("BUTTON1_RELEASED") and sel.active:
        sel.end, sel.active = (y, x), False
        if sel.start == sel.end:
            sel.clear()
            if not modal:
                board.click(y, x)
        else:
            return selection_text(lines, sel.start, sel.end)
    elif bstate & _bit("BUTTON1_CLICKED"):
        if not modal:
            board.click(y, x)
    return None


def copy_to_clipboard(text: str) -> str:
    """OSC 52 reaches the terminal's clipboard (also over ssh); a local tool is a fallback."""
    data = text.encode()
    try:
        os.write(1, b"\x1b]52;c;" + base64.b64encode(data) + b"\x07")
    except OSError:
        pass
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                p.stdin.write(data)
                p.stdin.close()
                return cmd[0]
            except OSError:
                continue
    return "terminal"


def _children() -> dict[int, list[int]]:
    tree: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        tree.setdefault(ppid, []).append(int(entry))
    return tree


def _direct_children(pid: int) -> list[int]:
    """The kernel's own list of a process's children, one small file per thread: no walk over every process."""
    found: list[int] = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return found  # gone
    for tid in tasks:
        try:
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                found += [int(c) for c in f.read().split()]
        except (OSError, ValueError):
            continue
    return found


CHILDREN_FILES = os.path.exists(f"/proc/self/task/{os.getpid()}/children")  # needs CONFIG_PROC_CHILDREN; most kernels have it


def descendants(pid: int) -> list[int]:
    """Every process below `pid`. Opening a session checks this ten times a second, so it reads only the
    branch under `pid` where the kernel lists children; walking all of /proc that often kept a core half busy."""
    if CHILDREN_FILES:
        children = _direct_children
    else:
        tree = _children()

        def children(p: int) -> list[int]:
            return tree.get(p, [])
    found, queue = [], [pid]
    while queue:
        for child in children(queue.pop()):
            found.append(child)
            queue.append(child)
    return found


def _cli_args(argv: list[str]) -> list[str]:
    """The arguments `claude` was given, without the script an interpreter runs (`node cli.js agents` -> ['agents'])."""
    args = argv[1:]
    runner = os.path.basename(argv[0]) if argv else ""
    if args and (runner.startswith(("node", "bun", "deno", "python")) or args[0].endswith((".js", ".mjs"))):
        args = args[1:]  # the first argument is the script the interpreter runs
    return args


def is_agent_view_argv(argv: list[str]) -> bool:
    """True for `claude agents …` (or `node cli.js agents …`), the view `claude attach` relaunches on ←."""
    args = _cli_args(argv)
    return bool(args) and args[0] == "agents"


def is_session_infrastructure(argv: list[str]) -> bool:
    """True for what `claude attach` may have started on the session's behalf, and which must outlive it: the
    supervisor (`claude daemon run`, started as the attach client's child when none was running), the process
    hosting the session (`bg-pty-host`, `bg-spare`) and the session itself (`--resume`)."""
    args = _cli_args(argv)
    return bool(args) and (args[0] in ("daemon", "bg-pty-host", "--bg-pty-host", "bg-spare", "--bg-spare") or "--resume" in args)


def attach_kill_set(pids: list[int], argv_of: Optional[Callable[[int], list[str]]] = None, tree: Optional[Callable[[int], list[int]]] = None) -> list[int]:
    """What the board may kill when you leave a session: the attach client and the relaunched agent view, never
    the supervisor or the session beneath it. Killing those made the session look like "process gone while
    supervisor was down", and it stopped working the moment you went back to the board."""
    argv_of, tree = argv_of or read_argv, tree or descendants
    keep: set[int] = set()
    for p in pids:
        if is_session_infrastructure(argv_of(p)):
            keep.add(p)
            keep.update(tree(p))
    return [p for p in pids if p not in keep]


def read_argv(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def has_relaunch_marker(pid: int) -> bool:
    """The relaunched agent view is started with CLAUDE_AGENTS_SELECT in its environment."""
    if "CLAUDE_AGENTS_SELECT" in os.environ:
        return False  # we inherited it ourselves, so every child would look like a relaunch
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return b"\0CLAUDE_AGENTS_SELECT=" in b"\0" + f.read()
    except OSError:
        return False


class ScreenFilter:
    """Keeps a session from switching the terminal between its alternate and normal screens.

    `claude attach` leaves the alternate screen when you press ← and only then starts the next thing, so the
    terminal showed your shell for about a second. Here entering the alternate screen becomes a clear, and
    leaving it ends the relay: nothing the session draws after that point reaches the terminal, and `left`
    tells the caller to hand the screen back to the board straight away."""

    SWITCH = re.compile(rb"\x1b\[\?(?:1049|1047|47)([hl])")
    TARGETS = [b"\x1b[?1049h", b"\x1b[?1049l", b"\x1b[?1047h", b"\x1b[?1047l", b"\x1b[?47h", b"\x1b[?47l"]
    CLEAR = b"\x1b[2J\x1b[H"

    def __init__(self) -> None:
        self.held = b""
        self.entered = False
        self.left = False

    def feed(self, data: bytes) -> bytes:
        data = self.held + data
        self.held = b""
        out, pos = b"", 0
        for m in self.SWITCH.finditer(data):
            out += data[pos : m.start()]
            pos = m.end()
            if m.group(1) == b"h":
                self.entered = True
                out += self.CLEAR
            elif self.entered:
                self.left = True
                return out  # whatever follows was meant for the normal screen
        rest = data[pos:]
        cut = rest.rfind(b"\x1b")
        if cut != -1:
            tail = rest[cut:]
            if len(tail) < 8 and any(t.startswith(tail) and t != tail for t in self.TARGETS):
                self.held, rest = tail, rest[:cut]  # an escape code split across two reads
        return out + rest


def _write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            data = data[os.write(fd, data) :]
        except InterruptedError:
            continue
        except OSError:
            return


def _copy_winsize(to_fd: int) -> None:
    try:
        size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        fcntl.ioctl(to_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _is_stopped(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "T"
    except (OSError, IndexError):
        return False


@dataclass(frozen=True)
class AttachResult:
    cut: bool  # the screen change that follows pressing ← was cut off
    returncode: Optional[int]
    output: str  # what the session printed before the board took over again, without escape codes
    killed: bool  # the board ended it itself (← relaunch, the session screen closed, Ctrl+Z)

    @property
    def failed(self) -> bool:
        return not self.killed and self.returncode not in (0, None)


def attach_failure_notice(result: AttachResult, name: str) -> Optional[tuple[str, list[str]]]:
    """(footer message, popup lines) for a session that could not be opened, or None when nothing went wrong."""
    if not result.failed:
        return None
    lines = [l.strip() for l in result.output.splitlines() if l.strip()]
    reason = [l for l in lines if not l.startswith("Waking session")] or lines or ["claude attach stopped without saying why"]
    advice = []
    if "not trusted" in result.output.lower():
        advice = ["", "Claude Code has not been told it may work in that folder yet. Run `claude` once there,",
                  "accept the trust prompt, then open this session again."]
    return f"✗ could not open “{name}”: details in the popup", reason + advice


ATTACH_SCAN_SECONDS = 0.1  # how often an open session's processes are checked for the ← relaunch and Ctrl+Z


def attach_session(claude: str, session_id: str, poll: float = 0.03, scan_every: float = ATTACH_SCAN_SECONDS) -> AttachResult:
    """Runs `claude attach` behind a pseudo-terminal of our own and relays keys and output both ways.

    Why not hand it the real terminal: pressing ← makes `claude attach` leave its screen and start a fresh
    `claude agents`, which the board kills so you land back here. Seeing the output lets the board keep the
    terminal on its own screen throughout (ScreenFilter), and go quiet the moment the relaunch starts,
    instead of showing the normal terminal for about a second. Ctrl+Z (a stopped session) detaches too.
    Returns what happened: whether the relaunch was cut off, how the session ended and what it printed."""
    master, slave = pty.openpty()
    _copy_winsize(slave)
    proc = subprocess.Popen(
        [claude, "attach", session_id], stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )
    os.close(slave)
    resized: list[int] = []
    previous = signal.signal(signal.SIGWINCH, lambda *_: resized.append(1))
    screen, cut, muted, last_scan = ScreenFilter(), False, False, 0.0
    captured, painted, killed = b"", False, False
    stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()
    sys.stdout.flush()
    try:
        while True:
            if killed and proc.poll() is not None:
                break  # the client is gone; whatever it left running on purpose may still hold the terminal open
            if resized:
                resized.clear()
                _copy_winsize(master)
            try:
                ready, _, _ = select.select([stdin_fd, master], [], [], poll)
            except InterruptedError:
                continue
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break  # the session ended
                if not muted:
                    chunk = screen.feed(data)
                    captured = (captured + chunk)[-8192:]  # only what the terminal is shown
                    if chunk and not painted:
                        painted = True
                        chunk = ScreenFilter.CLEAR + chunk  # the session's first words must not land on top of the board
                    _write_all(stdout_fd, chunk)
                    if screen.left:
                        muted = True
            if stdin_fd in ready:
                typed = os.read(stdin_fd, 65536)
                if typed:
                    _write_all(master, typed)
            if time.time() - last_scan >= scan_every:
                last_scan = time.time()
                pids = [proc.pid] + descendants(proc.pid)
                relaunched = any(is_agent_view_argv(read_argv(p)) or has_relaunch_marker(p) for p in pids)
                if relaunched or screen.left or _is_stopped(proc.pid):
                    cut, muted, killed = cut or relaunched, True, True
                    for p in reversed(attach_kill_set(pids)):
                        try:
                            os.kill(p, signal.SIGKILL)
                        except OSError:
                            pass
    finally:
        signal.signal(signal.SIGWINCH, previous)
        os.close(master)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            killed = True
    text = ANSI_RE.sub("", captured.decode(errors="replace")).replace("\r", "")
    return AttachResult(cut=cut, returncode=proc.returncode, output=text, killed=killed)


# Everything the board turns on, switched off for the session we hand the terminal to. The alternate screen is
# deliberately NOT left: staying on it is what keeps the normal terminal from flashing in and out.
HAND_OVER = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[?1004l\x1b[<u\x1b[?1l\x1b>\x1b[?25h\x1b[0m"
SYNC_BEGIN, SYNC_END = "\x1b[?2026h", "\x1b[?2026l"  # synchronized output: terminals that support it paint the update at once
ENTER_ALT_SCREEN = "\x1b[?1049h"  # in case the session left the alternate screen on its way out


# ───────────────────────────── curses shell ─────────────────────────────

THEME: dict[str, tuple[int, int, int]] = {}


def _theme() -> dict[str, tuple[int, int, int]]:
    b, d = curses.A_BOLD, 0
    t = {
        "normal": (-1, -1, d), "bold": (-1, -1, b), "dim": (246, -1, d), "faint": (241, -1, d), "strip": (252, -1, d),
        "strip_bold": (255, -1, b),
        "rule": (238, -1, d), "accent": (209, -1, b), "head": (252, -1, b), "key": (215, -1, b),
        "name": (255, -1, b), "name_sel": (231, -1, b), "edit": (231, 24, b), "edit_sel": (24, 231, b), "tab_on": (231, 24, b), "tab": (246, -1, d),
        "work": (215, -1, b), "wait": (221, -1, b), "done": (108, -1, d), "fail": (203, -1, b), "warn": (221, -1, b),
        "tag_review": (16, 183, d), "tag_ticket": (16, 117, d), "tag_pr": (16, 151, d), "tag_custom": (252, 238, d),
        "tag_adr": (16, 180, d), "tag_dr": (16, 224, d),
    }
    for i, c in enumerate([75, 176, 114, 215, 141, 80]):
        t[f"proj{i}"] = (c, -1, b)
    t.update({"chip_bad": (16, 210, d), "chip_warn": (16, 222, d), "chip_good": (16, 151, d), "chip_info": (252, 238, d)})
    for i, c in enumerate([216, 229, 187, 175, 109, 223]):
        t[f"tag_c{i}"] = (16, c, d)
    return t


class Painter:
    SEL_BG = 236

    def __init__(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        self.theme = _theme()
        self.attrs: dict[tuple[str, bool], int] = {}

    def _c(self, c: int) -> int:
        return c if c < curses.COLORS else (-1 if c < 0 else (c % 8 if curses.COLORS >= 8 else -1))

    def attr(self, style: str, selected: bool) -> int:
        key = (style, selected)
        if key not in self.attrs:
            fg, bg, extra = self.theme.get(style, self.theme["normal"])
            if selected and bg == -1:
                bg = self.SEL_BG
            pair = len(self.attrs) + 1
            try:
                curses.init_pair(pair, self._c(fg), self._c(bg))
                self.attrs[key] = curses.color_pair(pair) | extra
            except curses.error:
                self.attrs[key] = extra
        return self.attrs[key]

    def highlight(self, scr, region: tuple[Cell, Cell]) -> None:
        h, w = scr.getmaxyx()
        (y0, x0), (y1, x1) = region
        for y in range(max(0, y0), min(h, y1 + 1)):
            start = x0 if y == y0 else 0
            end = min(w - 1, x1 if y == y1 else w - 1)
            if end >= start:
                try:
                    scr.chgat(y, start, end - start + 1, curses.A_REVERSE)
                except curses.error:
                    pass

    def draw(self, scr, lines: list[Line]) -> None:
        h, w = scr.getmaxyx()
        for y, line in enumerate(lines[:h]):
            scr.move(y, 0)
            scr.clrtoeol()
            if line.selected:
                try:
                    scr.addstr(y, 0, " " * (w - 1), self.attr("normal", True))
                except curses.error:
                    pass
            x = 0
            for text, style in line.segs:
                if x >= w or not text:
                    continue
                try:
                    scr.addstr(y, x, text[: max(0, w - x - (1 if y == h - 1 else 0))], self.attr(style, line.selected))
                except curses.error:
                    pass
                x += cell_width(text)


KEYS = {
    curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_RIGHT: "right", curses.KEY_LEFT: "left",
    curses.KEY_HOME: "home", curses.KEY_END: "end", curses.KEY_DC: "delete", 23: "ctrl+w",
    curses.KEY_BACKSPACE: "backspace", 127: "backspace", 8: "backspace",
    13: "enter", curses.KEY_ENTER: "enter", 10: "ctrl+j", 22: "ctrl+v", 25: "ctrl+y", 5: "ctrl+e", 9: "tab",
    18: "ctrl+r", 20: "ctrl+t", 24: "ctrl+x", 19: "ctrl+s", 6: "ctrl+f", 16: "ctrl+p", 7: "ctrl+g", 2: "ctrl+b", 21: "ctrl+u", 3: "ctrl+c", 1: "ctrl+a", 12: "ctrl+l", 15: "ctrl+o",
}


CSI_KEYS = {  # escape sequences curses did not decode itself (the part after ESC [)
    "D": "left", "C": "right", "H": "home", "F": "end", "1~": "home", "7~": "home", "4~": "end", "8~": "end", "3~": "delete",
    "1;5D": "ctrl+left", "1;5C": "ctrl+right", "1;3D": "alt+left", "1;3C": "alt+right", "3;5~": "ctrl+delete",
}
PASTE_START, PASTE_END = "[200~", "\x1b[201~"


def _pending(scr, wait_ms: int = 0):
    """The next key if one is already waiting (or arrives within wait_ms), else None."""
    scr.timeout(wait_ms)
    try:
        return scr.get_wch()
    except curses.error:
        return None
    finally:
        scr.timeout(250)


def _bracketed_paste(scr) -> str:
    """Everything up to the closing marker of a bracketed paste, which the terminal wraps pasted text in."""
    text = ""
    while True:
        nxt = _pending(scr, 200)
        if nxt is None:
            return text  # the closing marker never came; keep what arrived
        if isinstance(nxt, str):
            text += nxt
            if text.endswith(PASTE_END):
                return text[: -len(PASTE_END)]


def read_key(scr) -> tuple[str, str]:
    """Returns (key, char). Blocks up to the tick timeout; ('tick','') on timeout.

    Several characters arriving at once are a paste: they come back as one ('paste', text) event, so a
    newline inside pasted text can never act as Enter. Terminals that bracket pastes are handled too."""
    try:
        ch = scr.get_wch()
    except curses.error:
        return "tick", ""
    except KeyboardInterrupt:
        return "tick", ""  # an outside SIGINT must not quit the board
    if isinstance(ch, str):
        if ch == "\x1b":
            nxt = _pending(scr)
            if nxt is None:
                return "esc", ""
            if nxt == "[":
                seq = ""
                while len(seq) < 5:
                    more = _pending(scr, 50)
                    if not isinstance(more, str):
                        break
                    seq += more
                    if more == "~" or more.isalpha():
                        break
                if seq == PASTE_START[1:]:
                    return "paste", _bracketed_paste(scr)
                return CSI_KEYS.get(seq, "tick"), ""
            if nxt == "\x7f":
                return "alt+backspace", ""
            if isinstance(nxt, str) and nxt in "bf":
                return ("alt+left" if nxt == "b" else "alt+right"), ""
            if isinstance(nxt, str) and nxt in "123456":
                return f"alt+{nxt}", ""
            return "tick", ""
        if ch == "\t" or ch >= " " or ch in "\r\n":
            burst = ch
            while True:
                more = _pending(scr)
                if more is None:
                    break
                if not isinstance(more, str) or more == "\x1b":
                    curses.unget_wch(more)
                    break
                burst += more
            if len(burst) > 1:
                return "paste", burst
            ch = burst
        if ch == " ":
            return "space", " "
        code = ord(ch)
        if code in KEYS:
            return KEYS[code], ""
        if code < 32:
            return "tick", ""
        return "char", ch
    if ch == curses.KEY_RESIZE:
        return "tick", ""
    if ch == curses.KEY_MOUSE:
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return "tick", ""
        return "mouse", f"{my} {mx} {bstate}"
    name = curses.keyname(ch).decode(errors="ignore")
    if name == "kUP3":
        return "alt+up", ""
    if name == "kDN3":
        return "alt+down", ""
    named = {"kLFT5": "ctrl+left", "kRIT5": "ctrl+right", "kLFT3": "alt+left", "kRIT3": "alt+right", "kDC5": "ctrl+delete", "kHOM": "home", "kEND": "end"}
    if name in named:
        return named[name], ""
    return KEYS.get(ch, "tick"), ""


def copy_note(how: str) -> str:
    """Where the text went; honest when the only route was the terminal's own OSC 52, which many terminals ignore."""
    if how == "terminal":
        return "sent to your terminal (OSC 52); if it did not arrive, install xclip (sudo apt install xclip)"
    return f"via {how}"


def read_clipboard(which=shutil.which, run=subprocess.run) -> Optional[str]:
    """The system clipboard's text, for ctrl+v (ctrl+shift+v is the terminal's own paste)."""
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"], ["pbpaste"]):
        if which(cmd[0]):
            try:
                proc = run(cmd, capture_output=True, text=True, timeout=2)
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
    return None


def set_bracketed_paste(on: bool) -> None:
    sys.stdout.write("\x1b[?2004h" if on else "\x1b[?2004l")
    sys.stdout.flush()


class Loader(threading.Thread):
    def __init__(self, board: Board, demo: bool):
        super().__init__(daemon=True)
        self.board, self.demo, self.error = board, demo, ""
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.demo and not self.board.attached:  # nobody sees the board while a session is open
                    self.board.set_sessions(load_sessions())
                self.error = ""
            except Exception as exc:  # keep the board alive; surface the reason in the footer
                self.error = f"refresh failed: {exc}"
            self.pause()

    def pause(self) -> None:
        """Until the next refresh is due, or sooner when the board asks (coming back from a session)."""
        if self.board.sessions_due.wait(REFRESH_SECONDS):
            self.board.sessions_due.clear()


class UsageLoader(threading.Thread):
    def __init__(self, board: Board, scanner: UsageScanner):
        super().__init__(daemon=True)
        self.board, self.scanner = board, scanner
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.scanner.scan()
                self.board.usage = summarize(self.scanner.turns(), time.time())
            except Exception:  # the panel is decoration; never let it take the board down
                pass
            self.stop.wait(20)


class DiskLoader(threading.Thread):
    """Free space every half minute; the size of Claude Code's data and of the worktrees every five minutes."""

    def __init__(self, board: Board, usage: Callable = shutil.disk_usage, sizer: Callable[[str], int] = dir_size):
        super().__init__(daemon=True)
        self.board, self.usage, self.sizer = board, usage, sizer
        self.stop = threading.Event()
        self.claude_bytes: Optional[int] = None
        self.worktree_bytes: Optional[int] = None
        self.sizes_at = 0.0

    def paths(self) -> list[str]:
        found = [str(Path.home()), self.board.launch_cwd]
        found += [s.project_path for s in list(self.board.sessions) if s.project_path]
        return list(dict.fromkeys(found))

    def worktree_dirs(self) -> list[str]:
        roots = {os.path.realpath(os.path.join(p, ".claude", "worktrees")) for p in self.paths()}
        return sorted(r for r in roots if os.path.isdir(r))

    def measure_once(self, with_sizes: bool = True) -> DiskInfo:
        if with_sizes and time.time() - self.sizes_at >= DISK_SIZES_REFRESH_SECONDS:
            self.board.disk = DiskInfo(read_disks(self.paths(), self.usage), self.claude_bytes, self.worktree_bytes)  # show free space first
            self.claude_bytes = self.sizer(str(Path.home() / ".claude"))
            self.worktree_bytes = sum(self.sizer(d) for d in self.worktree_dirs())
            self.sizes_at = time.time()
        self.board.disk = DiskInfo(read_disks(self.paths(), self.usage), self.claude_bytes, self.worktree_bytes)
        return self.board.disk

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.measure_once()
            except Exception:  # decoration: never take the board down
                pass
            self.stop.wait(DISK_REFRESH_SECONDS)


class LimitsLoader(threading.Thread):
    def __init__(self, board: Board):
        super().__init__(daemon=True)
        self.board = board
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            self.board.limits = read_limits()
            self.stop.wait(5)


class PrLoader(threading.Thread):
    """Refreshes pull requests on the configured interval, or when asked; the interval is read every second
    so changing it in the settings takes effect at once."""

    def __init__(self, board: Board, run=subprocess.run):
        super().__init__(daemon=True)
        self.board, self.run_ = board, run
        self.stop = threading.Event()
        self.remotes: dict[str, Optional[prs.Remote]] = {}

    def projects(self) -> dict[str, prs.Remote]:
        found: dict[str, prs.Remote] = {}
        for s in list(self.board.sessions):
            path = s.project_path
            if not path or path in found:
                continue
            if path not in self.remotes:
                self.remotes[path] = prs.resolve_remote(path, self.run_)
            if self.remotes[path]:
                found[path] = self.remotes[path]
        return found

    def refresh(self) -> None:
        board = self.board
        board.pr_busy = True
        try:
            board.prs = board.without_approved(prs.fetch_all(self.projects(), dict(board.meta.settings), self.run_, time.time()))
            outcome = self.outcome(board)
        except Exception as exc:  # keep the board alive and say what went wrong
            board.prs = prs.PrData(errors=[f"refresh failed: {exc}"], fetched_at=time.time())
            outcome = f"✗ refresh failed: {exc}"
        finally:
            board.pr_busy = False
        if board.msg.startswith(REFRESHING):  # someone asked for this refresh and is still looking: say how it went
            board.msg = outcome

    @staticmethod
    def outcome(board: Board) -> str:
        c, problems = board.pr_counts(), len(board.prs.errors)
        text = f"✓ pull requests refreshed · {c['review']} to review · {c['attention']} of yours need you"
        return text + (f" · ⚠ {problems} source{'s' if problems != 1 else ''} had problems (see the PR views)" if problems else "")

    def run(self) -> None:
        last, first = 0.0, True
        while not self.stop.is_set():
            interval = self.board.pr_interval()
            if first or self.board.refresh_requested.is_set() or (interval and time.time() - last >= interval):
                first = False
                self.board.refresh_requested.clear()
                self.refresh()
                last = time.time()
            self.stop.wait(1)


def run_cli(args: list[str], cwd: Optional[str] = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([CLAUDE, *args], capture_output=True, text=True, cwd=cwd, timeout=timeout)


def open_url(url: str) -> str:
    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener:
        try:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return f"opened {url}"
        except OSError:
            pass
    return f"copied {url} ({copy_to_clipboard(url)})"


def review_prompts(board: Board, items: list) -> list[tuple[str, str, str, int]]:
    """One (project name, folder, prompt, PR count) per project: a session reviews every PR chosen in it."""
    by_path: dict[str, list] = {}
    for pr in items:
        by_path.setdefault(board.pr_project(pr)[1], []).append(pr)
    out = []
    for path, group in by_path.items():
        gitlab = group[0].provider == "gitlab"
        template = board.meta.settings.get(
            "review_command_gitlab" if gitlab else "review_command", DEFAULT_REVIEW_COMMAND_GITLAB if gitlab else DEFAULT_REVIEW_COMMAND
        )
        urls = " ".join(p.url for p in sorted(group, key=lambda p: p.number))
        prompt = template.replace("{urls}", urls) if "{urls}" in template else f"{template} {urls}"
        out.append((board.pr_project(group[0])[0], path, prompt, len(group)))
    return out


def start_review_sessions(board: Board, items: list) -> None:
    started, failed = [], []
    for name, path, prompt, count in review_prompts(board, items):
        try:
            r = run_cli(bg_command(prompt, board.model()), cwd=path or None)
        except (OSError, subprocess.SubprocessError) as exc:
            failed.append(f"{name}: {exc}")
            continue
        if r.returncode == 0:
            started.append(f"{count} in {name}")
        else:
            failed.append(f"{name}: {((r.stderr or r.stdout).strip().splitlines() or ['failed'])[0]}")
    board.marked.clear()
    board.msg = ("✓ review session started for " + ", ".join(started) if started else "") + (("  ✗ " + "; ".join(failed)) if failed else "")
    board.set_sessions(load_sessions())


def conflict_prompt(pr) -> str:
    gitlab = pr.provider == "gitlab"
    return CONFLICT_PROMPT.format(kind="MR" if gitlab else "PR", ref=pr_ref(pr), url=pr.url, head=pr.head or "the PR branch", base=pr.base or "the base branch")


def start_conflict_session(board: Board, pr) -> None:
    """A background session in the PR's project that merges the base into the branch, resolves the conflicts and pushes."""
    name, path = board.pr_project(pr)
    try:
        r = run_cli(bg_command(conflict_prompt(pr), board.model()), cwd=path or None)
    except (OSError, subprocess.SubprocessError) as exc:
        board.msg = f"✗ could not start fixing {pr_ref(pr)}: {exc}"
        return
    if r.returncode == 0:
        board.msg = f"✓ fixing the conflicts in {pr_ref(pr)} in {name}: a ⟳ marks the PR while the session works"
    else:
        board.msg = f"✗ could not start fixing {pr_ref(pr)}: {((r.stderr or r.stdout).strip().splitlines() or ['failed'])[0]}"
    board.set_sessions(load_sessions())


ACTION_LABEL = {"stop": "stopping…", "remove": "removing…"}


def run_session_action(board: Board, effect: Effect) -> None:
    """Stops or deletes a session (blocking: this is the slow part) and reports how it went."""
    s = next((x for x in board.sessions if x.id == effect.session_id), None)
    label = f"“{board.name_of(s)}”" if s else effect.session_id
    try:
        if effect.kind == "remove":
            run_cli(["stop", effect.session_id])  # a session whose process is still alive refuses to be deleted
        r = run_cli(["stop" if effect.kind == "stop" else "rm", effect.session_id])
    except (OSError, subprocess.SubprocessError) as exc:
        board.msg = f"✗ could not {effect.kind} {label}: {exc}"
        return
    verb = "stopped" if effect.kind == "stop" else "deleted"
    if r.returncode == 0:
        board.msg = f"✓ {verb} {label}"
    else:
        reason = next((line for line in (r.stderr + "\n" + r.stdout).splitlines() if line.strip()), "no output")
        board.msg = f"✗ could not {effect.kind} {label}: {reason.strip()}"
    board.set_sessions(load_sessions())


def run_approval(board: Board, pr, run=subprocess.run) -> None:
    """Approves the PR (blocking: this is the slow part); once it worked the PR leaves the review queue."""
    reason = prs.approve(pr, run)
    if reason:
        board.msg = f"✗ could not approve #{pr.number}: {reason}"
        return
    flat = [p.key for p in board.pr_flat()]
    if board.pr_sel == pr.key:  # the selection moves on to the next PR, as after a delete
        rest = [k for k in flat if k != pr.key]
        i = flat.index(pr.key) if pr.key in flat else 0
        board.pr_sel = rest[min(i, len(rest) - 1)] if rest else None
    board.approved.add(pr.key)
    board.marked.discard(pr.key)
    if board.prs:
        board.without_approved(board.prs)
    board.msg = f"✓ approved #{pr.number} “{pr.title}”"


def start_approval(board: Board, pr) -> threading.Thread:
    """Runs the approval in the background so the board keeps drawing; the row says what is going on."""
    board.pr_busy_keys[pr.key] = "approving…"

    def work() -> None:
        try:
            run_approval(board, pr)
        finally:
            board.pr_busy_keys.pop(pr.key, None)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def start_session_action(board: Board, effect: Effect) -> threading.Thread:
    """Runs a stop or delete in the background so the board keeps drawing; the row shows what is going on."""
    if effect.kind == "remove":
        board.successors[effect.session_id] = board.choose_successor(effect.session_id)
    board.busy[effect.session_id] = ACTION_LABEL[effect.kind]
    board.msg = f"{ACTION_LABEL[effect.kind]}"

    def work() -> None:
        try:
            run_session_action(board, effect)
        finally:
            board.busy.pop(effect.session_id, None)

    worker = threading.Thread(target=work, daemon=True)
    board.workers.append(worker)
    worker.start()
    return worker


def run_dispatch(board: Board, effect: Effect) -> Optional[str]:
    """Starts the background session (blocking: this is the slow part); returns its id once it is known."""
    r = run_cli(bg_command(effect.prompt, effect.model), cwd=effect.cwd or None)
    first = (r.stdout or r.stderr).strip().split("\n")[0] or "session started"
    selecting = register_new_session(board, effect, first, r.returncode == 0)
    board.msg = first + (f" · tagged {', '.join(effect.tags)}" if effect.tags else "") + (f" · on {model_label(effect.model)}" if effect.model else "") + (" · → opens it" if selecting else "")
    return board.select_when_seen[0] if selecting and board.select_when_seen else None


def start_dispatch(board: Board, effect: Effect) -> threading.Thread:
    """Starts a new session without freezing the board: its row appears at once as "starting…"."""
    sid = board.start_placeholder(effect.prompt, effect.cwd)
    board.msg = "starting…"

    def work() -> None:
        real_id = None
        try:
            real_id = run_dispatch(board, effect)
            board.set_sessions(load_sessions())
        except Exception as exc:  # the row must not be left spinning, whatever went wrong
            board.msg = f"✗ dispatch failed: {exc}"
        finally:
            board.finish_placeholder(sid, real_id)

    worker = threading.Thread(target=work, daemon=True)
    board.workers.append(worker)
    worker.start()
    return worker


def register_new_session(board: Board, effect: Effect, first_line: str, started: bool) -> bool:
    """Tags a session just started from the prompt and arranges for it to be selected once it shows up,
    so one → opens it. Returns True when it will be selected."""
    found = re.search(r"\b([0-9a-f]{8})\b", first_line) if started else None
    if found:
        if effect.tags:
            board.meta.update(found.group(1), tags=effect.tags)
        board.select_when_seen = (found.group(1), board.now())
        return True
    if effect.tags:
        board.expect_session(effect.prompt, effect.cwd, effect.tags)
    return False


def perform(scr, board: Board, effect: Effect, loader: Loader) -> bool:
    """Runs an effect; returns False when the board should quit."""
    if effect.kind == "quit":
        return False
    if effect.kind == "refresh_prs":
        board.refresh_requested.set()
        board.msg = REFRESHING
        return True
    if effect.kind == "browse":
        board.msg = open_url(effect.url)
        return True
    if effect.kind == "copy":
        how = copy_to_clipboard(effect.text)
        lines = effect.text.count("\n") + 1
        board.msg = f"{'cut' if effect.cut else 'copied'} the prompt ({len(effect.text)} characters{f', {lines} lines' if lines > 1 else ''}) {copy_note(how)}"
        return True
    if effect.kind == "review":
        start_review_sessions(board, effect.items)
        return True
    if effect.kind == "fix_conflicts":
        start_conflict_session(board, effect.items[0])
        return True
    if effect.kind == "approve":
        start_approval(board, effect.items[0])
        return True
    if effect.kind == "attach":
        curses.def_prog_mode()  # remember the board's terminal settings
        scr.keypad(False)  # the terminal stays raw: attach_session relays every key to the session
        sys.stdout.write(HAND_OVER)
        sys.stdout.flush()
        result = None
        board.attached = True
        try:
            result = attach_session(CLAUDE, effect.session_id)
        finally:
            board.attached = False
            board.sessions_due.set()  # the list waited while the session was open
            sys.stdout.write(SYNC_BEGIN + HAND_OVER + ENTER_ALT_SCREEN)
            sys.stdout.flush()
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            curses.reset_prog_mode()
            scr.keypad(True)
            enable_mouse()
            set_bracketed_paste(True)
            scr.clearok(True)  # repaint everything: the session drew over our screen
            board.sync_open = True
        opened = next((x for x in board.sessions if x.id == effect.session_id), None)
        notice = attach_failure_notice(result, board.name_of(opened) if opened else effect.session_id) if result else None
        if notice:
            board.msg = notice[0]
            board.peek = {"title": f"could not open {board.name_of(opened) if opened else effect.session_id}", "lines": notice[1], "top": 0, "wrap": True}
        return True
    try:
        if effect.kind == "peek":
            out = run_cli(["logs", effect.session_id]).stdout
            lines = ANSI_RE.sub("", out).replace("\r", "").split("\n")
            s = next((x for x in board.sessions if x.id == effect.session_id), None)
            board.peek = {"title": f"output · {board.name_of(s) if s else effect.session_id}", "lines": lines, "top": 10**6}
        elif effect.kind in ("stop", "remove"):
            start_session_action(board, effect)
        elif effect.kind == "dispatch":
            start_dispatch(board, effect)
    except (OSError, subprocess.SubprocessError) as exc:
        board.msg = f"{effect.kind} failed: {exc}"
    return True


def set_motion_tracking(on: bool) -> None:
    """ncurses only asks the terminal for button press/release (mode 1000). Mode 1002 adds motion while a
    button is held, which is what lets the selection be drawn as the mouse moves, not only on release."""
    sys.stdout.write("\x1b[?1002h" if on else "\x1b[?1002l")
    sys.stdout.flush()


def enable_mouse() -> None:
    curses.mouseinterval(0)  # report press and release separately so drags can be told from clicks
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    set_motion_tracking(True)


def main_loop(scr, demo: bool) -> None:
    curses.set_escdelay(25)
    curses.raw()  # cbreak leaves ctrl+s as XOFF flow control and would freeze the screen
    curses.nonl()  # Enter is CR, so ctrl+j (LF) can be told apart and used as "new line"
    set_bracketed_paste(True)
    scr.keypad(True)
    scr.timeout(250)
    enable_mouse()
    selection = Selection()
    screen_text: list[str] = []
    painter = Painter()
    board = Board(Meta(), os.getcwd())
    board.known_projects_fn = known_projects
    if demo:
        board.set_sessions(demo_sessions(board.now()))
    else:
        try:
            board.set_sessions(load_sessions())
        except Exception as exc:
            board.msg = f"could not load sessions: {exc}"
    loader = Loader(board, demo)
    loader.start()
    if demo:
        board.usage = demo_usage()
        board.limits = demo_limits(board.now())
        board.disk = demo_disk()
        board.prs = demo_prs(board.now())
    else:
        UsageLoader(board, UsageScanner()).start()
        LimitsLoader(board).start()
        PrLoader(board).start()
        DiskLoader(board).start()
    try:
        while True:
            h, w = scr.getmaxyx()
            if w < 50 or h < 10:
                scr.erase()
                scr.addstr(0, 0, "terminal too small (min 50x10)"[: max(0, w - 1)])
                scr.refresh()
            else:
                if loader.error and not board.msg:
                    board.msg = loader.error
                lines, caret = compose(board, w, h, demo)
                screen_text = [plain(l) for l in lines]
                painter.draw(scr, lines)
                if selection.region():
                    painter.highlight(scr, selection.region())
                if caret:
                    curses.curs_set(1)
                    scr.move(caret[0], min(caret[1], w - 1))
                else:
                    curses.curs_set(0)
                scr.refresh()
                if board.sync_open:
                    board.sync_open = False
                    sys.stdout.write(SYNC_END)
                    sys.stdout.flush()
            key, ch = read_key(scr)
            if key == "ctrl+v":
                clip = read_clipboard()
                if clip:
                    key, ch = "paste", clip
                else:
                    board.msg = "nothing to paste: the clipboard is empty, or no clipboard tool is installed (sudo apt install xclip); ctrl+shift+v pastes through your terminal"
                    continue
            if key == "mouse":
                y, x, bstate = (int(v) for v in ch.split())
                copied = mouse_event(selection, board, y, x, bstate, screen_text)
                if copied:
                    how = copy_to_clipboard(copied)
                    board.msg = f"copied {len(copied)} characters {copy_note(how)}"
                queued, board.queued_effect = board.queued_effect, None
                if queued and not perform(scr, board, queued, loader):
                    break
                continue
            if key != "tick":
                selection.clear()
            effect = board.handle(key, ch)
            if effect and not perform(scr, board, effect, loader):
                break
    finally:
        loader.stop.set()
        board.sessions_due.set()
        set_bracketed_paste(False)
        set_motion_tracking(False)


# ───────────────────────────── demo data & entry point ─────────────────────────────


def demo_sessions(now: float) -> list[Session]:
    def mk(i, name, state, project, detail, mins, intent="", branch="", tokens=0, age_done=0):
        path = f"/srv/www/{project}"
        return Session(
            id=f"d{i:07d}", name=name, state=state, cwd=path, project=project, project_path=path, branch=branch,
            detail=detail, intent=intent, tokens=tokens, started_at=now - mins * 60,
            updated_at=now - (age_done or mins) * 60, auto_tags=derive_tags(name, intent, detail, branch),
        )

    return [
        mk(1, "review checkout refactor", "awaiting", "billing-api", "Approve running `composer test` against the staging DB?", 7,
           "Code review of PR #482 for AM-1234", "worktree-checkout-refactor"),
        mk(2, "search indexing rewrite", "working", "mobile-app", "Batch 4 of 9 reindexed; waiting for the next shard", 94,
           "reindexing for #636", "worktree-search-indexing"),
        mk(3, "report cache layer", "working", "mobile-app", "Running the full test suite after the style fix", 31, branch="worktree-report-cache"),
        mk(4, "listing sync retries", "working", "web-app", "Running the failing job spec in a loop to find the flake", 12, "Fix flaky sync, ticket WEB-5521"),
        mk(5, "onboarding flow plan", "done", "mobile-app", "Draft written: screens, copy and open questions marked [TODO]", 60, age_done=60 * 24 * 3),
        mk(6, "docs site migration", "done", "mobile-app", "Notes on moving the docs to the new generator", 40, age_done=60 * 24 * 13),
        mk(7, "MR !328 follow-ups", "done", "mobile-app", "Dialog state now lives in the store; the hint closes the popup", 55, "Review MR !328", age_done=60 * 5),
        mk(8, "failed deploy dry-run", "failed", "infra-tools", "terraform plan exited 1: backend config missing", 20, age_done=60 * 30),
    ]


def demo_prs(now: float) -> prs.PrData:
    day = 86400
    mine = [
        prs.MyPr("gitlab", "acme/mobile-app", 341, "feature(sync): offline mode for the sync screen", "https://gitlab.com/acme/mobile-app/-/merge_requests/341", "master", "offline-sync",
                 created_at=now - 5 * day, updated_at=now - 4 * day, merge="conflict", decision="none", requested=("reviewer",)),
        prs.MyPr("gitlab", "acme/mobile-app", 342, "refactor(sync): extract the retry policy into its own class", "https://gitlab.com/acme/mobile-app/-/merge_requests/342", "offline-sync", "retry-policy",
                 depth=1, parent=341, created_at=now - 5 * day, updated_at=now - 4 * day, merge="clean", decision="none", requested=("reviewer",)),
        prs.MyPr("github", "acme/web-app", 511, "WEB-5521: Fix listing sync retries", "https://github.com/acme/web-app/pull/511", "main", "WEB-5521-retries",
                 created_at=now - 3 * day, updated_at=now - 2 * day, decision="changes", merge="clean", checks="fail", requested=("maria",), reviewed=True, ticket="WEB-5521"),
        prs.MyPr("github", "acme/web-app", 498, "WEB-5399: CSV export for the citations report", "https://github.com/acme/web-app/pull/498", "main", "WEB-5399-csv",
                 created_at=now - 9 * day, updated_at=now - 1 * day, decision="approved", merge="clean", checks="pass", requested=("maria",), reviewed=True, ticket="WEB-5399"),
        prs.MyPr("github", "acme/billing-api", 233, "Add invoice export", "https://github.com/acme/billing-api/pull/233", "main", "invoice-export",
                 created_at=now - 2 * day, updated_at=now - 2 * day, decision="required", merge="clean", checks="pending"),
        prs.MyPr("github", "acme/billing-api", 234, "Invoice PDF layout", "https://github.com/acme/billing-api/pull/234", "invoice-export", "invoice-pdf",
                 depth=1, parent=233, created_at=now - 1 * day, updated_at=now - 1 * day, draft=True, merge="clean"),
    ]
    review = [
        prs.ReviewPr("github", "acme/billing-api", 482, "Refactor checkout totals", "https://github.com/acme/billing-api/pull/482", "maria", created_at=now - 2 * day, flags=("re-requested",)),
        prs.ReviewPr("github", "acme/billing-api", 231, "Bump stripe-php to 15.2", "https://github.com/acme/billing-api/pull/231", "dave", created_at=now - 1 * day),
        prs.ReviewPr("github", "acme/web-app", 479, "Add CSV export to the citations report", "https://github.com/acme/web-app/pull/479", "tom", created_at=now - 5 * day, flags=("teammate-approved",)),
    ]
    repos = {"github:acme/billing-api": "/srv/www/billing-api", "github:acme/web-app": "/srv/www/web-app", "gitlab:acme/mobile-app": "/srv/www/mobile-app"}
    return prs.PrData(mine=mine, review=review, errors=[], fetched_at=now - 4 * 60, repos=repos)


def demo_disk() -> DiskInfo:
    return DiskInfo([DiskUsage("Disk", 95 << 30, 61 << 30, 30 << 30)], int(1.3 * (1 << 30)), 3 << 30)


def demo_limits(now: float) -> Limits:
    return Limits(
        [LimitWindow("five_hour", 38.5, now + 2 * 3600 + 14 * 60), LimitWindow("seven_day", 71, now + 3 * 86400 + 5 * 3600)],
        now - 3 * 60,
    )


def demo_usage() -> UsageSummary:
    return UsageSummary(
        periods={
            "today": {"turns": 412, "out": 96_000, "inp": 1_200_000, "cache_read": 31_000_000, "sessions": 3},
            "7d": {"turns": 3100, "out": 812_000, "inp": 9_000_000, "cache_read": 240_000_000, "sessions": 9},
            "30d": {"turns": 11900, "out": 2_400_000, "inp": 30_000_000, "cache_read": 900_000_000, "sessions": 31},
            "all": {"turns": 18500, "out": 5_100_000, "inp": 60_000_000, "cache_read": 2_100_000_000, "sessions": 52},
        },
        daily=[120, 340, 90, 0, 0, 410, 560, 300, 80, 0, 220, 640, 480, 412],
        models=[("fable-5", 1_700_000), ("opus-4-6", 520_000), ("sonnet-4-5", 180_000)],
        projects=[("mobile-app", 9100), ("web-app", 1800), ("billing-api", 600), ("infra-tools", 400)],
        first_day="2026-01-11",
    )


def dump(width: int, height: int, demo: bool, view: str = "status") -> None:
    board = Board(Meta(Path(os.devnull)), os.getcwd())
    board.meta._save = lambda: None
    board.set_sessions(demo_sessions(board.now()) if demo else load_sessions())
    board.limits = demo_limits(board.now()) if demo else read_limits()
    board.disk = demo_disk() if demo else DiskLoader(board).measure_once()
    if demo:
        board.usage = demo_usage()
    else:
        scanner = UsageScanner()
        scanner.scan()
        board.usage = summarize(scanner.turns(), time.time())
    board.mode = view
    if demo:
        board.prs = demo_prs(board.now())
    elif view in PR_VIEWS:
        loader = PrLoader(board)
        loader.refresh()
    board.sel = board.flat()[1].id if len(board.flat()) > 1 else None
    board.pr_sel = board.pr_flat()[0].key if board.pr_flat() else None
    lines, _ = compose(board, width, height, demo)
    print("\n".join(plain(l).rstrip() for l in lines))


Check = tuple[str, str, str]  # (status: ok | warn | fail, what, detail)


def doctor_checks(run: Callable = subprocess.run, which: Callable = shutil.which, env: Optional[dict] = None) -> list[Check]:
    """Everything the board relies on, so a first run that does not work says why and what to do."""
    env = os.environ if env is None else env
    out: list[Check] = []

    def ok(what: str, detail: str = "") -> None:
        out.append(("ok", what, detail))

    def warn(what: str, detail: str) -> None:
        out.append(("warn", what, detail))

    def fail(what: str, detail: str) -> None:
        out.append(("fail", what, detail))

    def call(cmd: list[str], timeout: int = 15) -> Optional[subprocess.CompletedProcess]:
        try:
            return run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    if sys.version_info >= (3, 10):
        ok("Python", ".".join(map(str, sys.version_info[:3])))
    else:
        fail("Python", f"{sys.version_info[0]}.{sys.version_info[1]} is too old: 3.10 or newer is needed")
    term = env.get("TERM", "")
    if not term or term == "dumb":
        fail("Terminal", "TERM is not set to a real terminal type")
    elif "256color" in term or env.get("COLORTERM"):
        ok("Terminal", f"{term} with colours")
    else:
        warn("Terminal", f"TERM={term}: colours will look off; a 256-colour terminal (xterm-256color) is best")
    encoding = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or locale.getpreferredencoding(False) or "").upper()
    if "UTF" in encoding:
        ok("Unicode", encoding)
    else:
        warn("Unicode", "the locale is not UTF-8, so icons and the 🦎 may not draw; try LANG=en_US.UTF-8")

    if not which(CLAUDE):
        fail("Claude Code", f"`{CLAUDE}` is not on your PATH: install Claude Code first (https://claude.com/claude-code)")
    else:
        version = call([CLAUDE, "--version"])
        ok("Claude Code", (version.stdout.strip().splitlines() or ["found"])[0] if version else "found")
        listing = call([CLAUDE, "agents", "--json", "--all"], timeout=30)
        try:
            count = len(json.loads(listing.stdout)) if listing and listing.returncode == 0 else None
        except ValueError:
            count = None
        if count is None:
            fail("Background sessions", "`claude agents --json --all` did not work: update Claude Code, then try it by hand")
        else:
            ok("Background sessions", f"`claude agents --json` works ({count} session{'s' if count != 1 else ''} listed)")

    if which("git"):
        ok("git", "used to tell which GitHub/GitLab repository a project belongs to")
    else:
        warn("git", "not found: pull-request views need it")
    for tool, what, how in (("gh", "GitHub pull requests", "gh auth login"), ("glab", "GitLab merge requests", "glab auth login")):
        if not which(tool):
            warn(tool, f"optional, not installed: needed for {what}")
        else:
            status = call([tool, "auth", "status"], timeout=10)
            if status and status.returncode == 0:
                ok(tool, f"installed and logged in ({what})")
            else:
                warn(tool, f"installed but not logged in: run `{how}` for {what}")

    clipboard = next((t for t in ("wl-copy", "xclip", "xsel", "pbcopy") if which(t)), None)
    if clipboard:
        ok("Clipboard", f"{clipboard}: copy, cut and ctrl+v work")
    else:
        hint = "sudo apt install wl-clipboard" if env.get("XDG_SESSION_TYPE") == "wayland" else "sudo apt install xclip"
        warn("Clipboard", f"no clipboard tool: copying relies on terminal support and ctrl+v cannot paste. Install one ({hint})")

    if LIMITS_PATH.exists():
        age = time.time() - LIMITS_PATH.stat().st_mtime
        ok("Account limits", f"reading found, updated {fmt_until(age)} ago")
    else:
        warn("Account limits", "no reading yet: install the agent-board-limits mod (see the README) to show your 5-hour and weekly limits")
    try:
        META_PATH.parent.mkdir(parents=True, exist_ok=True)
        ok("Settings", str(META_PATH))
    except OSError as exc:
        fail("Settings", f"cannot create {META_PATH.parent}: {exc}")
    return out


def run_doctor() -> int:
    marks = {"ok": "✓", "warn": "!", "fail": "✗"}
    checks = doctor_checks()
    print(f"{APP_MARK} {APP_NAME} {__version__}: checking your setup\n")
    for status, what, detail in checks:
        print(f"  {marks[status]} {what:<20} {detail}")
    fails = sum(1 for c in checks if c[0] == "fail")
    warns = sum(1 for c in checks if c[0] == "warn")
    print()
    if fails:
        print(f"{fails} problem{'s' if fails != 1 else ''} to fix before the board will work.")
    elif warns:
        print(f"Ready to go. {warns} optional thing{'s' if warns != 1 else ''} would make it better (the ! lines).")
    else:
        print("Everything is in place. Start it with: agent-board")
    return 1 if fails else 0


def update_self(run: Callable = subprocess.run, here: Optional[Path] = None) -> int:
    """`agent-board --update`: pull the latest version when the board was installed by install.sh (a git checkout)."""
    here = here or Path(__file__).resolve().parent
    if not (here / ".git").exists():
        print("This copy was not installed by install.sh, so it cannot update itself.\n"
              "Re-run the installer, or: pipx upgrade agent-board / pip install --upgrade agent-board")
        return 1
    proc = run(["git", "-C", str(here), "pull", "--ff-only"], capture_output=True, text=True)
    print((proc.stdout or proc.stderr).strip())
    if proc.returncode != 0:
        print("Could not update. Re-running the installer replaces the checkout.")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-board", description=f"{APP_NAME} {APP_MARK}: a keyboard-driven view of your Claude Code background sessions.",
        epilog="Press ? inside the board for the shortcuts of the view you are looking at.",
    )
    p.add_argument("--demo", action="store_true", help="show made-up sessions and pull requests (touches nothing)")
    p.add_argument("--doctor", action="store_true", help="check that everything the board needs is installed and working")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("--update", action="store_true", help="update to the latest version (when installed with install.sh)")
    p.add_argument("--dump", action="store_true", help="print one screen as plain text instead of starting the board")
    p.add_argument("--view", choices=VIEW_ORDER, default="status", help="with --dump: which view to print")
    p.add_argument("--width", type=int, default=110, help="with --dump: screen width")
    p.add_argument("--height", type=int, default=32, help="with --dump: screen height")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"agent-board {__version__}")
        return 0
    if args.doctor:
        return run_doctor()
    if args.update:
        return update_self()
    if args.dump:
        dump(args.width, args.height, args.demo, args.view)
        return 0
    if not sys.stdout.isatty():
        print(f"{APP_NAME} needs a terminal (use --dump for a plain-text snapshot)", file=sys.stderr)
        return 2
    sys.stdout.write(f"\x1b]2;{APP_MARK} {APP_NAME}\x07")  # the terminal window title
    sys.stdout.flush()
    curses.wrapper(main_loop, args.demo)
    return 0


def cli() -> None:
    """The `agent-board` command."""
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    cli()
