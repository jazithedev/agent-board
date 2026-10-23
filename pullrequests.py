"""Pull requests for agent-board: what you still have to review, and where your own PRs stand.

GitHub goes through one `gh api graphql` request per refresh: your open PRs and the PRs that request your
review, with their review state, merge state and checks. GitLab goes through `glab api`.
Every call is read-only. All process access goes through one injectable `run`.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

Runner = Callable[..., subprocess.CompletedProcess]

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
        if self.draft:  # a draft is not asking for review or a merge yet, whatever else is true of it
            return "draft"
        if self.attention:
            return "attention"
        return "ready" if self.decision == "approved" else "waiting"

    @property
    def notes(self) -> list[str]:
        notes = []
        if self.group == "ready" and self.merge == "behind":
            notes.append("behind base")
        if self.group == "waiting" and not self.requested and not self.reviewed:
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
    flags: tuple[str, ...] = ()  # new | re-requested | teammate-changes-requested | teammate-approved
    approved_by_me: bool = False  # your latest review is an approval that still stands
    approved_before: bool = False  # you approved it at some point (GitHub only)
    head_sha: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}#{self.number}"

    @property
    def repo_key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}"

    @property
    def needs_reapproval(self) -> bool:
        """You approved it before, and it asks for your approval again: dismissed by new commits, or re-requested."""
        return self.approved_before and (not self.approved_by_me or "re-requested" in self.flags)

    @property
    def collective(self) -> bool:
        """The collective PR of a stack: it only gathers PRs that were reviewed one by one."""
        return "[collective]" in self.title.lower()


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


TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
REVIEWED = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}


def _login(actor: object) -> str:
    return (actor or {}).get("login", "") if isinstance(actor, dict) else ""


def _nodes(conn: object) -> list[dict]:
    return [n for n in ((conn or {}).get("nodes") or []) if isinstance(n, dict)] if isinstance(conn, dict) else []


def _requested(node: dict) -> tuple[str, ...]:
    """Who is still asked to review: people by login, teams by name."""
    names = []
    for n in _nodes(node.get("reviewRequests")):
        who = n.get("requestedReviewer") or {}
        if who.get("login") or who.get("name"):
            names.append(who.get("login") or who.get("name"))
    return tuple(names)


def _reviews(node: dict) -> dict[str, str]:
    """Each person's latest review state; bots (Copilot and the like) are not reviewers."""
    out = {}
    for n in _nodes(node.get("latestReviews")):
        author = n.get("author") or {}
        login = author.get("login", "")
        if login and author.get("__typename") != "Bot" and not login.endswith("[bot]"):
            out[login] = n.get("state", "")
    return out


def ticket_of(head: str, title: str) -> str:
    """The ticket key in the branch name, else in the title."""
    m = TICKET_RE.search(head.upper()) or TICKET_RE.search(title)
    return m.group(1) if m else ""


def github_my_pr(node: dict) -> MyPr:
    commits = _nodes(node.get("commits"))
    rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
    merge = _MERGE.get(node.get("mergeStateStatus") or "", "unknown")
    checks = _CHECKS.get(rollup.get("state") or "", "")
    head, title = node.get("headRefName") or "", node.get("title") or ""
    return MyPr(
        provider="github", repo=(node.get("repository") or {}).get("nameWithOwner", ""), number=int(node["number"]), title=title,
        url=node.get("url", ""), base=node.get("baseRefName") or "", head=head, draft=bool(node.get("isDraft")), ticket=ticket_of(head, title),
        created_at=parse_time(node.get("createdAt")), updated_at=parse_time(node.get("updatedAt")),
        decision=_DECISION.get(node.get("reviewDecision") or "", "none"), merge=merge, checks=checks,
        checks_stale=merge == "conflict" and bool(checks),  # a conflict stops new runs, so the result is an old one
        requested=_requested(node), reviewed=bool(_reviews(node)),
    )


