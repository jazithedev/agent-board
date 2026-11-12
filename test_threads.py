"""The Mentions tab: Jira and Confluence threads that wait on you."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_board as ab
import atlassian as atl

NOW = 1_800_000_000.0
H, DAY = 3600, 86400


def thread(key, asked=2 * H, answered=0.0, source="jira", kind="mention", who="Anna", project="BI"):
    ref = key.split(":")[1] if source == "jira" else "ENG"
    return atl.Thread(f"{source if kind != 'approve' else 'approve'}:{key.split(':')[1]}", source, kind, ref, f"Title of {key}", f"https://example.com/{key}",
                      project if source == "jira" else "", "Open", who, NOW - asked if asked else 0.0, NOW - answered if answered else 0.0, NOW - 60,
                      comments=[atl.Comment("you", NOW - 5 * H, "my earlier answer", mine=True), atl.Comment(who, NOW - asked, f"@Jazi a question from {who}?", mentions_me=True)],
                      total=2)


def board(*threads, now=NOW):
    meta = ab.Meta(Path(tempfile.mkdtemp()) / "meta.json")
    b = ab.Board(meta, "/srv/www/tools", now=lambda: now)
    b.atl = atl.Config(Path(tempfile.mkdtemp()) / "atlassian.json")
    b.threads = atl.Cache("https://example.atlassian.net", "acc-me", NOW - 300, {t.key: t for t in threads})
    b.mode = "mentions"
    return b


def screen(b, width=120, height=40):
    return "\n".join(ab.plain(l) for l in ab.compose(b, width, height)[0])


def defaults():
    return (thread("jira:BI-1"), thread("confluence:9", asked=3 * DAY, source="confluence", kind="reply", who="Roman"),
            thread("confluence:500", asked=4 * H, source="confluence", kind="approve", who="Roman"),
            thread("jira:BI-2", asked=2 * DAY, answered=1 * DAY), thread("jira:BI-3", asked=9 * DAY, answered=5 * DAY))


class Groups(unittest.TestCase):
    def test_threads_are_grouped_by_what_you_owe(self):
        b = board(*defaults())
        groups = [(state, [t.key for t in items]) for state, _, items in b.thread_groups()]
        self.assertEqual(groups, [("waiting", ["jira:BI-1", "confluence:9"]), ("approve", ["approve:500"]), ("answered", ["jira:BI-2"])])  # BI-3: answered too long ago

    def test_the_screen_shows_the_groups_the_question_and_the_count_in_the_strip(self):
        text = screen(board(*defaults()))
        for part in ("Waiting on you  2", "Asked to approve  1", "Answered by you  1", "BI-1", "replied to you", "“@Jazi a question from Anna?”"):
            self.assertIn(part, text)
        self.assertIn(" Mentions (3) ", text.split("\n")[1])

    def test_a_filter_narrows_the_threads(self):
        b = board(*defaults())
        b.query = "roman"
        self.assertEqual([t.key for t in b.thread_flat()], ["confluence:9", "approve:500"])

    def test_without_a_login_the_tab_says_how_to_set_one_up(self):
        b = board()
        b.threads = None
        self.assertIn("ctrl+s → Atlassian site, e-mail and API token", screen(b))

    def test_nothing_waiting_is_said_plainly(self):
        self.assertIn("Nothing in Jira or Confluence waits on you", screen(board()))


class Keys(unittest.TestCase):
    def setUp(self):
        self.b = board(*defaults())
        self.b.handle("down")

    def test_enter_opens_the_last_comments_under_the_row_and_closes_them_again(self):
        self.assertEqual(self.b.thread_sel, "jira:BI-1")
        self.b.handle("enter")
        text = screen(self.b)
        self.assertIn("│ you · 5h", text)
        self.assertIn("│   my earlier answer", text)
        self.assertIn("2 comments · Open · o opens it in the browser", text)
        self.b.handle("enter")
        self.assertNotIn("│ you · 5h", screen(self.b))

    def test_o_opens_the_browser_and_r_asks_for_a_reply_session(self):
        self.assertEqual(self.b.handle("char", "o").url, "https://example.com/jira:BI-1")
        effect = self.b.handle("char", "r")
        self.assertEqual((effect.kind, effect.items[0].key), ("reply", "jira:BI-1"))

    def test_d_hides_a_thread_until_someone_asks_again(self):
        self.b.handle("char", "d")
        self.assertNotIn("jira:BI-1", [t.key for t in self.b.thread_flat()])
        self.assertEqual(self.b.thread_sel, "confluence:9")  # the next row takes the selection
        later = board(*defaults(), now=NOW + H)
        later.meta = self.b.meta
        self.assertNotIn("jira:BI-1", [t.key for t in later.thread_flat()])  # remembered in meta.json
        t = self.b.threads.threads["jira:BI-1"]
        t.asked_at = NOW + 60  # a new mention
        self.assertIn("jira:BI-1", [t.key for t in self.b.thread_flat()])

    def test_z_snoozes_for_a_day(self):
        self.b.handle("char", "z")
        self.assertNotIn("jira:BI-1", [t.key for t in self.b.thread_flat()])
        self.assertEqual(self.b.snoozed(), 1)
        self.assertIn("1 snoozed", screen(self.b))
        tomorrow = board(*defaults(), now=NOW + DAY + 1)
        tomorrow.meta = self.b.meta
        self.assertIn("jira:BI-1", [t.key for t in tomorrow.thread_flat()])

    def test_right_opens_the_reply_session_on_the_thread(self):
        s = ab.Session(id="s1", name="Reply: BI-1", state="working", cwd="/srv/www/tools", project="tools", project_path="/srv/www/tools", started_at=NOW, updated_at=NOW)
        self.b.set_sessions([s])
        self.assertIn("⟳", screen(self.b))
        self.assertEqual(self.b.handle("right").session_id, "s1")

    def test_ctrl_l_loads_now_and_esc_goes_back(self):
        self.assertEqual(self.b.handle("ctrl+l").kind, "refresh_mentions")
        self.b.handle("esc")
        self.assertEqual(self.b.tab, "sessions")

    def test_a_click_selects_and_opens_the_thread(self):
        screen(self.b)
        row = next(y for y, k in self.b.hit.items() if k == "confluence:9")
        self.b.click(row, 10)
        self.assertEqual((self.b.thread_sel, self.b.thread_open), ("confluence:9", "confluence:9"))


class Replies(unittest.TestCase):
    def test_the_prompt_names_the_thread_and_forbids_posting(self):
        b = board(*defaults())
        prompt = ab.reply_prompt(b, b.threads.threads["jira:BI-1"])
        for part in ("Anna asked me", "Jira BI-1", "https://example.com/jira:BI-1", "Do not post anything"):
            self.assertIn(part, prompt)

    def test_the_session_starts_in_the_folder_set_for_the_jira_project(self):
        b = board(*defaults())
        b.meta.set_setting("reply_folders", "BI=/srv/www/tools, FP = /srv/www/web")
        self.assertEqual(b.reply_folders(), {"BI": "/srv/www/tools", "FP": "/srv/www/web"})
        calls = []
        ok = SimpleNamespace(returncode=0, stdout="started", stderr="")
        with mock.patch.object(ab, "run_cli", lambda args, cwd=None, timeout=60: calls.append((args, cwd)) or ok), mock.patch.object(ab, "load_sessions", lambda: []):
            ab.start_reply_session(b, b.threads.threads["jira:BI-1"])
            ab.start_reply_session(b, b.threads.threads["confluence:9"])
        self.assertEqual(calls[0][1], "/srv/www/tools")
        self.assertIn("Reply: BI-1", calls[0][0])
        self.assertEqual(calls[1][1], str(Path.home()))  # no folder for Confluence: home
        self.assertTrue(b.msg.startswith("✓"))


class Settings(unittest.TestCase):
    def open_field(self, b, name):
        b.handle("ctrl+s")
        b.settingsui["idx"] = [n for n, _ in b.settings_fields()].index(name)

    def test_the_token_is_never_drawn(self):
        b = board(*defaults())
        self.open_field(b, "atl_token")
        b.handle("enter")
        b.handle("paste", "super-secret-token")
        self.assertNotIn("super-secret", screen(b))
        b.handle("enter")
        self.assertEqual(b.atl.token, "super-secret-token")
        self.assertTrue(b.mentions_requested.is_set())
        self.assertIn("set (…oken)", screen(b))

    def test_an_empty_token_keeps_the_old_one(self):
        b = board(*defaults())
        b.atl.set("token", "kept")
        self.open_field(b, "atl_token")
        b.handle("enter")
        b.handle("enter")
        self.assertEqual(b.atl.token, "kept")

    def test_refresh_interval_and_first_load_cycle(self):
        b = board(*defaults())
        self.assertEqual((b.mention_interval(), b.first_load_days()), (15 * 60, 7))
        self.open_field(b, "mention_refresh")
        b.handle("right")
        self.assertEqual(b.mention_interval(), 30 * 60)
        b.handle("down")
        b.handle("right")
        self.assertEqual(b.first_load_days(), 14)


class Loading(unittest.TestCase):
    def test_a_load_saves_the_cache_and_says_how_it_went(self):
        b = board()
        b.threads = None
        b.atl.set("site", "acme")
        b.atl.set("email", "me@example.com")
        b.atl.set("token", "t")
        path = Path(tempfile.mkdtemp()) / "mentions.json"

        def http(method, url, body=None):
            if url.endswith("/myself"):
                return {"accountId": "acc-me"}
            if "/search/jql" in url:
                return {"issues": [], "isLast": True}
            return {"results": []}

        b.msg = ab.REFRESHING_MENTIONS
        ab.MentionLoader(b, http, path).refresh()
        self.assertEqual(b.msg, "✓ Jira and Confluence loaded · 0 threads wait on you")
        self.assertEqual(atl.Cache.load(path).account, "acc-me")

    def test_without_a_login_nothing_is_loaded(self):
        b = board()
        b.msg = ab.REFRESHING_MENTIONS
        ab.MentionLoader(b, lambda *a: self.fail("no call expected"), Path(tempfile.mkdtemp()) / "m.json").refresh()
        self.assertIn("ctrl+s", b.msg)


if __name__ == "__main__":
    unittest.main()
