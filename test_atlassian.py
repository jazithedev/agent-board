import os
import tempfile
import unittest
from pathlib import Path

import atlassian as atl

ME = "acc-me"
SITE = "https://acme.atlassian.net"
NOW = 1_800_000_000.0


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def adf(*parts):
    """A paragraph of text pieces and ("@", account, name) mentions."""
    content = [{"type": "mention", "attrs": {"id": p[1], "text": f"@{p[2]}"}} if isinstance(p, tuple) else {"type": "text", "text": p} for p in parts]
    return {"type": "doc", "content": [{"type": "paragraph", "content": content}]}


def jira_comment(who, name, at, *parts):
    return {"author": {"accountId": who, "displayName": name}, "created": iso(at), "body": adf(*parts)}


def issue(key="BI-1", description=None, reporter="acc-anna"):
    return {"key": key, "fields": {"summary": "Cancel endpoint", "status": {"name": "Blocked"}, "project": {"key": key.split("-")[0]},
                                   "updated": iso(NOW), "created": iso(NOW - 9000), "reporter": {"accountId": reporter, "displayName": "Anna"},
                                   "description": description}}


def conf_comment(cid, who, name, at, storage="", view="", parent=None):
    return {"id": cid, "ancestors": [{"id": parent, "type": "comment"}] if parent else [],
            "history": {"createdBy": {"accountId": who, "displayName": name}, "createdDate": iso(at)},
            "body": {"storage": {"value": storage}, "view": {"value": view}}}


def mention_storage(account):
    return f'<p><ac:link><ri:user ri:account-id="{account}" /></ac:link> question?</p>'


PAGE = {"id": "500", "title": "Manage APIs", "space": {"key": "ENG"}, "_links": {"webui": "/spaces/ENG/pages/500/Manage+APIs"}}


class Basics(unittest.TestCase):
    def test_a_site_is_normalised_from_a_name_a_host_or_a_link(self):
        for value in ("acme", "acme.atlassian.net", "https://acme.atlassian.net/", "https://acme.atlassian.net/browse/BI-1"):
            self.assertEqual(atl.normalize_site(value), SITE)

    def test_the_first_load_looks_back_the_set_days_and_later_ones_only_since_the_last_load(self):
        self.assertEqual(atl.window_minutes(0, NOW, 7), 7 * 24 * 60)
        self.assertEqual(atl.window_minutes(NOW - 15 * 60, NOW, 7), 15 + atl.OVERLAP_MINUTES)
        self.assertEqual(atl.window_minutes(NOW - 20, NOW, 7), 1 + atl.OVERLAP_MINUTES)
        self.assertEqual(atl.window_minutes(NOW - 40 * 86400, NOW, 7), 40 * 24 * 60 + atl.OVERLAP_MINUTES)  # a long gap is covered in full

    def test_jira_and_confluence_times_are_read(self):
        self.assertEqual(atl.parse_time("2026-11-03T15:52:20.587+0000"), atl.parse_time("2026-11-03T15:52:20.587Z"))
        self.assertGreater(atl.parse_time("2026-11-03T15:52:20+0100"), 0)
        self.assertEqual(atl.parse_time("nonsense"), 0.0)

    def test_adf_text_keeps_mentions_and_lines(self):
        doc = {"type": "doc", "content": [adf("Hi ", ("@", ME, "Jazi"), ", a question")["content"][0],
                                          {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]}]}
        self.assertEqual(atl.adf_text(doc).strip().split("\n"), ["Hi @Jazi, a question", "· one"])
        self.assertEqual(atl.adf_mentions(doc), {ME})

    def test_confluence_html_becomes_text(self):
        self.assertEqual(atl.html_text("<p>a &amp; b</p><p>c<br/>d</p>"), "a & b\nc\nd")


