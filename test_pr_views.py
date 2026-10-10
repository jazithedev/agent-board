import tempfile
import unittest
from pathlib import Path

import agent_board as ab
import pullrequests as prs

NOW = 1_800_000_000.0
DAY = 86400


def session(i, project="billing-api", state="working", prs_tag=None, updated=0):
    path = f"/srv/www/{project}"
    tags = ab.derive_tags(f"review {prs_tag}") if prs_tag else []
    return ab.Session(id=f"s{i}", name=f"session {i}", state=state, cwd=path, project=project, project_path=path,
                      started_at=NOW - 600, updated_at=NOW - updated, auto_tags=tags)


def review(n, repo="acme/billing-api", provider="github", **kw):
    return prs.ReviewPr(provider, repo, n, f"Review me {n}", f"https://example.com/{repo}/pull/{n}", "maria", created_at=NOW - n * DAY, **kw)


def mine(n, repo="acme/billing-api", provider="github", **kw):
    base = dict(created_at=NOW - DAY, updated_at=NOW - DAY, merge="clean")
    base.update(kw)
    return prs.MyPr(provider, repo, n, f"Mine {n}", f"https://example.com/{repo}/pull/{n}", **base)


REPOS = {"github:acme/billing-api": "/srv/www/billing-api", "github:acme/web-app": "/srv/www/web-app", "gitlab:scum-map/scum-map": "/srv/www/scum-map"}


def board(mode="review", **data):
    meta = ab.Meta(Path(tempfile.mkdtemp()) / "meta.json")
    b = ab.Board(meta, "/srv/www/scum-map", now=lambda: NOW)
    b.set_sessions([session(1, prs_tag="PR #5"), session(2, state="done", prs_tag="PR #5"), session(3, "scum-map")])
    b.prs = prs.PrData(repos=REPOS, fetched_at=NOW - 240, **data)
    b.mode = mode
    return b


def keys(b, *names):
    last = None
    for n in names:
        last = b.handle("space" if n == " " else "char", n) if len(n) == 1 else b.handle(n)
    return last


class Views(unittest.TestCase):
    def test_ctrl_s_cycles_all_four_views_and_wraps(self):
        b = board("status")
        seen = []
        for _ in range(5):
            b.handle("ctrl+s")
            seen.append(b.mode)
        self.assertEqual(seen, ["project", "review", "mine", "status", "project"])

    def test_esc_unwinds_marks_then_search_then_returns_to_the_status_view(self):
        b = board(review=[review(5), review(6)])
        keys(b, "down", " ")
        self.assertTrue(b.marked)
        b.handle("esc")
        self.assertEqual((b.marked, b.mode), (set(), "review"))
        b.query = "x"
        b.handle("esc")
        self.assertEqual((b.query, b.mode), ("", "review"))
        b.handle("esc")
        self.assertEqual(b.mode, "status")