def review_flags(node: dict, me: str) -> tuple[str, ...]:
    """re-requested: asked again after you reviewed. teammate-…: someone other than you and the author decided."""
    author = _login(node.get("author"))
    reviews = _reviews(node)
    others = {who: state for who, state in reviews.items() if who not in (me, author)}
    flags = []
    if reviews.get(me) in REVIEWED and me in _requested(node):
        flags.append("re-requested")
    if "CHANGES_REQUESTED" in others.values():
        flags.append("teammate-changes-requested")
    if "APPROVED" in others.values():
        flags.append("teammate-approved")
    return tuple(flags) or ("new",)


def github_review_pr(node: dict, me: str) -> ReviewPr:
    return ReviewPr(
        provider="github", repo=(node.get("repository") or {}).get("nameWithOwner", ""), number=int(node["number"]),
        title=node.get("title") or "", url=node.get("url", ""), author=_login(node.get("author")), draft=bool(node.get("isDraft")),
        created_at=parse_time(node.get("createdAt")), updated_at=parse_time(node.get("updatedAt")), flags=review_flags(node, me),
        approved_by_me=_reviews(node).get(me) == "APPROVED", head_sha=node.get("headRefOid") or "",
        approved_before=bool(me) and me in approvers(node),
    )


def approvers(node: dict) -> set[str]:
    """Everyone who ever approved: standing approvals, and those dismissed later, which GitHub turns into DISMISSED reviews
    and only the dismissal event still remembers as approvals."""
    found = {_login(n.get("author")) for n in _nodes(node.get("approvals"))}
    found |= {_login((n.get("review") or {}).get("author")) for n in _nodes(node.get("dismissals")) if n.get("previousReviewState") == "APPROVED"}
    return found - {""}


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


_PR_FIELDS = "number title url createdAt updatedAt isDraft repository { nameWithOwner } author { login }"
_PEOPLE = """reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
      latestReviews(first: 20) { nodes { state author { __typename login } } }"""
_REVIEW_HISTORY = """approvals: reviews(states: APPROVED, last: 50) { nodes { author { login } } }
      dismissals: timelineItems(itemTypes: [REVIEW_DISMISSED_EVENT], last: 50) {
        nodes { ... on ReviewDismissedEvent { previousReviewState review { author { login } } } } }"""
_REQUESTS = """requests: timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 50) {
        nodes { ... on ReviewRequestedEvent { requestedReviewer { ... on User { login } ... on Team { slug organization { login } } } } } }"""
GITHUB_QUERY = f"""query {{
  viewer {{ login }}
  mine: search(query: "is:pr is:open author:@me archived:false", type: ISSUE, first: 100) {{
    nodes {{ ... on PullRequest {{ {_PR_FIELDS} baseRefName headRefName reviewDecision mergeStateStatus
      {_PEOPLE}
      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }} }} }}
  }}
  review: search(query: "is:pr is:open archived:false review-requested:@me", type: ISSUE, first: 100) {{
    nodes {{ ... on PullRequest {{ {_PR_FIELDS} headRefOid
      {_PEOPLE}
      {_REVIEW_HISTORY} }} }}
  }}
}}"""