class JiraThreads(unittest.TestCase):
    def thread(self, comments, **kw):
        return atl.jira_thread(issue(**kw), comments, len(comments), ME, SITE)

    def test_a_mention_after_your_last_comment_waits_on_you(self):
        t = self.thread([jira_comment(ME, "Me", NOW - 300, "my answer"), jira_comment("acc-anna", "Anna", NOW - 100, ("@", ME, "Jazi"), " one more?")])
        self.assertTrue(t.waiting)
        self.assertEqual((t.asked_by, t.ref, t.project, t.url), ("Anna", "BI-1", "BI", f"{SITE}/browse/BI-1"))
        self.assertEqual(t.question, "@Jazi one more?")

    def test_a_mention_you_answered_does_not(self):
        t = self.thread([jira_comment("acc-anna", "Anna", NOW - 300, ("@", ME, "Jazi"), " ?"), jira_comment(ME, "Me", NOW - 100, "done")])
        self.assertFalse(t.waiting)
        self.assertEqual(t.answered_at, NOW - 100)

    def test_a_mention_where_you_never_commented_waits_on_you(self):
        self.assertTrue(self.thread([jira_comment("acc-anna", "Anna", NOW - 300, ("@", ME, "Jazi"))]).waiting)

    def test_a_later_comment_that_does_not_name_you_keeps_it_waiting(self):
        t = self.thread([jira_comment("acc-anna", "Anna", NOW - 300, ("@", ME, "Jazi")), jira_comment("acc-alex", "Alex", NOW - 100, "noted")])
        self.assertTrue(t.waiting)
        self.assertEqual(t.asked_by, "Anna")

    def test_an_issue_that_never_asked_you_is_no_thread(self):
        self.assertIsNone(self.thread([jira_comment("acc-anna", "Anna", NOW - 300, "hello")]))

    def test_a_mention_in_the_description_counts_unless_you_wrote_it(self):
        self.assertTrue(self.thread([], description=adf(("@", ME, "Jazi"), " please look")).waiting)
        self.assertIsNone(self.thread([], description=adf(("@", ME, "Jazi")), reporter=ME))


class ConfluenceThreads(unittest.TestCase):
    def test_comments_are_grouped_by_their_top_comment(self):
        comments = [
            conf_comment("1", "acc-roman", "Roman", NOW - 500, mention_storage(ME), "<p>@Jazi question?</p>"),
            conf_comment("2", ME, "Me", NOW - 400, "<p>answer</p>", "<p>answer</p>", parent="1"),
            conf_comment("3", "acc-roman", "Roman", NOW - 300, "<p>other</p>", "<p>other</p>"),  # not yours: no thread
        ]
        threads = atl.confluence_threads(PAGE, comments, ME, SITE)
        self.assertEqual([t.key for t in threads], ["confluence:1"])
        t = threads[0]
        self.assertFalse(t.waiting)
        self.assertEqual((t.title, t.ref, t.page, t.total), ("Manage APIs", "ENG", "500", 2))
        self.assertEqual(t.url, f"{SITE}/wiki/spaces/ENG/pages/500/Manage+APIs?focusedCommentId=1")

    def test_a_reply_under_your_comment_waits_on_you_without_a_mention(self):
        comments = [conf_comment("1", ME, "Me", NOW - 500, "<p>q</p>"), conf_comment("2", "acc-roman", "Roman", NOW - 100, "<p>a</p>", "<p>a</p>", parent="1")]
        t = atl.confluence_threads(PAGE, comments, ME, SITE)[0]
        self.assertTrue(t.waiting)
        self.assertEqual((t.kind, t.asked_by, t.question), ("reply", "Roman", "a"))


class Approvals(unittest.TestCase):
    def record(self, status, approver=ME):
        storage = (f'<table><tr><th>Status</th><td><ac:structured-macro ac:name="status"><ac:parameter ac:name="title">{status}</ac:parameter>'
                   f'</ac:structured-macro></td></tr><tr><th><p>Approver</p></th><td><ac:link><ri:user ri:account-id="{approver}" /></ac:link></td></tr></table>')
        return {**PAGE, "body": {"storage": {"value": storage}}, "version": {"when": iso(NOW - 60), "by": {"displayName": "Roman"}}}

    def test_a_record_with_you_as_approver_asks_until_it_is_decided(self):
        t = atl.approval_thread(self.record("IN PROGRESS"), ME, SITE)
        self.assertEqual((t.kind, t.status, t.asked_by, t.key), ("approve", "IN PROGRESS", "Roman", "approve:500"))
        self.assertIsNone(atl.approval_thread(self.record("DECIDED"), ME, SITE))

    def test_someone_else_as_approver_is_not_yours(self):
        self.assertIsNone(atl.approval_thread(self.record("IN PROGRESS", approver="acc-other"), ME, SITE))