class ReviewQueue(unittest.TestCase):
    def setUp(self):
        self.b = board("review", review=[review(5), review(6), review(7, repo="acme/web-app")])

    def test_groups_by_project_listing_every_project_in_scope_even_when_empty(self):
        titles = [g[0] for g in self.b.pr_groups()]
        self.assertEqual(sorted(titles), ["billing-api", "scum-map", "web-app"])
        self.assertEqual([len(g[2]) for g in self.b.pr_groups()][-1], 0)

    def test_oldest_waiting_comes_first_within_a_project(self):
        billing = next(g for g in self.b.pr_groups() if g[0] == "billing-api")
        self.assertEqual([p.number for p, _ in billing[2]], [6, 5])

    def test_space_marks_and_moves_on_and_ctrl_a_toggles_all(self):
        keys(self.b, "down", " ")
        self.assertEqual(len(self.b.marked), 1)
        self.assertEqual(self.b.pr_sel, self.b.pr_flat()[1].key)
        self.b.handle("ctrl+a")
        self.assertEqual(len(self.b.marked), 3)
        self.b.handle("ctrl+a")
        self.assertEqual(self.b.marked, set())

    def test_r_reviews_all_marked_prs_in_one_effect(self):
        keys(self.b, "down", " ", " ")
        effect = self.b.handle("char", "r")
        self.assertEqual(effect.kind, "review")
        self.assertEqual(len(effect.items), 2)

    def test_r_without_marks_reviews_the_current_pr_and_explains_when_none(self):
        keys(self.b, "down")
        self.assertEqual([p.number for p in self.b.handle("char", "r").items], [6])
        empty = board("review", review=[])
        self.assertIsNone(empty.handle("char", "r"))
        self.assertIn("nothing selected", empty.msg)

    def test_one_prompt_per_project_with_every_url_for_that_project(self):
        marked = [review(5), review(6), review(7, repo="acme/web-app")]
        got = {name: (path, prompt, n) for name, path, prompt, n in ab.review_prompts(self.b, marked)}
        self.assertEqual(set(got), {"billing-api", "web-app"})
        path, prompt, n = got["billing-api"]
        self.assertEqual((path, n), ("/srv/www/billing-api", 2))
        self.assertEqual(prompt, "/code-review:code-review https://example.com/acme/billing-api/pull/5 https://example.com/acme/billing-api/pull/6")

    def test_gitlab_uses_its_own_prompt_and_both_are_configurable(self):
        gl = review(9, repo="scum-map/scum-map", provider="gitlab")
        [(_, _, prompt, _)] = ab.review_prompts(self.b, [gl])
        self.assertTrue(prompt.startswith("Review these GitLab merge requests"))
        self.assertTrue(prompt.endswith(gl.url))
        self.b.meta.settings["review_command"] = "/my-review"
        [(_, _, prompt, _)] = ab.review_prompts(self.b, [review(5)])
        self.assertEqual(prompt, "/my-review https://example.com/acme/billing-api/pull/5")  # no {urls}: urls are appended

    def test_a_review_session_gets_the_code_review_and_pr_tags_from_its_own_prompt(self):
        [(_, _, prompt, _)] = ab.review_prompts(self.b, [review(5)])
        self.assertEqual([t.label for t in ab.derive_tags(prompt)], ["Code Review", "PR #5"])


class Opening(unittest.TestCase):
    def test_enter_jumps_to_a_session_already_on_that_pr_preferring_a_working_one(self):
        b = board("review", review=[review(5)])
        keys(b, "down")
        self.assertEqual(b.handle("enter"), ab.Effect("attach", session_id="s1"))

    def test_enter_without_a_linked_session_opens_the_browser_and_o_always_does(self):
        b = board("review", review=[review(6)])
        keys(b, "down")
        self.assertEqual(b.handle("enter"), ab.Effect("browse", url="https://example.com/acme/billing-api/pull/6"))
        b2 = board("review", review=[review(5)])
        keys(b2, "down")
        self.assertEqual(b2.handle("char", "o").kind, "browse")

    def test_the_link_marker_is_shown_on_the_row(self):
        b = board("review", review=[review(5)])
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 110, 24)[0])
        row = next(l for l in text.splitlines() if "Review me 5" in l)
        self.assertIn("⟳2", row)

    def test_ctrl_l_asks_for_a_refresh_in_every_view(self):
        for mode in ("status", "review", "mine"):
            self.assertEqual(board(mode).handle("ctrl+l"), ab.Effect("refresh_prs"))


