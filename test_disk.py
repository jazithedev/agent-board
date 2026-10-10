import os
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

import agent_board as ab

NOW = 1_800_000_000.0
GB = 1 << 30
Usage = namedtuple("Usage", "total used free")


def disk(total_gb=95, used_gb=61, free_gb=30, label="Disk"):
    return ab.DiskUsage(label, total_gb * GB, used_gb * GB, free_gb * GB)


def info(*disks, claude=int(1.3 * GB), worktrees=3 * GB):
    return ab.DiskInfo(list(disks) or [disk()], claude, worktrees)


def plain_rows(rows):
    return [ab.plain(r) for r in rows]


class Formatting(unittest.TestCase):
    def test_sizes_read_like_df_does(self):
        self.assertEqual([ab.fmt_bytes(n) for n in (0, 900, 2048, 535 << 20, int(1.3 * GB), 30 * GB, 95 * GB, 2 << 40)],
                         ["0B", "900B", "2.0K", "535M", "1.3G", "30G", "95G", "2.0T"])

    def test_the_bar_turns_yellow_below_15_percent_free_and_red_below_5(self):
        self.assertEqual([ab.disk_style(p) for p in (0, 84.9, 85, 94.9, 95, 100)], ["done", "done", "wait", "wait", "fail", "fail"])

    def test_percent_is_used_over_used_plus_available_like_df(self):
        self.assertAlmostEqual(disk().percent, 61 / 91 * 100)
        self.assertEqual(ab.DiskUsage("x", 0, 0, 0).percent, 0.0)


class Measuring(unittest.TestCase):
    def test_one_entry_per_filesystem_home_first_and_plain_label_when_there_is_one(self):
        mounts = {"/home/u": "/", "/srv/www/a": "/", "/srv/www/b": "/"}
        got = ab.read_disks(list(mounts), usage=lambda m: Usage(100, 40, 60), mount_of=mounts.get)
        self.assertEqual([(d.label, d.total, d.used, d.free) for d in got], [("Disk", 100, 40, 60)])

    def test_projects_on_another_disk_get_their_own_labelled_row_after_the_home_disk(self):
        mounts = {"/home/u": "/", "/data/proj": "/data", "/srv/x": "/data"}
        got = ab.read_disks(list(mounts), usage=lambda m: Usage(100, 10 if m == "/" else 90, 90 if m == "/" else 10), mount_of=mounts.get)
        self.assertEqual([d.label for d in got], ["Disk /", "Disk /data"])
        self.assertEqual(len(got), 2)

    def test_a_vanished_path_or_unreadable_mount_is_skipped_not_fatal(self):
        def mount_of(p):
            if p == "gone":
                raise OSError
            return "/"

        def usage(m):
            raise OSError

        self.assertEqual(ab.read_disks(["gone", "/x"], usage=usage, mount_of=mount_of), [])

    def test_the_real_machine_gives_a_sane_answer(self):
        [first, *_] = ab.read_disks([str(Path.home())])
        self.assertGreater(first.total, 0)
        self.assertLessEqual(first.free, first.total)

    def test_mount_point_of_an_existing_path_is_a_mount(self):
        self.assertTrue(os.path.ismount(ab.mount_point(tempfile.gettempdir())))


class DirectorySize(unittest.TestCase):
    def tree(self):
        root = Path(tempfile.mkdtemp())
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "a.bin").write_bytes(os.urandom(300_000))
        (root / "sub" / "b.bin").write_bytes(os.urandom(200_000))
        (root / "sub" / "deep" / "c.bin").write_bytes(os.urandom(100_000))
        return root

    def test_counts_every_file_in_every_subfolder(self):
        root = self.tree()
        self.assertGreaterEqual(ab.dir_size(str(root)), 600_000)
        self.assertLess(ab.dir_size(str(root)), 1_000_000)

    def test_agrees_with_du(self):
        import shutil
        import subprocess

        if not shutil.which("du"):
            self.skipTest("no du")
        root = self.tree()
        du = int(subprocess.run(["du", "-s", "--block-size=1", str(root)], capture_output=True, text=True).stdout.split()[0])
        self.assertAlmostEqual(ab.dir_size(str(root)), du, delta=16_384)

    def test_a_hard_link_is_counted_once(self):
        root = self.tree()
        before = ab.dir_size(str(root))
        os.link(root / "a.bin", root / "sub" / "a-again.bin")
        self.assertLess(ab.dir_size(str(root)) - before, 20_000)  # only the new directory entry, not another 300 kB

    def test_a_symlink_is_not_followed(self):
        root = self.tree()
        outside = Path(tempfile.mkdtemp())
        (outside / "big.bin").write_bytes(os.urandom(2_000_000))
        before = ab.dir_size(str(root))
        os.symlink(outside, root / "link")
        self.assertLess(ab.dir_size(str(root)) - before, 20_000)

    def test_a_missing_or_unreadable_folder_is_zero_not_an_error(self):
        self.assertEqual(ab.dir_size("/definitely/not/here"), 0)


