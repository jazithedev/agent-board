import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_board as ab

NOW = 1_800_000_000.0


def session(i, name, state="done"):
    return ab.Session(id=f"id{i}", name=name, state=state, project="p", project_path="/srv/www/p", started_at=NOW - 60, updated_at=NOW - 60)


def board():
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/p", now=lambda: NOW)
    b.set_sessions([session(1, "alpha", "working"), session(2, "beta")])
    return b


class SlowCli:
    """A stand-in for `claude` that holds each command until the test lets it finish."""

    def __init__(self, returncode=0, stderr=""):
        self.calls, self.release, self.started = [], threading.Event(), threading.Event()
        self.returncode, self.stderr = returncode, stderr

    def __call__(self, args, cwd=None, timeout=60):
        self.calls.append(args)
        self.started.set()
        self.release.wait(5)
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


def patched(cli, remaining):
    return mock.patch.multiple(ab, run_cli=cli, load_sessions=lambda: remaining)


def finish(b, cli):
    cli.release.set()
    for w in b.workers:
        w.join(5)


class WhileItRuns(unittest.TestCase):
    def test_the_action_does_not_block_the_board(self):
        b, cli = board(), SlowCli()
        with patched(cli, [session(1, "alpha", "working")]):
            started = time.time()
            self.assertTrue(ab.perform(None, b, ab.Effect("remove", session_id="id2"), None))
            self.assertLess(time.time() - started, 1.0)  # returns at once, the command is still running
            self.assertTrue(cli.started.wait(2))
            self.assertEqual(b.busy, {"id2": "removing…"})
            finish(b, cli)

    def test_the_row_shows_a_spinner_and_removing_while_it_runs(self):
        b, cli = board(), SlowCli()
        with patched(cli, [session(1, "alpha", "working")]):
            ab.start_session_action(b, ab.Effect("remove", session_id="id2"))
            lines, _ = ab.compose(b, 110, 20)
            row = next(l for l in lines if "beta" in ab.plain(l))
            self.assertIn("removing…", ab.plain(row))
            self.assertIn("chip_warn", {style for _, style in row.segs})
            self.assertTrue(any("this can take a few seconds" in ab.plain(l) for l in lines))
            finish(b, cli)

    def test_the_spinner_turns_so_the_screen_visibly_moves(self):
        b, cli = board(), SlowCli()
        with patched(cli, []):
            ab.start_session_action(b, ab.Effect("remove", session_id="id2"))
            icons = set()
            for tick in range(6):
                b.tick = tick
                row = next(l for l in ab.compose(b, 110, 20)[0] if "beta" in ab.plain(l))
                icons.add(ab.plain(row).lstrip()[0])
            self.assertGreater(len(icons), 2)
            finish(b, cli)

    def test_stopping_says_stopping_and_other_rows_are_untouched(self):
        b, cli = board(), SlowCli()
        with patched(cli, []):
            ab.start_session_action(b, ab.Effect("stop", session_id="id1"))
            text = "\n".join(ab.plain(l) for l in ab.compose(b, 110, 20)[0])
            self.assertIn("stopping…", text)
            self.assertNotIn("removing…", text)
            beta = next(ab.plain(l) for l in ab.compose(b, 110, 20)[0] if "beta" in ab.plain(l))
            self.assertNotIn("…", beta.split("beta")[1][:12])
            finish(b, cli)

    def test_it_finishes_by_clearing_the_marker_reloading_and_saying_what_happened(self):
        b, cli = board(), SlowCli()
        with patched(cli, [session(1, "alpha", "working")]):
            ab.start_session_action(b, ab.Effect("remove", session_id="id2"))
            finish(b, cli)
            self.assertEqual(b.busy, {})
            self.assertEqual(b.msg, "✓ deleted “beta”")
            self.assertEqual([s.id for s in b.sessions], ["id1"])  # the list was reloaded without it

    def test_deleting_stops_the_session_first_then_removes_it(self):
        b, cli = board(), SlowCli()
        cli.release.set()
        with patched(cli, []):
            ab.start_session_action(b, ab.Effect("remove", session_id="id2"))
            finish(b, cli)
        self.assertEqual(cli.calls, [["stop", "id2"], ["rm", "id2"]])

    def test_stopping_is_one_command(self):
        b, cli = board(), SlowCli()
        cli.release.set()
        with patched(cli, []):
            ab.start_session_action(b, ab.Effect("stop", session_id="id1"))
            finish(b, cli)
        self.assertEqual(cli.calls, [["stop", "id1"]])
        self.assertEqual(b.msg, "✓ stopped “alpha”")

    def test_a_failure_clears_the_marker_and_says_why(self):
        b, cli = board(), SlowCli(returncode=1, stderr="cannot remove a running session\n")
        cli.release.set()
        with patched(cli, [session(1, "alpha", "working"), session(2, "beta")]):
            ab.start_session_action(b, ab.Effect("remove", session_id="id2"))
            finish(b, cli)
        self.assertEqual(b.busy, {})
        self.assertTrue(b.msg.startswith("✗ could not remove “beta”"))
        self.assertIn("cannot remove a running session", b.msg)

    def test_an_error_starting_claude_clears_the_marker_too(self):
        b = board()

        def broken(*a, **k):
            raise OSError("no such file")

        with mock.patch.multiple(ab, run_cli=broken, load_sessions=lambda: []):
            ab.start_session_action(b, ab.Effect("remove", session_id="id2"))
            for w in b.workers:
                w.join(5)
        self.assertEqual(b.busy, {})
        self.assertIn("no such file", b.msg)


class KeysOnABusyRow(unittest.TestCase):
    def setUp(self):
        self.b, self.cli = board(), SlowCli()
        self.b.sel = "id2"
        self.b.busy["id2"] = "removing…"

    def test_it_cannot_be_opened_peeked_or_asked_about_again(self):
        for key in ("enter", "right", "space", "ctrl+x"):
            self.b.msg = ""
            self.assertIsNone(self.b.handle(key), key)
            self.assertIn("is removing…", self.b.msg, key)
        self.assertIsNone(self.b.confirm)

    def test_other_sessions_still_work_normally(self):
        self.b.sel = "id1"
        self.assertEqual(self.b.handle("enter"), ab.Effect("attach", session_id="id1"))

    def test_the_whole_double_ctrl_x_flow_ends_in_a_background_action(self):
        b = board()
        b.sel = "id2"
        b.handle("ctrl+x")
        effect = b.handle("ctrl+x")  # the second press confirms
        self.assertEqual(effect.kind, "remove")
        cli = SlowCli()
        with patched(cli, []):
            self.assertTrue(ab.perform(None, b, effect, None))
            self.assertIn("id2", b.busy)
            finish(b, cli)

    def test_every_width_fits_with_a_busy_row(self):
        for width in (50, 70, 100, 140):
            for line in ab.compose(self.b, width, 20)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))


if __name__ == "__main__":
    unittest.main()
