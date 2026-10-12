import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import agent_board as ab

NOW = 1_800_000_000.0


def make(i, state="done", name="", project="scum-map", updated=0, **kw):
    path = f"/srv/www/{project}"
    return ab.Session(
        id=f"id{i}", name=name or f"session {i}", state=state, cwd=path, project=project, project_path=path,
        started_at=NOW - 600, updated_at=NOW - updated, **kw,
    )


def board(sessions, tmp=None):
    meta = ab.Meta(Path(tmp or tempfile.mkdtemp()) / "meta.json")
    b = ab.Board(meta, "/srv/www/scum-map", now=lambda: NOW)
    b.set_sessions(sessions)
    return b


class DeriveTags(unittest.TestCase):
    def labels(self, *src):
        return [t.label for t in ab.derive_tags(*src)]

    def test_code_review_ticket_and_pr(self):
        self.assertEqual(
            self.labels("Code review of PR #123 for AM-1234"), ["Code Review", "Ticket: AM-1234", "PR #123"]
        )

    def test_pr_from_urls_and_bang_notation(self):
        self.assertEqual(self.labels("see https://gitlab.com/x/y/-/merge_requests/328"), ["PR #328"])
        self.assertEqual(self.labels("merged !305 and !310"), ["PR #305", "PR #310"])

    def test_ignores_standards_that_look_like_tickets(self):
        self.assertEqual(self.labels("use UTF-8 and SHA-256, see PRD-007 and ADR-2"), [])

    def test_plain_text_has_no_tags(self):
        self.assertEqual(self.labels("Create a mod for dashboards"), [])

    def test_ticket_cap_and_dedupe(self):
        self.assertEqual(self.labels("AM-1 AM-1 AM-2 AM-3"), ["Ticket: AM-1", "Ticket: AM-2"])


class Projects(unittest.TestCase):
    def test_worktree_cwd_collapses_to_repo(self):
        self.assertEqual(
            ab.project_of("", "/srv/www/scum-map/.claude/worktrees/route-finding-plan"),
            ("scum-map", "/srv/www/scum-map"),
        )

    def test_origin_cwd_wins(self):
        self.assertEqual(ab.project_of("/srv/www/scum-map", "/tmp/elsewhere"), ("scum-map", "/srv/www/scum-map"))

    def test_age_format(self):
        self.assertEqual([ab.fmt_age(x) for x in (5, 90, 7200, 200000)], ["5s", "1m", "2h", "2d"])


class Loading(unittest.TestCase):
    def test_merges_cli_rows_with_state_files(self):
        jobs = Path(tempfile.mkdtemp())
        (jobs / "abc12345").mkdir()
        (jobs / "abc12345" / "state.json").write_text(json.dumps({
            "detail": "Reviewing PR #9", "intent": "AM-77 review", "originCwd": "/srv/www/scum-map",
            "worktreeBranch": "worktree-x", "worktreePath": "/srv/www/scum-map/.claude/worktrees/x",
            "updatedAt": "2026-10-09T18:00:00.000Z", "tokens": 12,
        }))
        rows = [{"id": "abc12345", "name": "my job", "state": "working", "cwd": "/srv/www/scum-map/.claude/worktrees/x",
                 "startedAt": 1_790_000_000_000}]
        run = lambda *a, **k: SimpleNamespace(stdout=json.dumps(rows))
        [s] = ab.load_sessions(run=run, jobs_dir=jobs)
        self.assertEqual((s.project, s.branch, s.worktree, s.group), ("scum-map", "worktree-x", "x", "working"))
        self.assertEqual([t.label for t in s.auto_tags], ["Code Review", "Ticket: AM-77", "PR #9"])

    def test_falls_back_to_scanning_jobs_when_cli_fails(self):
        jobs = Path(tempfile.mkdtemp())
        (jobs / "zz").mkdir()
        (jobs / "zz" / "state.json").write_text(json.dumps({"name": "from disk", "state": "done", "cwd": "/srv/www/a"}))
        def run(*a, **k):
            raise OSError("no claude")
        [s] = ab.load_sessions(run=run, jobs_dir=jobs)
        self.assertEqual((s.id, s.name, s.project), ("zz", "from disk", "a"))


class Interaction(unittest.TestCase):
    def setUp(self):
        self.b = board([make(1, "working", "alpha"), make(2, "done", "beta", updated=10), make(3, "done", "gamma", updated=20)])

    def test_down_from_input_selects_first_visible_row_and_esc_returns(self):
        self.b.handle("down")
        self.assertEqual(self.b.sel, "id1")
        self.b.handle("esc")
        self.assertIsNone(self.b.sel)

    def test_up_stops_at_the_first_session_as_down_stops_at_the_last(self):
        self.b.handle("down")
        self.b.handle("down")
        for _ in range(5):
            self.b.handle("up")
        self.assertEqual(self.b.sel, self.b.flat()[0].id)  # never wraps around to the bottom of the list
        for _ in range(5):
            self.b.handle("down")
        self.assertEqual(self.b.sel, self.b.flat()[-1].id)

    def test_enter_and_right_attach(self):
        self.b.handle("down")
        self.assertEqual(self.b.handle("enter"), ab.Effect("attach", session_id="id1"))
        self.assertEqual(self.b.handle("right"), ab.Effect("attach", session_id="id1"))

    def test_alt_number_opens_nth_visible(self):
        self.assertEqual(self.b.handle("alt+2"), ab.Effect("attach", session_id="id2"))
        self.assertIsNone(self.b.handle("alt+6"))

    def test_typing_then_enter_dispatches_into_target_project(self):
        for c in "fix it":
            self.b.handle("space" if c == " " else "char", c)
        effect = self.b.handle("enter")
        self.assertEqual((effect.kind, effect.prompt, effect.cwd), ("dispatch", "fix it", "/srv/www/scum-map"))
        self.assertEqual(self.b.input, "")

    def test_ctrl_p_opens_picker_listing_projects_with_session_counts(self):
        self.b.sessions.append(make(9, project="other"))
        self.b.handle("ctrl+p")
        items = {name: (total, working) for name, _, total, working in self.b.picker_items()}
        self.assertEqual(items, {"scum-map": (3, 1), "other": (1, 0)})

    def test_picker_enter_sets_target_used_by_next_dispatch(self):
        self.b.sessions.append(make(9, project="other"))
        self.b.handle("ctrl+p")
        self.b.handle("down")
        self.b.handle("enter")
        self.assertIsNone(self.b.picker)
        for c in "go":
            self.b.handle("char", c)
        effect = self.b.handle("enter")
        self.assertEqual(effect.cwd, "/srv/www/other")

    def test_picker_filters_and_esc_keeps_previous_target(self):
        self.b.sessions.append(make(9, project="other"))
        self.b.handle("ctrl+p")
        for c in "oth":
            self.b.handle("char", c)
        self.assertEqual([i[0] for i in self.b.picker_items()], ["other"])
        self.b.handle("esc")
        self.assertIsNone(self.b.picker)
        self.assertEqual(self.b.target()[1], "/srv/www/scum-map")

    def test_picker_accepts_any_existing_directory(self):
        tmp = tempfile.mkdtemp()
        self.b.handle("ctrl+p")
        for c in tmp:
            self.b.handle("char", c)
        self.assertEqual(self.b.picker_items()[-1][1], tmp)
        self.b.handle("enter")
        self.assertEqual(self.b.target()[1], tmp)

    def test_picker_swallows_typing_so_input_stays_empty(self):
        self.b.handle("ctrl+p")
        self.b.handle("char", "z")
        self.assertEqual(self.b.input, "")

    def test_ctrl_x_without_selection_explains_instead_of_doing_nothing(self):
        self.b.handle("ctrl+x")
        self.assertIn("select a session", self.b.msg)
        self.assertIsNone(self.b.confirm)

    def test_rename_persists_and_empty_resets(self):
        self.b.handle("down")
        self.b.handle("ctrl+r")
        for _ in "alpha":
            self.b.handle("backspace")
        for c in "Shiny":
            self.b.handle("char", c)
        self.b.handle("enter")
        self.assertEqual(self.b.name_of(self.b.selected()), "Shiny")
        self.assertEqual(ab.Meta(self.b.meta.path).get("id1")["name"], "Shiny")
        self.b.handle("ctrl+r")
        self.b.handle("ctrl+u")
        self.b.handle("enter")
        self.assertEqual(self.b.name_of(self.b.selected()), "alpha")

    def test_pin_moves_session_to_top_of_its_group(self):
        self.b.handle("alt+3")  # no effect on selection
        self.b.sel = "id3"
        self.b.handle("ctrl+t")
        done = [s.id for s in self.b.groups()[1][2]]
        self.assertEqual(done, ["id3", "id2"])

    def test_stop_working_and_delete_done_need_confirmation(self):
        self.b.sel = "id1"
        self.b.handle("ctrl+x")
        self.assertEqual(self.b.handle("char", "x"), ab.Effect("stop", session_id="id1"))
        self.b.sel = "id2"
        self.b.handle("ctrl+x")
        self.assertEqual(self.b.handle("char", "x"), ab.Effect("remove", session_id="id2"))
        self.b.handle("ctrl+x")
        self.assertIsNone(self.b.handle("char", "n"))

    def test_find_filters_by_tag_and_project(self):
        self.b.sessions[0].auto_tags = ab.derive_tags("review of AM-5")
        self.b.handle("ctrl+f")
        for c in "am-5":
            self.b.handle("char", c)
        self.assertEqual([s.id for s in self.b.flat()], ["id1"])
        self.b.handle("esc")
        self.assertEqual(len(self.b.flat()), 3)

    def test_esc_unwinds_before_quitting(self):
        self.b.handle("down")
        self.assertIsNone(self.b.handle("esc"))
        self.assertIsNone(self.b.sel)
        self.assertEqual(self.b.handle("esc"), ab.Effect("quit"))

    def test_ctrl_s_switches_to_project_view_with_one_group_per_project(self):
        self.b.sessions.append(make(4, project="other"))
        self.b.handle("ctrl+s")
        self.assertEqual(sorted(g[0] for g in self.b.groups()), ["other", "scum-map"])

    def test_question_mark_opens_help_only_with_empty_input(self):
        self.b.handle("char", "?")
        self.assertTrue(self.b.help)
        self.b.handle("esc")
        self.b.handle("char", "a")
        self.b.handle("char", "?")
        self.assertEqual(self.b.input, "a?")

    def test_ctrl_c_never_quits_from_any_state(self):
        self.b.handle("ctrl+r")
        self.b.handle("down")
        self.b.handle("ctrl+x")
        self.assertIsNone(self.b.handle("ctrl+c"))
        self.assertIsNone(self.b.handle("ctrl+c"))

    def test_selection_clears_when_session_disappears(self):
        self.b.sel = "id2"
        self.b.set_sessions([make(1, "working")])
        self.assertIsNone(self.b.sel)


