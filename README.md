# Jazi's Agent Board 🦎

[![CI](https://github.com/jazithedev/agent-board/actions/workflows/ci.yml/badge.svg)](https://github.com/jazithedev/agent-board/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A keyboard-driven terminal board for your **Claude Code background sessions**. It does what `claude agents`
does, and adds what you end up wanting once you run many sessions: which **project** each one belongs to,
**tags** (picked by hand or guessed from the prompt), **pull requests** that need you, your **usage and
rate limits**, a **model picker**, and Claude Code-style prompt handling (pasted-text chips, drafts that
survive while you look at other sessions).

Pure Python standard library: nothing to `pip install`, no accounts, no network calls of its own.

```
 🦎 Jazi's Agent Board    Sessions   Review queue   My PRs   demo data   1 awaiting · 3 working · 4 completed
 ⇣ 3 to review    ⚑ 2 of yours need you (1 conflict · 1 changes · 1 CI failing)    ✓ 1 ready to merge───

  Awaiting input  1
  ◆  review checkout refactor  Code Review   Ticket: AM-1234   PR #482            ● billing-api    7m
     Approve running `composer test` against the staging DB?            ⎇ worktree-checkout-refactor

  Working  3
▌ ·  listing sync retries  Ticket: WEB-5521                                           ● web-app   12m
▌    Running the failing job spec in a loop to find the flake
  ·  report cache layer                                                            ● mobile-app   31m
     Running the full test suite after the style fix                         ⎇ worktree-report-cache
  ·  search indexing rewrite                                                       ● mobile-app    1h
     Batch 4 of 9 reindexed; waiting for the next shard                   ⎇ worktree-search-indexing

  Completed  4
  ●  MR !328 follow-ups  Code Review   PR #328                                     ● mobile-app    5h
     Dialog state now lives in the store; the hint closes the popup
  ✗  failed deploy dry-run                                                        ● infra-tools    1d
     terraform plan exited 1: backend config missing
  ●  onboarding flow plan                                                          ● mobile-app    3d
     Draft written: screens, copy and open questions marked [TODO]
  ●  docs site migration                                                           ● mobile-app   13d
     Notes on moving the docs to the new generator

  USAGE  this machine · from local transcripts ──────────────────────────────────────────────────────
             turns  output  sessions    Models · 30d output
  Today        412     96k         3    fable-5          ███████░░░  71%
  7 days      3.1k    812k         9    opus-4-6         ██░░░░░░░░  22%
  30 days      12k    2.4M        31    sonnet-4-5       █░░░░░░░░░   8%
  All time     18k    5.1M        52
  14 days  ▂▅▂▁▁▅▇▄▂▁▃█▆▆
  cache reads 900M tokens, 30 days

  LIMITS  as of 3m ago ──────────────────────────────────────────────────────────────────────────────
  5-hour   ████████░░░░░░░░░░░░  38%  resets in 2h 13m  (Sat 23:11)
  Weekly   ██████████████░░░░░░  71%  resets in 3d 4h  (Wed 01:57)
  Disk     █████████████░░░░░░░  67%  30G free of 95G  ·  Claude data 1.3G  ·  worktrees 3.0G
────────────────────────────────────────────────────────────────────────────────────────────────────────
 ❯ describe a task for a new session                 in agent-board  ·  default model  ctrl+p ctrl+e
 enter open · space peek · ctrl+r rename · ctrl+g tags · ctrl+t pin · ctrl+x stop · ? shortcuts
```

> Made-up demo data (`agent-board --demo`). Your real sessions, projects and pull requests show up the same way.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/jazithedev/agent-board/HEAD/install.sh | sh
```

That is all. It needs `python3` (3.10 or newer) and `git` or `curl`; no `sudo`, nothing system-wide, no PyPI
packages. It puts the code in `~/.local/share/agent-board`, adds an `agent-board` command to `~/.local/bin`,
and finishes by checking your setup so you know straight away if something is missing.

## Start

```sh
agent-board
```

That's the whole thing. Handy variations:

| Command | What it does |
|---|---|
| `agent-board` | open the board with your real sessions |
| `agent-board --demo` | explore it with made-up data; touches nothing |
| `agent-board --doctor` | check that Claude Code, `gh`/`glab`, a clipboard tool, etc. are set up, and say how to fix what is not |
| `agent-board --update` | update to the latest version |
| `agent-board --help` | all options |

Press **`?`** inside the board: it lists exactly the shortcuts that work in the view you are looking at.

Re-run the install command any time to update, or `curl -fsSL https://raw.githubusercontent.com/jazithedev/agent-board/HEAD/install.sh | sh -s -- --uninstall` to remove it
(your tags and settings in `~/.config/agent-board` are kept).

<details>
<summary>Other ways to install</summary>

**pipx / pip** (needs pip 23 or newer; Ubuntu 22.04's bundled pip is too old, use the installer above there):

```sh
pipx install git+https://github.com/jazithedev/agent-board
# or:  python3 -m pip install --user git+https://github.com/jazithedev/agent-board
```

**Straight from a clone**, with no install step at all:

```sh
git clone https://github.com/jazithedev/agent-board && cd agent-board
./agent_board.py
```

</details>

## Requirements

- **Claude Code** with background sessions (`claude agents --json` must work: `agent-board --doctor` checks it).
- **Python 3.10+** with the standard `curses` module (included with Python on Linux and macOS).
- A terminal with 256 colours and UTF-8. Mouse support is optional; it works in most terminals.
- Optional: `gh` and/or `glab` for the pull-request views, `xclip` / `wl-clipboard` for the clipboard (see below).

Developed and tested on Linux (X11). CI runs the tests on Python 3.10 to 3.13. macOS should work but is untested.

## What you get

**Sessions**, grouped as *Awaiting input*, *Working*, *Idle* and *Completed*. A session whose process has finished
its turn is shown as **Idle**, even when Claude Code's own state still says "working".

- **Projects**: every session shows its project (worktrees fold into their repository); `ctrl+b` groups the list by project instead of by status, and the board remembers your choice.
- **Tags**: `Code Review`, `Ticket: ABC-123` and `PR #42` are recognised from a session's text. Add your own from a picker (`ctrl+g`): create, rename and delete tags, and new sessions get suggested tags from your first prompt (editable rules).
- **Rename, pin, stop, delete, peek** at a session's recent output, all from the keyboard.
- **Start a session** by typing in the prompt at the bottom: choose its **project** (`ctrl+p`) and its **model** (`ctrl+e`). The project list holds every directory you have used Claude Code in, not only those with sessions; `ctrl+t` in it pins a project (any `/path` you type can be pinned too). Typing `@` in the prompt lists the same projects like `/` lists commands: `tab` completes `@name` and the session starts in that project. A mention that opens the prompt only picks the project and is not sent; inside a sentence ("add this skill to my @web-app") the name stays in the text.
- **Slash commands**: type `/` for the list of commands and skills, as in `claude agents`. It filters as you type, `↑` `↓` choose, and `tab` or `enter` completes. Your own skills and commands, those of your installed plugins and the project's are read from disk; Claude Code's built-in commands are a snapshot (2.1.296), and anything else you can still type by hand.
- **Open a session** with Enter, → or a mouse click. Coming back to the board is instant: no flash of your normal terminal.
- **Prompt like Claude Code**: long pastes collapse into `[Pasted text #1 +97 lines]` chips (the full text is what gets sent), `ctrl+j` adds a new line, and a half-written prompt survives while you open other sessions. The caret moves with `←` `→`, a click, `ctrl+←` / `ctrl+→` (by word) and `home` / `end`; `delete`, `ctrl+w` and `alt+backspace` edit around it.
- **Mouse**: click opens a session; dragging highlights text live and copies it.

**Pull requests** (optional). Two more tabs in the title row, **Review queue** and **My PRs**, for the projects that have sessions on the board. `alt+←` / `alt+→` move between tabs, `ctrl+s` cycles them, and a click on a tab opens it:

```
 🦎 Jazi's Agent Board    Sessions   Review queue   My PRs   demo data   1 awaiting · 3 working · 4 completed
 ⇣ 3 to review    ⚑ 2 of yours need you (1 conflict · 1 changes · 1 CI failing)    ✓ 1 ready to merge───

  ● billing-api  2   github · acme/billing-api
▌ [ ]  #482  Refactor checkout totals  re-requested                                   @maria    2d  ⟳
  [ ]  #231  Bump stripe-php to 15.2                                                      @dave    1d

  ● web-app  1   github · acme/web-app
  [ ]  #479  Add CSV export to the citations report  teammate-approved                     @tom    5d

  ● mobile-app  0   gitlab · acme/mobile-app
     nothing waiting on you ✓

  USAGE  this machine · from local transcripts ──────────────────────────────────────────────────────
             turns  output  sessions    Models · 30d output
  Today        412     96k         3    fable-5          ███████░░░  71%
  7 days      3.1k    812k         9    opus-4-6         ██░░░░░░░░  22%
  30 days      12k    2.4M        31    sonnet-4-5       █░░░░░░░░░   8%
  All time     18k    5.1M        52
  14 days  ▂▅▂▁▁▅▇▄▂▁▃█▆▆
  cache reads 900M tokens, 30 days

  LIMITS  as of 3m ago ──────────────────────────────────────────────────────────────────────────────
  5-hour   ████████░░░░░░░░░░░░  38%  resets in 2h 13m  (Sat 23:11)
  Weekly   ██████████████░░░░░░  71%  resets in 3d 4h  (Wed 01:57)
  Disk     █████████████░░░░░░░  67%  30G free of 95G  ·  Claude data 1.3G  ·  worktrees 3.0G
────────────────────────────────────────────────────────────────────────────────────────────────────────
   space marks a PR for review, r starts a review session
 space mark · ctrl+a all · r review · enter open · o browser · ctrl+l refresh · ctrl+o settings · ct…
```

- **Review queue**: what is waiting for your review. `space` marks PRs, `r` starts **one review session per project** for the marked ones.
- **My pull requests**: yours, sorted by what needs you: conflicts, changes requested, failing checks, unresolved threads, or *ready to merge*. Stacked PRs stay nested.
- A one-line strip under the title keeps the counts visible in every view. Refresh is every 15 minutes by default; change it (or set it to manual) in settings (`ctrl+o`), or press `ctrl+l`.
- GitHub uses `gh`; GitLab uses `glab`. Everything is **read-only**.

**Usage, limits and disk** (optional). The bottom of the screen can show your recent usage (turns, tokens, models, projects, from your local transcripts), your **5-hour and weekly rate limits** (account-wide) with a countdown to each reset, and a **Disk** row: how full the disk is, how much is free, and how much Claude Code's own data and your sessions' git worktrees take. The bar turns yellow below 15% free and red below 5%. If your projects live on another disk, each disk gets its own row.

## Pull requests: setup

1. Log in once: `gh auth login` and/or `glab auth login`.
2. **Your own GitHub PRs** are read through the `pull-request` plugin's script, so stacks and review states match `/pull-request:my-prs-list`:

   ```
   /plugin marketplace add jazithedev/ai-marketplace
   /plugin install pull-request@jazis-ai-marketplace
   ```

   Without it everything else still works (GitLab, the review queue); the board just says it could not list your own GitHub PRs.
3. **Review queue for a team**: press `ctrl+o` and enter your team slugs for the organisation (`core, backend | infra` means "include core and backend, leave infra out"). Without slugs the queue shows PRs where you were requested by name.
4. **Review sessions** run `/code-review:code-review <urls>` by default (from the same marketplace: `/plugin install code-review@jazis-ai-marketplace`). Change the command, and the prompt used for GitLab merge requests, in `ctrl+o` if you use something else, for example the built-in `/code-review`.

## Account limits: setup

Claude Code only exposes your rate limits inside a running session, so a tiny companion mod copies the latest reading to a file the board reads. Install it from inside Claude Code:

```
/plugin install agent-board-limits --marketplace jazithedev/agent-board
```

or let the installer do it: `curl -fsSL https://raw.githubusercontent.com/jazithedev/agent-board/HEAD/install.sh | sh -s -- --with-limits-mod`.

The block shows "no reading yet" until a session that loads the mod finishes a turn, and the reading is as fresh as your last turn (its age is shown).
The mod only writes `~/.cache/agent-board/limits.json`.

## Clipboard

Copying (a mouse drag, `ctrl+x` / `ctrl+y` on the prompt) and `ctrl+v` need a clipboard tool. Without one, copying only works if your terminal supports OSC 52, and many do not (GNOME Terminal, Xfce Terminal).

```sh
sudo apt install xclip          # X11
sudo apt install wl-clipboard   # Wayland
```

`ctrl+shift+v` (your terminal's own paste) always works. `agent-board --doctor` tells you which case you are in.

## Keys

`?` shows the full list for the current view. The essentials, in the session views:

| Key | Does |
|---|---|
| `alt+←` `alt+→` · click a tab | switch between the Sessions, Review queue and My PRs tabs |
| `ctrl+b` | group the sessions by status or by project |
| `↑` `↓` · click | select a session · open it |
| `home` `end` | jump to the first / last session (in the prompt they move the caret) |
| `enter` `→` | open the selected session |
| type, then `enter` | start a new background session; it is selected, so `→` opens it |
| `/` | list slash commands and skills: `↑` `↓` choose, `tab` or `enter` completes |
| `ctrl+p` · `ctrl+e` | choose the project · the model for new sessions |
| `space` | peek at a session's recent output |
| `ctrl+r` · `ctrl+g` · `ctrl+t` | rename (in the session's own row) · tags · pin |
| `ctrl+x` | stop (working or idle) or delete (finished): asks first, `x` or `ctrl+x` again confirms |
| `ctrl+s` | next view: status, project, review queue, my pull requests |
| `ctrl+f` | find |
| `ctrl+l` · `ctrl+o` | refresh pull requests · settings |
| `ctrl+c` | clear the typed prompt (never quits) |
| `esc` | leave the list, clear the prompt, then quit |

## What it reads and writes

It is a viewer plus a remote control, and it is careful about both:

- **Reads**: `claude agents --json --all`, each job's `~/.claude/jobs/<id>/state.json`, your transcripts in `~/.claude/projects/` (usage numbers only), and `git remote` of your projects.
- **Writes**: only its own files: `~/.config/agent-board/meta.json` (renames, tags, pins, settings) and, by the optional mod, `~/.cache/agent-board/limits.json`. It never edits Claude Code's files.
- **Runs** `claude attach`, `stop`, `rm`, `logs` and `--bg` when you ask, always after a confirmation for `stop`/`rm`.
- Pull-request lookups are read-only `gh` / `glab` calls. Nothing is sent anywhere else.

## Configuration

Everything lives in the board (`ctrl+o`, `ctrl+g`, `ctrl+e`). For scripts and tests there are environment variables:

| Variable | Default |
|---|---|
| `AGENT_BOARD_CLAUDE` | `claude`: the Claude Code command |
| `AGENT_BOARD_META` | `~/.config/agent-board/meta.json` |
| `AGENT_BOARD_LIMITS` | `~/.cache/agent-board/limits.json` |
| `AGENT_BOARD_JOBS_DIR` | `~/.claude/jobs` |
| `AGENT_BOARD_PROJECTS_DIR` | `~/.claude/projects` |

## Troubleshooting

Run **`agent-board --doctor`** first: it checks Python, your terminal, Claude Code, `gh` / `glab`, the clipboard and the limits mod, and says how to fix each.

- **Empty board**: you have no background sessions yet. Type a task in the prompt, or run `claude --bg "…"`.
- **`agent-board: command not found`** right after installing: add `~/.local/bin` to your `PATH` (the installer prints the exact line) and open a new terminal.
- **Odd symbols or colours**: use a UTF-8 locale and a 256-colour terminal (`TERM=xterm-256color`).
- **Inside tmux**: enable mouse passthrough and OSC 52 (`set -g set-clipboard on`) if you want drag-to-copy without a clipboard tool.
- **`ctrl+s` seems to freeze the terminal**: that is terminal flow control; the board turns it off while it runs.

## Good to know

- This is an **unofficial** companion tool, not affiliated with or endorsed by Anthropic. It relies on parts of Claude Code that are not a documented, stable API (`claude agents --json`, the jobs folder), so a future Claude Code release can break it. Developed against 2.1.295 and 2.1.296. Please open an issue if it does.
- Opening a session runs `claude attach` behind a pseudo-terminal of the board's own, which is what makes returning instant. Pressing ← inside a session, or Ctrl+Z, returns you to the board; the session keeps running.

## Development

```sh
git clone https://github.com/jazithedev/agent-board && cd agent-board
python3 -m unittest discover        # ~370 tests, about 20 seconds
python3 -m pip install ruff && ruff check .
./agent_board.py --dump --demo --view review   # print a screen as plain text
```

| File | |
|---|---|
| `agent_board.py` | the board: data, state, rendering, input handling |
| `pullrequests.py` | pull-request and merge-request fetching (GitHub via `gh`, GitLab via `glab`) |
| `mods/agent-board-limits/` | the companion Claude Code mod |
| `install.sh` | the installer |
| `test_*.py` | unit tests; several drive real pseudo-terminals |
| `.github/` | the pipelines below, Dependabot and the pull-request template |

### Pipelines

Every pull request, and every push to the default branch, runs these on GitHub:

| Check | What it guarantees |
|---|---|
| **Lint** | `ruff`: no unused or undefined names, no obvious bug patterns |
| **Tests** | the whole suite on Python 3.10, 3.11, 3.12 and 3.13; it also scans every published file for e-mail addresses and home-directory paths |
| **Installer** | `install.sh` passes `shellcheck`, installs into an empty home, updates, runs and uninstalls cleanly |
| **Packaging** | the wheel builds, `twine check` passes, and the installed `agent-board` command runs |
| **Manifests** | the plugin and marketplace files agree with each other and with the files they name |
| **Secret scan** | `gitleaks` over the whole history |
| **Workflow files** | `actionlint` checks the pipelines themselves |
| **CodeQL** | GitHub's security analysis, also weekly |

One summary check, **CI passed**, is green only when all of them are, so it is the one to require before merging. Dependabot keeps the actions up to date.

## License

[MIT](LICENSE) © Krzysztof Trzos (Jazi) · [jazi.pl](https://jazi.pl)
