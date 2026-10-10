import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_board as ab

ROOT = Path(__file__).resolve().parent


def run_main(*argv):
    out = io.StringIO()
    with redirect_stdout(out), mock.patch.object(sys, "stdout", out):
        try:
            code = ab.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


class CommandLine(unittest.TestCase):
    def test_version(self):
        self.assertEqual(run_main("--version"), (0, f"agent-board {ab.__version__}\n"))

    def test_help_lists_every_option(self):
        code, text = run_main("--help")
        self.assertEqual(code, 0)
        for option in ("--demo", "--doctor", "--update", "--version", "--dump", "--view"):
            self.assertIn(option, text)

    def test_the_demo_can_be_dumped_for_every_view(self):
        for view in ab.VIEW_ORDER:
            code, text = run_main("--dump", "--demo", "--view", view, "--width", "100", "--height", "20")
            self.assertEqual(code, 0)
            self.assertIn("Agent Board", text)

    def test_without_a_terminal_it_explains_instead_of_crashing(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err), mock.patch.object(sys.stdout, "isatty", lambda: False, create=True):
            code = ab.main([])
        self.assertEqual(code, 2)
        self.assertIn("needs a terminal", err.getvalue())

    def test_a_wrong_option_is_rejected(self):
        with self.assertRaises(SystemExit) as caught, mock.patch.object(sys, "stderr", io.StringIO()):
            ab.main(["--no-such-option"])
        self.assertEqual(caught.exception.code, 2)


