import fcntl
import os
import pty
import select
import stat
import struct
import tempfile
import termios
import textwrap
import time
import tty
import unittest
from pathlib import Path
from unittest import mock

import agent_board as ab

NOW = 1_800_000_000.0


def board(model=None):
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/scum-map", now=lambda: NOW)
    b.set_sessions([ab.Session(id="id1", name="alpha", state="working", project="scum-map", project_path="/srv/www/scum-map")])
    if model:
        b.meta.settings["model"] = model
    return b


def typed(b, text):
    for c in text:
        b.handle("space" if c == " " else "char", c)


class ModelPicker(unittest.TestCase):
    def test_the_list_starts_with_default_then_aliases_and_full_ids(self):
        b = board()
        b.handle("ctrl+e")
        values = [v for _, v, _ in b.model_items()]
        self.assertEqual(values[:5], ["", "fable", "opus", "sonnet", "haiku"])
        self.assertIn("claude-opus-5-5", values)

    def test_enter_saves_the_choice_and_it_survives_a_restart(self):
        b = board()
        b.handle("ctrl+e")
        b.handle("down")
        b.handle("down")
        b.handle("enter")
        self.assertEqual(b.model(), "opus")
        self.assertIsNone(b.modelpicker)
        self.assertIn("opus", b.msg)
        self.assertEqual(ab.Meta(b.meta.path).settings["model"], "opus")

    def test_default_removes_the_setting(self):
        b = board("opus")
        b.handle("ctrl+e")
        self.assertEqual(b.modelpicker["idx"], 2)  # the cursor starts on the current model
        b.handle("up")
        b.handle("up")
        b.handle("enter")
        self.assertEqual(b.model(), "")
        self.assertNotIn("model", b.meta.settings)

    def test_filtering_and_an_unlisted_id_typed_as_is(self):
        b = board()
        b.handle("ctrl+e")
        typed(b, "son")
        self.assertEqual([v for _, v, _ in b.model_items()][:2], ["sonnet", "claude-sonnet-5-5"])
        b.handle("ctrl+u")
        typed(b, "claude-opus-4-1")
        self.assertEqual(b.model_items()[-1], ("claude-opus-4-1", "claude-opus-4-1", "use this id as typed"))
        b.handle("enter")
        self.assertEqual(b.model(), "claude-opus-4-1")

    def test_models_seen_in_recent_usage_are_offered_without_duplicates(self):
        b = board()
        b.usage = ab.demo_usage()  # fable-5, opus-4-6, sonnet-4-5
        b.handle("ctrl+e")
        recent = {v: n for _, v, n in b.model_items() if "recently" in n}
        self.assertEqual(set(recent), {"claude-fable-5", "claude-opus-4-6", "claude-sonnet-4-5"})
        values = [v for _, v, _ in b.model_items()]
        self.assertEqual(len(values), len(set(values)))

    def test_esc_ctrl_e_and_ctrl_c_close_it_without_changing_anything(self):
        for key in ("esc", "ctrl+e", "ctrl+c"):
            b = board("haiku")
            b.handle("ctrl+e")
            b.handle("down")
            self.assertIsNone(b.handle(key))
            self.assertIsNone(b.modelpicker)
            self.assertEqual(b.model(), "haiku")

    def test_paste_goes_into_the_filter_and_typing_never_reaches_the_prompt(self):
        b = board()
        b.handle("ctrl+e")
        b.handle("paste", "claude-\nopus")
        self.assertEqual(b.modelpicker["text"], "claude- opus")
        self.assertEqual(b.input, "")


class UsingTheModel(unittest.TestCase):
    def test_a_new_session_carries_the_chosen_model(self):
        b = board("sonnet")
        typed(b, "fix it")
        effect = b.handle("enter")
        self.assertEqual((effect.kind, effect.model), ("dispatch", "sonnet"))

    def test_command_line_has_the_model_flag_only_when_one_is_chosen(self):
        self.assertEqual(ab.bg_command("do it", "opus"), ["--bg", "--model", "opus", "do it"])
        self.assertEqual(ab.bg_command("do it", ""), ["--bg", "do it"])

    def test_review_sessions_use_the_model_too(self):
        b = board("opus")
        b.prs = ab.prs.PrData(repos={"github:o/r": "/srv/www/scum-map"})
        pr = ab.prs.ReviewPr("github", "o/r", 5, "t", "https://github.com/o/r/pull/5")
        calls = []

        def fake_cli(args, cwd=None, timeout=60):
            calls.append(args)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(ab, "run_cli", fake_cli), mock.patch.object(ab, "load_sessions", lambda: []):
            ab.start_review_sessions(b, [pr])
        self.assertEqual(calls[0][:3], ["--bg", "--model", "opus"])

    def test_labels(self):
        self.assertEqual([ab.model_label(v) for v in ("", "opus", "claude-opus-5-5")], ["default model", "opus", "opus-5-5"])

    def test_prompt_line_and_hint_show_the_model_and_fit_every_width(self):
        b = board("opus")
        for width in (50, 80, 120):
            for line in ab.compose(b, width, 20)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))
        prompt = next(ab.plain(l) for l in ab.compose(b, 120, 20)[0] if "❯" in ab.plain(l))
        self.assertIn("opus", prompt)
        typed(b, "hello")
        self.assertIn("on opus", ab.plain(ab.Line(ab._hint_segs(b))))

    def test_the_popup_shows_the_current_model_marked_and_fits(self):
        b = board("opus")
        b.handle("ctrl+e")
        for width in (60, 100, 140):
            for line in ab.compose(b, width, 24)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))
        text = "\n".join(ab.plain(l) for l in ab.compose(b, 110, 24)[0])
        row = next(l for l in text.splitlines() if "latest Opus" in l)
        self.assertIn("✓", row)
        self.assertIn("model for new sessions", text)