class RenameInTheList(unittest.TestCase):
    """Renaming happens in the session's own row, so your eyes stay where the session is."""

    def setUp(self):
        self.b = board([make(1, "working", "alpha", auto_tags=ab.derive_tags("review AM-5")), make(2, "done", "beta")])
        self.b.handle("down")
        self.b.handle("down")  # the second row
        self.b.handle("ctrl+r")

    def screen(self, width=110, height=24):
        lines, caret = ab.compose(self.b, width, height)
        return lines, caret

    def row_of(self, lines, needle):
        return next(i for i, l in enumerate(lines) if needle in ab.plain(l))

    def test_the_row_turns_into_the_edit_field_prefilled_with_the_current_name(self):
        lines, _ = self.screen()
        row = lines[self.row_of(lines, "beta")]
        self.assertIn("edit", {style for _, style in row.segs})
        self.assertIn("beta", ab.plain(row))

    def test_the_bottom_prompt_is_untouched_and_no_rename_prompt_appears_there(self):
        lines, _ = self.screen()
        text = "\n".join(ab.plain(l) for l in lines)
        self.assertNotIn("✎ rename", text)
        prompt = next(ab.plain(l) for l in lines if "❯" in ab.plain(l))
        self.assertIn("describe a task", prompt)
        self.assertIn("enter save · esc cancel", text)

    def test_the_cursor_sits_at_the_end_of_the_name_on_that_very_row(self):
        lines, caret = self.screen()
        y = self.row_of(lines, "beta")
        self.assertEqual(caret[0], y)
        row = ab.plain(lines[y])
        self.assertEqual(caret[1], row.index("beta") + len("beta"))

    def keys(self, *keys):
        for k in keys:
            self.b.handle("char", k[5:]) if k.startswith("char:") else self.b.handle(k)

    def test_the_caret_moves_and_typing_goes_in_where_it_is(self):
        self.keys("left", "left", "char:X")  # be|ta
        self.assertEqual(self.b.edit["text"], "beXta")
        self.keys("home", "char:[", "end", "char:]")
        self.assertEqual(self.b.edit["text"], "[beXta]")
        self.keys("left", "backspace", "home", "delete")  # removes the a before ] and then the [
        self.assertEqual(self.b.edit["text"], "beXt]")
        self.keys("right", "right", "right", "right", "right", "right")  # stops at the end
        self.assertEqual(self.b.edit["cursor"], len("beXt]"))

    def test_words_are_skipped_and_deleted_with_ctrl_and_alt(self):
        self.keys("ctrl+u", *[f"char:{c}" for c in "fix the login bug"])
        self.keys("ctrl+left", "ctrl+left")
        self.assertEqual(self.b.edit["cursor"], len("fix the "))
        self.keys("alt+right")
        self.assertEqual(self.b.edit["cursor"], len("fix the login"))
        self.keys("ctrl+w")
        self.assertEqual(self.b.edit["text"], "fix the  bug")

    def test_a_paste_goes_in_at_the_caret(self):
        self.keys("home", "right", "right")
        self.b._handle_paste("XY")
        self.assertEqual(self.b.edit["text"], "beXYta")
        self.assertEqual(self.b.edit["cursor"], 4)

    def test_the_screen_caret_follows_the_edit_caret(self):
        self.keys("left", "left")
        lines, caret = self.screen()
        y = self.row_of(lines, "beta")
        self.assertEqual(caret, (y, ab.plain(lines[y]).index("beta") + 2))

    def test_the_find_bar_moves_its_caret_too(self):
        self.keys("esc")
        self.b.sel = None
        self.b.handle("ctrl+f")
        self.keys("char:b", "char:t", "left", "char:e")
        self.assertEqual(self.b.edit["text"], "bet")
        self.assertEqual(self.b.query, "bet")

    def test_typing_shows_up_in_the_row_as_you_type_and_the_cursor_follows(self):
        for _ in "beta":
            self.b.handle("backspace")
        for c in "Gamma":
            self.b.handle("char", c)
        lines, caret = self.screen()
        y = self.row_of(lines, "Gamma")
        self.assertEqual(caret, (y, ab.plain(lines[y]).index("Gamma") + 5))

    def test_enter_saves_and_the_row_returns_to_normal_with_its_tags(self):
        self.b.handle("ctrl+u")
        for c in "Renamed":
            self.b.handle("char", c)
        self.b.handle("enter")
        lines, _ = self.screen()
        row = lines[self.row_of(lines, "Renamed")]
        self.assertNotIn("edit", {style for _, style in row.segs})
        self.assertEqual(self.b.name_of(self.b.selected()), "Renamed")

    def test_escape_cancels_and_restores_the_original_name(self):
        for c in "zz":
            self.b.handle("char", c)
        self.b.handle("esc")
        lines, caret = self.screen()
        self.assertTrue(any("beta" in ab.plain(l) for l in lines))
        self.assertFalse(any("betazz" in ab.plain(l) for l in lines))
        self.assertIsNone(self.b.edit)

    def test_tags_make_room_while_editing_a_session_that_has_them(self):
        b = board([make(1, "working", "alpha", auto_tags=ab.derive_tags("review AM-5"))])
        b.handle("down")
        b.handle("ctrl+r")
        lines, _ = ab.compose(b, 80, 20)
        row = next(ab.plain(l) for l in lines if "alpha" in ab.plain(l))
        self.assertNotIn("Ticket", row)  # the chips return when you finish
        b.handle("esc")
        row = next(ab.plain(l) for l in ab.compose(b, 80, 20)[0] if "alpha" in ab.plain(l))
        self.assertIn("Ticket: AM-5", row)

    def test_only_the_edited_row_changes_and_find_still_edits_at_the_bottom(self):
        lines, _ = self.screen()
        others = [l for l in lines if "alpha" in ab.plain(l)]
        self.assertTrue(others)
        self.assertNotIn("edit", {style for l in others for _, style in l.segs})
        self.b.handle("esc")
        self.b.handle("ctrl+f")
        self.assertTrue(any("⌕ find" in ab.plain(l) for l in self.screen()[0]))

    def test_it_works_in_the_by_project_view_too(self):
        self.b.handle("esc")
        self.b.handle("ctrl+s")
        self.b.handle("ctrl+r")
        lines, caret = self.screen()
        self.assertEqual(caret[0], self.row_of(lines, "beta"))

    def test_a_long_name_keeps_its_end_visible_and_every_width_fits(self):
        for _ in "beta":
            self.b.handle("backspace")
        for c in "a very long new name " * 6:
            self.b.handle("space" if c == " " else "char", c)
        for width in (50, 70, 100, 140):
            lines, caret = self.screen(width)
            for line in lines:
                self.assertLessEqual(ab.seg_width(line.segs), width)
            self.assertIsNotNone(caret)
            self.assertLess(caret[1], width)
        self.assertIn("new name", ab.plain(lines[self.row_of(lines, "new name")]))

    def test_the_edited_row_is_scrolled_into_view_in_a_long_list_with_the_cursor_on_screen(self):
        b = board([make(i, "done", f"old {i}", updated=i) for i in range(40)])
        b.sel = "id39"
        b.handle("ctrl+r")
        lines, caret = ab.compose(b, 100, 16)
        self.assertLess(caret[0], 16)
        self.assertIn("old 39", ab.plain(lines[caret[0]]))


