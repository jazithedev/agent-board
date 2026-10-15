import curses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import agent_board as ab

NOW = 1_800_000_000.0


class FakeScreen:
    """Feeds read_key a queue of keys; an empty queue behaves like a timeout."""

    def __init__(self, *keys):
        self.keys = list(keys)

    def timeout(self, ms):
        pass

    def get_wch(self):
        if not self.keys:
            raise curses.error("no input")
        return self.keys.pop(0)


def read(*keys):
    return ab.read_key(FakeScreen(*keys))


class ReadKey(unittest.TestCase):
    def test_a_single_character_is_a_key_press_not_a_paste(self):
        self.assertEqual(read("a"), ("char", "a"))
        self.assertEqual(read(" "), ("space", " "))

    def test_enter_alone_is_enter_and_ctrl_j_is_a_new_line(self):
        self.assertEqual(read("\r"), ("enter", ""))
        self.assertEqual(read("\n"), ("ctrl+j", ""))

    def test_tab_is_a_key_so_commands_can_be_completed_with_it(self):
        self.assertEqual(read("\t"), ("tab", ""))

    def test_a_tab_inside_pasted_text_is_still_just_text(self):
        self.assertEqual(read("a", "\t", "b"), ("paste", "a\tb"))

    def test_ctrl_v_is_reported_so_the_clipboard_can_be_read(self):
        self.assertEqual(read("\x16"), ("ctrl+v", ""))

    def test_characters_arriving_together_are_one_paste(self):
        self.assertEqual(read("h", "e", "l", "l", "o", " ", "w"), ("paste", "hello w"))

    def test_a_newline_inside_a_burst_stays_text_and_never_acts_as_enter(self):
        self.assertEqual(read("a", "\r", "b"), ("paste", "a\rb"))
        self.assertEqual(read("\r", "x"), ("paste", "\rx"))

    def test_bracketed_paste_is_unwrapped(self):
        keys = ["\x1b", "[", "2", "0", "0", "~", *"line one\nline two", "\x1b", "[", "2", "0", "1", "~"]
        self.assertEqual(read(*keys), ("paste", "line one\nline two"))

    def test_bracketed_paste_with_a_missing_end_marker_keeps_what_arrived(self):
        self.assertEqual(read("\x1b", "[", "2", "0", "0", "~", *"partial"), ("paste", "partial"))

    def test_bracketed_paste_may_hold_unicode_and_escape_looking_text(self):
        keys = ["\x1b", "[", "2", "0", "0", "~", *"zażółć 🦎 [x]", "\x1b", "[", "2", "0", "1", "~"]
        self.assertEqual(read(*keys), ("paste", "zażółć 🦎 [x]"))

    def test_escape_still_means_escape_and_alt_digits_still_work(self):
        self.assertEqual(read("\x1b"), ("esc", ""))
        self.assertEqual(read("\x1b", "3"), ("alt+3", ""))

    def test_unrelated_escape_sequences_are_ignored_not_typed(self):
        self.assertEqual(read("\x1b", "[", "1", "5", "~"), ("tick", ""))

    def test_special_keys_inside_a_burst_are_not_swallowed(self):
        screen = FakeScreen("a", curses.KEY_UP)
        # the up-arrow is left for the next read instead of being lost in the paste
        pushed = []
        original = curses.unget_wch
        curses.unget_wch = lambda k: pushed.append(k)
        try:
            self.assertEqual(ab.read_key(screen), ("char", "a"))
        finally:
            curses.unget_wch = original
        self.assertEqual(pushed, [curses.KEY_UP])


class Clipboard(unittest.TestCase):
    def test_uses_the_first_clipboard_tool_that_is_installed(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[0])
            return SimpleNamespace(returncode=0, stdout="from " + cmd[0])

        text = ab.read_clipboard(which=lambda name: name == "xclip", run=run)
        self.assertEqual((text, calls), ("from xclip", ["xclip"]))

    def test_none_when_no_tool_or_nothing_on_the_clipboard(self):
        self.assertIsNone(ab.read_clipboard(which=lambda n: None, run=None))
        empty = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="")
        self.assertIsNone(ab.read_clipboard(which=lambda n: n == "wl-paste", run=empty))
        failing = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="x")
        self.assertIsNone(ab.read_clipboard(which=lambda n: True, run=failing))


def board():
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/scum-map", now=lambda: NOW)
    b.set_sessions([ab.Session(id="id1", name="alpha", state="working", project="scum-map", project_path="/srv/www/scum-map")])
    return b


