"""Jira and Confluence threads that wait on you, for agent-board's Mentions tab.

A thread waits on you when someone asked you there after your last word: a comment that @mentions you, a
reply under your own Confluence comment, or a decision record that names you as its Approver.

Read-only REST calls with an Atlassian API token (plain urllib, no dependencies). The first load looks back a
few days; every later load asks only for what changed since the previous one, and the threads are kept in a
cache file between runs. All network access goes through one injectable `http`.
"""
from __future__ import annotations

import base64
import html
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

CONFIG_PATH = Path(os.environ.get("AGENT_BOARD_ATLASSIAN", Path.home() / ".config" / "agent-board" / "atlassian.json"))
CACHE_PATH = Path(os.environ.get("AGENT_BOARD_MENTIONS_CACHE", Path.home() / ".cache" / "agent-board" / "mentions.json"))
OVERLAP_MINUTES = 1  # each load starts a minute before the last one: Jira searches by the minute and clocks drift
KEEP_DAYS = 30  # threads with nothing open for you are forgotten after this long
KEEP_COMMENTS = 6  # the last comments of a thread kept for reading it in the board
DECIDED = {"DECIDED", "APPROVED", "DONE", "REJECTED", "SUPERSEDED", "CANCELLED", "CANCELED"}  # a decision record past these needs no approval
CHUNK = 40  # ids per `container in (...)` clause

Http = Callable[[str, str, Optional[dict]], dict]  # (method, url, json body) -> json


class AtlassianError(RuntimeError):
    pass


# ───────────────────────────── config ─────────────────────────────


class Config:
    """The site, e-mail and API token, in their own file readable only by you (not in meta.json, which holds no secrets)."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        self.site = str(data.get("site") or "")
        self.email = str(data.get("email") or "")
        self.token = str(data.get("token") or "")

    @property
    def ready(self) -> bool:
        return bool(self.site and self.email and self.token)

    def set(self, name: str, value: str) -> None:
        value = value.strip()
        if name == "site" and value:
            value = normalize_site(value)
        setattr(self, name, value)
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # created private, never readable by others
            with os.fdopen(fd, "w") as f:
                json.dump({"site": self.site, "email": self.email, "token": self.token}, f, indent=2)
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
        except OSError:
            pass


def normalize_site(value: str) -> str:
    """"acme", "acme.atlassian.net" or a pasted link all become https://acme.atlassian.net."""
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    parts = urllib.parse.urlsplit(value)
    host = parts.netloc or parts.path
    if "." not in host:
        host += ".atlassian.net"
    return f"https://{host}"


def urllib_http(config: Config, timeout: int = 30) -> Http:
    auth = base64.b64encode(f"{config.email}:{config.token}".encode()).decode()

    def call(method: str, url: str, body: Optional[dict] = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Basic {auth}", "Accept": "application/json", **({"Content-Type": "application/json"} if data else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AtlassianError(f"Atlassian refused the login ({exc.code}): check the e-mail and API token in ctrl+s") from exc
            detail = ""
            try:
                payload = json.loads(exc.read() or b"{}")
                detail = "; ".join(payload.get("errorMessages") or []) or payload.get("message", "")
            except (ValueError, AttributeError):
                pass
            raise AtlassianError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).path}" + (f": {detail}" if detail else "")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AtlassianError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {getattr(exc, 'reason', exc)}") from exc
        except ValueError as exc:
            raise AtlassianError(f"not JSON from {urllib.parse.urlsplit(url).path}") from exc

    return call


# ───────────────────────────── model ─────────────────────────────


@dataclass
class Comment:
    author: str
    at: float
    text: str
    mine: bool = False
    mentions_me: bool = False


