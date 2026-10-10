"""Pull requests for agent-board: what you still have to review, and where your own PRs stand.

GitHub goes through `gh` and, where it exists, the `pull-request` plugin's own read-only scripts
(my_prs_list.py / to_review_list.py), so stacks, re-requests and team scoping behave as they do in
/pull-request:my-prs-list and /pull-request:to-review-list. GitLab goes through `glab api`.
Every call is read-only. All process access goes through one injectable `run`.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

Runner = Callable[..., subprocess.CompletedProcess]

PLUGIN_CACHE = Path.home() / ".claude" / "plugins" / "cache"
GITLAB_HOST = "gitlab.com"
SOURCES = ("github", "gitlab")


class FetchError(RuntimeError):
    pass


# ───────────────────────────── remotes ─────────────────────────────


@dataclass(frozen=True)
class Remote:
    provider: str  # github | gitlab
    repo: str  # owner/name, or group/subgroup/name on GitLab

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}"

    @property
    def org(self) -> str:
        return self.repo.split("/", 1)[0]


REMOTE_RE = re.compile(r"^(?:git@|ssh://git@|https?://(?:[^@/]+@)?)(github\.com|gitlab\.com)[:/](.+?)(?:\.git)?/?$")


def parse_remote(url: str) -> Optional[Remote]:
    m = REMOTE_RE.match(url.strip())
    if not m:
        return None
    return Remote("github" if m.group(1) == "github.com" else "gitlab", m.group(2))


def resolve_remote(path: str, run: Runner = subprocess.run) -> Optional[Remote]:
    try:
        proc = run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_remote(proc.stdout) if proc.returncode == 0 else None


# ───────────────────────────── models ─────────────────────────────


def parse_time(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class MyPr:
    provider: str
    repo: str
    number: int
    title: str
    url: str
    base: str = ""
    head: str = ""
    draft: bool = False
    ticket: str = ""
    depth: int = 0
    parent: Optional[int] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    decision: str = "none"  # approved | changes | required | none
    merge: str = "unknown"  # clean | conflict | behind | blocked | unstable | unknown
    checks: str = ""  # pass | fail | pending | ""
    checks_stale: bool = False
    requested: tuple[str, ...] = ()
    reviewed: bool = False
    unresolved: bool = False  # unresolved discussion threads (GitLab)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}#{self.number}"

    @property
    def repo_key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}"

    @property
    def attention(self) -> list[str]:
        flags = []
        if self.merge == "conflict":
            flags.append("conflicts")
        if self.decision == "changes":
            flags.append("changes requested")
        if self.checks == "fail" and not self.checks_stale:
            flags.append("checks failing")
        if self.unresolved:
            flags.append("unresolved threads")
        return flags

    @property
    def group(self) -> str:
        if self.attention:
            return "attention"
        return "ready" if self.decision == "approved" else "waiting"

    @property
    def notes(self) -> list[str]:
        notes = []
        if self.draft:
            notes.append("draft")
        if self.group == "ready" and self.merge == "behind":
            notes.append("behind base")
        if self.group == "waiting" and not self.draft and not self.requested and not self.reviewed:
            notes.append("nobody asked")
        if self.checks == "pending":
            notes.append("checks running")
        return notes


@dataclass(frozen=True)
class ReviewPr:
    provider: str
    repo: str
    number: int
    title: str
    url: str
    author: str = ""
    draft: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    flags: tuple[str, ...] = ()  # new | re-requested | teammate-approved | changes-requested | stale

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}#{self.number}"

    @property
    def repo_key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}"


@dataclass
class PrData:
    mine: list[MyPr] = field(default_factory=list)
    review: list[ReviewPr] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetched_at: float = 0.0
    repos: dict[str, str] = field(default_factory=dict)  # remote key -> project path


# ───────────────────────────── normalisers ─────────────────────────────

_DECISION = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes", "REVIEW_REQUIRED": "required"}
_MERGE = {"CLEAN": "clean", "HAS_HOOKS": "clean", "DIRTY": "conflict", "BEHIND": "behind", "BLOCKED": "blocked", "UNSTABLE": "unstable"}
_CHECKS = {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail", "PENDING": "pending", "EXPECTED": "pending"}


def github_my_prs(payload: dict) -> list[MyPr]:
    """From my_prs_list.py --json."""
    out = []
    for p in payload.get("prs", []):
        reviews = p.get("reviews") or {}
        out.append(MyPr(
            provider="github", repo=p.get("repo", ""), number=int(p["number"]), title=p.get("title", ""), url=p.get("url", ""),
            base=p.get("base", ""), head=p.get("head", ""), draft=bool(p.get("is_draft")), ticket=p.get("ticket") or "",
            depth=int(p.get("depth") or 0), parent=p.get("parent"),
            created_at=parse_time(p.get("created_at")), updated_at=parse_time(p.get("updated_at")),
            decision=_DECISION.get(p.get("decision") or "", "none"), merge=_MERGE.get(p.get("merge_state") or "", "unknown"),
            checks=_CHECKS.get(p.get("checks") or "", ""), checks_stale=bool(p.get("checks_stale")),
            requested=tuple(p.get("requested") or ()), reviewed=bool(reviews),
        ))
    return out


def repo_from_api_url(url: str) -> str:
    return url.split("/repos/", 1)[1] if "/repos/" in url else ""


def github_review_from_search(items: list[dict], include_drafts: bool) -> list[ReviewPr]:
    out = []
    for i in items:
        if i.get("draft") and not include_drafts:
            continue
        out.append(ReviewPr(
            provider="github", repo=repo_from_api_url(i.get("repository_url", "")), number=int(i["number"]),
            title=i.get("title", ""), url=i.get("html_url", ""), author=(i.get("user") or {}).get("login", "") if isinstance(i.get("user"), dict) else str(i.get("user") or ""),
            draft=bool(i.get("draft")), created_at=parse_time(i.get("created_at")), updated_at=parse_time(i.get("updated_at")),
            flags=("new",),
        ))
    return out


def github_review_from_script(payload: dict, repo: str) -> list[ReviewPr]:
    """From to_review_list.py --json."""
    out = []
    for p in payload.get("prs", []):
        flags = tuple(f for f in p.get("flags", []) if f != "draft") or ("new",)
        out.append(ReviewPr(
            provider="github", repo=repo, number=int(p["number"]), title=p.get("title", ""), url=p.get("url", ""),
            author=p.get("author", ""), draft="draft" in p.get("flags", []), created_at=parse_time(p.get("created_at")),
            updated_at=parse_time(p.get("created_at")), flags=flags,
        ))
    return out


def gitlab_repo_of(mr: dict) -> str:
    full = (mr.get("references") or {}).get("full", "")
    return full.rsplit("!", 1)[0] if "!" in full else ""


_GITLAB_MERGE = {"mergeable": "clean", "need_rebase": "behind", "conflict": "conflict", "not_approved": "blocked", "blocked_status": "blocked"}
_GITLAB_CHECKS = {"success": "pass", "failed": "fail", "running": "pending", "pending": "pending", "created": "pending", "canceled": ""}


def gitlab_my_pr(mr: dict, detail: dict, approvals: dict) -> MyPr:
    status = detail.get("detailed_merge_status") or mr.get("detailed_merge_status") or ""
    merge = "conflict" if detail.get("has_conflicts") or mr.get("has_conflicts") else _GITLAB_MERGE.get(status, "unknown")
    pipeline = detail.get("head_pipeline") or {}
    approved = bool(approvals.get("approved")) or bool(approvals.get("approved_by"))
    return MyPr(
        provider="gitlab", repo=gitlab_repo_of(mr), number=int(mr["iid"]), title=mr.get("title", ""), url=mr.get("web_url", ""),
        base=mr.get("target_branch", ""), head=mr.get("source_branch", ""), draft=bool(mr.get("draft")),
        created_at=parse_time(mr.get("created_at")), updated_at=parse_time(mr.get("updated_at")),
        decision="approved" if approved else "none", merge=merge, checks=_GITLAB_CHECKS.get(pipeline.get("status", ""), ""),
        requested=tuple(sorted((r.get("username") or "") for r in (mr.get("reviewers") or []) if r.get("username"))),
        reviewed=bool(approvals.get("approved_by")),
        unresolved=(detail.get("blocking_discussions_resolved", mr.get("blocking_discussions_resolved", True)) is False),
    )


def gitlab_review_pr(mr: dict) -> ReviewPr:
    return ReviewPr(
        provider="gitlab", repo=gitlab_repo_of(mr), number=int(mr["iid"]), title=mr.get("title", ""), url=mr.get("web_url", ""),
        author=(mr.get("author") or {}).get("username", ""), draft=bool(mr.get("draft")),
        created_at=parse_time(mr.get("created_at")), updated_at=parse_time(mr.get("updated_at")), flags=("new",),
    )


def link_stacks(prs: list[MyPr]) -> list[MyPr]:
    """Where the source gave no stack info (GitLab), a PR whose base is another PR's head is stacked on it."""
    from dataclasses import replace

    heads = {(p.repo, p.head): p for p in prs if p.head}
    out = []
    for p in prs:
        parent = heads.get((p.repo, p.base))
        if p.parent is None and parent and parent.number != p.number:
            depth, seen, cur = 1, {p.number}, parent
            while (up := heads.get((cur.repo, cur.base))) and up.number not in seen:
                seen.add(up.number)
                depth, cur = depth + 1, up
            p = replace(p, parent=parent.number, depth=depth)
        out.append(p)
    return out