class PasteRouting(unittest.TestCase):
    def test_paste_into_the_prompt_keeps_its_lines_and_enter_then_starts_one_session(self):
        b = board()
        b.handle("paste", "First line\r\nSecond line\n\nThird")
        self.assertEqual(b.input, "First line\nSecond line\n\nThird")
        effect = b.handle("enter")
        self.assertEqual((effect.kind, effect.prompt), ("dispatch", "First line\nSecond line\n\nThird"))

    def test_paste_appends_to_what_is_already_typed_and_drops_the_selection(self):
        b = board()
        b.handle("down")
        for c in "fix: ":
            b.handle("space" if c == " " else "char", c)
        b.handle("paste", "the crash")
        self.assertEqual((b.input, b.sel), ("fix: the crash", None))

    def test_control_characters_are_stripped_and_tabs_become_spaces(self):
        b = board()
        b.handle("paste", "a\x07b\tc\x1bd")
        self.assertEqual(b.input, "ab cd")

    def test_ctrl_j_inserts_a_line_break(self):
        b = board()
        b.handle("char", "a")
        b.handle("ctrl+j")
        b.handle("char", "b")
        self.assertEqual(b.input, "a\nb")

    def test_single_line_fields_flatten_the_paste(self):
        b = board()
        b.handle("ctrl+f")
        b.handle("paste", "two\nwords")
        self.assertEqual((b.edit["text"], b.query), ("two words", "two words"))
        b.handle("esc")
        b.handle("down")
        b.handle("ctrl+r")
        b.handle("ctrl+u")
        b.handle("paste", "new\nname")
        self.assertEqual(b.edit["text"], "new name")

    def test_paste_into_the_project_picker_the_tag_picker_and_settings(self):
        b = board()
        b.handle("ctrl+p")
        b.handle("paste", "/srv\nwww")
        self.assertEqual(b.picker["text"], "/srv www")
        b.handle("esc")
        b.handle("down")
        b.handle("ctrl+g")
        b.handle("paste", "Customer\nFacing")
        self.assertEqual(b.tagpicker["text"], "Customer Facing")
        b.handle("esc")
        b.handle("ctrl+o")
        for _ in range(4):  # PR refresh, drafts, review queue age, auto re-review, then the review command
            b.handle("down")
        b.handle("enter")
        b.handle("ctrl+u")
        b.handle("paste", "/my-review {urls}")
        self.assertEqual(b.settingsui["edit"]["text"], "/my-review {urls}")

    def test_paste_does_nothing_while_a_confirmation_or_help_is_open(self):
        b = board()
        b.handle("down")
        b.handle("ctrl+x")
        b.handle("paste", "x")
        self.assertEqual(b.input, "")
        self.assertIsNotNone(b.confirm)

    def test_pull_request_views_have_no_prompt_so_paste_explains(self):
        b = board()
        b.mode = "review"
        b.handle("paste", "text")
        self.assertEqual(b.input, "")
        self.assertIn("status or project view", b.msg)


class CutAndCopy(unittest.TestCase):
    def typed(self, text):
        b = board()
        b.handle("paste", text)
        return b

    def test_ctrl_x_cuts_the_whole_multi_line_prompt_and_clears_it(self):
        b = self.typed("line one\nline two")
        effect = b.handle("ctrl+x")
        self.assertEqual((effect.kind, effect.text, effect.cut), ("copy", "line one\nline two", True))
        self.assertEqual(b.input, "")

    def test_ctrl_y_copies_without_clearing(self):
        b = self.typed("keep me")
        effect = b.handle("ctrl+y")
        self.assertEqual((effect.text, effect.cut, b.input), ("keep me", False, "keep me"))

    def test_with_a_session_selected_ctrl_x_still_stops_or_deletes_not_cuts(self):
        b = self.typed("draft prompt")
        b.handle("down")
        self.assertIsNone(b.handle("ctrl+x"))
        self.assertIsNotNone(b.confirm)
        self.assertEqual(b.input, "draft prompt")

    def test_nothing_to_cut_when_the_prompt_is_empty(self):
        b = board()
        self.assertIsNone(b.handle("ctrl+x"))
        self.assertIsNone(b.handle("ctrl+y"))

    def test_the_hint_mentions_the_shortcut_while_typing(self):
        b = self.typed("hello")
        self.assertIn("ctrl+x", ab.plain(ab.Line(ab._hint_segs(b))))

    def test_key_codes(self):
        self.assertEqual(read("\x18"), ("ctrl+x", ""))
        self.assertEqual(read("\x19"), ("ctrl+y", ""))