@dataclass
class Thread:
    key: str  # "jira:BI-5149", "confluence:<root comment id>" or "approve:<page id>"
    source: str  # jira | confluence
    kind: str  # mention | reply | approve
    ref: str  # BI-5149, or the Confluence space key
    title: str
    url: str
    project: str = ""  # the Jira project key, for the folder a reply session starts in
    status: str = ""
    asked_by: str = ""
    asked_at: float = 0.0  # when someone last asked you there
    answered_at: float = 0.0  # when you last wrote there
    updated_at: float = 0.0
    page: str = ""  # the Confluence page id the thread sits on
    comments: list[Comment] = field(default_factory=list)
    total: int = 0  # comments in the whole thread

    @property
    def waiting(self) -> bool:
        return self.asked_at > self.answered_at

    @property
    def question(self) -> str:
        """The comment that asked you, for the line under the row."""
        asking = [c for c in self.comments if not c.mine and c.at <= self.asked_at]
        return asking[-1].text if asking else ""

    @staticmethod
    def from_dict(d: dict) -> "Thread":
        comments = [Comment(**c) for c in d.get("comments", []) if isinstance(c, dict)]
        known = {k: v for k, v in d.items() if k in Thread.__dataclass_fields__ and k != "comments"}
        return Thread(**known, comments=comments)


@dataclass
class Cache:
    """What was fetched, kept between runs: deleting the file only means the next load is a first load again."""

    site: str = ""
    account: str = ""
    loaded_at: float = 0.0
    threads: dict[str, Thread] = field(default_factory=dict)
    watched: dict[str, float] = field(default_factory=dict)  # Confluence pages you commented on -> when, to catch replies

    @staticmethod
    def load(path: Path = CACHE_PATH) -> "Cache":
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            return Cache()
        if not isinstance(d, dict):
            return Cache()
        threads = {}
        for k, v in (d.get("threads") or {}).items():
            try:
                threads[k] = Thread.from_dict(v)
            except TypeError:
                continue
        return Cache(str(d.get("site", "")), str(d.get("account", "")), float(d.get("loaded_at") or 0), threads, dict(d.get("watched") or {}))

    def save(self, path: Path = CACHE_PATH) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            payload = {"site": self.site, "account": self.account, "loaded_at": self.loaded_at,
                       "threads": {k: asdict(t) for k, t in self.threads.items()}, "watched": self.watched}
            tmp.write_text(json.dumps(payload, indent=1))
            tmp.replace(path)
        except OSError:
            pass

    def prune(self, now: float, keep: set[str] = frozenset()) -> None:
        """Forgets threads quiet for KEEP_DAYS that wait on nothing (or are in `keep`, those you marked done), and old watched pages."""
        cutoff = now - KEEP_DAYS * 86400
        for k, t in list(self.threads.items()):
            last = max(t.asked_at, t.answered_at, t.updated_at)
            if last < cutoff and (not t.waiting or k in keep):
                del self.threads[k]
        self.watched = {p: at for p, at in self.watched.items() if at >= cutoff}


def window_minutes(loaded_at: float, now: float, first_days: int) -> int:
    """How far back this load looks: the first one `first_days`, later ones only since the last load, plus the overlap."""
    if not loaded_at:
        return first_days * 24 * 60
    return max(1, math.ceil((now - loaded_at) / 60)) + OVERLAP_MINUTES


def parse_time(value: object) -> float:
    """Jira's 2026-11-03T15:52:20.587+0000 and Confluence's 2026-10-19T10:00:00.000Z, as epoch seconds."""
    if not isinstance(value, str) or not value:
        return 0.0
    v = value.strip().replace("Z", "+00:00")
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(re.sub(r"\.\d+", "", v)).timestamp()
        except ValueError:
            return 0.0


# ───────────────────────────── text ─────────────────────────────

ADF_BLOCKS = {"paragraph", "heading", "listItem", "codeBlock", "blockquote", "rule", "panel", "tableRow", "mediaSingle"}


def adf_text(node: object) -> str:
    """Plain text of an Atlassian Document Format node: mentions as their @Name, blocks on their own lines."""
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return node if isinstance(node, str) else ""
    kind, attrs = node.get("type"), node.get("attrs") or {}
    if kind == "text":
        return node.get("text", "")
    if kind == "mention":
        name = attrs.get("text") or attrs.get("id", "")
        return name if name.startswith("@") else f"@{name}"
    if kind == "hardBreak":
        return "\n"
    if kind == "emoji":
        return attrs.get("text") or attrs.get("shortName", "")
    if kind in ("inlineCard", "blockCard"):
        return attrs.get("url", "")
    inner = adf_text(node.get("content") or [])
    if kind == "listItem":
        inner = "· " + inner.strip("\n")
    return inner.strip("\n") + "\n" if kind in ADF_BLOCKS else inner


