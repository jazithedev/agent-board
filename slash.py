"""Slash commands for the prompt, as `claude agents` offers them: Claude Code's built-in commands plus every skill and
custom command found on disk (your own, those of installed plugins, and the project's)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ACCOUNT_SKILLS_NAMESPACE = "anthropic-skills"  # skills synced from your claude.ai account
CACHE_SECONDS = 60


@dataclass(frozen=True)
class SlashEntry:
    name: str  # without the leading slash
    kind: str  # command | skill
    description: str
    source: str = ""


# Claude Code's own commands, as its prompt lists them (Claude Code 2.1.296). They are not stored on disk, so this
# is a snapshot: a newer Claude Code may have more, and anything can still be typed by hand.
BUILTIN_COMMANDS: list[tuple[str, str]] = [
    ('add-dir', 'Add a new working directory'),
    ('advisor', 'Let Claude consult a stronger model at key moments'),
    ('artifacts', 'Browse your published and shared artifacts'),
    ('auto-mode-setup', 'Teach auto mode about your environment, plus optional rule tweaks'),
    ('autocompact', 'Set how full the context gets before auto-summarizing'),
    ('autofix-pr', 'Monitor and autofix any issues with the current PR'),
    ('background', 'Send this session to the background and free the terminal'),
    ('branch', 'Create a branch of the current conversation at this point'),
    ('btw', 'Ask a quick side question without interrupting the main conversation'),
    ('bug', 'Report a bug or share your conversation'),
    ('cd', 'Move this session to a new working directory'),
    ('chrome', 'Open Claude in Chrome settings'),
    ('clear', 'Start a new session with empty context; previous session stays on disk (resumable with /resume)'),
    ('color', 'Set the prompt bar color for this session'),
    ('compact', 'Free up context by summarizing the conversation so far'),
    ('config', 'Open settings'),
    ('context', 'Visualize current context usage as a colored grid'),
    ('copy', "Copy Claude's last response to clipboard (or /copy N for the Nth-latest)"),
    ('design-login', 'Authorize design-system access for /design-sync with your claude.ai account'),
    ('diff', 'Toggle the diff panel showing uncommitted changes'),
    ('effort', 'Set effort level for model usage'),
    ('exit', 'Exit the CLI'),
    ('export', 'Export the current conversation to a file or clipboard'),
    ('fast', 'Toggle fast mode (Opus 5.5)'),
    ('feedback', 'Send feedback to Anthropic or report a bug'),
    ('focus', 'Toggle focus view: just your prompt, summary, and response'),
    ('fork', 'Copy this conversation into a new background session and keep working here'),
    ('goal', 'Set a goal Claude checks before stopping'),
    ('help', 'Show help and available commands'),
    ('hooks', 'View hook configurations for tool events'),
    ('ide', 'Manage IDE integrations and show status'),
    ('import', 'Import config from another AI coding agent'),
    ('install-github-app', 'Set up Claude GitHub Actions for a repository'),
    ('install-slack-app', 'Install the Claude Slack app'),
    ('keybindings', 'Open your keyboard shortcuts file'),
    ('list-agents', 'List subagents, teammates, and other Claude sessions you can message'),
    ('login', 'Sign in with your Anthropic account'),
    ('logout', 'Sign out from your Anthropic account'),
    ('mcp', 'Manage MCP servers'),
    ('memory', 'Edit CLAUDE.md files and memory settings'),
    ('mobile', 'Show QR code to download the Claude mobile app'),
    ('model', 'Set the AI model for Claude Code'),
    ('output-style', 'List output styles or switch to one'),
    ('permissions', 'Manage allow and deny tool permission rules'),
    ('plan', 'Enable plan mode or view the current session plan'),
    ('plugin', 'Manage Claude Code plugins'),
    ('powerup', 'Discover Claude Code features through quick interactive lessons'),
    ('radio', 'Listen to Claude FM lo-fi radio'),
    ('rate-limit-options', 'Manage usage limits and upgrade options'),
    ('recap', 'Generate a one-line session recap now'),
    ('release-notes', 'View release notes'),
    ('reload-plugins', 'Activate pending plugin changes in the current session'),
    ('reload-skills', 'Pick up skills added or changed on disk during this session'),
    ('remote-control', 'Control this session from your phone or claude.ai/code'),
    ('remote-env', 'Choose the default environment for cloud agents'),
    ('rename', 'Rename the current conversation'),
    ('resume', 'Resume a previous conversation'),
    ('rewind', 'Restore the code and/or conversation to a previous point'),
    ('sandbox', 'Configure sandboxing for command execution'),
    ('scroll-speed', 'Adjust mouse wheel scroll speed'),
    ('skill-doctor', 'Show which loaded skills are unused and costing context'),
    ('skills', 'List available skills'),
    ('status', 'Show Claude Code status including version, model, account, API connectivity, and tool statuses'),
    ('stickers', 'Order Claude Code stickers'),
    ('subtask', 'Send a subagent off with your full context; its result comes back here'),
    ('tasks', 'View and manage everything running in the background'),
    ('teleport', 'Send this session to the cloud, or resume one from claude.ai'),
    ('terminal-setup', 'Install Shift+Enter key binding for newlines'),
    ('theme', 'Change the theme'),
    ('tui', 'Set the terminal UI renderer (default | fullscreen)'),
    ('upgrade', 'Upgrade to Max for higher rate limits and more Opus'),
    ('usage', 'Show session cost, plan usage, and activity stats'),
    ('usage-credits', 'Configure usage credits or request them from your admin when you hit a limit'),
    ('voice', 'Toggle voice mode'),
    ('web-setup', 'Set up cloud sessions with your GitHub account'),
    ('workflows', 'Browse running and completed workflows'),
]


# Skills that ship inside Claude Code (no files on disk), from the same listing and the same snapshot caveat.
BUNDLED_SKILLS: list[tuple[str, str]] = [
    ('artifact-capabilities', 'Runtime capabilities a published Artifact page can be granted — behavior static HTML cannot provide on its own, such as the page reading live or connected data, remembering what people do on it (a poll, a sign-up …'),
    ('artifact-diagramming', 'Diagramming know-how for Artifacts - when a picture earns its place, how to draw one that shows the real mechanism, and the inline-SVG mechanics that keep it legible in both themes.'),
    ('batch', 'Research and plan a large-scale change, then execute it in parallel across 5–30 isolated worktree agents that each open a PR.'),
    ('claude-api', 'Reference for the Claude API / Anthropic SDK — model ids, pricing, params, streaming, tool use, MCP, agents, caching, token counting, model migration.'),
    ('code-review', "Review the current diff, or a PR number/branch/path target, for correctness bugs (plus reuse/simplification/efficiency cleanups where the model's review recipe covers them) at the given effort leve…"),
    ('dataviz', 'Use this skill whenever you are about to create ANY chart, graph, plot, dashboard, or data visualization, in ANY output medium — an HTML or React artifact, inline SVG, plotting code in any library (mat…'),
    ('debug', 'Enable debug logging for this session and help diagnose issues'),
    ('design', 'Make a new Design artifact from a brief'),
    ('design-sync', 'Push a React design system to claude.ai/design. This runs a converter that bundles the real component code (from Storybook or a bare package) and uploads it. Use when the user runs /design-sync or says "sync my d…'),
    ('doctor', "Health-check the user's Claude Code setup and fix issues: diagnose installation health — what the `claude doctor` terminal diagnostics cover — from local data (duplicate or leftover installs, PATH, unparseabl…"),
    ('fewer-permission-prompts', 'Scan your transcripts for common read-only Bash and MCP tool calls, then add a prioritized allowlist to project .claude/settings.json to reduce permission prompts.'),
    ('init', 'Initialize a new CLAUDE.md file with codebase documentation'),
    ('insights', 'Generate a report analyzing your Claude Code sessions'),
    ('loop', 'Run a prompt or slash command on a recurring interval (e.g. /loop 5m /foo). Omit the interval to let the model self-pace.'),
    ('plugin-authoring', "Make a mod: a change to Claude's own interface or behaviour, such as a live pane or panel, a band above the prompt, a status line, a toast, a slash command, or a hook on tool calls or prompts. Use to make, change o…"),
    ('run', "Launch and drive this project's app to see a change working. Use when asked to run, start, or screenshot the app, or to confirm a change works in the real app (not just tests). First looks for a project s…"),
    ('run-skill-generator', "Author or improve the run-<unit> skill - a per-project skill that tells agents how to build, launch, and drive this project's app. Use when the user asks to set up the project, get it running, write run instruct…"),
    ('schedule', 'Create, update, list, or run scheduled cloud agents (routines) that execute on a cron schedule.'),
    ('security-review', 'Complete a security review of the pending changes on the current branch'),
    ('simplify', 'Review the changed code for reuse, simplification, efficiency, and altitude cleanups, then apply the fixes. Quality only — it does not hunt for bugs; use /code-review for that.'),
    ('slides', 'Make a new Slides deck artifact from a brief'),
    ('statusline', "Set up Claude Code's status line UI"),
    ('team-onboarding', 'Help teammates ramp on Claude Code with a guide from your usage'),
    ('update-config', 'Use this skill to configure the Claude Code harness via settings.json. Automated behaviors ("from now on when X", "each time X", "whenever X", "before/after X") require hooks configured in settings.json - the har…'),
    ('verify', "Verify that a code change actually does what it's supposed to by exercising it end-to-end and observing behavior — drive the affected flow, not just tests or typecheck. Run before committing nontrivial changes; boo…"),
    ('workflow-authoring', 'Reference for writing a Workflow tool script (script API and gotchas, resume, quality patterns, worked examples). Load before authoring a script for a workflow the user already opted into; it does not itself autho…'),
]

# ───────────────────────────── reading skill and command files ─────────────────────────────


def _unquote(raw: str, quote: str) -> str:
    if quote == "'":
        return raw.replace("''", "'")
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw.replace('\\"', '"')


def _closing_quote(text: str, quote: str) -> int:
    """Index of the quote that ends a quoted YAML scalar whose opening quote is already removed, or -1."""
    i = 0
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def parse_frontmatter(text: str) -> dict[str, str]:
    """The simple `key: value` pairs between the leading --- lines; quoted and folded (> or |) values included."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    lines = (text[3:end] if end != -1 else text[3:]).split("\n")
    out: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in ('"', "'"):
            quote, buf = value[0], value[1:]
            while _closing_quote(buf, quote) == -1 and i + 1 < len(lines):
                i += 1
                buf += " " + lines[i].strip()
            close = _closing_quote(buf, quote)
            value = _unquote(buf[: close if close != -1 else len(buf)], quote)
        elif value in (">", "|", ">-", "|-", ">+", "|+"):
            parts = []
            while i + 1 < len(lines) and (lines[i + 1].startswith((" ", "\t")) or not lines[i + 1].strip()):
                i += 1
                parts.append(lines[i].strip())
            value = " ".join(p for p in parts if p)
        else:
            while i + 1 < len(lines) and lines[i + 1].startswith((" ", "\t")) and not re.match(r"^\s+[A-Za-z0-9_-]+:\s", lines[i + 1]):
                i += 1
                value += " " + lines[i].strip()
        out[key] = value
        i += 1
    return out


