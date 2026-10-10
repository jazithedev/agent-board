import json
import tempfile
import unittest
from pathlib import Path

import agent_board as ab
import slash

NOW = 1_800_000_000.0


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def skill(folder: Path, name: str, description: str, extra: str = "") -> None:
    write(folder / name / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n")


class Frontmatter(unittest.TestCase):
    def test_plain_quoted_and_single_quoted_values(self):
        text = '---\nname: thing\ndescription: "Does \\"X\\" well\\nand more"\nother: \'it\'\'s fine\'\n---\nbody'
        meta = slash.parse_frontmatter(text)
        self.assertEqual(meta["name"], "thing")
        self.assertEqual(meta["description"], 'Does "X" well\nand more')
        self.assertEqual(meta["other"], "it's fine")

    def test_folded_and_literal_block_values(self):
        text = "---\nname: a\ndescription: >\n  first part\n  second part\nnext: |\n  line one\n  line two\n---\n"
        meta = slash.parse_frontmatter(text)
        self.assertEqual(meta["description"], "first part second part")
        self.assertEqual(meta["next"], "line one line two")

    def test_a_quoted_value_that_runs_over_several_lines(self):
        meta = slash.parse_frontmatter('---\ndescription: "Triggers include\n  a, b and c"\nname: x\n---\n')
        self.assertEqual(meta["description"], "Triggers include a, b and c")
        self.assertEqual(meta["name"], "x")

    def test_a_plain_value_continued_on_the_next_line(self):
        meta = slash.parse_frontmatter("---\ndescription: starts here\n  and goes on\nname: y\n---\n")
        self.assertEqual(meta["description"], "starts here and goes on")
        self.assertEqual(meta["name"], "y")

    def test_no_frontmatter_means_nothing(self):
        self.assertEqual(slash.parse_frontmatter("# just a title\n"), {})
        self.assertEqual(slash.parse_frontmatter(""), {})


class Reading(unittest.TestCase):
    def test_a_skill_the_user_cannot_invoke_is_left_out(self):
        tmp = Path(tempfile.mkdtemp())
        skill(tmp, "hidden", "only for the model", "user-invocable: false\n")
        skill(tmp, "shown", "for people")
        self.assertEqual([e.name for e in slash._skills_in(tmp, "", "x")], ["shown"])

    def test_the_namespace_is_added_unless_the_name_already_has_one(self):
        tmp = Path(tempfile.mkdtemp())
        skill(tmp, "plain", "d")
        write(tmp / "ns" / "SKILL.md", "---\nname: other:thing\ndescription: d\n---\n")
        self.assertEqual(sorted(e.name for e in slash._skills_in(tmp, "plug", "x")), ["other:thing", "plug:plain"])

    def test_descriptions_are_one_line(self):
        tmp = Path(tempfile.mkdtemp())
        write(tmp / "a" / "SKILL.md", '---\nname: a\ndescription: "line one\\n\\nline   two"\n---\n')
        self.assertEqual(slash._skills_in(tmp, "", "x")[0].description, "line one line two")

    def test_commands_in_subfolders_are_namespaced_by_their_folder(self):
        tmp = Path(tempfile.mkdtemp())
        write(tmp / "top.md", "---\ndescription: top\n---\n")
        write(tmp / "git" / "sync.md", "---\ndescription: nested\n---\n")
        names = {e.name: e for e in slash._commands_in(tmp, "", "command", "x")}
        self.assertEqual(set(names), {"top", "git:sync"})
        self.assertEqual(names["top"].kind, "command")

    def test_an_unreadable_or_broken_file_is_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        write(tmp / "bad" / "SKILL.md", "---\nname: [unclosed\n---\n")
        self.assertEqual(len(slash._skills_in(tmp, "", "x")), 1)  # parsed leniently, never an error
        self.assertEqual(slash._skills_in(tmp / "nope", "", "x"), [])


class Discovery(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.project = Path(tempfile.mkdtemp())
        claude = self.home / ".claude"
        # an installed plugin with a skill and a command, a disabled one, and one installed for another project only
        for plugin in ("tools", "off", "elsewhere"):
            root = claude / "plugins" / "cache" / "mkt" / plugin / "1.0"
            skill(root / "skills", f"{plugin}-skill", "from a plugin")
            write(root / "commands" / "go.md", "---\ndescription: a plugin command\n---\n")
        write(claude / "plugins" / "installed_plugins.json", json.dumps({"version": 2, "plugins": {
            "tools@mkt": [{"scope": "user", "installPath": str(claude / "plugins" / "cache" / "mkt" / "tools" / "1.0")}],
            "off@mkt": [{"scope": "user", "installPath": str(claude / "plugins" / "cache" / "mkt" / "off" / "1.0")}],
            "elsewhere@mkt": [{"scope": "project", "projectPath": "/another/project", "installPath": str(claude / "plugins" / "cache" / "mkt" / "elsewhere" / "1.0")}],
        }}))
        write(claude / "settings.json", json.dumps({"enabledPlugins": {"off@mkt": False, "tools@mkt": True}}))
        # org-synced plugins (the ~g2 suffix is dropped), claude.ai account skills, your own skills and commands
        skill(claude / "plugins" / "synced" / "org" / "team-plugin~g2" / "skills", "analyse", "synced plugin skill")
        skill(claude / "skills" / "synced" / "org", "docs", "account skill")
        skill(claude / "skills", "mine", "my own skill")
        write(claude / "commands" / "ship.md", "---\ndescription: my command\n---\n")
        skill(self.project / ".claude" / "skills", "local", "project skill")
        write(self.project / ".claude" / "commands" / "fix.md", "---\ndescription: project command\n---\n")

    def names(self, **kw):
        return {e.name: e for e in slash.discover(str(self.project), self.home, **kw)}

    def test_every_source_is_read(self):
        got = self.names(builtin=[("help", "Show help")])
        self.assertEqual(set(got), {
            "help", "tools:tools-skill", "tools:go", "team-plugin:analyse", "anthropic-skills:docs", "mine", "ship", "local", "fix"})

    def test_kinds_follow_what_claude_agents_shows(self):
        got = self.names(builtin=[("help", "Show help")])
        self.assertEqual({k: v.kind for k, v in got.items()}, {
            "help": "command", "tools:tools-skill": "skill", "tools:go": "skill", "team-plugin:analyse": "skill",
            "anthropic-skills:docs": "skill", "mine": "skill", "ship": "command", "local": "skill", "fix": "command"})

    def test_a_disabled_plugin_and_one_installed_for_another_project_are_not_offered(self):
        got = self.names()
        self.assertFalse([n for n in got if n.startswith(("off:", "elsewhere:"))])

    def test_a_project_scoped_plugin_is_offered_in_its_own_project(self):
        claude = self.home / ".claude" / "plugins" / "installed_plugins.json"
        data = json.loads(claude.read_text())
        data["plugins"]["elsewhere@mkt"][0]["projectPath"] = str(self.project)
        claude.write_text(json.dumps(data))
        self.assertIn("elsewhere:elsewhere-skill", self.names())

    def test_a_project_setting_can_switch_a_plugin_off(self):
        write(self.project / ".claude" / "settings.local.json", json.dumps({"enabledPlugins": {"tools@mkt": False}}))
        self.assertNotIn("tools:tools-skill", self.names())

    def test_the_project_wins_over_you_and_you_win_over_the_built_ins(self):
        skill(self.home / ".claude" / "skills", "compact", "my own compact")
        skill(self.project / ".claude" / "skills", "compact", "the project's compact")
        got = self.names(builtin=[("compact", "built in")])
        self.assertEqual(got["compact"].description, "the project's compact")

    def test_the_built_in_snapshot_is_included_by_default_and_sorted(self):
        entries = slash.discover(str(self.project), self.home)
        names = [e.name for e in entries]
        for expected in ("help", "clear", "compact", "init", "simplify"):
            self.assertIn(expected, names)
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_a_machine_with_nothing_set_up_still_offers_the_built_ins(self):
        entries = slash.discover("/no/such/project", Path(tempfile.mkdtemp()))
        self.assertGreater(len(entries), 60)

    def test_it_is_fast_enough_to_run_when_you_press_slash(self):
        import time

        started = time.time()
        slash.discover(str(self.project), self.home)
        self.assertLess(time.time() - started, 0.5)


def entries(*names):
    return [slash.SlashEntry(n, "skill", f"about {n}") for n in names]


class Matching(unittest.TestCase):
    ALL = entries("chrome-devtools-mcp:debug-optimize-lcp", "clean-coder", "code-review:code-review", "legacy-code-expert",
                  "add-dir", "superpowers:receiving-code-review", "decode", "help")

    def names(self, query):
        return [e.name for e in slash.match(self.ALL, query)]

    def test_nothing_typed_lists_everything_alphabetically(self):
        got = self.names("")
        self.assertEqual(got, sorted(got, key=str.lower))
        self.assertEqual(len(got), len(self.ALL))

    def test_names_that_start_with_it_come_first_then_names_with_a_word_that_does(self):
        self.assertEqual(self.names("cod"), ["code-review:code-review", "clean-coder", "legacy-code-expert", "superpowers:receiving-code-review", "decode"])

    def test_the_real_cod_case_chrome_devtools_is_not_a_match(self):
        self.assertNotIn("chrome-devtools-mcp:debug-optimize-lcp", self.names("cod"))

    def test_a_match_inside_a_word_comes_after_the_ones_that_start_with_it(self):
        self.assertEqual(self.names("eco"), ["decode"])  # no word starts with it: it only occurs inside one
        both = [e.name for e in slash.match(entries("xcode", "code-x", "a-code"), "cod")]
        self.assertEqual(both, ["code-x", "a-code", "xcode"])  # prefix, then word start, then inside a word

    def test_case_does_not_matter(self):
        self.assertEqual(self.names("HELP"), ["help"])

    def test_a_namespace_prefix_matches(self):
        self.assertEqual(self.names("code-review:"), ["code-review:code-review"])

    def test_nothing_matches_gives_an_empty_list(self):
        self.assertEqual(self.names("zzz"), [])


class CatalogCache(unittest.TestCase):
    def test_discovers_once_per_project_and_again_after_a_minute(self):
        calls, clock = [], [0.0]

        def loader(project):
            calls.append(project)
            return entries("a")

        catalog = slash.Catalog(loader, now=lambda: clock[0])
        catalog.entries("/p1")
        catalog.entries("/p1")
        catalog.entries("/p2")
        self.assertEqual(calls, ["/p1", "/p2"])
        clock[0] += slash.CACHE_SECONDS + 1
        catalog.entries("/p1")
        self.assertEqual(calls, ["/p1", "/p2", "/p1"])


def board(*names):
    b = ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/p", now=lambda: NOW)
    b.set_sessions([ab.Session(id="id1", name="alpha", state="done", project="p", project_path="/srv/www/p")])
    b.slash_catalog = slash.Catalog(lambda project: entries(*(names or ("add-dir", "clean-coder", "code-review:code-review", "help", "init"))))
    return b


def type_(b, text):
    for c in text:
        b.handle("space" if c == " " else "char", c)


class InThePrompt(unittest.TestCase):
    def test_a_slash_opens_the_list_with_everything_and_the_first_entry_highlighted(self):
        b = board()
        type_(b, "/")
        matches, idx = b.slash_view()
        self.assertEqual([e.name for e in matches], ["add-dir", "clean-coder", "code-review:code-review", "help", "init"])
        self.assertEqual(idx, 0)

    def test_typing_filters_and_resets_the_highlight(self):
        b = board()
        type_(b, "/")
        b.handle("down")
        b.handle("down")
        type_(b, "c")
        matches, idx = b.slash_view()
        self.assertEqual([e.name for e in matches], ["clean-coder", "code-review:code-review"])
        self.assertEqual(idx, 0)

    def test_arrows_move_the_highlight_and_stop_at_the_ends(self):
        b = board()
        type_(b, "/")
        for _ in range(10):
            b.handle("down")
        self.assertEqual(b.slash_view()[1], 4)
        for _ in range(10):
            b.handle("up")
        self.assertEqual(b.slash_view()[1], 0)

    def test_the_arrows_do_not_leave_the_prompt_for_the_session_list_while_it_is_open(self):
        b = board()
        type_(b, "/")
        b.handle("down")
        self.assertIsNone(b.sel)
        b.handle("esc")
        b.handle("down")  # the list is closed again, so ↓ goes to the sessions as before
        self.assertEqual(b.sel, "id1")

    def test_tab_completes_the_highlighted_command_and_adds_a_space_for_its_arguments(self):
        b = board()
        type_(b, "/cod")
        b.handle("tab")
        self.assertEqual(b.input, "/code-review:code-review ")
        self.assertIsNone(b.slash_query())  # the list closes once there is a space

    def test_tab_completes_whichever_entry_the_arrows_chose(self):
        b = board()
        type_(b, "/c")
        b.handle("down")
        b.handle("tab")
        self.assertEqual(b.input, "/code-review:code-review ")

    def test_enter_completes_instead_of_starting_a_session_with_a_half_typed_command(self):
        b = board()
        type_(b, "/cl")
        self.assertIsNone(b.handle("enter"))
        self.assertEqual(b.input, "/clean-coder ")

    def test_enter_on_a_command_typed_out_in_full_starts_the_session(self):
        b = board()
        type_(b, "/help")
        effect = b.handle("enter")
        self.assertEqual((effect.kind, effect.prompt), ("dispatch", "/help"))

    def test_after_completing_enter_sends_the_command_with_what_you_typed_after_it(self):
        b = board()
        type_(b, "/cl")
        b.handle("tab")
        type_(b, "tidy the report")
        effect = b.handle("enter")
        self.assertEqual(effect.prompt, "/clean-coder tidy the report")

    def test_escape_clears_the_prompt_and_closes_the_list(self):
        b = board()
        type_(b, "/co")
        b.handle("esc")
        self.assertEqual(b.input, "")
        self.assertIsNone(b.slash_query())

    def test_a_space_closes_it_and_a_path_or_unknown_word_never_opens_it(self):
        b = board()
        type_(b, "/help x")
        self.assertIsNone(b.slash_query())
        b2 = board()
        type_(b2, "/srv/www/x")
        self.assertEqual(b2.slash_view()[0], [])  # nothing starts like that: no list, enter just sends it
        effect = b2.handle("enter")
        self.assertEqual(effect.prompt, "/srv/www/x")

    def test_only_a_leading_slash_counts(self):
        b = board()
        type_(b, "fix /help")
        self.assertIsNone(b.slash_query())
        b2 = board()
        type_(b2, "a/")
        self.assertIsNone(b2.slash_query())

    def test_it_stays_closed_when_the_focus_is_not_on_the_prompt(self):
        b = board()
        type_(b, "/")
        b.sel = "id1"  # a session is selected
        self.assertIsNone(b.slash_query())
        b.sel = None
        b.mode = "review"
        self.assertIsNone(b.slash_query())
        b.mode = "status"
        b.handle("ctrl+p")  # a popup is open
        self.assertIsNone(b.slash_query())

    def test_it_looks_for_commands_in_the_project_new_sessions_start_in(self):
        asked = []
        b = board()
        b.slash_catalog = slash.Catalog(lambda project: asked.append(project) or entries("x"))
        type_(b, "/")
        b.slash_view()
        self.assertEqual(asked, ["/srv/www/p"])

    def test_deleting_back_to_nothing_closes_the_list(self):
        b = board()
        type_(b, "/h")
        b.handle("backspace")
        b.handle("backspace")
        self.assertIsNone(b.slash_query())
        self.assertEqual(b.input, "")


class Drawing(unittest.TestCase):
    def screen(self, b, width=100, height=24):
        return [ab.plain(l) for l in ab.compose(b, width, height)[0]]

    def test_the_list_sits_just_above_the_prompt_with_a_marker_on_the_highlighted_entry(self):
        b = board()
        type_(b, "/c")
        b.handle("down")
        lines = self.screen(b)
        prompt = next(i for i, l in enumerate(lines) if "❯ /c" in l and "in p" in l or l.startswith(" ❯ /c"))
        rule = prompt - 1
        self.assertTrue(lines[rule].startswith("─"))
        self.assertIn("/clean-coder", lines[rule - 2])
        self.assertTrue(lines[rule - 1].startswith("❯ /code-review:code-review"))
        self.assertIn("skill · about code-review:code-review", lines[rule - 1])

    def test_the_highlighted_row_is_drawn_selected_and_the_others_are_not(self):
        b = board()
        type_(b, "/")
        rows = [l for l in ab.compose(b, 100, 24)[0] if "skill ·" in ab.plain(l)]
        self.assertEqual([l.selected for l in rows], [True, False, False, False, False])

    def test_the_typed_text_stays_on_the_prompt_line_and_the_hint_counts_the_commands(self):
        b = board()
        type_(b, "/c")
        text = "\n".join(self.screen(b))
        self.assertIn(" ❯ /c", text)
        self.assertIn("2 commands", text)
        self.assertIn("tab or enter completes", text)

    def test_a_long_name_keeps_its_tail_after_an_ellipsis(self):
        b = board("superpowers:requesting-code-review-and-more-things")
        type_(b, "/")
        line = next(l for l in self.screen(b) if "skill ·" in l)
        self.assertIn("…", line)
        self.assertIn("more-things", line)

    def test_at_most_ten_rows_and_the_highlighted_one_stays_in_the_window(self):
        b = board(*[f"cmd-{i:02d}" for i in range(40)])
        type_(b, "/")
        for _ in range(25):
            b.handle("down")
        lines = self.screen(b, height=30)
        rows = [l for l in lines if "skill ·" in l]
        self.assertEqual(len(rows), 10)
        self.assertTrue(any(l.startswith("❯ /cmd-25") for l in rows))

    def test_a_short_screen_gets_a_shorter_list_not_a_broken_screen(self):
        b = board(*[f"cmd-{i:02d}" for i in range(40)])
        type_(b, "/")
        lines = ab.compose(b, 100, 12)[0]
        self.assertLessEqual(len(lines), 12)
        self.assertTrue(any("❯ /cmd-00" in ab.plain(l) for l in lines))

    def test_every_width_fits(self):
        b = board("a-very-long-namespace:an-even-longer-skill-name-than-usual", "help")
        type_(b, "/")
        for width in (50, 70, 100, 140):
            for line in ab.compose(b, width, 24)[0]:
                self.assertLessEqual(ab.seg_width(line.segs), width, ab.plain(line))

    def test_it_covers_the_panels_but_never_the_prompt_or_the_title(self):
        b = board()
        b.usage, b.limits, b.disk = ab.demo_usage(), ab.demo_limits(NOW), ab.demo_disk()
        type_(b, "/")
        lines = self.screen(b, height=36)
        self.assertIn("Agent Board", lines[1])
        self.assertTrue(any(l.startswith(" ❯ /") for l in lines))

    def test_no_list_when_nothing_matches(self):
        b = board()
        type_(b, "/zzz")
        self.assertFalse(any("skill ·" in l for l in self.screen(b)))


if __name__ == "__main__":
    unittest.main()