class NewSessionSelected(unittest.TestCase):
    """A session started from the prompt is selected when it appears, so one → opens it."""

    def setUp(self):
        self.b = board([make(1, "working", "alpha")])
        self.dispatch = ab.Effect("dispatch", prompt="do it", cwd="/srv/www/scum-map", tags=["Bug"])

    def appears(self, sid="deadbeef", **kw):
        s = make(9, "working", "new one", **kw)
        s.id = sid
        return s

    def test_the_id_in_claude_s_output_is_registered_and_the_tags_saved(self):
        self.assertTrue(ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef (idle)", True))
        self.assertEqual(self.b.select_when_seen[0], "deadbeef")
        self.assertEqual(self.b.meta.get("deadbeef")["tags"], ["Bug"])

    def test_a_failed_start_or_output_without_an_id_selects_nothing(self):
        self.assertFalse(ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef", False))
        self.assertFalse(ab.register_new_session(self.b, self.dispatch, "something went sideways", True))
        self.assertIsNone(self.b.select_when_seen)
        self.assertEqual(len(self.b.pending), 2)  # one per attempt: the tags still find the session later by its prompt

    def test_it_is_selected_the_moment_it_shows_up(self):
        ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef", True)
        self.b.set_sessions([make(1, "working", "alpha"), self.appears()])
        self.assertEqual(self.b.sel, "deadbeef")
        self.assertIsNone(self.b.select_when_seen)

    def test_one_right_arrow_or_enter_then_opens_it(self):
        ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef", True)
        self.b.set_sessions([make(1, "working", "alpha"), self.appears()])
        self.assertEqual(self.b.handle("right"), ab.Effect("attach", session_id="deadbeef"))
        self.assertEqual(self.b.handle("enter"), ab.Effect("attach", session_id="deadbeef"))

    def test_a_session_that_is_slow_to_appear_is_still_selected_when_it_does(self):
        ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef", True)
        self.b.set_sessions([make(1, "working", "alpha")])
        self.assertIsNone(self.b.sel)
        self.assertIsNotNone(self.b.select_when_seen)  # still waiting
        self.b.set_sessions([make(1, "working", "alpha"), self.appears()])
        self.assertEqual(self.b.sel, "deadbeef")

    def test_it_never_steals_the_selection_from_a_session_you_already_moved_to(self):
        ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef", True)
        self.b.sel = "id1"
        self.b.set_sessions([make(1, "working", "alpha"), self.appears()])
        self.assertEqual(self.b.sel, "id1")
        self.assertIsNone(self.b.select_when_seen)

    def test_a_search_that_hides_it_means_it_is_not_selected(self):
        ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef", True)
        self.b.query = "alpha"
        self.b.set_sessions([make(1, "working", "alpha"), self.appears()])
        self.assertIsNone(self.b.sel)

    def test_it_gives_up_after_a_minute(self):
        clock = [NOW]
        self.b.now = lambda: clock[0]
        ab.register_new_session(self.b, self.dispatch, "backgrounded · deadbeef", True)
        clock[0] += 61
        self.b.set_sessions([make(1, "working", "alpha")])
        self.assertIsNone(self.b.select_when_seen)
        self.b.set_sessions([make(1, "working", "alpha"), self.appears()])
        self.assertIsNone(self.b.sel)

    def test_without_an_id_the_prompt_match_selects_it_too(self):
        ab.register_new_session(self.b, ab.Effect("dispatch", prompt="fix the crash", cwd="/srv/www/scum-map", tags=["Bug"]), "ok", True)
        fresh = self.appears(intent="fix the crash please")
        fresh.started_at = NOW + 1
        self.b.set_sessions([make(1, "working", "alpha"), fresh])
        self.assertEqual(self.b.sel, "deadbeef")

    def test_it_is_scrolled_into_view_in_a_long_list(self):
        b = board([make(i, "done", f"old {i}", updated=100 + i) for i in range(40)])
        ab.register_new_session(b, self.dispatch, "backgrounded · deadbeef", True)
        new = self.appears()
        b.set_sessions(b.sessions + [new])
        lines = [ab.plain(l) for l in ab.compose(b, 100, 20)[0]]
        self.assertTrue(any("new one" in l for l in lines))

    def test_the_prompt_is_cleared_when_the_session_is_started(self):
        for c in "do it":
            self.b.handle("space" if c == " " else "char", c)
        self.b.handle("enter")
        self.assertEqual(self.b.input, "")


class ConfirmWithX(unittest.TestCase):
    """ctrl+x asks, x confirms; anything else (y included) cancels."""

    def setUp(self):
        self.b = board([make(1, "working", "alpha"), make(2, "done", "beta")])

    def ask(self, sid):
        self.b.sel = sid
        self.b.handle("ctrl+x")

    def test_x_confirms_stopping_a_working_session(self):
        self.ask("id1")
        self.assertEqual(self.b.handle("char", "x"), ab.Effect("stop", session_id="id1"))
        self.assertIsNone(self.b.confirm)

    def test_x_confirms_deleting_a_finished_session(self):
        self.ask("id2")
        self.assertEqual(self.b.handle("char", "x"), ab.Effect("remove", session_id="id2"))

    def test_capital_x_works_too(self):
        self.ask("id1")
        self.assertEqual(self.b.handle("char", "X").kind, "stop")

    def test_y_no_longer_confirms_it_cancels(self):
        self.ask("id2")
        self.assertIsNone(self.b.handle("char", "y"))
        self.assertIsNone(self.b.confirm)
        self.assertEqual(self.b.msg, "cancelled")

    def test_any_other_key_cancels_and_nothing_is_run(self):
        for key, ch in (("char", "n"), ("enter", ""), ("esc", ""), ("space", " ")):
            self.ask("id2")
            self.assertIsNone(self.b.handle(key, ch), key)
            self.assertIsNone(self.b.confirm, key)

    def test_pressing_ctrl_x_again_confirms_too(self):
        self.ask("id2")
        self.assertEqual(self.b.handle("ctrl+x"), ab.Effect("remove", session_id="id2"))
        self.assertIsNone(self.b.confirm)
        self.ask("id1")
        self.assertEqual(self.b.handle("ctrl+x"), ab.Effect("stop", session_id="id1"))

    def test_three_presses_do_not_run_it_twice(self):
        self.ask("id2")
        self.assertEqual(self.b.handle("ctrl+x").kind, "remove")
        self.assertIsNone(self.b.handle("ctrl+x"))  # the confirmation is used up; this just asks again
        self.assertIsNotNone(self.b.confirm)

    def test_the_footer_names_the_key(self):
        self.ask("id2")
        hint = ab.plain(ab.Line(ab._hint_segs(self.b)))
        self.assertIn("x or ctrl+x to confirm", hint)
        self.assertNotIn("y to confirm", hint)

    def test_deleting_a_tag_uses_the_same_key_and_the_same_wording(self):
        self.b.meta.library = ["Bug"]
        self.b.sel = "id1"
        self.b.handle("ctrl+g")
        self.b.handle("ctrl+x")
        self.assertIn("x or ctrl+x to confirm", ab.plain(ab.Line(ab._hint_segs(self.b))))
        self.b.handle("char", "y")
        self.assertIn("Bug", self.b.meta.library)  # y cancelled it
        self.b.handle("ctrl+x")
        self.b.handle("char", "x")
        self.assertNotIn("Bug", self.b.meta.library)
        self.b.meta.library = ["Bug"]
        self.b.handle("ctrl+x")
        self.b.handle("ctrl+x")  # ctrl+x again confirms here too
        self.assertNotIn("Bug", self.b.meta.library)

    def test_the_shared_constant_is_what_both_confirmations_use(self):
        self.assertEqual(ab.CONFIRM_KEY, "x")


class IdleSessions(unittest.TestCase):
    """A session whose process is alive but has finished its turn is Idle, whatever its last written state says."""

    def build(self, row, state):
        base_state = {"state": "working", "detail": "I'll then adjust the mod to match.", "tempo": "idle", "inFlight": {"tasks": 0, "queued": 0, "drainableMonitors": 0}}
        base_row = {"id": "5e8bdad2", "name": "clean view mod", "state": "working", "status": "idle", "cwd": "/srv/www/agent-board", "startedAt": 1_790_000_000_000}
        return ab.build_session({**base_row, **row}, {**base_state, **state})

    def test_working_state_with_an_idle_process_is_idle_not_working(self):
        self.assertEqual(self.build({}, {}).group, "idle")

    def test_a_busy_process_is_still_working(self):
        self.assertEqual(self.build({"status": "busy"}, {"tempo": "active"}).group, "working")

    def test_tempo_alone_is_enough_when_the_row_has_no_status(self):
        row = {"status": None}
        self.assertEqual(self.build(row, {"tempo": "idle"}).group, "idle")
        self.assertEqual(self.build(row, {"tempo": "active"}).group, "working")

    def test_background_work_in_flight_keeps_it_working_even_between_turns(self):
        for key in ("tasks", "queued", "drainableMonitors"):
            s = self.build({}, {"inFlight": {"tasks": 0, "queued": 0, "drainableMonitors": 0, key: 1}})
            self.assertEqual(s.group, "working", key)

    def test_a_turn_that_ended_with_a_background_task_running_is_waiting_on_it(self):
        # what Claude Code writes while a `run_in_background` command is still going after the turn ended
        state = {"tempo": "idle", "inFlight": {"tasks": 1, "queued": 0, "kinds": ["local_bash"], "drainableMonitors": 0},
                 "fan": [{"id": "be8w1ny2d", "kind": "shell", "label": "gh pr checks 28957 --watch", "startedAt": 1_790_000_000_000}]}
        s = self.build({"status": "busy"}, state)
        self.assertEqual(s.group, "working")
        self.assertEqual(s.waiting_on, ["gh pr checks 28957 --watch"])

    def test_a_task_without_a_label_is_named_by_its_kind(self):
        state = {"inFlight": {"tasks": 1, "queued": 0, "kinds": ["local_bash"], "drainableMonitors": 0}}
        self.assertEqual(self.build({"status": "busy"}, state).waiting_on, ["a shell command"])

    def test_a_session_still_in_its_turn_is_not_waiting_on_anything(self):
        state = {"tempo": "active", "inFlight": {"tasks": 1, "queued": 0, "kinds": ["local_bash"], "drainableMonitors": 0},
                 "fan": [{"kind": "shell", "label": "sleep 100"}]}
        self.assertEqual(self.build({"status": "busy"}, state).waiting_on, [])

    def test_the_row_says_what_it_is_waiting_on_instead_of_spinning(self):
        b = board([make(1, "working", "sentry issue investigation", detail="waiting on CI for fc42f86", waiting_on=["gh pr checks 28957 --watch"])])
        first, second = ab.row_lines(b, b.sessions[0], 110, False)
        self.assertIn(ab.WAITING_MARK, ab.plain(first))
        self.assertNotIn(ab.SPINNER[b.tick % len(ab.SPINNER)], ab.plain(first))
        self.assertIn("waiting on gh pr checks 28957 --watch · waiting on CI for fc42f86", ab.plain(second))
        two = board([make(1, "working", "x", waiting_on=["sleep 100", "npm test"])])
        self.assertIn("waiting on 2 background tasks: sleep 100, npm test", ab.plain(ab.row_lines(two, two.sessions[0], 110, False)[1]))

    def test_finished_and_awaiting_sessions_are_unaffected(self):
        self.assertEqual(self.build({"state": "done"}, {"state": "done"}).group, "done")
        self.assertEqual(self.build({"state": "waiting"}, {"state": "waiting"}).group, "awaiting")

    def test_a_missing_inflight_record_does_not_crash_and_counts_as_not_busy(self):
        self.assertEqual(self.build({}, {"inFlight": None}).group, "idle")

    def test_idle_gets_its_own_group_between_working_and_completed(self):
        b = board([make(1, "working", "busy one"), make(2, "working", "quiet one", idle=True), make(3, "done", "old one")])
        self.assertEqual([g[0] for g in b.groups()], ["Working", "Idle", "Completed"])
        self.assertEqual(b.counts(), {"awaiting": 0, "working": 1, "idle": 1, "done": 1})

    def test_the_summary_mentions_idle_only_when_there_is_one(self):
        quiet = board([make(1, "working", "a")])
        self.assertNotIn("idle", ab.plain(ab.compose(quiet, 120, 20)[0][1]))
        b = board([make(1, "working", "a"), make(2, "working", "b", idle=True)])
        self.assertIn("1 idle", ab.plain(ab.compose(b, 120, 20)[0][1]))

    def test_the_idle_row_is_drawn_with_its_own_icon_and_age_since_it_stopped(self):
        s = make(2, "working", "quiet one", idle=True, updated=600)
        b = board([s])
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 110, 16)[0])
        row = next(l for l in text.splitlines() if "quiet one" in l)
        self.assertIn("○", row)
        self.assertIn("10m", row)  # how long ago it stopped, not how long it has been running
        self.assertIn("Idle", text)

    def test_stopping_an_idle_session_offers_stop_not_delete(self):
        b = board([make(1, "working", "quiet one", idle=True)])
        b.handle("down")
        self.assertIn("stop", ab.plain(ab.Line(ab._hint_segs(b))))
        b.handle("ctrl+x")
        self.assertEqual(b.confirm["kind"], "stop")