def adf_mentions(node: object) -> set[str]:
    if isinstance(node, list):
        return set().union(*(adf_mentions(n) for n in node)) if node else set()
    if not isinstance(node, dict):
        return set()
    found = {node["attrs"]["id"]} if node.get("type") == "mention" and (node.get("attrs") or {}).get("id") else set()
    return found | adf_mentions(node.get("content") or [])


def html_text(markup: str) -> str:
    """Plain text of Confluence's rendered HTML."""
    text = re.sub(r"<(br|/p|/li|/h\d|/tr|/div|/pre)\b[^>]*>", "\n", markup or "", flags=re.I)
    text = re.sub(r"<li\b[^>]*>", "· ", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def storage_mentions(storage: str, account: str) -> bool:
    return bool(account) and f'ri:account-id="{account}"' in (storage or "")


def clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text or "").strip()


# ───────────────────────────── Jira ─────────────────────────────


def jira_search(http: Http, site: str, jql: str, fields: list[str]) -> list[dict]:
    issues, token = [], None
    for _ in range(20):  # 20 pages of 100 is far more than one window brings
        body = {"jql": jql, "fields": fields, "maxResults": 100, **({"nextPageToken": token} if token else {})}
        page = http("POST", f"{site}/rest/api/3/search/jql", body)
        issues += page.get("issues") or []
        token = page.get("nextPageToken")
        if not token or page.get("isLast"):
            break
    return issues


def jira_comments(http: Http, site: str, key: str) -> tuple[list[dict], int]:
    """The newest 100 comments of an issue, oldest first, and how many there are."""
    page = http("GET", f"{site}/rest/api/3/issue/{urllib.parse.quote(key)}/comment?orderBy=-created&maxResults=100", None)
    comments = list(reversed(page.get("comments") or []))
    return comments, int(page.get("total") or len(comments))


def jira_thread(issue: dict, comments: list[dict], total: int, account: str, site: str) -> Optional[Thread]:
    """The issue as a thread, or None when nobody there asked you anything."""
    f = issue.get("fields") or {}
    key = issue.get("key", "")
    asked_at, asked_by, answered_at = 0.0, "", 0.0
    reporter = (f.get("reporter") or {}).get("accountId", "")
    if account in adf_mentions(f.get("description")) and reporter != account:
        asked_at, asked_by = parse_time(f.get("created")), (f.get("reporter") or {}).get("displayName", "")
    kept: list[Comment] = []
    for c in comments:
        author = c.get("author") or {}
        at = parse_time(c.get("created"))
        mine = author.get("accountId") == account
        mentions_me = account in adf_mentions(c.get("body"))
        if mine:
            answered_at = max(answered_at, at)
        elif mentions_me and at >= asked_at:
            asked_at, asked_by = at, author.get("displayName", "")
        kept.append(Comment(author.get("displayName", "?"), at, clean(adf_text(c.get("body"))), mine, mentions_me and not mine))
    if not asked_at:
        return None
    return Thread(
        key=f"jira:{key}", source="jira", kind="mention", ref=key, title=f.get("summary", ""), url=f"{site}/browse/{key}",
        project=(f.get("project") or {}).get("key", key.split("-")[0]), status=(f.get("status") or {}).get("name", ""),
        asked_by=asked_by, asked_at=asked_at, answered_at=answered_at, updated_at=parse_time(f.get("updated")),
        comments=kept[-KEEP_COMMENTS:], total=total,
    )


def load_jira(cache: Cache, http: Http, site: str, minutes: int) -> None:
    a = cache.account
    jql = f'(comment ~ "{a}" OR description ~ "{a}") AND updated >= -{minutes}m ORDER BY updated DESC'
    for issue in jira_search(http, site, jql, ["summary", "status", "project", "updated", "created", "reporter", "description"]):
        comments, total = jira_comments(http, site, issue.get("key", ""))
        t = jira_thread(issue, comments, total, a, site)
        key = f"jira:{issue.get('key', '')}"
        if t:
            cache.threads[key] = t
        else:
            cache.threads.pop(key, None)


# ───────────────────────────── Confluence ─────────────────────────────

PAGE_ID_RE = re.compile(r"/pages/(\d+)")


