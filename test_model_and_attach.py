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
    mode = sys.argv[2]
    if mode == "fail":
        sys.stderr.write("Waking session abc\u2026\r\n"); sys.stderr.flush(); time.sleep(0.3)
        sys.stderr.write("Couldn't wake abc \u2014 Workspace not trusted. Run `claude` once there, then retry.\r\n"); sys.exit(1)
    if mode == "okexit":
        sys.stdout.write("bye\r\n"); sys.stdout.flush(); sys.exit(0)
    if sys.argv[1] == "daemon":  # what `claude attach` starts as its own child when no supervisor is running
        os.setsid()  # like the real one: its own session, so closing the terminal does not hang it up
        open(os.environ["FAKE_DAEMON"], "w").write(str(os.getpid())); time.sleep(30); sys.exit(0)
    if mode == "leave-daemon":
        import subprocess
        subprocess.Popen([sys.argv[0], "daemon", "run", "--origin", "transient"])
        time.sleep(0.25)
        mode = "leave"
    tty.setraw(0)
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


def run_bridge(mode, keys, wait=1.5, size=(40, 100), full=False):
    """Runs attach_session in a child on a pseudo-terminal; returns (terminal output, seconds taken, typed log)."""
    tmp = Path(tempfile.mkdtemp())
    fake = tmp / "claude"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    log, result = tmp / "log", tmp / "result"
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["FAKE_LOG"] = str(log)
        os.environ["FAKE_DAEMON"] = str(tmp / "daemon.pid")
        tty.setraw(0)  # the board's terminal is always raw (curses); keys must reach the relay unbuffered
        started = time.time()
        outcome = ab.attach_session(str(fake), mode)
        import json
        daemon_alive = None
        if (tmp / "daemon.pid").exists():
            dpid = int((tmp / "daemon.pid").read_text())
            try:
                os.kill(dpid, 0)
                daemon_alive = True
                os.kill(dpid, 9)  # tidy up: the test only needed to know whether the board spared it
            except OSError:
                daemon_alive = False
        result.write_text(json.dumps({"taken": time.time() - started, "daemon_alive": daemon_alive, "cut": outcome.cut, "returncode": outcome.returncode,
                                      "output": outcome.output, "killed": outcome.killed, "failed": outcome.failed}))
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
    import json
    details = json.loads(result.read_text()) if result.exists() else None
    taken = details["taken"] if details else None
    if full:
        return out.decode(errors="replace"), taken, log.read_text() if log.exists() else "", details
    return out.decode(errors="replace"), taken, log.read_text() if log.exists() else ""


class AttachOutcome(unittest.TestCase):
    """What attach_session reports: did it fail, and what did the session say."""

    def test_a_session_that_cannot_be_woken_is_a_failure_with_its_own_words(self):
        out, taken, _, r = run_bridge("fail", [], wait=1.2, full=True)
        self.assertTrue(r["failed"])
        self.assertEqual(r["returncode"], 1)
        self.assertFalse(r["killed"])
        self.assertIn("Workspace not trusted", r["output"])
        self.assertNotIn("\x1b", r["output"])  # escape codes stripped

    def test_the_session_s_first_words_land_on_a_cleared_screen_never_on_top_of_the_board(self):
        out, _, _, _ = run_bridge("fail", [], wait=1.2, full=True)
        self.assertIn("\x1b[2J", out)
        self.assertLess(out.index("\x1b[2J"), out.index("Waking session"))

    def test_a_normal_exit_is_not_a_failure(self):
        out, taken, _, r = run_bridge("okexit", [], wait=1.0, full=True)
        self.assertFalse(r["failed"])
        self.assertEqual(r["returncode"], 0)

    def test_a_session_the_board_ended_itself_is_never_a_failure_even_though_it_was_killed(self):
        _, _, _, left = run_bridge("leave", [b"x"], full=True)
        self.assertTrue(left["killed"])
        self.assertFalse(left["failed"])
        self.assertNotIn("AFTER-LEAVE", left["output"])
        _, _, _, stopped = run_bridge("stop", [b"z"], full=True)
        self.assertTrue(stopped["killed"])
        self.assertFalse(stopped["failed"])