class TagSystem(unittest.TestCase):
    def setUp(self):
        self.b = board([make(1, "working", "alpha", auto_tags=ab.derive_tags("Code review AM-5")), make(2, "done", "beta")])
        self.b.meta.library = ["Bug", "Feature"]
        self.b.handle("down")  # select id1
        self.b.handle("ctrl+g")

    def type(self, text):
        for c in text:
            self.b.handle("space" if c == " " else "char", c)

    def applied(self, sid="id1"):
        return self.b.meta.get(sid).get("tags", [])

    def test_enter_toggles_a_library_tag_on_and_off(self):
        self.b.handle("enter")
        self.assertEqual(self.applied(), ["Bug"])
        self.b.handle("enter")
        self.assertEqual(self.applied(), [])

    def test_arrow_keys_pick_which_tag(self):
        self.b.handle("down")
        self.b.handle("enter")
        self.assertEqual(self.applied(), ["Feature"])

    def test_typing_filters_and_an_unknown_name_offers_create(self):
        self.type("fea")
        self.assertEqual(self.b.tagpicker_items(), [("tag", "Feature"), ("create", "fea")])  # the match comes first
        self.b.handle("enter")
        self.assertEqual(self.applied(), ["Feature"])
        self.b.handle("ctrl+u")
        self.type("Customer Facing")
        self.assertEqual(self.b.tagpicker_items(), [("create", "Customer Facing")])
        self.b.handle("enter")
        self.assertIn("Customer Facing", self.b.meta.library)
        self.assertEqual(self.applied(), ["Feature", "Customer Facing"])
        self.assertEqual(self.b.tagpicker["text"], "")

    def test_create_is_not_offered_for_existing_or_derived_tags(self):
        self.type("bug")
        self.assertEqual(self.b.tagpicker_items(), [("tag", "Bug")])
        self.b.handle("ctrl+u")
        self.type("code review")
        self.assertEqual(self.b.tagpicker_items(), [])

    def test_rename_updates_library_and_every_session(self):
        self.b.handle("enter")
        self.b.meta.update("id2", tags=["Bug"])
        self.b.handle("ctrl+r")
        self.b.handle("ctrl+u")
        self.type("Defect")
        self.b.handle("enter")
        self.assertEqual(self.b.meta.library, ["Defect", "Feature"])
        self.assertEqual((self.applied("id1"), self.applied("id2")), (["Defect"], ["Defect"]))

    def test_delete_needs_confirmation_and_strips_the_tag_everywhere(self):
        self.b.handle("enter")
        self.b.handle("ctrl+x")
        self.b.handle("char", "n")
        self.assertIn("Bug", self.b.meta.library)
        self.b.handle("ctrl+x")
        self.b.handle("char", "x")
        self.assertNotIn("Bug", self.b.meta.library)
        self.assertEqual(self.applied(), [])

    def test_esc_and_ctrl_g_close_the_picker_and_changes_persist(self):
        self.b.handle("enter")
        self.b.handle("esc")
        self.assertIsNone(self.b.tagpicker)
        self.assertEqual(ab.Meta(self.b.meta.path).get("id1")["tags"], ["Bug"])
        self.b.handle("ctrl+g")
        self.b.handle("ctrl+g")
        self.assertIsNone(self.b.tagpicker)

    def test_typing_in_the_picker_never_reaches_the_new_session_input(self):
        self.type("xyz")
        self.assertEqual(self.b.input, "")

    def test_library_tag_matching_a_derived_tag_by_name_shows_as_checked(self):
        self.b.meta.library = ["Bug", "code review", "Feature"]  # different case on purpose
        text = "\n".join(ab.plain(l) for l in ab.compose(self.b, 110, 24)[0])
        row = next(l for l in text.splitlines() if "code review" in l and "[" in l)
        self.assertIn("[x]", row)
        self.assertIn("derived", row)

    def test_derived_tag_cannot_be_toggled_and_explains_why(self):
        self.b.meta.library = ["Code Review", "Bug"]
        self.b.handle("enter")  # the cursor is on "Code Review", derived for this session
        self.assertEqual(self.applied(), [])
        self.assertIn("can't be toggled", self.b.msg)

    def test_manual_tag_is_checked_without_the_derived_marker(self):
        self.b.handle("down")
        self.b.handle("enter")  # toggles Feature
        self.b.meta.library = ["Bug", "Feature"]
        text = "\n".join(ab.plain(l) for l in ab.compose(self.b, 110, 24)[0])
        feature = next(l for l in text.splitlines() if "Feature" in l and "[" in l)
        self.assertIn("[x]", feature)
        self.assertNotIn("derived", feature)

    def test_popup_uses_the_same_colour_as_the_row_for_a_derived_tag(self):
        self.b.meta.library = ["Code Review"]
        lines, _ = ab.compose(self.b, 110, 24)
        styles = {style for line in lines for text, style in line.segs if text.strip() == "Code Review"}
        self.assertEqual(styles, {"tag_review"})

    def test_custom_tag_colour_is_stable_per_label(self):
        a, b = ab.Tag("Urgent", "custom"), ab.Tag("urgent", "custom")
        self.assertEqual(ab.tag_style(a), ab.tag_style(b))
        self.assertTrue(ab.tag_style(a).startswith("tag_c"))

    def test_picker_overlay_fits_every_width_and_shows_checkboxes(self):
        self.b.handle("enter")
        for width in (50, 80, 120):
            lines, _ = ab.compose(self.b, width, 24)
            for line in lines:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))
        text = "\n".join(ab.plain(l) for l in ab.compose(self.b, 100, 24)[0])
        self.assertIn("[x]", text)
        self.assertIn("[ ]", text)
        self.assertIn("derived: Code Review, Ticket: AM-5", text)