class DraftAndSessions(unittest.TestCase):
    """A draft in the prompt must never get in the way of opening a session you navigated to."""

    def setUp(self):
        self.b = board()
        self.b.handle("paste", "half-written task")
        self.b.handle("down")

    def test_enter_opens_the_selected_session_instead_of_starting_a_new_one(self):
        self.assertEqual(self.b.handle("enter"), ab.Effect("attach", session_id="id1"))
        self.assertEqual(self.b.input, "half-written task")

    def test_right_arrow_and_space_also_act_on_the_selected_session(self):
        self.assertEqual(self.b.handle("right"), ab.Effect("attach", session_id="id1"))
        self.assertEqual(self.b.handle("space"), ab.Effect("peek", session_id="id1"))
        self.assertEqual(self.b.input, "half-written task")

    def test_alt_number_opens_a_session_with_a_draft_too(self):
        self.assertEqual(self.b.handle("alt+1"), ab.Effect("attach", session_id="id1"))

    def test_the_draft_survives_the_round_trip_and_can_still_be_sent(self):
        self.b.handle("enter")  # opens the session; the board comes back later
        self.b.handle("esc")  # focus back on the prompt
        self.assertIsNone(self.b.sel)
        effect = self.b.handle("enter")
        self.assertEqual((effect.kind, effect.prompt), ("dispatch", "half-written task"))

    def test_typing_returns_focus_to_the_prompt_and_continues_the_draft(self):
        self.b.handle("char", "!")
        self.assertEqual((self.b.sel, self.b.input), (None, "half-written task!"))

    def test_backspace_and_ctrl_u_return_to_the_prompt_too(self):
        self.b.handle("backspace")
        self.assertEqual((self.b.sel, self.b.input), (None, "half-written tas"))
        self.b.handle("down")
        self.b.handle("ctrl+u")
        self.assertEqual((self.b.sel, self.b.input), (None, ""))

    def test_esc_first_leaves_the_list_without_destroying_the_draft(self):
        self.b.handle("esc")
        self.assertEqual((self.b.sel, self.b.input), (None, "half-written task"))
        self.b.handle("esc")
        self.assertEqual(self.b.input, "")

    def test_ctrl_x_on_a_selected_session_still_stops_it_and_keeps_the_draft(self):
        self.b.handle("ctrl+x")
        self.assertIsNotNone(self.b.confirm)
        self.assertEqual(self.b.input, "half-written task")

    def test_the_screen_says_the_draft_is_kept_and_dims_it(self):
        lines, _ = ab.compose(self.b, 120, 24)
        text = "\n".join(ab.plain(l) for l in lines)
        self.assertIn("draft kept", text)
        draft = [style for l in lines for t, style in l.segs if "half-written task" in t]
        self.assertEqual(draft, ["dim"])
        self.b.handle("esc")
        lines, _ = ab.compose(self.b, 120, 24)
        self.assertEqual([style for l in lines for t, style in l.segs if "half-written task" in t], ["bold"])


class CtrlC(unittest.TestCase):
    """ctrl+c never closes the board: esc is the way out."""

    def test_with_a_draft_it_clears_the_prompt(self):
        b = board()
        b.handle("paste", "something half written\nover two lines")
        self.assertIsNone(b.handle("ctrl+c"))
        self.assertEqual((b.input, b.msg), ("", "prompt cleared"))

    def test_on_an_empty_prompt_it_does_nothing_but_point_at_esc(self):
        b = board()
        for _ in range(3):
            self.assertIsNone(b.handle("ctrl+c"))
        self.assertIn("esc quits", b.msg)
        self.assertEqual(b.handle("esc"), ab.Effect("quit"))

    def test_a_draft_is_cleared_even_while_a_session_is_selected_and_the_selection_stays(self):
        b = board()
        b.handle("paste", "draft")
        b.handle("down")
        self.assertIsNone(b.handle("ctrl+c"))
        self.assertEqual((b.input, b.sel), ("", "id1"))

    def test_popups_are_closed_like_esc_and_the_hidden_draft_is_left_alone(self):
        for opener, attr in (("ctrl+p", "picker"), ("ctrl+o", "settingsui"), ("ctrl+f", "edit")):
            b = board()
            b.handle("paste", "draft")
            b.handle(opener)
            self.assertIsNotNone(getattr(b, attr))
            self.assertIsNone(b.handle("ctrl+c"))
            self.assertIsNone(getattr(b, attr))
            self.assertEqual(b.input, "draft")

    def test_confirmation_help_and_peek_are_dismissed_not_confirmed(self):
        b = board()
        b.handle("down")
        b.handle("ctrl+x")
        self.assertIsNone(b.handle("ctrl+c"))
        self.assertIsNone(b.confirm)
        b.handle("char", "?")
        self.assertIsNone(b.handle("ctrl+c"))

    def test_pull_request_views_ignore_it_too(self):
        b = board()
        b.mode = "mine"
        self.assertIsNone(b.handle("ctrl+c"))
        self.assertEqual(b.mode, "mine")

    def test_an_outside_interrupt_during_input_is_not_an_esc(self):
        class Interrupting(FakeScreen):
            def get_wch(self):
                raise KeyboardInterrupt

        self.assertEqual(ab.read_key(Interrupting()), ("tick", ""))