class MyPullRequests(unittest.TestCase):
    def setUp(self):
        self.b = board("mine", mine=[
            mine(1, merge="conflict"), mine(2, decision="approved"), mine(3, decision="required", requested=("tom",)),
            mine(4, parent=3, depth=1, draft=True), mine(5, decision="changes", checks="fail"),
        ])

    def test_groups_in_order_attention_ready_waiting(self):
        self.assertEqual([(g[0], len(g[2])) for g in self.b.pr_groups()], [("Needs attention", 2), ("Ready to merge", 1), ("Waiting", 2)])

    def test_stacked_prs_follow_their_parent_with_a_depth(self):
        waiting = self.b.pr_groups()[2][2]
        self.assertEqual([(p.number, d) for p, d in waiting], [(3, 0), (4, 1)])

    def test_stacked_child_whose_parent_sits_elsewhere_is_not_indented(self):
        items = ab.order_stack([mine(10, merge="conflict"), mine(11, parent=10, depth=1, merge="clean")][1:])
        self.assertEqual([(p.number, d) for p, d in items], [(11, 0)])

    def test_strip_counts_what_needs_you(self):
        text = ab.plain(ab.Line(ab.strip_segs(self.b, 120)))
        self.assertIn("⇣ 0 to review", text)
        self.assertIn("2 of yours need you", text)
        self.assertIn("1 conflict", text)
        self.assertIn("1 changes", text)
        self.assertIn("1 CI failing", text)
        self.assertIn("✓ 1 ready to merge", text)
        self.assertIn("4m ago", text)

    def test_strip_before_any_data_and_when_a_source_failed(self):
        b = board("status")
        b.prs = None
        self.assertIn("not loaded", ab.plain(ab.Line(ab.strip_segs(b, 80))))
        b.pr_busy = True
        self.assertIn("loading", ab.plain(ab.Line(ab.strip_segs(b, 80))))
        b2 = board("mine", mine=[mine(1)], errors=["GitHub (review queue): gh exploded"])
        self.assertIn("⚠ 1", ab.plain(ab.Line(ab.strip_segs(b2, 120))))
        self.assertIn("gh exploded", "\n".join(ab.plain(l) for l in ab.compose(b2, 110, 24)[0]))

    def test_strip_turns_yellow_when_the_data_is_older_than_two_intervals(self):
        b = board("mine", mine=[mine(1)])
        b.prs.fetched_at = NOW - 3600
        style = [s for t, s in ab.strip_segs(b, 120) if "ago" in t][0]
        self.assertEqual(style, "wait")

    def test_rows_show_reasons_project_and_age(self):
        text = "\n".join(ab.plain(l) for l in ab.compose(self.b, 120, 28)[0])
        for needle in ("Needs attention", "conflicts", "changes requested", "checks failing", "draft", "billing-api", "1d"):
            self.assertIn(needle, text)

    def test_a_waiting_pr_nobody_was_asked_to_review_says_so(self):
        b = board("mine", mine=[mine(7, decision="required")])
        self.assertIn("nobody asked", "\n".join(ab.plain(l) for l in ab.compose(b, 110, 20)[0]))

    def test_filter_applies_to_pull_requests(self):
        self.b.query = "Mine 2"
        self.assertEqual([p.number for p in self.b.pr_flat()], [2])

    def test_click_selects_a_pr_row_and_opens_it_like_enter(self):
        ab.compose(self.b, 110, 24)
        y = next(y for y, k in self.b.hit.items() if k.endswith("#2"))
        self.b.click(y)
        self.assertTrue(self.b.pr_sel.endswith("#2"))
        self.assertEqual(self.b.queued_effect, ab.Effect("browse", url="https://example.com/acme/billing-api/pull/2"))

    def test_clicking_a_pr_a_session_is_already_on_jumps_to_that_session(self):
        b = board("review", review=[review(5)])
        ab.compose(b, 110, 24)
        b.click(next(iter(b.hit)))
        self.assertEqual(b.queued_effect, ab.Effect("attach", session_id="s1"))