def _one_line(text: str) -> str:
    return " ".join(text.split())


def read_entry(path: Path, default_name: str, namespace: str, kind: str, source: str) -> Optional[SlashEntry]:
    try:
        meta = parse_frontmatter(path.read_text(errors="replace")[:6000])
    except OSError:
        return None
    if meta.get("user-invocable", "true").strip().lower() == "false":
        return None  # a skill that is only for the model to call
    name = meta.get("name", "").strip() or default_name
    full = name if ":" in name or not namespace else f"{namespace}:{name}"
    return SlashEntry(full, kind, _one_line(meta.get("description", "")), source)


def _skills_in(folder: Path, namespace: str, source: str) -> list[SlashEntry]:
    found = []
    for skill_file in sorted(folder.glob("*/SKILL.md")):
        entry = read_entry(skill_file, skill_file.parent.name, namespace, "skill", source)
        if entry:
            found.append(entry)
    return found


def _commands_in(folder: Path, namespace: str, kind: str, source: str) -> list[SlashEntry]:
    found = []
    for command_file in sorted(folder.rglob("*.md")):
        relative = command_file.relative_to(folder).with_suffix("")
        stem = ":".join(relative.parts)
        entry = read_entry(command_file, stem, namespace, kind, source)
        if entry:
            found.append(entry)
    return found


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _disabled_plugins(home: Path, project: str) -> set[str]:
    off: set[str] = set()
    for settings in (home / ".claude" / "settings.json", Path(project) / ".claude" / "settings.json", Path(project) / ".claude" / "settings.local.json"):
        for plugin_id, enabled in (_json(settings).get("enabledPlugins") or {}).items():
            (off.discard if enabled else off.add)(plugin_id)
    return off