class Drawing(unittest.TestCase):
    def test_a_row_shows_bar_percent_free_total_and_what_claude_and_worktrees_take(self):
        [row] = plain_rows(ab.disk_rows(info(), 110))
        for needle in ("Disk", "67%", "30G free of 95G", "Claude data 1.3G", "worktrees 3.0G", "█"):
            self.assertIn(needle, row)

    def test_a_nearly_full_disk_says_so_in_red(self):
        rows = ab.disk_rows(info(disk(100, 97, 3)), 110)
        self.assertIn("nearly full", ab.plain(rows[0]))
        styles = {style for text, style in rows[0].segs if "█" in text or "░" in text}
        self.assertEqual(styles, {"fail"})

    def test_a_filling_disk_is_yellow(self):
        rows = ab.disk_rows(info(disk(100, 88, 12)), 110)
        self.assertEqual({style for text, style in rows[0].segs if "█" in text}, {"wait"})
        self.assertNotIn("nearly full", ab.plain(rows[0]))

    def test_the_footprint_is_only_on_the_first_disk_and_only_once_measured(self):
        two = ab.disk_rows(info(disk(label="Disk /"), disk(100, 50, 50, label="Disk /data")), 120)
        self.assertIn("Claude data", ab.plain(two[0]))
        self.assertNotIn("Claude data", ab.plain(two[1]))
        measuring = ab.disk_rows(ab.DiskInfo([disk()]), 120)
        self.assertNotIn("Claude data", ab.plain(measuring[0]))
        self.assertIn("30G free", ab.plain(measuring[0]))

    def test_every_row_fits_every_width(self):
        for width in range(30, 161, 5):
            for row in ab.disk_rows(info(disk(label="Disk /a/very/long/mount/point")), width):
                self.assertLessEqual(ab.seg_width(row.segs), width, ab.plain(row))

    def test_the_row_comes_after_the_limits_and_the_title_no_longer_claims_account_wide(self):
        lim = ab.demo_limits(NOW)
        rows = plain_rows(ab.limits_panel(lim, 110, NOW, info()))
        self.assertEqual([r.split()[0] for r in rows[1:]], ["5-hour", "Weekly", "Disk"])
        self.assertNotIn("account-wide", rows[0])
        self.assertIn("as of 3m ago", rows[0])

    def test_disk_still_shows_when_there_is_no_limits_reading_yet(self):
        rows = plain_rows(ab.limits_panel(None, 110, NOW, info()))
        self.assertIn("no reading yet", rows[1])
        self.assertTrue(rows[2].lstrip().startswith("Disk"))

    def test_without_disk_data_nothing_changes(self):
        self.assertEqual(len(ab.limits_panel(ab.demo_limits(NOW), 110, NOW)), 3)
        self.assertEqual(len(ab.limits_panel(ab.demo_limits(NOW), 110, NOW, ab.DiskInfo([]))), 3)


def board():
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/scum-map", now=lambda: NOW)
    b.set_sessions([ab.Session(id="id1", name="alpha", state="working", project="scum-map", project_path="/srv/www/scum-map")])
    return b