class AutoTagging(unittest.TestCase):
    def meta(self):
        return ab.Meta(Path(tempfile.mkdtemp()) / "m.json")

    def test_rules_pick_library_tags_in_order_of_appearance_capped_at_three(self):
        tags = ab.suggest_tags("Fix the crash, add a test, refactor it and document in the README", self.meta())
        self.assertEqual(tags, ["Bug", "Feature", "Tests"])

    def test_default_library_has_review_tags_and_not_the_removed_ones(self):
        self.assertIn("Code Review", ab.DEFAULT_LIBRARY)
        self.assertIn("Pull Request", ab.DEFAULT_LIBRARY)
        for removed in ("Perf", "Research", "Refactor"):
            self.assertNotIn(removed, ab.DEFAULT_LIBRARY)
            self.assertNotIn(removed, [r["tag"] for r in ab.DEFAULT_RULES])
        self.assertEqual(ab.suggest_tags("please open a pull request for it", self.meta()), ["Pull Request"])

    def test_no_match_no_tags_and_bad_regex_is_ignored(self):
        m = self.meta()
        m.rules = [{"tag": "X", "pattern": "([unclosed"}, {"tag": "Y", "pattern": "zzz"}]
        self.assertEqual(ab.suggest_tags("hello", m), [])

    def test_dispatch_effect_carries_suggested_tags_and_adds_them_to_the_library(self):
        b = board([make(1)])
        b.meta.library = []
        for c in "fix the crash":
            b.handle("space" if c == " " else "char", c)
        effect = b.handle("enter")
        self.assertEqual(effect.tags, ["Bug"])
        self.assertIn("Bug", b.meta.library)

    def test_hint_previews_derived_and_suggested_tags_while_typing(self):
        b = board([make(1)])
        for c in "fix AM-12 bug":
            b.handle("space" if c == " " else "char", c)
        hint = ab.plain(ab.Line(ab._hint_segs(b)))
        self.assertIn("will tag", hint)
        self.assertIn("Ticket: AM-12", hint)
        self.assertIn("Bug", hint)

    def test_pending_tags_attach_when_the_new_session_appears(self):
        b = board([make(1)])
        b.expect_session("fix the crash in checkout", "/srv/www/scum-map", ["Bug"])
        fresh = make(7, "working", "new one", intent="fix the crash in checkout please")
        fresh.started_at = NOW + 1
        b.set_sessions([make(1), fresh])
        self.assertEqual(b.meta.get("id7")["tags"], ["Bug"])
        self.assertEqual(b.pending, [])

    def test_old_flat_meta_file_still_loads_and_library_persists(self):
        path = Path(tempfile.mkdtemp()) / "m.json"
        path.write_text(json.dumps({"abc": {"name": "kept", "pinned": True}}))
        m = ab.Meta(path)
        self.assertEqual(m.get("abc")["name"], "kept")
        self.assertEqual(m.library, ab.DEFAULT_LIBRARY)
        m.add_tag("Customer")
        again = ab.Meta(path)
        self.assertIn("Customer", again.library)
        self.assertEqual(again.get("abc")["pinned"], True)