# ───────────────────────────── fetching ─────────────────────────────


def find_skill_script(skill: str, filename: str, cache: Path = PLUGIN_CACHE) -> Optional[Path]:
    def version(p: Path) -> tuple[int, ...]:
        parts = re.findall(r"\d+", p.parts[p.parts.index("pull-request") + 1]) if "pull-request" in p.parts else []
        return tuple(int(x) for x in parts)

    found = sorted(cache.glob(f"*/pull-request/*/skills/{skill}/scripts/{filename}"), key=version)
    return found[-1] if found else None


def _json(run: Runner, cmd: list[str], cwd: Optional[str] = None, timeout: int = 120, lines: bool = False):
    try:
        proc = run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    except FileNotFoundError as exc:
        raise FetchError(f"{cmd[0]} is not installed") from exc
    except subprocess.SubprocessError as exc:
        raise FetchError(f"{cmd[0]} did not finish: {exc}") from exc
    if proc.returncode != 0:
        reason = next((l for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.strip()), "failed")
        raise FetchError(reason.strip()[:160])
    try:
        if lines:
            return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise FetchError(f"{cmd[0]} returned something unreadable") from exc


def glab(run: Runner, endpoint: str, home: str) -> object:
    return _json(run, ["glab", "api", "--hostname", GITLAB_HOST, endpoint], cwd=home)