def github_query(repos: list[str], since: Optional[str]) -> str:
    """GITHUB_QUERY, plus the open PRs of these repositories you reviewed, and (when `since` is given) all the others with
    their review-request history.

    GitHub drops a review request once you review, and a team's once any member reviews; new commits that dismiss your
    approval do not bring yours back, and a PR asked of your team leaves review-requested:@me as soon as a teammate
    approves. Your reviews and the request history still show both."""
    if not repos:
        return GITHUB_QUERY
    repo_scope = " ".join(f"repo:{r}" for r in repos)
    extra = f"""
  dismissed: search(query: "is:pr is:open archived:false -author:@me reviewed-by:@me {repo_scope} sort:updated-desc", type: ISSUE, first: 100) {{
    nodes {{ ... on PullRequest {{ {_PR_FIELDS} headRefOid
      {_PEOPLE}
      {_REVIEW_HISTORY} }} }}
  }}"""
    if since is not None:
        scope = repo_scope + (f" created:>={since}" if since else "")
        extra += f"""
  team: search(query: "is:pr is:open archived:false -author:@me -reviewed-by:@me {scope} sort:updated-desc", type: ISSUE, first: 100) {{
    nodes {{ ... on PullRequest {{ {_PR_FIELDS} headRefOid
      {_PEOPLE}
      {_REVIEW_HISTORY}
      {_REQUESTS} }} }}
  }}"""
    return GITHUB_QUERY[: GITHUB_QUERY.rstrip().rfind("}")] + extra.lstrip("\n") + "\n}"