def cql_search(http: Http, site: str, cql: str) -> list[dict]:
    out: list[dict] = []
    url: Optional[str] = f"{site}/wiki/rest/api/search?" + urllib.parse.urlencode({"cql": cql, "limit": 100, "expand": "content.container"})
    for _ in range(20):
        if not url:
            break
        page = http("GET", url, None)
        out += page.get("results") or []
        nxt = (page.get("_links") or {}).get("next")
        url = f"{site}/wiki{nxt}" if nxt else None
    return out


def hit_page(hit: dict) -> str:
    """The page a search hit (a comment or the page itself) belongs to."""
    content = hit.get("content") or {}
    if content.get("type") == "page":
        return str(content.get("id", ""))
    container = content.get("container") or {}
    if container.get("id"):
        return str(container["id"])
    m = PAGE_ID_RE.search(hit.get("url", "") or (content.get("_links") or {}).get("webui", ""))
    return m.group(1) if m else ""


def page_comments(http: Http, site: str, page: str) -> list[dict]:
    """Every footer and inline comment on a page, replies included."""
    out: list[dict] = []
    query = "expand=ancestors,history,body.view,body.storage,container&depth=all&limit=100&location=footer&location=inline"
    url: Optional[str] = f"{site}/wiki/rest/api/content/{page}/child/comment?{query}"
    for _ in range(20):
        if not url:
            break
        data = http("GET", url, None)
        out += data.get("results") or []
        nxt = (data.get("_links") or {}).get("next")
        url = f"{site}/wiki{nxt}" if nxt else None
    return out


def page_info(http: Http, site: str, page: str) -> dict:
    return http("GET", f"{site}/wiki/rest/api/content/{page}?expand=space,version,body.storage", None)


def confluence_threads(page: dict, comments: list[dict], account: str, site: str) -> list[Thread]:
    """One thread per top-level comment you take part in: mentioned in it, or replied to under your own comment."""
    by_root: dict[str, list[dict]] = {}
    for c in comments:
        parents = [a for a in c.get("ancestors") or [] if a.get("type", "comment") == "comment"]
        by_root.setdefault(str(parents[0]["id"] if parents else c.get("id")), []).append(c)
    space = (page.get("space") or {}).get("key", "")
    page_url = site + "/wiki" + ((page.get("_links") or {}).get("webui") or f"/pages/{page.get('id', '')}")
    out = []
    for root, items in by_root.items():
        items.sort(key=lambda c: parse_time((c.get("history") or {}).get("createdDate")))
        asked_at, asked_by, answered_at, kind = 0.0, "", 0.0, "mention"
        kept: list[Comment] = []
        for c in items:
            hist = c.get("history") or {}
            who = hist.get("createdBy") or {}
            at = parse_time(hist.get("createdDate"))
            body = c.get("body") or {}
            mine = who.get("accountId") == account
            mentions_me = storage_mentions((body.get("storage") or {}).get("value", ""), account)
            if mine:
                answered_at = max(answered_at, at)
            elif mentions_me or answered_at:  # a mention, or any reply after you wrote in this thread
                if at >= asked_at:
                    asked_at, asked_by, kind = at, who.get("displayName", ""), "mention" if mentions_me else "reply"
            kept.append(Comment(who.get("displayName", "?"), at, clean(html_text((body.get("view") or {}).get("value", ""))), mine, mentions_me and not mine))
        if not asked_at and not answered_at:
            continue  # you are not part of this one
        out.append(Thread(
            key=f"confluence:{root}", source="confluence", kind=kind, ref=space, title=page.get("title", ""),
            url=f"{page_url}?focusedCommentId={root}", status=space, asked_by=asked_by, asked_at=asked_at, answered_at=answered_at,
            updated_at=max(c.at for c in kept) if kept else 0.0, page=str(page.get("id", "")), comments=kept[-KEEP_COMMENTS:], total=len(items),
        ))
    return out


TR_RE = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
STATUS_TITLE_RE = re.compile(r'<ac:parameter ac:name="title">([^<]*)</ac:parameter>', re.I)