def fetch_github(remotes: dict[str, Remote], settings: dict, run: Runner, cache: Path, errors: list[str]) -> tuple[list[MyPr], list[ReviewPr]]:
    scope = {r.repo.lower() for r in remotes.values() if r.provider == "github"}
    mine: list[MyPr] = []
    review: list[ReviewPr] = []
    if not scope:
        return mine, review
    script = find_skill_script("my-prs-list", "my_prs_list.py", cache)
    if script is None:
        errors.append("GitHub: the pull-request plugin was not found, so your own PRs can't be listed")
    else:
        try:
            mine = [p for p in github_my_prs(_json(run, ["python3", str(script), "--json"])) if p.repo.lower() in scope]
        except FetchError as exc:
            errors.append(f"GitHub (your PRs): {exc}")
    include_drafts = bool(settings.get("include_drafts"))
    try:
        items = _json(run, ["gh", "api", "--paginate", "-X", "GET", "search/issues", "-f", "q=is:pr is:open archived:false review-requested:@me",
                            "--jq", ".items[] | {number,title,html_url,repository_url,created_at,updated_at,draft,user:.user.login}"], lines=True)
        review = [p for p in github_review_from_search(items, include_drafts) if p.repo.lower() in scope]
    except FetchError as exc:
        errors.append(f"GitHub (review queue): {exc}")
    teams = {org.lower(): cfg for org, cfg in (settings.get("review_teams") or {}).items()}
    review_script = find_skill_script("to-review-list", "to_review_list.py", cache)
    for remote in remotes.values():
        cfg = teams.get(remote.org.lower()) if remote.provider == "github" else None
        if not cfg or not cfg.get("include") or review_script is None:
            continue
        cmd = ["python3", str(review_script), "--repo", remote.repo, "--json"]
        for slug in cfg["include"]:
            cmd += ["--team", slug]
        for slug in cfg.get("exclude", []):
            cmd += ["--exclude-team", slug]
        if include_drafts:
            cmd.append("--include-drafts")
        try:
            richer = github_review_from_script(_json(run, cmd, timeout=240), remote.repo)
            review = [p for p in review if p.repo.lower() != remote.repo.lower()] + richer
        except FetchError as exc:
            errors.append(f"GitHub (team review for {remote.repo}): {exc}")
    return mine, review


