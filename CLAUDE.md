# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Jazi's Agent Board: a curses TUI over Claude Code background sessions (`claude agents --json`, `~/.claude/jobs/<id>/state.json`), with projects, tags, pull requests (`gh` / `glab`), usage, rate limits and disk. **Pure Python standard library, no runtime dependencies**: do not add any.

## Commands

```sh
python3 -m unittest discover                                   # full suite (~600 tests, ~20 s); this is what CI runs, on 3.10–3.13
python3 -m unittest test_pr_views                              # one file
python3 -m unittest test_pr_views.MergeFromMyPrs               # one class
python3 -m unittest test_pr_views.MergeFromMyPrs.test_any_other_key_cancels   # one test
ruff check .                                                   # CI pins ruff 0.17.0 (uvx ruff check . works without installing)
python3 .github/scripts/check_manifests.py                     # plugin/marketplace manifests + __version__ format
./agent_board.py --dump --demo --view review --width 110 --height 32   # print one screen as plain text, no terminal needed
./agent_board.py --demo                                        # interactive, made-up data, touches nothing
```

pytest is not used. Tests import the modules from the repo root, so run them from there.

## Architecture

Three modules, all listed in `pyproject.toml` `py-modules` (a test fails if a new top-level module is missing from it):

- `agent_board.py`: everything else, split into `# ───── section ─────` banners (tags, model, metadata, usage, limits, disk, board state, rendering, attach, curses shell, demo data and CLI).
- `pullrequests.py` (imported as `prs`): fetching and modelling PRs/MRs. `MyPr` (your PRs; its `group` property picks the My PRs section: attention / ready / waiting / draft) and `ReviewPr` (review queue). GitHub is a single `gh api graphql` request per refresh; GitLab goes through `glab api`. It also holds the write commands (`approve`, `merge_command` / `merge`).
- `slash.py`: the `/` completion catalog (built-in commands plus skills and commands found on disk).

### The state / render / effect split

This is the main thing to understand before changing behaviour:

1. **`Board`** holds all UI state. `Board.handle(key, ch)` is the single input entry point. It mutates state and may **return an `Effect`** (a small dataclass: `kind` plus `items` / `text` / `session_id` …). It never does I/O.
2. **`compose(board, width, height)`** is a render function that returns `(lines, caret)`, where lines are `Line`s made of `(text, style)` segments. It is pure apart from one thing: it writes the click maps `board.hit` (screen row → session or PR key), `board.tab_hit` and `board.prompt_hit`, which `Board.click()` reads afterwards. Styles are names looked up in `_theme()`.
3. **`perform(scr, board, effect, loader)`** in the curses shell carries out effects: attaching to a session, `claude --bg` sessions, browser, clipboard, approve, merge and so on. Mouse clicks set `board.queued_effect`, and the main loop runs it.

Slow work runs on daemon threads so the screen keeps drawing. `Loader` (sessions), `UsageLoader`, `DiskLoader`, `LimitsLoader` and `PrLoader` refresh in the background. One-off actions (`start_approval`, `start_merge`, `start_session_action`) put a label in `board.pr_busy_keys` / `board.busy` while they run, and the row shows that label. Report outcomes through `board.msg`: a leading `✓` reads as success, a leading `✗` is styled as a failure.

### Sessions started on the user's behalf

Review, fix-conflicts and merge sessions are `claude --bg` runs (`bg_command` + `run_cli`, with cwd set to the PR's project) driven by prompt templates (`CONFLICT_PROMPT`, `MERGE_PROMPT`). Each prompt names the PR as `PR #N` / `MR !N`. That text is how the session gets a `pr` tag (`derive_tags`, capped per kind by `TAG_LIMITS`: two PRs) and its `Session.prs` (every PR number named, uncapped, remembered in meta), and `Board.linked_sessions()` uses those to put the ⟳ on the PR row.

### Persistence

`Meta` (`~/.config/agent-board/meta.json`, overridable with `AGENT_BOARD_META`) is the board's only state file: renames, tags, pins, the tags each session has shown before (`seen`), and settings (`meta.settings`, edited in the `ctrl+s` UI via `settings_fields()`). The board never writes Claude Code's files.

## Tests

- Build boards with the helpers at the top of each test file. They use a temp `Meta` and a fixed clock (`Board(..., now=lambda: NOW)`). Time-dependent code must use `board.now()` / an injected `now`, never `time.time()` directly.
- External commands are injected as `run=` callables (fakes return `SimpleNamespace(returncode=…, stdout=…, stderr=…)`), or patched with `mock.patch.object(ab, "run_cli" / "load_sessions" / …)`.
- Rendering is asserted on `ab.plain(line)` text from `ab.compose(...)`. Input is driven with `b.handle("char", "x")`, `b.handle("enter")`, `ab.mouse_event(...)`.
- `test_cli_and_install.py` scans every published file and fails on e-mail addresses (other than example.com / noreply) and on `/home/<user>/` paths. Use `/srv/www/...`-style paths and example.com URLs in tests and docs.

## Conventions for a user-visible change

From the PR template, and how the history is kept:

- Bump `__version__` in `agent_board.py`: minor for a feature, patch for a fix. The package version is read from it.
- Update `README.md` and the `?` help (`HELP_BY_VIEW` / `HELP_COMMON_END` in `agent_board.py`) for new keys or behaviour. `_hint_segs` holds the footer and confirm-line texts.
- Commit subjects are Conventional Commits with the new version at the end, e.g. `feat(prs): merge a pull request with m in My PRs (0.18.0)`. Typical scopes: `sessions`, `prs`, `review`, `prompt`, `ci`.
- Keep comments in the existing style: short, explaining *why*, often inline after the code.