class Settings(unittest.TestCase):
    def setUp(self):
        self.b = board("review", review=[review(5)])
        self.b.handle("ctrl+o")

    def test_refresh_interval_cycles_with_arrows_and_zero_means_manual(self):
        self.assertEqual(self.b.pr_interval(), 15 * 60)
        self.b.handle("right")
        self.assertEqual(self.b.pr_interval(), 30 * 60)
        for _ in range(2):  # 30 -> 60 -> 0
            self.b.handle("right")
        self.assertEqual(self.b.meta.settings["pr_refresh_minutes"], 0)
        self.assertEqual(self.b.pr_interval(), 0)
        self.b.handle("left")
        self.assertEqual(self.b.pr_interval(), 60 * 60)

    def test_default_interval_is_gentler_than_five_minutes(self):
        self.assertGreater(self.b.pr_interval(), 5 * 60)

    def test_draft_toggle_persists_and_requests_a_refresh(self):
        self.b.handle("down")
        self.b.refresh_requested.clear()
        self.b.handle("space")
        self.assertTrue(self.b.meta.settings["include_drafts"])
        self.assertTrue(self.b.refresh_requested.is_set())
        self.b.handle("space")
        self.assertNotIn("include_drafts", self.b.meta.settings)

    def test_review_command_can_be_edited_and_reset(self):
        for _ in range(2):
            self.b.handle("down")
        self.b.handle("enter")
        self.b.handle("ctrl+u")
        for c in "/my-review {urls}":
            self.b.handle("space" if c == " " else "char", c)
        self.b.handle("enter")
        self.assertEqual(self.b.meta.settings["review_command"], "/my-review {urls}")
        self.b.handle("enter")
        self.b.handle("ctrl+u")
        self.b.handle("enter")
        self.assertNotIn("review_command", self.b.meta.settings)

    def test_team_fields_appear_for_each_github_org_in_scope_and_parse_include_and_exclude(self):
        names = [n for n, _ in self.b.settings_fields()]
        self.assertEqual([n for n in names if n.startswith("teams:")], ["teams:acme"])
        self.b.settingsui["idx"] = names.index("teams:acme")
        self.b.refresh_requested.clear()
        self.b.handle("enter")
        for c in "core, backend | infra":
            self.b.handle("space" if c == " " else "char", c)
        self.b.handle("enter")
        self.assertEqual(self.b.meta.settings["review_teams"], {"acme": {"include": ["core", "backend"], "exclude": ["infra"]}})
        self.assertTrue(self.b.refresh_requested.is_set())
        self.assertEqual(self.b.setting_text("teams:acme"), "core, backend | infra")

    def test_clearing_the_teams_removes_them(self):
        self.b.meta.settings["review_teams"] = {"acme": {"include": ["core"], "exclude": []}}
        self.b._commit_setting("teams:acme", "")
        self.assertNotIn("review_teams", self.b.meta.settings)

    def test_settings_persist_across_restarts_and_do_not_disturb_old_files(self):
        self.b.handle("right")
        again = ab.Meta(self.b.meta.path)
        self.assertEqual(again.settings["pr_refresh_minutes"], 30)

    def test_popup_fits_every_width_and_esc_closes_it(self):
        for width in (60, 90, 130):
            for line in ab.compose(self.b, width, 24)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))
        text = "\n".join(ab.plain(l) for l in ab.compose(self.b, 110, 24)[0])
        self.assertIn("PR refresh", text)
        self.assertIn("every 15 min", text)
        self.b.handle("esc")
        self.assertIsNone(self.b.settingsui)