def approval(storage: str, account: str) -> Optional[str]:
    """For a decision record that names you as its Approver: its status ("" if it shows none); else None."""
    approver, status = False, ""
    for row in TR_RE.findall(storage or ""):
        label = html_text(re.sub(r"</t[hd]>.*", "", row, count=1, flags=re.S | re.I)).strip().lower()
        if label.startswith("approver") and storage_mentions(row, account):
            approver = True
        elif label.startswith("status"):
            m = STATUS_TITLE_RE.search(row)
            status = (m.group(1) if m else html_text(row).removeprefix(label)).strip()
    return status if approver else None


def approval_thread(page: dict, account: str, site: str) -> Optional[Thread]:
    status = approval(((page.get("body") or {}).get("storage") or {}).get("value", ""), account)
    if status is None or status.strip().upper() in DECIDED:
        return None
    version = page.get("version") or {}
    at = parse_time(version.get("when"))
    return Thread(
        key=f"approve:{page.get('id', '')}", source="confluence", kind="approve", ref=(page.get("space") or {}).get("key", ""),
        title=page.get("title", ""), url=site + "/wiki" + ((page.get("_links") or {}).get("webui") or f"/pages/{page.get('id', '')}"),
        status=status or "no status", asked_by=(version.get("by") or {}).get("displayName", ""), asked_at=at, updated_at=at, page=str(page.get("id", "")),
    )


def load_confluence(cache: Cache, http: Http, site: str, minutes: int, now: float) -> None:
    since = f'lastmodified >= now("-{minutes}m")'
    touched: set[str] = set()
    for hit in cql_search(http, site, f"type = comment AND mention = currentUser() AND {since}"):
        touched.add(hit_page(hit))
    for hit in cql_search(http, site, f"type = comment AND creator = currentUser() AND {since}"):
        page = hit_page(hit)
        touched.add(page)
        if page:
            cache.watched[page] = now  # replies under your comments there are looked for from now on
    watched = sorted(set(cache.watched) | {t.page for t in cache.threads.values() if t.source == "confluence" and t.kind != "approve" and t.page})
    for i in range(0, len(watched), CHUNK):
        ids = ", ".join(watched[i : i + CHUNK])
        for hit in cql_search(http, site, f"type = comment AND container in ({ids}) AND {since}"):
            touched.add(hit_page(hit))
    for page_id in sorted(p for p in touched if p):
        page = page_info(http, site, page_id)
        fresh = {t.key: t for t in confluence_threads(page, page_comments(http, site, page_id), cache.account, site)}
        for k in [k for k, t in cache.threads.items() if t.page == page_id and t.kind != "approve" and k not in fresh]:
            del cache.threads[k]  # comments deleted, or you are no longer part of them
        cache.threads.update(fresh)
    for hit in cql_search(http, site, f"type = page AND mention = currentUser() AND {since}"):
        page_id = hit_page(hit)
        if not page_id:
            continue
        t = approval_thread(page_info(http, site, page_id), cache.account, site)
        if t:
            old = cache.threads.get(t.key)
            t.answered_at = old.answered_at if old else 0.0
            cache.threads[t.key] = t
        else:
            cache.threads.pop(f"approve:{page_id}", None)  # decided, or you are no longer the approver


# ───────────────────────────── one load ─────────────────────────────


def refresh(cache: Cache, config: Config, now: float, first_days: int, http: Optional[Http] = None) -> list[str]:
    """Brings `cache` up to date in place; returns the problems (an empty list when everything loaded).

    The load time only moves on when both Jira and Confluence answered, so a failed load is retried over the same window."""
    http = http or urllib_http(config)
    site = normalize_site(config.site)
    if cache.site != site:  # another site: nothing cached belongs to it
        cache.site, cache.account, cache.loaded_at, cache.threads, cache.watched = site, "", 0.0, {}, {}
    if not cache.account:
        try:
            cache.account = str(http("GET", f"{site}/rest/api/3/myself", None).get("accountId") or "")
        except AtlassianError as exc:
            return [str(exc)]
        if not cache.account:
            return ["Atlassian did not say who you are: check the e-mail and API token in ctrl+s"]
    minutes = window_minutes(cache.loaded_at, now, first_days)
    errors = []
    for name, load in (("Jira", lambda: load_jira(cache, http, site, minutes)), ("Confluence", lambda: load_confluence(cache, http, site, minutes, now))):
        try:
            load()
        except AtlassianError as exc:
            errors.append(f"{name}: {exc}")
    if not errors:
        cache.loaded_at = now
    return errors