class AccountLimits(unittest.TestCase):
    def write(self, payload):
        path = Path(tempfile.mkdtemp()) / "limits.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_reads_what_the_mod_writes(self):
        path = self.write({"updatedAt": 1_790_000_000_000, "windows": [
            {"kind": "five_hour", "percentUsed": 23.5, "resetsAt": "2026-10-09T22:00:00Z"},
            {"kind": "seven_day", "percentUsed": 7}]})
        limits = ab.read_limits(path)
        self.assertEqual(limits.updated_at, 1_790_000_000)
        self.assertEqual([(w.kind, w.percent) for w in limits.windows], [("five_hour", 23.5), ("seven_day", 7.0)])
        self.assertGreater(limits.windows[0].resets_at, 0)
        self.assertEqual(limits.windows[1].resets_at, 0)

    def test_missing_empty_or_garbled_file_means_no_reading(self):
        self.assertIsNone(ab.read_limits(Path(tempfile.mkdtemp()) / "nope.json"))
        self.assertIsNone(ab.read_limits(self.write("not json")))
        self.assertIsNone(ab.read_limits(self.write({"updatedAt": 1, "windows": []})))
        self.assertIsNone(ab.read_limits(self.write({"windows": [{"kind": "x"}]})))

    def test_countdown_and_colour_thresholds(self):
        self.assertEqual([ab.fmt_until(s) for s in (30, 600, 8040, 90000)], ["1m", "10m", "2h 14m", "1d 1h"])
        self.assertEqual([ab.limit_style(p) for p in (0, 59.9, 60, 84.9, 85, 100)], ["done", "done", "wait", "wait", "fail", "fail"])

    def test_panel_shows_bars_percentages_and_reset_countdowns(self):
        lim = ab.Limits([ab.LimitWindow("five_hour", 38.5, NOW + 8040), ab.LimitWindow("seven_day", 90, NOW + 90000)], NOW - 180)
        lines = ab.limits_panel(lim, 110, NOW)
        text = "\n".join(ab.plain(l) for l in lines)
        for needle in ("LIMITS", "5-hour", "Weekly", "38%", "90%", "resets in 2h 14m", "resets in 1d 1h", "as of 3m ago"):
            self.assertIn(needle, text)
        bar_styles = {ab.plain(ab.Line([seg])): seg[1] for l in lines[1:] for seg in l.segs if "█" in seg[0] or "░" in seg[0]}
        self.assertEqual(sorted(bar_styles.values()), ["done", "fail"])

    def test_expired_window_and_stale_reading_are_called_out(self):
        lim = ab.Limits([ab.LimitWindow("five_hour", 80, NOW - 60)], NOW - 3 * 3600)
        text = "\n".join(ab.plain(l) for l in ab.limits_panel(lim, 110, NOW))
        self.assertIn("window has reset since", text)
        self.assertIn("as of 3h 00m ago", text)
        stale_style = [style for l in ab.limits_panel(lim, 110, NOW)[:1] for t, style in l.segs if "as of" in t]
        self.assertEqual(stale_style, ["wait"])

    def test_no_reading_explains_how_to_get_one(self):
        text = "\n".join(ab.plain(l) for l in ab.limits_panel(None, 120, NOW))
        self.assertIn("no reading yet", text)
        self.assertIn("agent-board-limits", text)

    def test_limits_sit_at_the_bottom_of_the_screen_above_the_input(self):
        b = board([make(1)])
        b.limits = ab.demo_limits(NOW)
        lines = [ab.plain(l) for l in ab.compose(b, 110, 24)[0]]
        i = next(i for i, l in enumerate(lines) if "LIMITS" in l)
        self.assertEqual(i, len(lines) - 6)  # LIMITS + 2 windows, then the rule, input and hint
        self.assertTrue(lines[i + 3].startswith("─"))

    def test_limits_fit_every_width_and_yield_to_a_long_list(self):
        b = board([make(1)])
        b.limits = ab.demo_limits(NOW)
        for width in (50, 80, 120):
            for line in ab.compose(b, width, 24)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))
        crowded = board([make(i, "done", f"s{i}", updated=i) for i in range(30)])
        crowded.limits = ab.demo_limits(NOW)
        self.assertNotIn("LIMITS", "\n".join(ab.plain(l) for l in ab.compose(crowded, 110, 16)[0]))  # too short to keep a usable list

    def test_panels_stay_pinned_under_a_list_that_scrolls(self):
        crowded = board([make(i, "done", f"s{i}", updated=i) for i in range(30)])
        crowded.limits = ab.demo_limits(NOW)
        crowded.usage = ab.demo_usage()
        crowded.sel = crowded.flat()[-1].id
        lines = [ab.plain(l) for l in ab.compose(crowded, 120, 50)[0]]
        limits_at = next(i for i, l in enumerate(lines) if "LIMITS" in l)
        usage_at = next(i for i, l in enumerate(lines) if "USAGE" in l)
        self.assertEqual(limits_at, len(lines) - 6)  # still right above the rule, input and hint
        self.assertLess(usage_at, limits_at)
        last = crowded.flat()[-1].name
        last_at = next(i for i, l in enumerate(lines) if re.search(rf"\b{last}\b", l))
        self.assertLess(last_at, usage_at)  # the selected last session scrolls into view above the panels
        self.assertEqual(lines[usage_at - 1].strip(), "")

    def test_usage_panel_is_dropped_before_the_list_gets_cramped(self):
        crowded = board([make(i, "done", f"s{i}", updated=i) for i in range(30)])
        crowded.limits = ab.demo_limits(NOW)
        crowded.usage = ab.demo_usage()
        text = "\n".join(ab.plain(l) for l in ab.compose(crowded, 120, 26)[0])
        self.assertIn("LIMITS", text)
        self.assertNotIn("USAGE", text)

    def test_usage_panel_gives_way_to_limits_not_the_other_way_round(self):
        b = board([make(1)])
        b.usage = ab.demo_usage()
        b.limits = ab.demo_limits(NOW)
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 120, 20)[0])
        self.assertIn("LIMITS", text)


class MouseAndClipboard(unittest.TestCase):
    def test_selection_text_spans_rows_and_trims_padding(self):
        lines = ["hello world   ", "second line    ", "third"]
        self.assertEqual(ab.selection_text(lines, (0, 6), (1, 5)), "world\nsecond")
        self.assertEqual(ab.selection_text(lines, (1, 5), (0, 6)), "world\nsecond")  # reversed drag

    def test_drag_returns_selected_text_and_click_selects_row(self):
        import curses
        b = board([make(1, "alpha"), make(2, "beta")])
        ab.compose(b, 100, 24)
        rows = {sid: y for y, sid in b.hit.items()}
        sel = ab.Selection()
        text_lines = [ab.plain(l) for l in ab.compose(b, 100, 24)[0]]
        y = rows["id2"]
        self.assertIsNone(ab.mouse_event(sel, b, y, 5, curses.BUTTON1_PRESSED, text_lines))
        self.assertIsNone(ab.mouse_event(sel, b, y, 9, curses.REPORT_MOUSE_POSITION, text_lines))
        copied = ab.mouse_event(sel, b, y, 12, curses.BUTTON1_RELEASED, text_lines)
        self.assertEqual(copied, text_lines[y][5:13].rstrip())
        self.assertIsNone(b.sel)  # a drag must not also select the row
        ab.mouse_event(sel, b, y, 3, curses.BUTTON1_PRESSED, text_lines)
        self.assertIsNone(ab.mouse_event(sel, b, y, 3, curses.BUTTON1_RELEASED, text_lines))
        self.assertEqual(b.sel, "id2")

    def test_mouse_ignored_for_clicks_while_picker_is_open(self):
        import curses
        b = board([make(1)])
        ab.compose(b, 100, 24)
        y = next(iter(b.hit))
        b.handle("ctrl+p")
        sel = ab.Selection()
        ab.mouse_event(sel, b, y, 3, curses.BUTTON1_PRESSED, [])
        ab.mouse_event(sel, b, y, 3, curses.BUTTON1_RELEASED, [])
        self.assertIsNone(b.sel)