class Loader(unittest.TestCase):
    def test_loader_scopes_to_the_projects_of_sessions_and_reads_their_remotes_once(self):
        from types import SimpleNamespace

        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "git":
                url = {"/srv/www/billing-api": "git@github.com:acme/billing-api.git", "/srv/www/scum-map": "git@gitlab.com:scum-map/scum-map.git"}.get(cmd[2], "")
                return SimpleNamespace(returncode=0 if url else 128, stdout=url + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="[]" if cmd[0] == "glab" else "", stderr="")

        b = board("review")
        b.set_sessions([session(1), session(2), session(3, "scum-map"), session(4, "no-remote")])
        loader = ab.PrLoader(b, run=run)
        self.assertEqual(sorted(r.repo for r in loader.projects().values()), ["acme/billing-api", "scum-map/scum-map"])
        loader.projects()
        self.assertEqual(sum(1 for c in calls if c[0] == "git"), 3)  # three distinct project folders, each asked once

    def test_a_crashing_fetch_is_shown_and_never_kills_the_loader(self):
        b = board("review")

        def boom(*a, **k):
            raise RuntimeError("kaput")

        loader = ab.PrLoader(b, run=boom)
        loader.refresh()
        self.assertIn("kaput", b.prs.errors[0])
        self.assertFalse(b.pr_busy)