class ScreenFilterTests(unittest.TestCase):
    def test_entering_the_alternate_screen_becomes_a_clear(self):
        f = ab.ScreenFilter()
        self.assertEqual(f.feed(b"a\x1b[?1049hb"), b"a\x1b[2J\x1b[Hb")
        self.assertTrue(f.entered)

    def test_leaving_ends_the_relay_and_drops_everything_after(self):
        f = ab.ScreenFilter()
        f.feed(b"\x1b[?1049h")
        self.assertEqual(f.feed(b"last frame\x1b[?1049lSession exited\r\n"), b"last frame")
        self.assertTrue(f.left)

    def test_a_leave_before_any_enter_is_dropped_but_does_not_end_the_relay(self):
        f = ab.ScreenFilter()
        self.assertEqual(f.feed(b"x\x1b[?1049ly"), b"xy")
        self.assertFalse(f.left)

    def test_escape_codes_split_across_reads_are_still_recognised(self):
        f = ab.ScreenFilter()
        first = f.feed(b"hi\x1b[?10")
        second = f.feed(b"49h!")
        self.assertEqual(first + second, b"hi\x1b[2J\x1b[H!")

    def test_other_escape_codes_and_other_private_modes_pass_untouched(self):
        f = ab.ScreenFilter()
        data = b"\x1b[31mred\x1b[0m \x1b[?25l \x1b[?2004h \x1b[?1000h \x1b]0;title\x07"
        self.assertEqual(f.feed(data), data)

    def test_a_trailing_escape_that_cannot_become_a_screen_switch_goes_straight_through(self):
        f = ab.ScreenFilter()
        self.assertEqual(f.feed(b"x\x1b[3"), b"x\x1b[3")


FAKE = textwrap.dedent(
    r"""
    #!/usr/bin/env python3
    import os, signal, sys, time, tty
    tty.setraw(0)
    mode = sys.argv[2]
    out = sys.stdout
    out.write("\x1b[?1049hFRAME-1 size=%dx%d\r\n" % tuple(os.get_terminal_size()))
    out.flush()
    while True:
        c = sys.stdin.read(1)
        if mode == "leave" and c == "x":
            out.write("\x1b[?1049lAFTER-LEAVE\r\n"); out.flush(); time.sleep(4); break
        if mode == "stop" and c == "z":
            os.kill(os.getpid(), signal.SIGSTOP)
        if mode == "echo" and c == "q":
            break
        open(os.environ["FAKE_LOG"], "a").write(c)
    """
).lstrip("\n")


def run_bridge(mode, keys, wait=1.5, size=(40, 100)):
    """Runs attach_session in a child on a pseudo-terminal; returns (terminal output, seconds taken, typed log)."""
    tmp = Path(tempfile.mkdtemp())
    fake = tmp / "claude"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    log, result = tmp / "log", tmp / "result"
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["FAKE_LOG"] = str(log)
        tty.setraw(0)  # the board's terminal is always raw (curses); keys must reach the relay unbuffered
        started = time.time()
        ab.attach_session(str(fake), mode)
        result.write_text(str(time.time() - started))
        os._exit(0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))

    def read(seconds):
        data, end = b"", time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
        return data

    out = read(0.8)
    for key in keys:
        os.write(fd, key)
        out += read(0.4)
    out += read(wait)
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    os.waitpid(pid, 0)
    taken = float(result.read_text()) if result.exists() else None
    return out.decode(errors="replace"), taken, log.read_text() if log.exists() else ""


class AttachBridge(unittest.TestCase):
    def test_leaving_the_session_screen_returns_at_once_even_if_the_session_lingers(self):
        out, taken, _ = run_bridge("leave", [b"x"])
        self.assertIsNotNone(taken, "attach_session never returned")
        self.assertLess(taken, 2.0)  # the fake keeps running for 4 s after leaving its screen
        self.assertIn("FRAME-1", out)

    def test_nothing_the_session_draws_after_leaving_reaches_the_terminal(self):
        out, _, _ = run_bridge("leave", [b"x"])
        self.assertNotIn("AFTER-LEAVE", out)
        self.assertNotIn("\x1b[?1049l", out)  # the terminal never goes back to the normal screen
        self.assertIn("\x1b[2J", out)  # entering the alternate screen became a clear

    def test_keys_typed_are_relayed_to_the_session(self):
        out, taken, log = run_bridge("echo", [b"a", b"b", b"q"])
        self.assertEqual(log, "ab")
        self.assertIsNotNone(taken)

    def test_the_window_size_is_passed_on(self):
        out, _, _ = run_bridge("echo", [b"q"], size=(33, 91))
        self.assertIn("size=91x33", out)

    def test_a_session_that_stops_itself_detaches_instead_of_hanging(self):
        out, taken, _ = run_bridge("stop", [b"z"])
        self.assertIsNotNone(taken, "attach_session hung on a stopped session")
        self.assertLess(taken, 3.0)


if __name__ == "__main__":
    unittest.main()