class FailureNotice(unittest.TestCase):
    def result(self, output, returncode=1, killed=False):
        return ab.AttachResult(cut=False, returncode=returncode, output=output, killed=killed)

    def test_nothing_to_say_when_nothing_went_wrong(self):
        self.assertIsNone(ab.attach_failure_notice(self.result("", 0), "x"))
        self.assertIsNone(ab.attach_failure_notice(self.result("whatever", -9, killed=True), "x"))
        self.assertIsNone(ab.attach_failure_notice(self.result("whatever", None), "x"))

    def test_the_reason_drops_the_progress_line_and_the_footer_names_the_session(self):
        footer, lines = ab.attach_failure_notice(self.result("Waking session abc…\nCouldn't wake abc — it broke.\n"), "clean view mod")
        self.assertIn("clean view mod", footer)
        self.assertTrue(footer.startswith("✗"))
        self.assertEqual(lines, ["Couldn't wake abc — it broke."])

    def test_an_untrusted_folder_gets_the_exact_fix(self):
        _, lines = ab.attach_failure_notice(self.result("Couldn't wake abc — Workspace not trusted. Run `claude` in /x once."), "s")
        text = " ".join(lines)
        self.assertIn("accept the trust prompt", text)
        self.assertIn("Run `claude` once there", text)

    def test_silence_still_produces_something_to_read(self):
        _, lines = ab.attach_failure_notice(self.result("   \n"), "s")
        self.assertIn("without saying why", lines[0])

    def test_failures_are_red_in_the_footer_and_other_notes_are_not(self):
        b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/x", now=lambda: NOW)
        b.msg = "✗ something failed"
        self.assertEqual(ab._hint_segs(b)[1][1], "fail")
        b.msg = "copied 3 characters"
        self.assertEqual(ab._hint_segs(b)[1][1], "accent")


class Wrapping(unittest.TestCase):
    def test_lines_wrap_on_words_and_blank_lines_survive(self):
        self.assertEqual(ab.wrap_lines(["aaa bbb ccc", "", "d"], 7), ["aaa bbb", "ccc", "", "d"])

    def test_an_unbreakable_word_is_still_broken_to_fit(self):
        self.assertTrue(all(len(l) <= 12 for l in ab.wrap_lines(["x" * 40], 12)))

    def test_an_error_popup_shows_all_of_a_long_message_at_every_width_while_a_log_peek_still_truncates(self):
        long = "Couldn't wake abc — Workspace not trusted. Run `claude` in /some/long/folder once and accept the trust prompt, then retry."
        for width in (60, 80, 120):
            b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/x", now=lambda: NOW)
            b.peek = {"title": "could not open s", "lines": [long], "top": 0, "wrap": True}
            text = " ".join(ab.plain(l).replace("│", " ") for l in ab.compose(b, width, 24)[0])
            self.assertIn("then retry.", " ".join(text.split()), width)
            for line in ab.compose(b, width, 24)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width)
        b.peek = {"title": "output", "lines": [long], "top": 0}  # a log: no wrap flag
        self.assertIn("…", "\n".join(ab.plain(l) for l in ab.compose(b, 60, 24)[0]))

    def test_the_popup_closes_with_any_key_and_the_board_is_usable_afterwards(self):
        b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/x", now=lambda: NOW)
        b.peek = {"title": "t", "lines": ["x"], "top": 0, "wrap": True}
        b.handle("esc")
        self.assertIsNone(b.peek)


