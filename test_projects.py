import json
import os
import tempfile
import unittest
from pathlib import Path

import agent_board as ab

NOW = 1_800_000_000.0


def transcript(root: Path, folder: str, cwd: str, mtime: float, lines_before: int = 2) -> None:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s1.jsonl"
    rows = [json.dumps({"type": "file-history-snapshot"})] * lines_before + [json.dumps({"type": "user", "cwd": cwd}, separators=(",", ":"))]
    f.write_text("\n".join(rows) + "\n")
    os.utime(f, (mtime, mtime))


def board(known=None):
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/here", now=lambda: NOW)
    b.set_sessions([ab.Session(id="id1", name="alpha", state="done", project="live", project_path="/srv/www/live", updated_at=NOW)])
    b.known_projects_fn = known
    return b


class KnownProjects(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "projects"
        ab._known_cache.clear()

    def dirs(self, *names):
        out = []
        for n in names:
            p = self.tmp / n
            p.mkdir(parents=True, exist_ok=True)
            out.append(str(p))
        return out

    def test_lists_directories_from_transcripts_most_recent_first(self):
        old, new = self.dirs("old", "new")
        transcript(self.root, "-old", old, 1000)
        transcript(self.root, "-new", new, 2000)
        self.assertEqual(ab.known_projects(self.root), [("new", new), ("old", old)])

    def test_the_real_path_comes_from_the_transcript_not_the_folder_name(self):
        (spaced,) = self.dirs("my-app.v2")  # a folder name could not say whether `-` or `.` was a slash
        transcript(self.root, "-tmp-my-app-v2", spaced, 1000, lines_before=40)
        self.assertEqual(ab.known_projects(self.root), [("my-app.v2", spaced)])

    def test_worktrees_fold_into_their_repository(self):
        (repo,) = self.dirs("repo")
        wt = Path(repo) / ".claude" / "worktrees" / "feature"
        wt.mkdir(parents=True)
        transcript(self.root, "-wt", str(wt), 1000)
        transcript(self.root, "-repo", repo, 500)
        self.assertEqual(ab.known_projects(self.root), [("repo", repo)])

    def test_missing_and_hidden_directories_are_left_out(self):
        (hidden,) = self.dirs(".claude/jobs")
        transcript(self.root, "-gone", str(self.tmp / "deleted"), 1000)
        transcript(self.root, "-hidden", hidden, 1000)
        self.assertEqual(ab.known_projects(self.root), [])

    def test_an_empty_or_missing_root_is_fine(self):
        self.assertEqual(ab.known_projects(self.tmp / "nope"), [])
        (self.root / "-empty").mkdir(parents=True)
        self.assertEqual(ab.known_projects(self.root), [])


class InThePicker(unittest.TestCase):
    def test_known_projects_follow_the_ones_with_sessions(self):
        a, b_ = tempfile.mkdtemp(), tempfile.mkdtemp()
        b = board(lambda: [("a", a), ("b", b_)])
        b.open_picker()
        names = [i[0] for i in b.picker_items()]
        self.assertEqual(names[:2], ["here", "live"])
        self.assertEqual([i[1] for i in b.picker_items()][2:], [a, b_])
        self.assertEqual(b.picker_items()[2][2:], (0, 0))  # no sessions there

    def test_a_known_project_can_be_chosen_for_new_sessions(self):
        (a,) = [tempfile.mkdtemp()]
        b = board(lambda: [("a", a)])
        b.open_picker()
        for _ in range(2):
            b.handle("down")
        b.handle("down")
        b.handle("enter")
        self.assertEqual(b.target()[1], a)

    def test_ctrl_t_pins_the_highlighted_project_and_it_survives_a_restart(self):
        (a,) = [tempfile.mkdtemp()]
        b = board(lambda: [])
        b.meta = ab.Meta(Path(tempfile.mkdtemp()) / "m.json")
        b.open_picker()
        for ch in a:
            b.handle("char", ch)  # typing a path offers it
        b.handle("ctrl+t")
        self.assertEqual(b.pinned_projects(), [a])
        self.assertIn("pinned", b.msg)
        again = ab.Meta(b.meta.path)
        self.assertEqual(again.settings["projects"], [a])

    def test_a_pinned_project_is_listed_without_sessions_or_history_and_unpins(self):
        (a,) = [tempfile.mkdtemp()]
        b = board(lambda: [])
        b.meta.set_setting("projects", [a, "/no/such/dir"])
        b.open_picker()
        self.assertIn(a, [i[1] for i in b.picker_items()])
        self.assertNotIn("/no/such/dir", [i[1] for i in b.picker_items()])  # gone from disk: not offered
        for _ in range(2):
            b.handle("down")
        b.handle("ctrl+t")
        self.assertEqual(b.pinned_projects(), ["/no/such/dir"])

    def test_the_overlay_marks_pins_and_projects_without_sessions(self):
        (a,) = [tempfile.mkdtemp()]
        b = board(lambda: [("a", a)])
        b.meta.set_setting("projects", [a])
        b.handle("ctrl+p")
        lines, _ = ab.compose(b, 120, 30)
        text = "\n".join(ab.plain(l) for l in lines)
        self.assertIn("★", text)
        self.assertIn("no sessions", text)
        self.assertIn("ctrl+t pins", text)

    def test_a_failing_source_leaves_the_picker_working(self):
        def boom():
            raise OSError("nope")

        b = board(boom)
        b.open_picker()
        self.assertEqual([i[0] for i in b.picker_items()][:2], ["here", "live"])


if __name__ == "__main__":
    unittest.main()