class RefreshMessage(unittest.TestCase):
    """ctrl+l says "refreshing…"; that note must give way to the outcome instead of staying on screen."""

    def loader(self, run):
        b = board("status")
        b.set_sessions([session(1)])
        return b, ab.PrLoader(b, run=run)

    @staticmethod
    def quiet_world(cmd, **kw):
        from types import SimpleNamespace
        if cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="git@github.com:acme/billing-api.git\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_ctrl_l_posts_the_refreshing_note(self):
        b = board("status")
        self.assertEqual(b.handle("ctrl+l"), ab.Effect("refresh_prs"))

    def test_the_note_becomes_a_success_message_with_the_counts_when_the_refresh_ends(self):
        b, loader = self.loader(self.quiet_world)
        b.msg = ab.REFRESHING
        loader.refresh()
        self.assertTrue(b.msg.startswith("✓ pull requests refreshed"), b.msg)
        self.assertIn("0 to review", b.msg)
        self.assertNotIn("refreshing", b.msg)
        self.assertFalse(b.pr_busy)

    def test_problems_are_counted_in_the_message(self):
        from types import SimpleNamespace

        def failing(cmd, **kw):
            if cmd[0] == "git":
                return SimpleNamespace(returncode=0, stdout="git@github.com:acme/billing-api.git\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        b, loader = self.loader(failing)
        b.msg = ab.REFRESHING
        loader.refresh()
        self.assertIn("had problems", b.msg)

    def test_a_crash_is_reported_as_a_failure(self):
        def boom(*a, **k):
            raise RuntimeError("kaput")

        b, loader = self.loader(boom)
        b.msg = ab.REFRESHING
        loader.refresh()
        self.assertTrue(b.msg.startswith("✗ refresh failed"), b.msg)

    def test_an_unrelated_message_is_left_alone(self):
        b, loader = self.loader(self.quiet_world)
        b.msg = "copied 12 characters"
        loader.refresh()
        self.assertEqual(b.msg, "copied 12 characters")

    def test_a_background_refresh_on_the_timer_stays_silent(self):
        b, loader = self.loader(self.quiet_world)
        b.msg = ""
        loader.refresh()
        self.assertEqual(b.msg, "")

    def test_ctrl_j_is_only_a_new_line_and_never_starts_a_refresh(self):
        b = board("status")
        self.assertIsNone(b.handle("ctrl+j"))
        self.assertNotEqual(b.msg, ab.REFRESHING)
        self.assertEqual(b.input, "\n")


class ContextualHelp(unittest.TestCase):
    """The ? overlay lists only the shortcuts that work in the view on screen."""

    def keys(self, mode):
        return [k for k, _ in ab.help_rows(mode)]

    def text(self, mode):
        return " ".join(f"{k} {v}" for k, v in ab.help_rows(mode))

    def test_session_views_list_session_and_prompt_shortcuts_only(self):
        text = self.text("status")
        for needle in ("rename the session", "peek at", "pin the session", "start a new background session", "choose the model"):
            self.assertIn(needle, text)
        for foreign in ("mark / unmark", "review the marked", "pull request in the browser"):
            self.assertNotIn(foreign, text)

    def test_the_review_queue_lists_marking_and_reviewing_not_session_actions(self):
        text = self.text("review")
        for needle in ("mark / unmark", "review the marked", "ctrl+a", "open the pull request in the browser"):
            self.assertIn(needle, text)
        for foreign in ("rename the session", "peek at", "pin the session", "choose the model", "new background session", "clipboard"):
            self.assertNotIn(foreign, text)

    def test_my_pull_requests_has_no_marking_reviewing_or_session_actions(self):
        text = self.text("mine")
        self.assertIn("open the pull request in the browser", text)
        for foreign in ("mark", "review the marked", "rename", "peek", "pin", "choose the model", "clipboard"):
            self.assertNotIn(foreign, text)

    def test_status_and_project_views_share_the_session_list(self):
        self.assertEqual(ab.help_rows("status"), ab.help_rows("project"))

    def test_every_view_can_switch_views_refresh_and_open_settings_and_leave_with_esc(self):
        for mode in ("status", "review", "mine"):
            keys = self.keys(mode)
            for needed in ("ctrl+s", "ctrl+l", "ctrl+o", "esc"):
                self.assertIn(needed, keys, f"{mode} lacks {needed}")

    def test_no_key_is_listed_twice_in_one_view(self):
        for mode in ("status", "review", "mine"):
            keys = self.keys(mode)
            self.assertEqual(len(keys), len(set(keys)), mode)

    def test_the_overlay_names_the_view_and_fits_every_width(self):
        b = board("review", review=[review(5)])
        for mode, label in (("status", "by status"), ("project", "by project"), ("review", "review queue"), ("mine", "my pull requests")):
            b.mode = mode
            b.help = True
            for width in (50, 70, 100, 140):
                lines = ab.compose(b, width, 30)[0]
                for line in lines:
                    self.assertLessEqual(ab.seg_width(line.segs), width, f"{mode}/{width}: {ab.plain(line)}")
            self.assertIn(f"shortcuts · {label}", "\n".join(ab.plain(l) for l in ab.compose(b, 100, 30)[0]))

    def test_what_the_review_list_promises_actually_happens_there(self):
        b = board("review", review=[review(5), review(6)])
        b.handle("down")
        b.handle("space")
        self.assertTrue(b.marked)  # space marks
        b.handle("ctrl+a")
        self.assertTrue(b.marked)  # ctrl+a marks all
        self.assertEqual(b.handle("char", "r").kind, "review")  # r reviews
        self.assertEqual(b.handle("char", "o").kind, "browse")  # o opens the browser

    def test_what_the_my_prs_list_leaves_out_really_does_nothing_there(self):
        b = board("mine", mine=[mine(1)])
        b.handle("down")
        b.handle("space")
        self.assertEqual(b.marked, set())
        self.assertIsNone(b.handle("char", "r"))


class Layout(unittest.TestCase):
    def test_every_view_fits_every_width(self):
        b = board("review", review=[review(5), review(6)], mine=[mine(1, merge="conflict")])
        b.usage = ab.demo_usage()
        b.limits = ab.demo_limits(NOW)
        b.marked = {"github:acme/billing-api#5"}
        for mode in ("status", "project", "review", "mine"):
            b.mode = mode
            for width in (50, 70, 100, 140):
                for line in ab.compose(b, width, 26)[0]:
                    self.assertLessEqual(ab.seg_width(line.segs), width, f"{mode}/{width}: {ab.plain(line)}")

    def test_the_demo_renders_in_every_view(self):
        b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/scum-map", now=lambda: NOW)
        b.set_sessions(ab.demo_sessions(NOW))
        b.prs = ab.demo_prs(NOW)
        b.mode = "mine"
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 120, 30)[0])
        self.assertIn("!341", text)
        self.assertIn("mobile-app", text)


if __name__ == "__main__":
    unittest.main()
