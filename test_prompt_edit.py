import tempfile
import unittest
from pathlib import Path

import agent_board as ab

NOW = 1_800_000_000.0


def board():
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/p", now=lambda: NOW)
    b.set_sessions([ab.Session(id="id1", name="alpha", state="done", project="p", project_path="/srv/www/p")])
    return b


def type_(b, text):
    for c in text:
        b.handle("space" if c == " " else "char", c)


class Caret(unittest.TestCase):
    def test_typing_lands_at_the_caret(self):
        b = board()
        type_(b, "hello world")
        for _ in range(5):
            b.handle("left")
        type_(b, "big ")
        self.assertEqual(b.input, "hello big world")
        self.assertEqual(b.cursor, len("hello big "))

    def test_home_end_and_the_edges(self):
        b = board()
        type_(b, "abc")
        b.handle("home")
        b.handle("left")
        self.assertEqual(b.cursor, 0)
        type_(b, "x")
        b.handle("end")
        b.handle("right")
        type_(b, "y")
        self.assertEqual(b.input, "xabcy")

    def test_backspace_and_delete_act_at_the_caret(self):
        b = board()
        type_(b, "abcd")
        b.handle("left")
        b.handle("left")
        b.handle("backspace")
        self.assertEqual(b.input, "acd")
        b.handle("delete")
        self.assertEqual(b.input, "ad")
        self.assertEqual(b.cursor, 1)

    def test_ctrl_arrows_jump_by_word(self):
        b = board()
        type_(b, "one two  three")
        b.handle("ctrl+left")
        self.assertEqual(b.cursor, len("one two  "))
        b.handle("ctrl+left")
        self.assertEqual(b.cursor, len("one "))
        b.handle("ctrl+right")
        self.assertEqual(b.cursor, len("one two"))
        b.handle("ctrl+right")
        b.handle("ctrl+right")
        self.assertEqual(b.cursor, len(b.input))

    def test_ctrl_w_and_alt_backspace_delete_the_word_before_the_caret(self):
        b = board()
        type_(b, "one two three")
        b.handle("ctrl+w")
        self.assertEqual(b.input, "one two ")
        b.handle("alt+backspace")
        self.assertEqual(b.input, "one ")

    def test_ctrl_delete_removes_the_word_after_the_caret(self):
        b = board()
        type_(b, "one two three")
        b.handle("home")
        b.handle("ctrl+delete")
        self.assertEqual(b.input, " two three")

    def test_a_pasted_chip_is_one_unit(self):
        b = board()
        type_(b, "a ")
        b.handle("paste", "x" * 900)
        chip = b.input[2:]
        type_(b, " b")
        b.handle("left")
        b.handle("left")
        b.handle("left")
        self.assertEqual(b.cursor, 2)  # one step over the whole chip
        b.handle("right")
        self.assertEqual(b.cursor, 2 + len(chip))
        b.handle("backspace")
        self.assertEqual(b.input, "a  b")
        self.assertEqual(b.pastes, {})

    def test_a_paste_and_a_new_line_go_in_at_the_caret(self):
        b = board()
        type_(b, "ab")
        b.handle("left")
        b.handle("paste", "XY")
        b.handle("ctrl+j")
        self.assertEqual(b.input, "aXY\nb")
        self.assertEqual(b.prompt_text(), "aXY\nb")

    def test_replacing_the_prompt_puts_the_caret_at_the_end(self):
        b = board()
        type_(b, "fix /hel")
        b.handle("tab")
        self.assertEqual(b.cursor, len(b.input))
        b.handle("ctrl+u")
        self.assertEqual((b.input, b.cursor), ("", 0))

    def test_arrows_still_open_a_selected_session(self):
        b = board()
        type_(b, "draft")
        b.handle("down")
        effect = b.handle("right")
        self.assertEqual(effect.kind, "attach")
        self.assertEqual(b.input, "draft")

    def test_the_slash_list_waits_for_the_caret_to_be_at_the_end(self):
        b = board()
        type_(b, "fix /he")
        b.handle("left")
        self.assertIsNone(b.slash_query())


class Window(unittest.TestCase):
    def test_everything_fits(self):
        segs, col, bounds = ab.prompt_window("hello", 2, 20, "bold")
        self.assertEqual(segs, [("hello", "bold")])
        self.assertEqual(col, 2)
        self.assertEqual(bounds[-1], (5, 5))

    def test_the_caret_stays_visible_in_long_text_at_either_end(self):
        text = "abcdefghij" * 5
        for cur in (0, 17, 50):
            segs, col, _ = ab.prompt_window(text, cur, 20, "bold")
            self.assertLessEqual(ab.seg_width(segs), 20)
            self.assertLessEqual(col, 20)
            shown = "".join(t for t, _ in segs).replace("…", "")
            self.assertIn(text[max(0, cur - 1) : cur + 1] or text[-1], shown + text[cur : cur + 1])

    def test_a_click_maps_back_to_a_text_index(self):
        b = board()
        type_(b, "hello world")
        b.prompt_hit = (30, 3, ab.prompt_window(b.input, b.cursor, 40, "bold")[2])
        b.click(30, 3 + 5)
        self.assertEqual(b.cursor, 5)
        b.click(30, 3 + 99)
        self.assertEqual(b.cursor, len(b.input))

    def test_compose_places_the_terminal_caret_at_the_text_caret(self):
        b = board()
        type_(b, "hello world")
        b.handle("home")
        _, caret = ab.compose(b, 100, 30)
        self.assertIsNotNone(caret)
        b.handle("end")
        _, end = ab.compose(b, 100, 30)
        self.assertEqual(end[1] - caret[1], len("hello world"))


if __name__ == "__main__":
    unittest.main()