class OnScreen(unittest.TestCase):
    def test_the_disk_row_is_the_last_line_of_the_bottom_block_above_the_prompt(self):
        b = board()
        b.limits, b.disk = ab.demo_limits(NOW), info()
        lines = [ab.plain(l) for l in ab.compose(b, 110, 24)[0]]
        i = next(i for i, l in enumerate(lines) if l.lstrip().startswith("Disk"))
        self.assertEqual(i, len(lines) - 4)  # then the rule, the prompt and the hint
        self.assertTrue(lines[i + 1].startswith("─"))

    def test_nothing_is_drawn_before_the_first_measurement(self):
        b = board()
        b.limits = ab.demo_limits(NOW)
        self.assertNotIn("Disk", "\n".join(ab.plain(l) for l in ab.compose(b, 110, 24)[0]))

    def test_two_disks_are_two_rows_and_the_screen_stays_within_its_height(self):
        b = board()
        b.limits, b.disk = ab.demo_limits(NOW), info(disk(label="Disk /"), disk(100, 50, 50, label="Disk /data"))
        lines = ab.compose(b, 110, 24)[0]
        self.assertEqual(sum(1 for l in lines if ab.plain(l).lstrip().startswith("Disk")), 2)
        self.assertLessEqual(len(lines), 24)

    def test_a_long_session_list_still_wins_the_space_over_the_whole_block(self):
        b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/x", now=lambda: NOW)
        b.set_sessions([ab.Session(id=f"i{n}", name=f"s{n}", state="done", project="p", project_path="/p", updated_at=NOW - n) for n in range(30)])
        b.limits, b.disk = ab.demo_limits(NOW), info()
        self.assertNotIn("Disk", "\n".join(ab.plain(l) for l in ab.compose(b, 110, 16)[0]))

    def test_every_width_fits_with_the_disk_row(self):
        b = board()
        b.limits, b.disk, b.usage = ab.demo_limits(NOW), info(), ab.demo_usage()
        for width in (50, 70, 100, 140):
            for line in ab.compose(b, width, 30)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))

    def test_the_demo_screen_has_the_row(self):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            ab.main(["--dump", "--demo", "--width", "110", "--height", "40"])
        self.assertIn("30G free of 95G", out.getvalue())


class Loader(unittest.TestCase):
    def make(self, tmp, calls):
        b = board()
        b.launch_cwd = str(tmp)  # not the real project: this machine's own worktrees must not leak into the test
        b.set_sessions([ab.Session(id="i", name="n", state="done", project="p", project_path=str(tmp))])

        def sizer(path):
            calls.append(path)
            return 1000

        return b, ab.DiskLoader(b, usage=lambda m: Usage(100, 40, 60), sizer=sizer)

    def test_publishes_free_space_and_the_two_footprints(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".claude" / "worktrees").mkdir(parents=True)
        calls = []
        b, loader = self.make(tmp, calls)
        result = loader.measure_once()
        self.assertIs(b.disk, result)
        self.assertEqual((result.claude_bytes, result.worktree_bytes), (1000, 1000))
        self.assertEqual(result.disks[0].free, 60)
        self.assertIn(os.path.realpath(tmp / ".claude" / "worktrees"), calls)

    def test_the_slow_sizes_are_refreshed_only_every_five_minutes_free_space_every_time(self):
        calls = []
        b, loader = self.make(Path(tempfile.mkdtemp()), calls)
        loader.measure_once()
        first = len(calls)
        loader.measure_once()
        self.assertEqual(len(calls), first)  # served from the cache
        loader.sizes_at -= ab.DISK_SIZES_REFRESH_SECONDS + 1
        loader.measure_once()
        self.assertGreater(len(calls), first)

    def test_free_space_is_published_before_the_slow_measurement_finishes(self):
        b = board()
        seen = []

        def slow_sizer(path):
            seen.append(b.disk)  # what the screen could already show while sizing
            return 5

        loader = ab.DiskLoader(b, usage=lambda m: Usage(100, 40, 60), sizer=slow_sizer)
        loader.measure_once()
        self.assertIsNotNone(seen[0])
        self.assertIsNone(seen[0].claude_bytes)
        self.assertEqual(seen[0].disks[0].free, 60)

    def test_paths_are_home_the_launch_folder_and_every_project_without_duplicates(self):
        b = board()
        loader = ab.DiskLoader(b)
        paths = loader.paths()
        self.assertEqual(paths[0], str(Path.home()))
        self.assertIn("/srv/www/scum-map", paths)
        self.assertEqual(len(paths), len(set(paths)))


if __name__ == "__main__":
    unittest.main()
