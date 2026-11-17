import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_board as ab

NOW = 1_800_000_000.0
CSI = "\x1b["


class RenderTerminal(unittest.TestCase):
    def test_a_spinner_repainted_in_place_is_one_line(self):
        out = f"✶ Discombobulating…\r✻ Discombobulating…\r✽ Crunched for 47s{CSI}K"
        self.assertEqual(ab.render_terminal(out), ["✽ Crunched for 47s"])

    def test_colours_and_titles_leave_only_the_text(self):
        out = f"{CSI}38;2;215;119;87mClaude{CSI}39m \x1b]0;title\x07Code"
        self.assertEqual(ab.render_terminal(out), ["Claude Code"])

    def test_cursor_moves_place_text_where_the_terminal_would(self):
        out = f"one\r{CSI}2Bthree{CSI}1A\r{CSI}3Ctwo"
        self.assertEqual(ab.render_terminal(out), ["one", "   two", "three"])

    def test_absolute_positions_and_columns(self):
        out = f"{CSI}2;5Hb{CSI}1;1Ha{CSI}10Gc"
        self.assertEqual(ab.render_terminal(out), ["a        c", "    b"])

    def test_a_cleared_screen_drops_what_came_before(self):
        out = f"old frame\rline 2{CSI}2J{CSI}Hnew frame"
        self.assertEqual(ab.render_terminal(out), ["new frame"])

    def test_erase_line_and_erase_below(self):
        self.assertEqual(ab.render_terminal(f"1234567\r{CSI}3C{CSI}K"), ["123"])
        self.assertEqual(ab.render_terminal(f"keep\nold\nolder{CSI}1A\r{CSI}J"), ["keep"])

    def test_saved_cursor_comes_back(self):
        self.assertEqual(ab.render_terminal("ab\x1b7cd\x1b8X"), ["abXd"])

    def test_plain_text_with_line_feeds_reads_as_written(self):
        self.assertEqual(ab.render_terminal("first\nsecond\r\nthird\n\n"), ["first", "second", "third"])

    def test_wide_characters_take_two_columns(self):
        out = f"日本\r{CSI}4Gx"  # column 4 is the right half of 本
        self.assertEqual(ab.render_terminal(out), ["日 x"])


class Peek(unittest.TestCase):
    def test_space_shows_the_last_screen_not_every_repaint(self):
        b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/p", now=lambda: NOW)
        b.set_sessions([ab.Session(id="id1", name="alpha", state="working", project="p", project_path="/srv/www/p",
                                   started_at=NOW - 60, updated_at=NOW - 60)])
        raw = f"{CSI}2J{CSI}H● Done.\r{CSI}2B✶ Discombobulating…\r✻ Crunched for 47s{CSI}K".encode()
        calls = []

        def cli(args, cwd=None, timeout=60, text=True):
            calls.append((args, text))
            return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

        with mock.patch.object(ab, "run_cli", cli):
            ab.perform(None, b, ab.Effect("peek", session_id="id1"), None)
        self.assertEqual(calls, [(["logs", "id1"], False)])  # bytes, so each \r stays a carriage return
        self.assertEqual(b.peek["lines"], ["● Done.", "", "✻ Crunched for 47s"])


if __name__ == "__main__":
    unittest.main()