class ClickToOpen(unittest.TestCase):
    def setUp(self):
        self.b = board([make(1, "working", "alpha"), make(2, "done", "beta")])
        ab.compose(self.b, 100, 24)
        self.rows = {sid: min(y for y, owner in self.b.hit.items() if owner == sid) for sid in ("id1", "id2")}

    def click(self, y, x=5):
        import curses
        sel = ab.Selection()
        lines = [ab.plain(l) for l in ab.compose(self.b, 100, 24)[0]]
        ab.mouse_event(sel, self.b, y, x, curses.BUTTON1_PRESSED, lines)
        return ab.mouse_event(sel, self.b, y, x, curses.BUTTON1_RELEASED, lines)

    def test_clicking_a_session_selects_it_and_queues_opening_it(self):
        self.assertIsNone(self.click(self.rows["id2"]))
        self.assertEqual(self.b.sel, "id2")
        self.assertEqual(self.b.queued_effect, ab.Effect("attach", session_id="id2"))

    def test_the_detail_line_of_a_session_opens_it_too(self):
        self.click(self.rows["id1"] + 1)
        self.assertEqual(self.b.queued_effect, ab.Effect("attach", session_id="id1"))

    def test_clicking_a_group_header_or_empty_space_opens_nothing(self):
        self.click(ab.HEADER_ROWS)  # the first group header
        self.click(22)
        self.assertIsNone(self.b.queued_effect)

    def test_a_drag_selects_text_and_does_not_open_anything(self):
        import curses
        sel = ab.Selection()
        lines = [ab.plain(l) for l in ab.compose(self.b, 100, 24)[0]]
        y = self.rows["id1"]
        ab.mouse_event(sel, self.b, y, 4, curses.BUTTON1_PRESSED, lines)
        ab.mouse_event(sel, self.b, y, 12, curses.REPORT_MOUSE_POSITION, lines)
        copied = ab.mouse_event(sel, self.b, y, 20, curses.BUTTON1_RELEASED, lines)
        self.assertTrue(copied)
        self.assertIsNone(self.b.queued_effect)

    def test_nothing_opens_while_a_popup_is_up(self):
        self.b.handle("ctrl+p")
        self.click(self.rows["id1"])
        self.assertIsNone(self.b.queued_effect)

    def test_a_draft_in_the_prompt_is_kept_when_a_click_opens_a_session(self):
        self.b.handle("paste", "half written")
        self.click(self.rows["id1"])
        self.assertEqual(self.b.input, "half written")
        self.assertEqual(self.b.queued_effect.kind, "attach")


class LiveSelection(unittest.TestCase):
    def test_the_selection_grows_with_the_mouse_before_the_button_is_released(self):
        import curses
        b = board([make(1, "working", "alpha")])
        sel = ab.Selection()
        lines = ["x" * 60] * 10
        self.assertIsNone(ab.mouse_event(sel, b, 6, 4, curses.BUTTON1_PRESSED, lines))
        self.assertIsNone(sel.region())  # nothing is selected until the mouse moves
        self.assertIsNone(ab.mouse_event(sel, b, 6, 12, curses.REPORT_MOUSE_POSITION, lines))
        self.assertEqual(sel.region(), ((6, 4), (6, 12)))
        self.assertIsNone(ab.mouse_event(sel, b, 8, 30, curses.REPORT_MOUSE_POSITION, lines))
        self.assertEqual(sel.region(), ((6, 4), (8, 30)))
        self.assertTrue(sel.active)

    def test_dragging_backwards_still_gives_an_ordered_region(self):
        import curses
        sel = ab.Selection()
        b = board([make(1)])
        ab.mouse_event(sel, b, 9, 40, curses.BUTTON1_PRESSED, [])
        ab.mouse_event(sel, b, 7, 10, curses.REPORT_MOUSE_POSITION, [])
        self.assertEqual(sel.region(), ((7, 10), (9, 40)))

    def test_motion_without_a_pressed_button_is_ignored(self):
        import curses
        sel = ab.Selection()
        ab.mouse_event(sel, board([make(1)]), 5, 5, curses.REPORT_MOUSE_POSITION, [])
        self.assertIsNone(sel.region())

    def test_motion_tracking_is_switched_on_and_off_with_the_right_terminal_codes(self):
        import io
        import sys
        out, sys.stdout = sys.stdout, io.StringIO()
        try:
            ab.set_motion_tracking(True)
            ab.set_motion_tracking(False)
            written = sys.stdout.getvalue()
        finally:
            sys.stdout = out
        self.assertEqual(written, "\x1b[?1002h\x1b[?1002l")


class AttachRelaunch(unittest.TestCase):
    def test_recognises_the_agent_view_the_attach_command_relaunches(self):
        self.assertTrue(ab.is_agent_view_argv(["claude", "agents", "--cwd", "/x"]))
        self.assertTrue(ab.is_agent_view_argv(["node", "cli.js", "agents"]))
        self.assertTrue(ab.is_agent_view_argv(["/usr/bin/python3", "/opt/claude-wrapper", "agents"]))

    def test_does_not_mistake_attach_to_a_session_named_agents(self):
        self.assertFalse(ab.is_agent_view_argv(["claude", "attach", "agents"]))
        self.assertFalse(ab.is_agent_view_argv(["claude"]))
        self.assertFalse(ab.is_agent_view_argv(["/usr/bin/python3", "/opt/claude-wrapper", "attach", "agents"]))


class RelaunchMarker(unittest.TestCase):
    def spawn(self, **env):
        import os
        import subprocess
        import sys
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env={**os.environ, **env})

    def test_detects_child_started_with_the_select_variable(self):
        p = self.spawn(CLAUDE_AGENTS_SELECT="abc")
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        import time
        time.sleep(0.3)
        self.assertTrue(ab.has_relaunch_marker(p.pid))

    def test_ordinary_child_is_not_a_relaunch(self):
        p = self.spawn()
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        import time
        time.sleep(0.3)
        self.assertFalse(ab.has_relaunch_marker(p.pid))


def turn_line(rid, ts, model="claude-opus-5-5", out=10, inp=1, cw=100, cr=1000, cwd="/srv/www/scum-map", sid="s1"):
    return json.dumps({
        "type": "assistant", "requestId": rid, "timestamp": ts, "cwd": cwd, "sessionId": sid,
        "message": {"model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                              "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}},
    }, separators=(",", ":")) + "\n"  # transcripts are written compact; the scanner's pre-filter relies on it