def fetch_gitlab(remotes: dict[str, Remote], settings: dict, run: Runner, errors: list[str], home: str) -> tuple[list[MyPr], list[ReviewPr]]:
    scope = {r.repo.lower() for r in remotes.values() if r.provider == "gitlab"}
    mine: list[MyPr] = []
    review: list[ReviewPr] = []
    if not scope:
        return mine, review
    try:
        for mr in glab(run, "merge_requests?scope=created_by_me&state=opened&per_page=100", home):
            if gitlab_repo_of(mr).lower() not in scope:
                continue
            pid, iid = mr["project_id"], mr["iid"]
            detail = glab(run, f"projects/{pid}/merge_requests/{iid}", home)
            approvals = glab(run, f"projects/{pid}/merge_requests/{iid}/approvals", home)
            mine.append(gitlab_my_pr(mr, detail, approvals))
    except FetchError as exc:
        errors.append(f"GitLab (your MRs): {exc}")
    try:
        me = glab(run, "user", home)["username"]
        for mr in glab(run, f"merge_requests?scope=all&state=opened&reviewer_username={me}&per_page=100", home):
            if gitlab_repo_of(mr).lower() not in scope or (mr.get("draft") and not settings.get("include_drafts")):
                continue
            approvals = glab(run, f"projects/{mr['project_id']}/merge_requests/{mr['iid']}/approvals", home)
            if any((a.get("user") or {}).get("username") == me for a in approvals.get("approved_by", [])):
                continue  # already approved by you: nothing left to do
            review.append(gitlab_review_pr(mr))
    except (FetchError, KeyError, TypeError) as exc:
        errors.append(f"GitLab (review queue): {exc}")
    return mine, review


def fetch_all(
    projects: dict[str, Remote], settings: dict, run: Runner, now: float, cache: Path = PLUGIN_CACHE, home: str = str(Path.home())
) -> PrData:
    """`projects` maps a project's folder to its git remote; only those repositories are looked at."""
    remotes = {path: r for path, r in projects.items()}
    errors: list[str] = []
    gh_mine, gh_review = fetch_github(remotes, settings, run, cache, errors)
    gl_mine, gl_review = fetch_gitlab(remotes, settings, run, errors, home)
    return PrData(
        mine=link_stacks(gh_mine + gl_mine), review=sorted(gh_review + gl_review, key=lambda p: p.created_at),
        errors=errors, fetched_at=now, repos={r.key: path for path, r in remotes.items()},
    )