def fake_world(**overrides):
    """A machine where everything is present; tests knock pieces out."""
    present = {"claude", "git", "gh", "glab", "xclip"}
    present -= set(overrides.pop("missing", ()))

    def which(name):
        return f"/usr/bin/{name}" if name in present else None

    def run(cmd, **kw):
        if cmd[1:3] == ["agents", "--json"]:
            return SimpleNamespace(returncode=overrides.get("agents_rc", 0), stdout=overrides.get("agents_out", "[]"), stderr="")
        if cmd[1:] == ["--version"]:
            return SimpleNamespace(returncode=0, stdout="2.1.296 (Claude Code)\n", stderr="")
        if cmd[1:3] == ["auth", "status"]:
            return SimpleNamespace(returncode=overrides.get("auth_rc", 0), stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return which, run


GOOD_ENV = {"TERM": "xterm-256color", "LANG": "en_US.UTF-8", "XDG_SESSION_TYPE": "x11"}


class Doctor(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(ab, "LIMITS_PATH", tmp / "limits.json"),
            mock.patch.object(ab, "META_PATH", tmp / "cfg" / "meta.json"),
            mock.patch.object(ab.prs, "find_skill_script", lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def check(self, env=None, **world):
        which, run = fake_world(**world)
        return {what: (status, detail) for status, what, detail in ab.doctor_checks(run=run, which=which, env=env or GOOD_ENV)}

    def test_a_healthy_machine_has_no_failures(self):
        checks = self.check(agents_out='[{"id": "a"}, {"id": "b"}]')
        self.assertFalse([w for w, (s, _) in checks.items() if s == "fail"])
        self.assertIn("2 sessions", checks["Background sessions"][1])
        self.assertEqual(checks["Claude Code"][0], "ok")

    def test_claude_missing_is_a_failure_with_a_way_forward(self):
        checks = self.check(missing=["claude"])
        status, detail = checks["Claude Code"]
        self.assertEqual(status, "fail")
        self.assertIn("install Claude Code", detail)
        self.assertNotIn("Background sessions", checks)

    def test_an_old_claude_without_agents_json_is_a_failure(self):
        checks = self.check(agents_rc=1)
        self.assertEqual(checks["Background sessions"][0], "fail")
        self.assertIn("update Claude Code", checks["Background sessions"][1])

    def test_garbage_from_agents_json_is_a_failure(self):
        self.assertEqual(self.check(agents_out="not json")["Background sessions"][0], "fail")

    def test_missing_clipboard_tool_is_only_a_warning_with_the_right_install_hint(self):
        checks = self.check(missing=["xclip"])
        self.assertEqual(checks["Clipboard"][0], "warn")
        self.assertIn("sudo apt install xclip", checks["Clipboard"][1])
        wayland = self.check(missing=["xclip"], env={**GOOD_ENV, "XDG_SESSION_TYPE": "wayland"})
        self.assertIn("wl-clipboard", wayland["Clipboard"][1])

    def test_gh_and_glab_are_optional_and_say_how_to_log_in(self):
        checks = self.check(missing=["gh"], auth_rc=1)
        self.assertEqual(checks["gh"][0], "warn")
        self.assertEqual(checks["glab"][0], "warn")
        self.assertIn("glab auth login", checks["glab"][1])
        self.assertEqual(self.check()["gh"][0], "ok")

    def test_terminal_and_locale_problems(self):
        self.assertEqual(self.check(env={"LANG": "en_US.UTF-8"})["Terminal"][0], "fail")
        self.assertEqual(self.check(env={"TERM": "vt100", "LANG": "en_US.UTF-8"})["Terminal"][0], "warn")
        self.assertEqual(self.check(env={"TERM": "xterm-256color", "LANG": "C"})["Unicode"][0], "warn")

    def test_the_limits_mod_is_reported_either_way(self):
        self.assertEqual(self.check()["Account limits"][0], "warn")
        ab.LIMITS_PATH.write_text("{}")
        self.assertEqual(self.check()["Account limits"][0], "ok")

    def test_exit_code_is_nonzero_only_for_real_problems(self):
        out = io.StringIO()
        with mock.patch.object(ab, "doctor_checks", lambda: [("warn", "x", "y")]), mock.patch.object(sys, "stdout", out):
            self.assertEqual(ab.run_doctor(), 0)
        with mock.patch.object(ab, "doctor_checks", lambda: [("fail", "x", "y")]), mock.patch.object(sys, "stdout", out):
            self.assertEqual(ab.run_doctor(), 1)
        self.assertIn("1 problem to fix", out.getvalue())


class SelfUpdate(unittest.TestCase):
    def test_a_copy_that_is_not_a_checkout_explains_how_to_update(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(ab.update_self(here=Path(tempfile.mkdtemp())), 1)
        self.assertIn("install.sh", out.getvalue())

    def test_a_checkout_is_pulled_fast_forward_only(self):
        here = Path(tempfile.mkdtemp())
        (here / ".git").mkdir()
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="Already up to date.\n", stderr="")

        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(ab.update_self(run=run, here=here), 0)
        self.assertEqual(calls[0], ["git", "-C", str(here), "pull", "--ff-only"])

    def test_a_failed_pull_is_reported(self):
        here = Path(tempfile.mkdtemp())
        (here / ".git").mkdir()
        run = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="diverged")
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(ab.update_self(run=run, here=here), 1)


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "needs git and sh")
class Installer(unittest.TestCase):
    """Runs the real install.sh in a throwaway home directory, from a local copy of the project."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.remote = self.home / "remote"
        self.remote.mkdir()
        for module in ROOT.glob("*.py"):
            if not module.name.startswith("test_"):
                shutil.copy(module, self.remote / module.name)  # every module the board imports, so a new one cannot be forgotten
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(self.remote)]
        subprocess.run(["git", "-C", str(self.remote), "init", "-q"], check=True)
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "test"], check=True)
        self.env = {
            "HOME": str(self.home), "PATH": os.environ["PATH"], "AGENT_BOARD_SOURCE": str(self.remote),
            "AGENT_BOARD_CLAUDE": "no-such-claude-for-tests", "TERM": "xterm-256color", "LANG": "en_US.UTF-8",
        }
        self.launcher = self.home / ".local" / "bin" / "agent-board"

    def install(self, *args):
        return subprocess.run(["sh", str(ROOT / "install.sh"), *args], env=self.env, capture_output=True, text=True, timeout=120)

    def test_installs_a_working_command_and_says_how_to_start_it(self):
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(os.access(self.launcher, os.X_OK))
        version = subprocess.run([str(self.launcher), "--version"], env=self.env, capture_output=True, text=True)
        self.assertEqual(version.stdout.strip(), f"agent-board {ab.__version__}")
        self.assertIn("Start it with:   agent-board", done.stdout)
        self.assertIn("agent-board --demo", done.stdout)

    def test_the_installed_command_runs_the_board_demo(self):
        self.install()
        shown = subprocess.run([str(self.launcher), "--dump", "--demo", "--view", "review"], env=self.env, capture_output=True, text=True)
        self.assertIn("review queue", shown.stdout)

    def test_the_end_of_the_install_runs_the_doctor_and_survives_its_problems(self):
        done = self.install()
        self.assertIn("checking your setup", done.stdout)
        self.assertIn("Claude Code", done.stdout)  # reported missing here, yet the install itself succeeded
        self.assertEqual(done.returncode, 0)

    def test_running_it_again_updates_in_place(self):
        self.install()
        again = self.install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("updating the existing copy", again.stdout)

    def test_a_missing_path_entry_is_explained_with_the_exact_line_to_add(self):
        done = self.install()
        self.assertIn("is not on your PATH", done.stderr)
        self.assertIn(f'export PATH="{self.home}/.local/bin:$PATH"', done.stdout)

    def test_no_path_warning_when_the_bin_directory_is_already_on_path(self):
        self.env["PATH"] = f"{self.home}/.local/bin:" + self.env["PATH"]
        self.assertNotIn("is not on your PATH", self.install().stderr)

    def test_uninstall_removes_the_command_and_keeps_your_settings(self):
        self.install()
        config = self.home / ".config" / "agent-board"
        config.mkdir(parents=True, exist_ok=True)
        (config / "meta.json").write_text("{}")
        done = self.install("--uninstall")
        self.assertEqual(done.returncode, 0)
        self.assertFalse(self.launcher.exists())
        self.assertFalse((self.home / ".local" / "share" / "agent-board").exists())
        self.assertTrue((config / "meta.json").exists())

    def test_an_unknown_option_fails_with_a_hint(self):
        done = self.install("--bogus")
        self.assertEqual(done.returncode, 1)
        self.assertIn("--help", done.stderr)

    def test_help_prints_the_usage_comment(self):
        done = self.install("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("--with-limits-mod", done.stdout)

    def test_the_limits_mod_flag_without_claude_is_only_a_warning(self):
        done = self.install("--with-limits-mod")
        self.assertEqual(done.returncode, 0)
        self.assertIn("limits mod was skipped", done.stderr)


class Packaging(unittest.TestCase):
    def test_the_version_in_pyproject_is_read_from_the_module_not_duplicated(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('attr = "agent_board.__version__"', text)
        self.assertIn('agent-board = "agent_board:cli"', text)

    def test_every_module_ships_in_the_wheel(self):
        import re

        listed = set(re.search(r"py-modules = \[([^\]]*)\]", (ROOT / "pyproject.toml").read_text()).group(1).replace('"', "").replace(" ", "").split(","))
        on_disk = {p.stem for p in ROOT.glob("*.py") if not p.name.startswith("test_")}
        self.assertEqual(listed, on_disk, "pyproject.toml py-modules and the modules on disk differ: the wheel would miss one")

    def test_nothing_personal_is_published_no_real_email_addresses_and_no_home_directories(self):
        import re

        email = re.compile(r"(?<![\w.+:-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # not the user:password@ part of a URL
        allowed_domains = {"example.com", "users.noreply.github.com"}  # anything else must be an ssh remote (git@host:path)
        files = [p for pattern in ("*.py", "*.md", "*.sh", "*.toml", "*.json", "*.ts", "*.yml") for p in ROOT.rglob(pattern) if ".git" not in p.parts]
        self.assertTrue(files)
        for path in files:
            text = path.read_text()
            for found in email.findall(text):
                local, _, domain = found.rpartition("@")
                self.assertTrue(local == "git" or domain in allowed_domains, f"{found} in {path.name}")
            self.assertIsNone(re.search(r"/home/[a-z0-9_-]+/", text), f"a home directory path in {path.name}")

if __name__ == "__main__":
    unittest.main()