def discover(project: str, home: Optional[Path] = None, builtin: Optional[list[tuple[str, str]]] = None) -> list[SlashEntry]:
    """Every slash command and skill available to a session started in `project`. Later sources win on a name clash."""
    home = home or Path.home()
    entries: dict[str, SlashEntry] = {}

    def add(items: list[SlashEntry]) -> None:
        for e in items:
            entries[e.name] = e

    add([SlashEntry(n, "command", d, "built in") for n, d in (BUILTIN_COMMANDS if builtin is None else builtin)])
    if builtin is None:
        add([SlashEntry(n, "skill", d, "built in") for n, d in BUNDLED_SKILLS])

    claude = home / ".claude"
    installed = _json(claude / "plugins" / "installed_plugins.json").get("plugins") or {}
    off = _disabled_plugins(home, project)
    for plugin_id, installs in installed.items():
        if plugin_id in off or not isinstance(installs, list):
            continue
        usable = [i for i in installs if isinstance(i, dict) and (i.get("scope") != "project" or i.get("projectPath") == project)]
        if not usable:
            continue
        root, plugin = Path(usable[-1].get("installPath", "")), plugin_id.split("@")[0]
        add(_skills_in(root / "skills", plugin, plugin))
        add(_commands_in(root / "commands", plugin, "skill", plugin))
    for plugin_dir in sorted((claude / "plugins" / "synced").glob("*/*")):
        if plugin_dir.is_dir():
            plugin = re.sub(r"~g\d+$", "", plugin_dir.name)
            add(_skills_in(plugin_dir / "skills", plugin, plugin))
            add(_commands_in(plugin_dir / "commands", plugin, "skill", plugin))
    for account_dir in sorted((claude / "skills" / "synced").glob("*")):
        if account_dir.is_dir():
            add(_skills_in(account_dir, ACCOUNT_SKILLS_NAMESPACE, "your claude.ai skills"))
    skills_home = claude / "skills"
    add([e for e in _skills_in(skills_home, "", "your skills") if not e.name.startswith("synced")])
    add(_commands_in(claude / "commands", "", "command", "your commands"))
    if project:
        add(_skills_in(Path(project) / ".claude" / "skills", "", "this project"))
        add(_commands_in(Path(project) / ".claude" / "commands", "", "command", "this project"))
    return sorted(entries.values(), key=lambda e: e.name.lower())


