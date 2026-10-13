import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
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
    def test_ctrl_s_cycles_the_three_tabs_and_wraps(self):
        b = board("status")
        seen = []
        for _ in range(4):
            b.handle("ctrl+s")
            seen.append(b.mode)
        self.assertEqual(seen, ["review", "mine", "status", "review"])


class Tabs(unittest.TestCase):
    """Sessions, Review queue and My PRs are tabs in the title row; status or project is how the Sessions tab groups."""

    def title(self, b, width=140):
        return ab.compose(b, width, 30)[0][1]

    def test_the_title_row_shows_the_tabs_with_the_current_one_marked(self):
        b = board("status")
        row = self.title(b)
        for label in ("Sessions", "Review queue", "My PRs"):
            self.assertIn(label, ab.plain(row))
        self.assertEqual([t for t, style in row.segs if style == "tab_on"], [" Sessions "])
        b.mode = "mine"
        self.assertEqual([t for t, style in self.title(b).segs if style == "tab_on"], [" My PRs "])

    def test_alt_arrows_move_between_tabs_and_stop_at_the_ends(self):
        b = board("status")
        keys(b, "alt+right")
        self.assertEqual(b.mode, "review")
        keys(b, "alt+right", "alt+right")
        self.assertEqual(b.mode, "mine")
        keys(b, "alt+left", "alt+left", "alt+left")
        self.assertEqual(b.mode, "status")

    def test_alt_arrows_switch_tabs_from_the_prompt_too(self):
        b = board("status")
        b.handle("paste", "two words")
        keys(b, "alt+right")
        self.assertEqual(b.mode, "review")
        self.assertEqual(b.input, "two words")

    def test_ctrl_b_switches_the_grouping_and_it_is_remembered(self):
        b = board("status")
        keys(b, "ctrl+b")
        self.assertEqual(b.mode, "project")
        keys(b, "alt+right", "alt+left")
        self.assertEqual(b.mode, "project")  # the Sessions tab comes back grouped as you left it
        again = ab.Board(b.meta, "/srv/www/scum-map", now=lambda: NOW)
        self.assertEqual(again.mode, "project")
        keys(b, "ctrl+b")
        self.assertEqual(b.mode, "status")

    def test_ctrl_b_does_nothing_on_a_pull_request_tab(self):
        b = board("review", review=[review(5)])
        keys(b, "ctrl+b")
        self.assertEqual(b.mode, "review")

    def test_the_grouping_is_named_beside_the_first_group(self):
        b = board("status")
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 120, 30)[0])
        self.assertIn("grouped by status", text)
        self.assertNotIn("by status  ", ab.plain(self.title(b, 120)))
        b.mode = "project"
        self.assertIn("grouped by project", "\n".join(ab.plain(l) for l in ab.compose(b, 120, 30)[0]))

    def test_a_click_on_a_tab_opens_it(self):
        b = board("status")
        row = ab.plain(self.title(b))
        b.click(1, row.index("My PRs") + 1)
        self.assertEqual(b.mode, "mine")
        b.click(1, row.index("Sessions") + 1)
        self.assertEqual(b.mode, "status")

    def test_esc_on_a_pull_request_tab_returns_to_sessions_grouped_as_before(self):
        b = board("status")
        keys(b, "ctrl+b", "alt+right", "esc")
        self.assertEqual(b.mode, "project")

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

    def test_groups_in_order_attention_ready_waiting_draft(self):
        self.assertEqual([(g[0], len(g[2])) for g in self.b.pr_groups()], [("Needs attention", 2), ("Ready to merge", 1), ("Waiting", 1), ("Draft", 1)])

    def test_the_draft_section_is_at_the_very_bottom(self):
        text = [ab.plain(l) for l in ab.compose(self.b, 120, 30)[0]]
        heading = next(i for i, l in enumerate(text) if l.strip().startswith("Draft"))
        self.assertTrue(any("Mine 4" in l for l in text[heading:]))
        self.assertFalse(any(l.strip().startswith(("Needs attention", "Ready to merge", "Waiting")) for l in text[heading:]))

    def test_a_draft_with_conflicts_counts_neither_as_needing_you_nor_ready(self):
        b = board("mine", mine=[mine(8, draft=True, merge="conflict"), mine(9, draft=True, decision="approved")])
        counts = b.pr_counts()
        self.assertEqual((counts["attention"], counts["ready"], counts["reasons"]), (0, 0, {}))
        self.assertIn("conflicts", "\n".join(ab.plain(l) for l in ab.compose(b, 110, 20)[0]))  # still shown on its row

    def test_stacked_prs_follow_their_parent_with_a_depth(self):
        b = board("mine", mine=[mine(3, decision="required", requested=("tom",)), mine(4, parent=3, depth=1, decision="required")])
        self.assertEqual([(p.number, d) for p, d in b.pr_groups()[0][2]], [(3, 0), (4, 1)])

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
        for needle in ("Needs attention", "conflicts", "changes requested", "checks failing", "Draft", "billing-api", "1d"):
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