class CopyNote(unittest.TestCase):
    def test_terminal_only_route_tells_the_user_how_to_make_it_reliable(self):
        self.assertIn("xclip", ab.copy_note("terminal"))
        self.assertEqual(ab.copy_note("xclip"), "via xclip")


LONG = "\n".join(f"line {i} of a long pasted block" for i in range(98))  # 98 lines, 97 line breaks


class PasteChips(unittest.TestCase):
    """Claude Code collapses a long paste into [Pasted text #1 +97 lines]; the text stays behind it."""

    def test_a_long_paste_becomes_a_chip_with_the_number_of_line_breaks(self):
        b = board()
        b.handle("paste", LONG)
        self.assertEqual(b.input, "[Pasted text #1 +97 lines]")

    def test_a_long_single_line_has_no_line_count(self):
        b = board()
        b.handle("paste", "x" * 900)
        self.assertEqual(b.input, "[Pasted text #1]")

    def test_a_short_paste_stays_inline(self):
        b = board()
        b.handle("paste", "just a few\nlines of text")
        self.assertEqual((b.input, b.pastes), ("just a few\nlines of text", {}))

    def test_the_threshold_is_exactly_800_characters(self):
        b = board()
        b.handle("paste", "x" * 800)
        self.assertEqual(b.input, "x" * 800)
        b2 = board()
        b2.handle("paste", "x" * 801)
        self.assertTrue(b2.input.startswith("[Pasted text"))

    def test_crlf_counts_as_one_line_break(self):
        b = board()
        b.handle("paste", "\r\n".join(["y" * 40] * 30))
        self.assertEqual(b.input, "[Pasted text #1 +29 lines]")

    def test_the_whole_text_is_sent_when_you_submit(self):
        b = board()
        b.handle("paste", LONG)
        for c in " please":
            b.handle("space" if c == " " else "char", c)
        effect = b.handle("enter")
        self.assertEqual(effect.prompt, LONG + " please")
        self.assertEqual((b.input, b.pastes, b.paste_seq), ("", {}, 0))

    def test_text_typed_around_chips_and_several_pastes_are_numbered_in_order(self):
        b = board()
        for c in "see ":
            b.handle("space" if c == " " else "char", c)
        b.handle("paste", LONG)
        b.handle("char", "&")
        b.handle("paste", "z" * 900)
        self.assertEqual(b.input, "see [Pasted text #1 +97 lines]&[Pasted text #2]")
        self.assertEqual(b.prompt_text(), "see " + LONG + "&" + "z" * 900)

    def test_one_backspace_removes_a_whole_chip_and_its_stored_text(self):
        b = board()
        b.handle("char", "a")
        b.handle("paste", LONG)
        b.handle("backspace")
        self.assertEqual((b.input, b.pastes), ("a", {}))
        b.handle("backspace")
        self.assertEqual(b.input, "")

    def test_backspace_over_text_after_a_chip_removes_one_character_only(self):
        b = board()
        b.handle("paste", LONG)
        b.handle("char", "x")
        b.handle("backspace")
        self.assertEqual(b.input, "[Pasted text #1 +97 lines]")

    def test_clearing_forgets_the_pastes_and_numbering_starts_again(self):
        for clear in ("ctrl+u", "ctrl+c", "esc"):
            b = board()
            b.handle("paste", LONG)
            b.handle(clear)
            self.assertEqual((b.input, b.pastes, b.paste_seq), ("", {}, 0), clear)
            b.handle("paste", LONG)
            self.assertEqual(b.input, "[Pasted text #1 +97 lines]")

    def test_cut_and_copy_take_the_real_text_not_the_chip(self):
        b = board()
        b.handle("paste", LONG)
        copied = b.handle("ctrl+y")
        self.assertEqual(copied.text, LONG)
        self.assertEqual(b.input, "[Pasted text #1 +97 lines]")
        cut = b.handle("ctrl+x")
        self.assertEqual(cut.text, LONG)
        self.assertEqual((b.input, b.pastes), ("", {}))

    def test_the_tag_preview_reads_the_pasted_text(self):
        b = board()
        b.handle("paste", "Please look at AM-4711 and fix the crash.\n" + "filler line\n" * 100)
        hint = ab.plain(ab.Line(ab._hint_segs(b)))
        self.assertIn("Ticket: AM-4711", hint)
        self.assertIn("Bug", hint)
        self.assertIn("backspace removes a pasted block", hint)

    def test_single_line_fields_never_make_chips(self):
        b = board()
        b.handle("ctrl+f")
        b.handle("paste", LONG)
        self.assertEqual(b.pastes, {})
        self.assertNotIn("[Pasted", b.edit["text"])

    def test_the_prompt_line_draws_the_chip_as_a_chip_and_stays_inside_the_screen(self):
        b = board()
        b.handle("char", "a")
        b.handle("paste", LONG)
        b.handle("paste", "q" * 1000)
        for width in (50, 80, 120):
            lines, caret = ab.compose(b, width, 20)
            row = next(l for l in lines if "❯" in ab.plain(l))
            self.assertLessEqual(ab.seg_width(row.segs), width)
            self.assertIsNotNone(caret)
        row = next(l for l in ab.compose(b, 120, 20)[0] if "❯" in ab.plain(l))
        chips = [t.strip() for t, style in row.segs if style == "chip_info"]
        self.assertEqual(chips, ["[Pasted text #1 +97 lines]", "[Pasted text #2]"])

    def test_a_chip_is_kept_whole_or_dropped_never_cut_in_half(self):
        segs = ab.prompt_segs("start " + "[Pasted text #1 +97 lines]" + " end", "bold")
        for width in range(5, 50):
            kept = ab.tail_segs(segs, width)
            self.assertLessEqual(ab.seg_width(kept), width)
            for text, style in kept:
                if style == "chip_info":
                    self.assertEqual(text.strip(), "[Pasted text #1 +97 lines]")

    def test_the_caret_follows_the_end_of_the_drawn_prompt(self):
        b = board()
        b.handle("paste", LONG)
        lines, caret = ab.compose(b, 100, 20)
        row_index = next(i for i, l in enumerate(lines) if "❯" in ab.plain(l))
        text = ab.plain(lines[row_index])
        self.assertEqual(caret[0], row_index)
        self.assertEqual(caret[1], text.index("]") + 2)  # just after the chip and its trailing space


