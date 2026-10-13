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


class SlowStart(SlowCli):
    def __call__(self, args, cwd=None, timeout=60):
        self.calls.append(args)
        self.started.set()
        self.release.wait(5)
        return SimpleNamespace(returncode=0, stdout="backgrounded · cafe1234 (idle)", stderr="")


class StartingASession(unittest.TestCase):
    def effect(self):
        return ab.Effect("dispatch", prompt="fix the login bug", cwd="/srv/www/p", tags=[])

    def test_the_row_appears_and_is_selected_before_claude_answers(self):
        b, cli = board(), SlowStart()
        with patched(cli, []):
            started = time.time()
            self.assertTrue(ab.perform(None, b, self.effect(), None))
            self.assertLess(time.time() - started, 1.0)  # the board is not frozen waiting for `claude --bg`
            self.assertTrue(cli.started.wait(2))
            self.assertEqual(b.sel, "starting-1")
            lines, _ = ab.compose(b, 110, 24)
            row = next(l for l in lines if "fix the login bug" in ab.plain(l))
            self.assertIn("starting…", ab.plain(row))
            finish(b, cli)

    def test_the_real_session_takes_over_the_row_and_the_selection(self):
        b, cli = board(), SlowStart()
        real = ab.Session(id="cafe1234", name="fix the login bug", state="working", project="p", project_path="/srv/www/p", started_at=NOW, updated_at=NOW)
        with patched(cli, [session(1, "alpha", "working"), real]):
            ab.perform(None, b, self.effect(), None)
            finish(b, cli)
        self.assertEqual(b.sel, "cafe1234")
        self.assertEqual(b.placeholders, [])
        self.assertEqual(b.busy, {})
        self.assertNotIn("starting-1", [s.id for s in b.sessions])

    def test_a_refresh_while_starting_keeps_the_row(self):
        b, cli = board(), SlowStart()
        with patched(cli, []):
            ab.perform(None, b, self.effect(), None)
            b.set_sessions([session(1, "alpha", "working")])
            self.assertIn("starting-1", [s.id for s in b.sessions])
            finish(b, cli)

    def test_a_failed_start_removes_the_row_and_says_why(self):
        b, cli = board(), SlowCli(returncode=1, stderr="no such directory")
        with patched(cli, [session(1, "alpha", "working")]):
            ab.perform(None, b, self.effect(), None)
            finish(b, cli)
        self.assertEqual(b.placeholders, [])
        self.assertIsNone(b.sel)
        self.assertIn("no such directory", b.msg)

    def test_the_starting_row_cannot_be_opened(self):
        b, cli = board(), SlowStart()
        with patched(cli, []):
            ab.perform(None, b, self.effect(), None)
            self.assertIsNone(b.handle("right"))
            self.assertIn("starting", b.msg)
            finish(b, cli)


def ranked(*names):
    """Finished sessions, newest first, so the list reads in the order given."""
    return [ab.Session(id=f"id{n}", name=n, state="done", project="p", project_path="/srv/www/p", updated_at=NOW - 60 * i, started_at=NOW - 60 * i) for i, n in enumerate(names)]


class AfterDeleting(unittest.TestCase):
    def setUp(self):
        self.b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/p", now=lambda: NOW)
        self.all = ranked("a", "b", "c", "d")
        self.b.set_sessions(list(self.all))

    def delete(self, name, left, release=True):
        cli = SlowCli()
        with patched(cli, left):
            ab.start_session_action(self.b, ab.Effect("remove", session_id=f"id{name}"))
            if release:
                finish(self.b, cli)

    def test_the_next_row_is_highlighted_once_the_session_is_gone(self):
        self.b.sel = "idb"
        self.delete("b", [s for s in self.all if s.name != "b"])
        self.assertEqual(self.b.sel, "idc")

    def test_the_last_row_hands_the_highlight_to_the_one_before(self):
        self.b.sel = "idd"
        self.delete("d", self.all[:3])
        self.assertEqual(self.b.sel, "idc")

    def test_the_only_session_leaves_nothing_highlighted(self):
        self.b.set_sessions(ranked("a"))
        self.b.sel = "ida"
        self.delete("a", [])
        self.assertIsNone(self.b.sel)

    def test_the_highlight_stays_on_the_deleted_row_until_it_vanishes(self):
        self.b.sel = "idb"
        self.delete("b", [s for s in self.all if s.name != "b"], release=False)
        self.assertEqual(self.b.sel, "idb")  # the row shows "removing…" meanwhile
        finish(self.b, SlowCli())

    def test_moving_elsewhere_while_it_deletes_is_respected(self):
        self.b.sel = "idb"
        cli = SlowCli()
        with patched(cli, [s for s in self.all if s.name != "b"]):
            ab.start_session_action(self.b, ab.Effect("remove", session_id="idb"))
            self.b.sel = "idd"
            finish(self.b, cli)
        self.assertEqual(self.b.sel, "idd")

    def test_deleting_two_in_a_row_never_lands_on_the_one_going_away(self):
        self.b.sel = "idb"
        cli = SlowCli()
        with patched(cli, [self.all[0], self.all[3]]):
            ab.start_session_action(self.b, ab.Effect("remove", session_id="idb"))
            self.b.sel = "idc"
            ab.start_session_action(self.b, ab.Effect("remove", session_id="idc"))
            finish(self.b, cli)
        self.assertEqual(self.b.sel, "idd")

    def test_stopping_keeps_the_highlight_where_it_is(self):
        self.b.sel = "idb"
        cli = SlowCli()
        with patched(cli, list(self.all)):
            ab.start_session_action(self.b, ab.Effect("stop", session_id="idb"))
            finish(self.b, cli)
        self.assertEqual(self.b.sel, "idb")

    def test_a_failed_delete_leaves_the_highlight_on_the_session(self):
        self.b.sel = "idb"
        cli = SlowCli(returncode=1, stderr="refused")
        with patched(cli, list(self.all)):
            ab.start_session_action(self.b, ab.Effect("remove", session_id="idb"))
            finish(self.b, cli)
        self.assertEqual(self.b.sel, "idb")


if __name__ == "__main__":
    unittest.main()