class FixConflicts(unittest.TestCase):
    """f on one of your PRs with conflicts starts a background session that resolves them."""

    def setUp(self):
        self.b = board("mine", mine=[mine(7, merge="conflict", head="BI-1-split", base="main"), mine(8)])
        self.b.pr_sel = self.b.pr_flat()[0].key

    def start(self, returncode=0, stderr=""):
        calls = []

        def fake_cli(args, cwd=None, timeout=60):
            calls.append((args, cwd))
            return mock.Mock(returncode=returncode, stdout="", stderr=stderr)

        with mock.patch.object(ab, "run_cli", fake_cli), mock.patch.object(ab, "load_sessions", lambda: []):
            ab.start_conflict_session(self.b, self.b.prs.mine[0])
        return calls

    def test_f_on_a_conflicted_pr_asks_for_a_session(self):
        effect = self.b.handle("char", "f")
        self.assertEqual((effect.kind, [p.number for p in effect.items]), ("fix_conflicts", [7]))

    def test_f_on_a_pr_without_conflicts_only_says_so(self):
        self.b.pr_sel = next(p.key for p in self.b.pr_flat() if p.number == 8)
        self.assertIsNone(self.b.handle("char", "f"))
        self.assertEqual(self.b.msg, "#8 has no conflicts to fix")

    def test_f_does_nothing_in_the_review_queue(self):
        b = board("review", review=[review(5)])
        b.pr_sel = b.pr_flat()[0].key
        self.assertIsNone(b.handle("char", "f"))

    def test_the_session_runs_in_the_project_with_the_branches_named(self):
        [(args, cwd)] = self.start()
        self.assertEqual(cwd, "/srv/www/billing-api")
        self.assertEqual(args[0], "--bg")
        prompt = args[-1]
        for part in ("PR #7", "https://example.com/acme/billing-api/pull/7", "`BI-1-split`", "origin/main", "never a force push", "worktree"):
            self.assertIn(part, prompt)
        self.assertIn("fixing the conflicts in #7", self.b.msg)

    def test_the_prompt_tags_the_session_with_the_pr_so_it_shows_the_link(self):
        prompt = ab.conflict_prompt(self.b.prs.mine[0])
        self.assertIn(ab.Tag("PR #7", "pr"), ab.derive_tags(prompt))

    def test_a_merge_request_is_called_one(self):
        mr = mine(3, provider="gitlab", repo="scum-map/scum-map", merge="conflict", head="fix", base="master")
        self.assertIn("MR !3", ab.conflict_prompt(mr))

    def test_a_failure_is_reported(self):
        self.start(returncode=1, stderr="not trusted\n")
        self.assertEqual(self.b.msg, "✗ could not start fixing #7: not trusted")


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
        for _ in range(3):
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

    def test_there_are_no_review_team_fields(self):
        self.assertEqual([n for n, _ in self.b.settings_fields()], ["refresh", "drafts", "age", "review_cmd", "review_cmd_gl"])


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