class Usage(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "-srv-www-scum-map").mkdir()
        self.file = self.root / "-srv-www-scum-map" / "a.jsonl"

    def test_streamed_duplicates_count_once_with_the_last_values(self):
        self.file.write_text(turn_line("r1", "2026-10-09T10:00:00Z", out=5) + turn_line("r1", "2026-10-09T10:00:01Z", out=50))
        sc = ab.UsageScanner(self.root)
        sc.scan()
        [t] = sc.turns()
        self.assertEqual((t.out, t.inp, t.cache_read, t.model), (50, 101, 1000, "opus-5-5"))

    def test_incremental_scan_waits_for_unfinished_line_then_reads_it(self):
        full = turn_line("r2", "2026-10-09T11:00:00Z")
        self.file.write_text(turn_line("r1", "2026-10-09T10:00:00Z") + full[:40])
        sc = ab.UsageScanner(self.root)
        sc.scan()
        self.assertEqual(len(sc.turns()), 1)
        with open(self.file, "a") as f:
            f.write(full[40:])
        sc.scan()
        self.assertEqual(len(sc.turns()), 2)

    def test_ignores_synthetic_and_unrelated_records(self):
        self.file.write_text(
            turn_line("r1", "2026-10-09T10:00:00Z", model="<synthetic>")
            + '{"type":"user","message":{"content":"hi"}}\n' + "not json at all\n"
        )
        sc = ab.UsageScanner(self.root)
        sc.scan()
        self.assertEqual(sc.turns(), [])

    def test_summary_periods_models_projects_and_daily_buckets(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 9, 12, 0).timestamp()
        def ts(days_ago):
            return (dt.datetime(2026, 10, 9, 9, 0) - dt.timedelta(days=days_ago)).astimezone(dt.timezone.utc).isoformat()
        self.file.write_text(
            turn_line("a", ts(0), out=10, sid="s1") + turn_line("b", ts(0), out=20, sid="s2", model="claude-sonnet-4-5-20250929")
            + turn_line("c", ts(3), out=30, sid="s1", cwd="/srv/www/other") + turn_line("d", ts(40), out=40, sid="s3")
        )
        sc = ab.UsageScanner(self.root)
        sc.scan()
        u = ab.summarize(sc.turns(), now)
        self.assertEqual((u.periods["today"]["turns"], u.periods["today"]["sessions"]), (2, 2))
        self.assertEqual((u.periods["7d"]["turns"], u.periods["30d"]["turns"], u.periods["all"]["turns"]), (3, 3, 4))
        self.assertEqual(u.periods["30d"]["out"], 60)
        self.assertEqual(u.daily[-1], 2)
        self.assertEqual(u.daily[-4], 1)
        self.assertEqual(len(u.daily), ab.USAGE_DAYS)
        self.assertEqual(u.models[0], ("opus-5-5", 40))
        self.assertIn("sonnet-4-5", dict(u.models))
        self.assertEqual(dict(u.projects), {"scum-map": 2, "other": 1})

    def test_formatting_helpers(self):
        self.assertEqual([ab.fmt_num(n) for n in (7, 1500, 24_000, 3_400_000, 2_100_000_000)], ["7", "1.5k", "24k", "3.4M", "2.1B"])
        self.assertEqual(ab.sparkline([0, 1, 4, 8]), "▁▂▅█")
        self.assertEqual(ab.sparkline([0, 0]), "▁▁")

    def test_panel_fits_every_width_and_shows_the_numbers(self):
        u = ab.demo_usage()
        for width in range(50, 161, 9):
            for line in ab.usage_panel(u, width, 9):
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))
        text = "\n".join(ab.plain(l) for l in ab.usage_panel(u, 120, 9))
        for needle in ("USAGE", "mobile-app", "fable-5", "2.4M", "Projects"):
            self.assertIn(needle, text)

    def test_panel_only_appears_when_the_list_leaves_room(self):
        b = board([make(1)])
        b.usage = ab.demo_usage()
        roomy = "\n".join(ab.plain(l) for l in ab.compose(b, 120, 30)[0])
        self.assertIn("USAGE", roomy)
        b2 = board([make(i, "done", f"s{i}", updated=i) for i in range(30)])
        b2.usage = ab.demo_usage()
        crowded = "\n".join(ab.plain(l) for l in ab.compose(b2, 120, 16)[0])
        self.assertNotIn("USAGE", crowded)
        self.assertLessEqual(len(ab.compose(b, 120, 30)[0]), 30)


class Branding(unittest.TestCase):
    def test_screen_starts_with_a_blank_padding_row_then_the_branded_title(self):
        lines = [ab.plain(l) for l in ab.compose(board([make(1)]), 110, 20)[0]]
        self.assertEqual(lines[0].strip(), "")
        self.assertIn("🦎", lines[1])
        self.assertIn("Jazi's Agent Board", lines[1])

    def test_the_padding_row_shifts_nothing_the_click_map_still_points_at_the_rows(self):
        b = board([make(1, "working", "alpha"), make(2, "done", "beta")])
        lines = [ab.plain(l) for l in ab.compose(b, 110, 24)[0]]
        first_row = {sid: min(y for y, owner in b.hit.items() if owner == sid) for sid in ("id1", "id2")}
        for sid, name in (("id1", "alpha"), ("id2", "beta")):
            self.assertIn(name, lines[first_row[sid]])  # the name row; the detail row below belongs to it too
        self.assertEqual(b.hit[first_row["id1"] + 1], "id1")

    def test_overlays_stay_clear_of_the_header_rows(self):
        b = board([make(1)])
        for opener in ("ctrl+p", "ctrl+o"):
            b.handle(opener)
            lines = [ab.plain(l) for l in ab.compose(b, 110, 24)[0]]
            self.assertIn("Agent Board", lines[1])
            self.assertTrue(all("╭" not in l for l in lines[:3]))
            b.handle("esc")

    def test_a_spacer_row_separates_the_strip_from_the_first_group(self):
        lines = [ab.plain(l) for l in ab.compose(board([make(1, "working")]), 110, 20)[0]]
        self.assertIn("PRs:", lines[2])
        self.assertEqual(lines[3].strip(), "")
        self.assertIn("Working", lines[4])

    def test_strip_text_is_not_drawn_in_the_faintest_grey(self):
        b = board([make(1)])
        b.prs = ab.prs.PrData(fetched_at=NOW - 120)
        styles = {style for text, style in ab.strip_segs(b, 120) if text.strip()}
        self.assertNotIn("faint", styles)
        self.assertTrue(styles & {"strip", "strip_bold", "accent", "wait", "done"})

    def test_total_height_is_unchanged_by_the_extra_row(self):
        for height in (12, 24, 40):
            self.assertLessEqual(len(ab.compose(board([make(1)]), 100, height)[0]), height)


class Rendering(unittest.TestCase):
    def test_every_line_fits_every_width(self):
        sessions = [
            make(1, "awaiting", "a very long session name " * 4, branch="worktree-" + "x" * 40,
                 detail="detail " * 40, auto_tags=ab.derive_tags("Code review PR #1 AM-1 WEB-2")),
            make(2, "working", "short"),
        ]
        b = board(sessions)
        b.sel = "id1"
        for width in range(50, 161, 7):
            lines, _ = ab.compose(b, width, 24)
            for line in lines:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))
            self.assertLessEqual(len(lines), 24)

    def test_right_aligned_content_keeps_a_margin_from_the_terminal_edge(self):
        b = board([make(1, "working", "audit", branch="worktree-x", auto_tags=ab.derive_tags("AM-1 review"))])
        b.usage = ab.demo_usage()
        b.sel = "id1"
        for width in (80, 110, 140):
            for line in ab.compose(b, width, 30)[0]:
                text = ab.plain(line).rstrip()
                if text.startswith("─") or "──────" in text:
                    continue  # the rules (and the strip that runs into one) deliberately span the full width
                self.assertLessEqual(len(text), width - ab.RIGHT_MARGIN, text)

    def test_row_shows_project_tags_and_age(self):
        b = board([make(1, "working", "audit", auto_tags=ab.derive_tags("Code review of PR #7 AM-9"))])
        lines, _ = ab.compose(b, 110, 20)
        text = "\n".join(ab.plain(l) for l in lines)
        for needle in ("audit", "Code Review", "Ticket: AM-9", "PR #7", "scum-map", "10m"):
            self.assertIn(needle, text)

    def test_selected_row_scrolls_into_view(self):
        b = board([make(i, "done", f"s{i}", updated=i) for i in range(30)])
        b.sel = "id29"
        lines, _ = ab.compose(b, 100, 14)
        self.assertIn("s29", "\n".join(ab.plain(l) for l in lines))

    def test_scrolling_back_up_to_a_groups_first_session_shows_its_heading(self):
        b = board([make(i, "waiting", f"w{i}", updated=i) for i in range(3)] + [make(i, "done", f"s{i}", updated=i) for i in range(3, 30)])
        b.sel = "id29"
        ab.compose(b, 100, 14)
        b.sel = "id3"  # the first Completed session
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 100, 14)[0])
        self.assertIn("Completed", text)
        b.sel = "id0"  # the very first session
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 100, 14)[0])
        self.assertIn("Awaiting input", text)
        self.assertEqual(b.scroll, 0)

    def test_returning_to_the_prompt_scrolls_the_list_back_to_the_top(self):
        b = board([make(i, "waiting", f"w{i}", updated=i) for i in range(3)] + [make(i, "done", f"s{i}", updated=i) for i in range(3, 30)])
        b.sel = "id29"
        ab.compose(b, 100, 14)
        b.sel = None
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 100, 14)[0])
        self.assertEqual(b.scroll, 0)
        self.assertIn("Awaiting input", text)

    def test_caret_only_when_input_is_focused(self):
        b = board([make(1)])
        self.assertIsNotNone(ab.compose(b, 100, 20)[1])
        b.sel = "id1"
        self.assertIsNone(ab.compose(b, 100, 20)[1])

    def test_help_overlay_lists_rename_shortcut(self):
        b = board([make(1)])
        b.help = True
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 100, 24)[0])
        self.assertIn("ctrl+r", text)


if __name__ == "__main__":
    unittest.main()