class Display(unittest.TestCase):
    def test_a_long_prompt_grows_into_rows_that_show_all_of_it(self):
        b = board()
        b.handle("paste", "start of it\n" + "middle " * 40 + "THE END")
        lines, caret = ab.compose(b, 80, 40)
        first = next(i for i, l in enumerate(lines) if "❯" in ab.plain(l))
        rows = [ab.plain(l) for l in lines[first:-1]]
        self.assertGreater(len(rows), 3)
        self.assertTrue(rows[0].startswith(" ❯ start of it"))
        self.assertEqual(sum(r.count("middle") for r in rows), 40)
        self.assertTrue(" ".join(r.strip() for r in rows).endswith("middle THE END"))
        self.assertTrue(all(ab.seg_width(l.segs) <= 80 for l in lines))
        self.assertEqual(caret[0], len(lines) - 2)  # on the prompt's last row, at the end of the text

    def test_a_line_break_starts_a_new_row(self):
        b = board()
        b.handle("paste", "one\ntwo")
        rows = [ab.plain(l) for l in ab.compose(b, 100, 20)[0]]
        i = next(i for i, r in enumerate(rows) if "❯" in r)
        self.assertTrue(rows[i].startswith(" ❯ one "))
        self.assertEqual(rows[i + 1].rstrip(), "   two")

    def test_fit_tail(self):
        self.assertEqual(ab.fit_tail("abcdef", 4), "…def")
        self.assertEqual(ab.fit_tail("abc", 4), "abc")
        self.assertEqual(ab.fit_tail("abc", 0), "")


if __name__ == "__main__":
    unittest.main()