class ReviewQueueAge(unittest.TestCase):
    """The review queue hides PRs opened longer ago than a setting (ctrl+o), 30 days unless changed."""

    def setUp(self):
        self.b = board("review", review=[review(2), review(45), review(200)])  # opened that many days ago

    def shown(self):
        return [p.number for p in self.b.pr_flat()]

    def test_hides_prs_older_than_30_days_by_default(self):
        self.assertEqual(self.b.review_age_days(), 30)
        self.assertEqual(self.shown(), [2])
        self.assertEqual(self.b.pr_counts()["review"], 1)
        self.assertEqual(self.b.hidden_by_age(), 2)

    def test_the_footer_says_how_many_are_hidden(self):
        self.assertIn("2 older than 30 days hidden (ctrl+o)", "\n".join(ab.plain(l) for l in ab.compose(self.b, 140, 30)[0]))

    def test_the_setting_cycles_and_zero_shows_every_pr(self):
        self.b.handle("ctrl+o")
        for _ in range(2):
            self.b.handle("down")
        self.b.handle("right")
        self.assertEqual((self.b.meta.settings["review_max_age_days"], self.shown()), (60, [45, 2]))
        for _ in range(4):  # 60 -> 90 -> 180 -> 365 -> 0
            self.b.handle("right")
        self.assertEqual((self.b.review_age_days(), self.shown()), (0, [200, 45, 2]))
        self.b.handle("right")  # wraps to 7
        self.b.handle("left")
        self.assertEqual(self.b.review_age_days(), 0)

    def test_the_default_is_not_written_to_the_settings_file(self):
        self.b.meta.settings["review_max_age_days"] = 14
        self.b.handle("ctrl+o")
        for _ in range(2):
            self.b.handle("down")
        self.b.handle("right")
        self.assertNotIn("review_max_age_days", self.b.meta.settings)
        self.assertEqual(self.b.review_age_days(), 30)

    def test_a_pr_with_no_date_is_kept(self):
        self.b.prs.review.append(prs.ReviewPr("github", "acme/billing-api", 4, "No date", "u", "maria"))
        self.assertIn(4, self.shown())

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


class Approve(unittest.TestCase):
    """`a` in the review queue approves the PR on GitHub / GitLab after one confirming key, then it leaves the queue."""

    def setUp(self):
        self.b = board("review", review=[review(5), review(6), review(7)])
        self.first = self.b.pr_flat()[0]
        self.b.pr_sel = self.first.key

    def test_a_asks_first_and_a_again_approves(self):
        self.assertIsNone(self.b.handle("char", "a"))
        self.assertEqual(self.b.confirm["kind"], "approve")
        hint = ab.plain(ab.compose(self.b, 140, 30)[0][-1])
        self.assertIn(f"Approve #{self.first.number}", hint)
        self.assertEqual(self.b.handle("char", "a"), ab.Effect("approve", items=[self.first]))

    def test_enter_confirms_too_and_any_other_key_cancels(self):
        self.b.handle("char", "a")
        self.assertEqual(self.b.handle("enter"), ab.Effect("approve", items=[self.first]))
        self.b.handle("char", "a")
        self.assertIsNone(self.b.handle("esc"))
        self.assertIsNone(self.b.confirm)
        self.assertEqual(self.b.msg, "cancelled")

    def test_my_prs_tab_has_no_approve(self):
        b = board("mine", mine=[mine(1)])
        b.handle("char", "a")
        self.assertIsNone(b.confirm)

    def test_an_approved_pr_leaves_the_queue_and_the_next_one_is_selected(self):
        self.b.marked = {self.first.key}
        ok = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
        ab.run_approval(self.b, self.first, ok)
        self.assertNotIn(self.first.key, [p.key for p in self.b.pr_flat()])
        self.assertNotIn(self.first.key, self.b.marked)
        self.assertEqual(self.b.pr_sel, self.b.pr_flat()[0].key)
        self.assertTrue(self.b.msg.startswith(f"✓ approved #{self.first.number}"))
        self.assertEqual(self.b.pr_counts()["review"], 2)

    def test_a_refresh_that_still_lists_it_does_not_bring_it_back(self):
        ok = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
        ab.run_approval(self.b, self.first, ok)
        stale = prs.PrData(review=[review(5), review(6), review(7)], repos=REPOS, fetched_at=NOW)
        self.assertNotIn(self.first.key, [p.key for p in self.b.without_approved(stale).review])

    def test_a_failed_approval_keeps_the_pr_and_says_why(self):
        no = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="GraphQL: Review cannot be requested from pull request author.")
        ab.run_approval(self.b, self.first, no)
        self.assertIn(self.first.key, [p.key for p in self.b.pr_flat()])
        self.assertEqual(self.b.msg, f"✗ could not approve #{self.first.number}: GraphQL: Review cannot be requested from pull request author.")

    def test_the_row_says_it_is_being_approved(self):
        self.b.pr_busy_keys[self.first.key] = "approving…"
        row = next(l for l in ab.compose(self.b, 140, 30)[0] if f"#{self.first.number}" in ab.plain(l))
        self.assertIn("approving…", ab.plain(row))
        self.b.handle("char", "a")
        self.assertIsNone(self.b.confirm)  # not twice