# ───────────────────────────── matching what has been typed ─────────────────────────────


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[:\-_ ]", name) if w]


def match(entries: list[SlashEntry], query: str) -> list[SlashEntry]:
    """Entries for the text typed after the slash. A name that starts with it comes first, then names with a word
    that starts with it (so /cod finds clean-coder and review-code, not chrome-devtools), then any other
    occurrence. Each group is alphabetical, as in `claude agents`."""
    ordered = sorted(entries, key=lambda e: e.name.lower())
    q = query.lower()
    if not q:
        return ordered
    first = [e for e in ordered if e.name.lower().startswith(q)]
    seen = {e.name for e in first}
    second = [e for e in ordered if e.name not in seen and any(w.lower().startswith(q) for w in _words(e.name))]
    seen |= {e.name for e in second}
    third = [e for e in ordered if e.name not in seen and q in e.name.lower()]
    return first + second + third


class Catalog:
    """Discovers once per project and keeps the answer for a minute: opening the list must feel instant."""

    def __init__(self, loader: Callable[[str], list[SlashEntry]] = discover, now: Callable[[], float] = time.time):
        self.loader, self.now = loader, now
        self.cache: dict[str, tuple[float, list[SlashEntry]]] = {}

    def entries(self, project: str) -> list[SlashEntry]:
        hit = self.cache.get(project)
        if hit and self.now() - hit[0] < CACHE_SECONDS:
            return hit[1]
        entries = self.loader(project)
        self.cache[project] = (self.now(), entries)
        return entries