class FakeAtlassian:
    """Answers the REST calls a load makes, and remembers them."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        if self.fail and self.fail in url:
            raise atl.AtlassianError("boom")
        if url.endswith("/myself"):
            return {"accountId": ME}
        if "/search/jql" in url:
            return {"issues": [issue("BI-7")], "isLast": True}
        if "/issue/BI-7/comment" in url:
            return {"comments": [jira_comment("acc-anna", "Anna", NOW - 60, ("@", ME, "Jazi"), " ?")], "total": 1}
        if "/wiki/rest/api/search" in url:
            if "mention" in url and "type+%3D+comment" in url:
                return {"results": [{"content": {"type": "comment", "id": "1", "container": {"id": "500"}}}]}
            return {"results": []}
        if "/child/comment" in url:
            return {"results": [conf_comment("1", "acc-roman", "Roman", NOW - 30, mention_storage(ME), "<p>@Jazi?</p>")]}
        if "/wiki/rest/api/content/500" in url:
            return PAGE
        return {}


class Refresh(unittest.TestCase):
    def setUp(self):
        self.config = atl.Config(Path(tempfile.mkdtemp()) / "atlassian.json")
        self.config.set("site", "acme")
        self.config.set("email", "me@example.com")
        self.config.set("token", "secret-token")

    def test_the_first_load_looks_back_a_week_and_finds_both_sources(self):
        cache, http = atl.Cache(), FakeAtlassian()
        self.assertEqual(atl.refresh(cache, self.config, NOW, 7, http), [])
        self.assertEqual(set(cache.threads), {"jira:BI-7", "confluence:1"})
        self.assertEqual((cache.account, cache.loaded_at, cache.site), (ME, NOW, SITE))
        jql = next(b["jql"] for m, u, b in http.calls if "/search/jql" in u)
        self.assertIn(f'comment ~ "{ME}"', jql)
        self.assertIn("updated >= -10080m", jql)

    def test_a_later_load_asks_only_since_the_last_one(self):
        cache, http = atl.Cache(), FakeAtlassian()
        atl.refresh(cache, self.config, NOW, 7, http)
        later = FakeAtlassian()
        atl.refresh(cache, self.config, NOW + 15 * 60, 7, later)
        jql = next(b["jql"] for m, u, b in later.calls if "/search/jql" in u)
        self.assertIn(f"updated >= -{15 + atl.OVERLAP_MINUTES}m", jql)
        self.assertFalse(any(u.endswith("/myself") for _, u, _ in later.calls))  # who you are is cached
        self.assertTrue(any("16m" in u for _, u, _ in later.calls if "/wiki/rest/api/search" in u))

    def test_a_failed_load_keeps_the_old_load_time_so_the_window_is_retried(self):
        cache = atl.Cache()
        atl.refresh(cache, self.config, NOW, 7, FakeAtlassian())
        errors = atl.refresh(cache, self.config, NOW + 900, 7, FakeAtlassian(fail="/wiki/"))
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("Confluence"))
        self.assertEqual(cache.loaded_at, NOW)

    def test_another_site_starts_over(self):
        cache = atl.Cache()
        atl.refresh(cache, self.config, NOW, 7, FakeAtlassian())
        self.config.set("site", "other")
        http = FakeAtlassian()
        atl.refresh(cache, self.config, NOW + 60, 7, http)
        self.assertEqual(cache.site, "https://other.atlassian.net")
        self.assertTrue(any(u == "https://other.atlassian.net/rest/api/3/myself" for _, u, _ in http.calls))
        jql = next(b["jql"] for m, u, b in http.calls if "/search/jql" in u)
        self.assertIn("updated >= -10080m", jql)  # a first load again


class Files(unittest.TestCase):
    def test_the_login_file_is_readable_only_by_you(self):
        path = Path(tempfile.mkdtemp()) / "atlassian.json"
        c = atl.Config(path)
        c.set("token", "secret")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(atl.Config(path).token, "secret")
        self.assertFalse(atl.Config(path).ready)

    def test_the_cache_survives_a_restart(self):
        path = Path(tempfile.mkdtemp()) / "mentions.json"
        t = atl.Thread("jira:BI-1", "jira", "mention", "BI-1", "T", "u", asked_at=NOW, comments=[atl.Comment("Anna", NOW, "hi", mentions_me=True)])
        atl.Cache(SITE, ME, NOW, {t.key: t}, {"500": NOW}).save(path)
        back = atl.Cache.load(path)
        self.assertEqual((back.site, back.account, back.loaded_at, back.watched), (SITE, ME, NOW, {"500": NOW}))
        self.assertEqual(back.threads["jira:BI-1"], t)

    def test_a_missing_or_broken_cache_is_a_first_load(self):
        path = Path(tempfile.mkdtemp()) / "mentions.json"
        self.assertEqual(atl.Cache.load(path).loaded_at, 0.0)
        path.write_text("{nope")
        self.assertEqual(atl.Cache.load(path).loaded_at, 0.0)

    def test_quiet_threads_are_forgotten_but_waiting_ones_stay(self):
        old = NOW - 40 * 86400
        waiting = atl.Thread("jira:A-1", "jira", "mention", "A-1", "", "", asked_at=old)
        answered = atl.Thread("jira:A-2", "jira", "mention", "A-2", "", "", asked_at=old, answered_at=old + 1)
        cache = atl.Cache(SITE, ME, NOW, {waiting.key: waiting, answered.key: answered})
        cache.prune(NOW)
        self.assertEqual(set(cache.threads), {"jira:A-1"})
        cache.prune(NOW, keep={"jira:A-1"})  # one you marked done goes too
        self.assertEqual(cache.threads, {})


if __name__ == "__main__":
    unittest.main()