def my_teams(run: Runner) -> set[str]:
    """Your GitHub teams as "org/slug" (lower case); empty when gh cannot list them (the token lacks read:org)."""
    try:
        proc = run(["gh", "api", "user/teams", "--paginate", "--jq", '.[] | .organization.login + "/" + .slug'], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {l.strip().lower() for l in proc.stdout.splitlines() if l.strip()} if proc.returncode == 0 else set()


def asked_of(node: dict, me: str, teams: set[str]) -> bool:
    """Whether you, or a team of yours, was ever asked to review this PR."""
    for n in _nodes(node.get("requests")):
        who = n.get("requestedReviewer") or {}
        if who.get("login") == me:
            return True
        if who.get("slug") and f"{(who.get('organization') or {}).get('login', '')}/{who['slug']}".lower() in teams:
            return True
    return False
MERGE_TRIES = 3  # GitHub works merge states out lazily: a cold query reports UNKNOWN, the next one the real value


def fetch_github(
    remotes: dict[str, Remote], settings: dict, run: Runner, errors: list[str], sleep: Callable[[float], None] = time.sleep, now: Optional[float] = None
) -> tuple[list[MyPr], list[ReviewPr]]:
    """Your open PRs and the PRs that request your review, in one request (asked again while merge states are unknown)."""
    scope = {r.repo.lower() for r in remotes.values() if r.provider == "github"}
    if not scope:
        return [], []
    keep_team = settings.get("keep_team_reviews", True) is not False
    teams = my_teams(run) if keep_team else set()
    days = settings.get("review_max_age_days", 30)
    since = None
    if keep_team and teams:
        since = datetime.fromtimestamp((now or time.time()) - int(days) * 86400, tz=timezone.utc).strftime("%Y-%m-%d") if days else ""
    query = github_query(sorted(scope), since)
    try:
        for attempt in range(MERGE_TRIES):
            data = (_json(run, ["gh", "api", "graphql", "-f", f"query={query}"]) or {}).get("data") or {}
            mine_nodes = [n for n in _nodes(data.get("mine")) if n.get("number")]
            if attempt == MERGE_TRIES - 1 or not any(n.get("mergeStateStatus") == "UNKNOWN" for n in mine_nodes):
                break
            sleep(1.0)
        me = _login(data.get("viewer"))
        mine = [github_my_pr(n) for n in mine_nodes]
        review = [github_review_pr(n, me) for n in _nodes(data.get("review")) if n.get("number")]
        listed = {p.key for p in review}
        for n in _nodes(data.get("dismissed")):  # new commits dismissed your approval and nobody asked again
            if n.get("number") and _login(n.get("author")) != me and _reviews(n).get(me) == "DISMISSED" and me in approvers(n):
                p = github_review_pr(n, me)
                if p.key not in listed:
                    review.append(p)
                    listed.add(p.key)
        for n in _nodes(data.get("team")):  # asked of you or your team, then a teammate reviewed: GitHub dropped the request
            if n.get("number") and _login(n.get("author")) != me and not _reviews(n).get(me) and asked_of(n, me, teams):
                p = github_review_pr(n, me)
                if p.key not in listed:
                    review.append(p)
                    listed.add(p.key)
    except (FetchError, AttributeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"GitHub (pull requests): {exc}")
        return [], []
    include_drafts = bool(settings.get("include_drafts"))
    return ([p for p in mine if p.repo.lower() in scope],
            [p for p in review if p.repo.lower() in scope and (include_drafts or not p.draft)])


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


def approve(pr: ReviewPr, run: Runner = subprocess.run) -> Optional[str]:
    """Approves a pull request (gh) or merge request (glab) as you; None when it worked, else the reason it did not."""
    if pr.provider == "gitlab":
        cmd = ["glab", "mr", "approve", str(pr.number), "--repo", f"https://{GITLAB_HOST}/{pr.repo}"]
    else:
        cmd = ["gh", "pr", "review", str(pr.number), "--repo", pr.repo, "--approve"]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return f"{cmd[0]} is not installed"
    except subprocess.SubprocessError as exc:
        return f"{cmd[0]} did not finish: {exc}"
    if proc.returncode == 0:
        return None
    return next((l.strip() for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.strip()), "failed")[:200]


MERGE_METHODS = ("squash", "merge", "rebase")


def merge_command(pr: MyPr, method: str) -> list[str]:
    """The gh / glab command that merges the pull request with `method` and deletes its branch on the remote."""
    if pr.provider == "gitlab":
        cmd = ["glab", "mr", "merge", str(pr.number), "--repo", f"https://{GITLAB_HOST}/{pr.repo}", "--yes", "--remove-source-branch"]
        return cmd + ({"squash": ["--squash"], "rebase": ["--rebase"]}.get(method, []))
    return ["gh", "pr", "merge", str(pr.number), "--repo", pr.repo, f"--{method}", "--delete-branch"]


def merge(pr: MyPr, method: str, run: Runner = subprocess.run) -> Optional[str]:
    """Merges a pull request now; None when it worked, else the reason it did not."""
    cmd = merge_command(pr, method)
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return f"{cmd[0]} is not installed"
    except subprocess.SubprocessError as exc:
        return f"{cmd[0]} did not finish: {exc}"
    if proc.returncode == 0:
        return None
    return next((l.strip() for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.strip()), "failed")[:200]


def viewer_approved(repo: str, number: int, run: Runner = subprocess.run) -> Optional[bool]:
    """Whether your approval stands on the PR's current head commit; None when GitHub could not be asked."""
    owner, _, name = repo.partition("/")
    query = (f'query {{ repository(owner: "{owner}", name: "{name}") {{ pullRequest(number: {int(number)}) {{ '
             "headRefOid viewerLatestReview { state commit { oid } } } } }")
    try:
        pull = _json(run, ["gh", "api", "graphql", "-f", f"query={query}"])["data"]["repository"]["pullRequest"]
    except (FetchError, KeyError, TypeError):
        return None
    review = pull.get("viewerLatestReview") or {}
    return review.get("state") == "APPROVED" and (review.get("commit") or {}).get("oid") == pull.get("headRefOid")


def fetch_all(
    projects: dict[str, Remote], settings: dict, run: Runner, now: float, home: str = str(Path.home()), sleep: Callable[[float], None] = time.sleep
) -> PrData:
    """`projects` maps a project's folder to its git remote; only those repositories are looked at."""
    remotes = {path: r for path, r in projects.items()}
    errors: list[str] = []
    gh_mine, gh_review = fetch_github(remotes, settings, run, errors, sleep, now)
    gl_mine, gl_review = fetch_gitlab(remotes, settings, run, errors, home)
    return PrData(
        mine=link_stacks(gh_mine + gl_mine), review=sorted(gh_review + gl_review, key=lambda p: p.created_at),
        errors=errors, fetched_at=now, repos={r.key: path for path, r in remotes.items()},
    )
