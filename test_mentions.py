import tempfile
import unittest
from pathlib import Path

import agent_board as ab

NOW = 1_800_000_000.0


def board(known=()):
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/here", now=lambda: NOW)
    b.set_sessions([
        ab.Session(id="id1", name="alpha", state="done", project="web-app", project_path="/srv/www/web-app", updated_at=NOW),
        ab.Session(id="id2", name="beta", state="working", project="mobile-app", project_path="/srv/www/mobile-app", updated_at=NOW - 5),
    ])
    b.known_projects_fn = lambda: list(known)
    return b


def type_(b, text):
    for c in text:
        b.handle("space" if c == " " else "char", c)


class TheList(unittest.TestCase):
    def test_an_at_sign_lists_projects_with_sessions_first(self):
        extra = Path(tempfile.mkdtemp()) / "extra"
        extra.mkdir()
        b = board([("extra", str(extra))])
        type_(b, "@")
        names = [m[0] for m in b.mention_view()[0]]
        self.assertEqual(names, ["here", "web-app", "mobile-app", "extra"])
        self.assertEqual(b.mention_view()[0][1][2:], (1, 0))

    def test_typing_filters_by_name_start_then_name_then_path(self):
        b = board()
        type_(b, "@app")
        self.assertEqual([m[0] for m in b.mention_view()[0]], ["web-app", "mobile-app"])
        b2 = board()
        type_(b2, "@mo")
        self.assertEqual([m[0] for m in b2.mention_view()[0]], ["mobile-app"])
        b3 = board()
        type_(b3, "@www")  # only the path says www
        self.assertEqual(len(b3.mention_view()[0]), 3)  # here, web-app, mobile-app all live under /srv/www

    def test_it_opens_after_other_text_too_but_not_inside_a_word(self):
        b = board()
        type_(b, "fix the login in @web")
        self.assertEqual(b.mention_query(), "web")
        b2 = board()
        type_(b2, "mail me@web")
        self.assertIsNone(b2.mention_query())

    def test_it_stays_shut_with_the_focus_elsewhere_and_without_the_caret_at_the_end(self):
        b = board()
        type_(b, "@w")
        b.handle("left")
        self.assertIsNone(b.mention_query())
        b2 = board()
        type_(b2, "@w")
        b2.handle("ctrl+p")
        self.assertIsNone(b2.mention_query())

    def test_arrows_choose_and_tab_completes_just_that_word(self):
        b = board()
        type_(b, "do it in @app")
        b.handle("down")
        b.handle("tab")
        self.assertEqual(b.input, "do it in @mobile-app ")

    def test_enter_on_a_half_typed_name_completes_and_a_full_name_sends(self):
        b = board()
        type_(b, "@web")
        self.assertIsNone(b.handle("enter"))
        self.assertEqual(b.input, "@web-app ")
        type_(b, "fix it")
        self.assertEqual(b.handle("enter").kind, "dispatch")

    def test_the_overlay_and_hint_show(self):
        b = board()
        type_(b, "@")
        lines, _ = ab.compose(b, 120, 30)
        text = "\n".join(ab.plain(l) for l in lines)
        self.assertIn("@web-app", text)
        self.assertIn("1 session", text)
        self.assertIn("the session starts there", text)


class StartingThere(unittest.TestCase):
    def test_the_session_starts_in_the_mentioned_project_and_the_mention_is_not_sent(self):
        b = board()
        type_(b, "@web-app fix the login")
        effect = b.handle("enter")
        self.assertEqual((effect.kind, effect.cwd, effect.prompt), ("dispatch", "/srv/www/web-app", "fix the login"))

    def test_a_mention_in_the_middle_or_the_end_works(self):
        b = board()
        type_(b, "fix the login in @mobile-app now")
        effect = b.handle("enter")
        self.assertEqual((effect.cwd, effect.prompt), ("/srv/www/mobile-app", "fix the login in now"))
        b2 = board()
        type_(b2, "fix the login @mobile-app")
        effect = b2.handle("enter")
        self.assertEqual((effect.cwd, effect.prompt), ("/srv/www/mobile-app", "fix the login"))

    def test_it_is_a_one_off_the_chosen_project_stays(self):
        b = board()
        type_(b, "@web-app one")
        b.handle("enter")
        type_(b, "two")
        effect = b.handle("enter")
        self.assertEqual(effect.cwd, "/srv/www/here")

    def test_unknown_at_words_are_left_alone(self):
        b = board()
        type_(b, "ask @alice about it")
        effect = b.handle("enter")
        self.assertEqual((effect.cwd, effect.prompt), ("/srv/www/here", "ask @alice about it"))

    def test_a_mention_without_a_task_does_not_start_anything(self):
        b = board()
        type_(b, "@web-app ")
        self.assertIsNone(b.handle("enter"))
        self.assertIn("write the task", b.msg)
        self.assertEqual(b.input, "@web-app ")

    def test_names_with_spaces_are_written_with_dashes(self):
        b = board()
        b.set_sessions([ab.Session(id="id3", name="x", state="done", project="my app", project_path="/srv/www/my app", updated_at=NOW)])
        type_(b, "@my-")
        b.handle("tab")
        self.assertEqual(b.input, "@my-app ")
        type_(b, "go")
        self.assertEqual(b.handle("enter").cwd, "/srv/www/my app")

    def test_pasted_text_is_never_searched_for_mentions(self):
        b = board()
        b.handle("paste", "@web-app " + "x" * 900)
        type_(b, " review")
        effect = b.handle("enter")
        self.assertEqual(effect.cwd, "/srv/www/here")
        self.assertTrue(effect.prompt.startswith("@web-app xxx"))

    def test_the_prompt_line_names_the_project_it_will_start_in(self):
        b = board()
        type_(b, "go @mobile-app ")
        self.assertEqual(b.start_target()[1], "/srv/www/mobile-app")
        lines, _ = ab.compose(b, 120, 30)
        self.assertTrue(any("in mobile-app" in ab.plain(l) for l in lines))


if __name__ == "__main__":
    unittest.main()