class WhatMayBeKilled(unittest.TestCase):
    CLAUDE = "/opt/claude/bin/claude"

    def test_the_supervisor_the_host_and_the_session_are_infrastructure(self):
        for argv in (
            [self.CLAUDE, "daemon", "run", "--origin", "transient", "--spawned-by", "{}"],
            [self.CLAUDE, "bg-pty-host", "--bg-pty-host", "/tmp/x.sock", "200", "50", "--", "claude"],
            [self.CLAUDE, "bg-spare", "--bg-spare", "/tmp/x.sock"],
            ["/opt/claude/versions/2.1.296", "--resume", "/data/projects/p/s.jsonl"],
            ["node", "cli.js", "daemon", "run"],
        ):
            self.assertTrue(ab.is_session_infrastructure(argv), argv)

    def test_the_attach_client_and_the_agent_view_are_not(self):
        for argv in ([self.CLAUDE, "attach", "fb25ad8c"], [self.CLAUDE, "agents"], [self.CLAUDE], []):
            self.assertFalse(ab.is_session_infrastructure(argv), argv)

    def test_only_the_attach_client_and_the_relaunched_view_are_killed(self):
        argvs = {
            10: [self.CLAUDE, "attach", "s1"],
            11: [self.CLAUDE, "daemon", "run", "--origin", "transient"],
            12: [self.CLAUDE, "bg-pty-host", "--bg-pty-host", "/tmp/a.sock"],
            13: ["/v/2.1.296", "--resume", "/p/s1.jsonl"],
            14: [self.CLAUDE, "bg-spare", "--bg-spare", "/tmp/b.sock"],
            15: [self.CLAUDE, "agents"],
        }
        tree = {11: [12, 14, 13], 12: [13], 10: [11, 15]}
        killed = ab.attach_kill_set([10, 11, 12, 14, 13, 15], argvs.get, lambda p: _below(tree, p))
        self.assertEqual(sorted(killed), [10, 15])


def _below(tree, pid):
    out, queue = [], [pid]
    while queue:
        for child in tree.get(queue.pop(), []):
            out.append(child)
            queue.append(child)
    return out


class Descendants(unittest.TestCase):
    """Opening a session checks its processes ten times a second, so finding them must stay cheap."""

    def tree(self):
        import subprocess
        parent = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
        self.addCleanup(lambda: (parent.kill(), parent.wait()))
        for _ in range(100):
            found = ab.descendants(parent.pid)
            if found:
                return parent.pid, found
            time.sleep(0.02)
        self.fail("the child never started")

    def test_finds_a_child_through_the_kernel_lists(self):
        pid, found = self.tree()
        self.assertEqual(len(found), 1)
        self.assertIn("sleep", " ".join(ab.read_argv(found[0])))

    def test_matches_the_full_walk_when_the_kernel_has_no_lists(self):
        pid, found = self.tree()
        with mock.patch.object(ab, "CHILDREN_FILES", False):
            self.assertEqual(ab.descendants(pid), found)

    def test_does_not_walk_every_process_when_the_kernel_lists_children(self):
        if not ab.CHILDREN_FILES:
            self.skipTest("this kernel does not list children")
        pid, _ = self.tree()
        with mock.patch.object(ab, "_children", side_effect=AssertionError("walked all of /proc")):
            ab.descendants(pid)

    def test_a_process_that_is_gone_has_none(self):
        self.assertEqual(ab.descendants(2**22 + 12345), [])


class RefreshWhileOpen(unittest.TestCase):
    """Nobody sees the board while a session is open, so `claude agents` is not run every two seconds then."""

    def run_once(self, b):
        loader = ab.Loader(b, demo=False)
        loader.stop.wait = lambda _: loader.stop.set()
        with mock.patch.object(ab, "load_sessions", return_value=[]) as load:
            loader.run()
        return load.call_count

    def test_refreshes_while_the_board_is_shown(self):
        self.assertEqual(self.run_once(board()), 1)

    def test_waits_while_a_session_is_open(self):
        b = board()
        b.attached = True
        self.assertEqual(self.run_once(b), 0)
        self.assertEqual([s.id for s in b.sessions], ["id1"])


class AttachBridge(unittest.TestCase):
    def test_leaving_the_session_screen_returns_at_once_even_if_the_session_lingers(self):
        out, taken, _ = run_bridge("leave", [b"x"])
        self.assertIsNotNone(taken, "attach_session never returned")
        self.assertLess(taken, 2.0)  # the fake keeps running for 4 s after leaving its screen
        self.assertIn("FRAME-1", out)

    def test_leaving_never_kills_the_supervisor_the_attach_client_started(self):
        # With no supervisor running, `claude attach` starts one as its own child; the session lives under it.
        _, taken, _, details = run_bridge("leave-daemon", [b"x"], full=True)
        self.assertIsNotNone(taken, "attach_session never returned")
        self.assertTrue(details["daemon_alive"], "the board killed the supervisor, and the session with it")

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
