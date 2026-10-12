import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pullrequests as pr

NOW = 1_800_000_000.0


class Remotes(unittest.TestCase):
    def test_parses_ssh_and_https_remotes(self):
        self.assertEqual(pr.parse_remote("git@github.com:octo/live-chat.git"), pr.Remote("github", "octo/live-chat"))
        self.assertEqual(pr.parse_remote("https://github.com/Acme/Tools"), pr.Remote("github", "Acme/Tools"))
        self.assertEqual(pr.parse_remote("git@gitlab.com:scum-map/scum-map.git"), pr.Remote("gitlab", "scum-map/scum-map"))
        self.assertEqual(pr.parse_remote("https://user:tok@gitlab.com/a/b/c.git").repo, "a/b/c")

    def test_unknown_hosts_are_ignored(self):
        self.assertIsNone(pr.parse_remote("git@bitbucket.org:x/y.git"))
        self.assertIsNone(pr.parse_remote("not a url"))

    def test_resolve_remote_asks_git_in_the_project_folder(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return SimpleNamespace(returncode=0, stdout="git@github.com:o/r.git\n")

        self.assertEqual(pr.resolve_remote("/srv/www/x", run), pr.Remote("github", "o/r"))
        self.assertEqual(seen[0][:3], ["git", "-C", "/srv/www/x"])
        self.assertIsNone(pr.resolve_remote("/x", lambda *a, **k: SimpleNamespace(returncode=128, stdout="")))


def mine(**kw):
    base = dict(provider="github", repo="o/r", number=1, title="t", url="u", decision="none", merge="clean")
    base.update(kw)
    return pr.MyPr(**base)


class Classification(unittest.TestCase):
    def test_each_attention_reason_is_reported(self):
        self.assertEqual(mine(merge="conflict").attention, ["conflicts"])
        self.assertEqual(mine(decision="changes").attention, ["changes requested"])
        self.assertEqual(mine(checks="fail").attention, ["checks failing"])
        self.assertEqual(mine(unresolved=True).attention, ["unresolved threads"])

    def test_checks_left_over_from_before_a_conflict_are_not_a_verdict(self):
        self.assertEqual(mine(merge="conflict", checks="fail", checks_stale=True).attention, ["conflicts"])

    def test_groups(self):
        self.assertEqual(mine(merge="conflict").group, "attention")
        self.assertEqual(mine(decision="approved").group, "ready")
        self.assertEqual(mine(decision="approved", merge="conflict").group, "attention")
        self.assertEqual(mine(decision="required").group, "waiting")

    def test_waiting_notes(self):
        self.assertIn("nobody asked", mine().notes)
        self.assertNotIn("nobody asked", mine(requested=("maria",)).notes)
        self.assertNotIn("nobody asked", mine(draft=True).notes)
        self.assertIn("draft", mine(draft=True).notes)
        self.assertIn("behind base", mine(decision="approved", merge="behind").notes)


class Normalising(unittest.TestCase):
    def test_github_my_prs_json(self):
        payload = {"prs": [{
            "repo": "o/r", "number": 7, "title": "T", "url": "https://x/7", "base": "main", "head": "WEB-1-x", "parent": None, "depth": 0,
            "ticket": "WEB-1", "is_draft": False, "decision": "CHANGES_REQUESTED", "reviews": {"maria": "CHANGES_REQUESTED"},
            "requested": ["tom"], "checks": "FAILURE", "checks_stale": False, "merge_state": "DIRTY",
            "created_at": "2026-10-01T10:00:00Z", "updated_at": "2026-10-08T10:00:00Z"}]}
        [p] = pr.github_my_prs(payload)
        self.assertEqual((p.decision, p.merge, p.checks, p.ticket, p.reviewed, p.requested), ("changes", "conflict", "fail", "WEB-1", True, ("tom",)))
        self.assertEqual(p.attention, ["conflicts", "changes requested", "checks failing"])

    def test_review_search_items_drop_drafts_unless_asked(self):
        items = [
            {"number": 1, "title": "a", "html_url": "u1", "repository_url": "https://api.github.com/repos/o/r", "user": "maria", "created_at": "2026-10-01T00:00:00Z", "draft": False},
            {"number": 2, "title": "b", "html_url": "u2", "repository_url": "https://api.github.com/repos/o/r", "user": "tom", "created_at": "2026-10-02T00:00:00Z", "draft": True},
        ]
        self.assertEqual([p.number for p in pr.github_review_from_search(items, include_drafts=False)], [1])
        both = pr.github_review_from_search(items, include_drafts=True)
        self.assertEqual([(p.number, p.repo, p.author, p.draft) for p in both], [(1, "o/r", "maria", False), (2, "o/r", "tom", True)])

    def test_gitlab_merge_request_mapping(self):
        mr = {"iid": 12, "title": "Fix", "web_url": "https://gitlab.com/g/p/-/merge_requests/12", "source_branch": "fix", "target_branch": "master",
              "draft": False, "references": {"full": "g/p!12"}, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z", "reviewers": [{"username": "maria"}]}
        detail = {"has_conflicts": False, "detailed_merge_status": "mergeable", "head_pipeline": {"status": "failed"}, "blocking_discussions_resolved": False}
        p = pr.gitlab_my_pr(mr, detail, {"approved": True, "approved_by": [{"user": {"username": "maria"}}]})
        self.assertEqual((p.repo, p.number, p.merge, p.checks, p.decision, p.unresolved, p.requested), ("g/p", 12, "clean", "fail", "approved", True, ("maria",)))
        self.assertEqual(p.attention, ["checks failing", "unresolved threads"])

    def test_gitlab_conflict_wins_over_status_text(self):
        mr = {"iid": 1, "references": {"full": "g/p!1"}}
        self.assertEqual(pr.gitlab_my_pr(mr, {"has_conflicts": True, "detailed_merge_status": "mergeable"}, {}).merge, "conflict")

    def test_stacks_are_linked_by_branch_where_the_source_gave_none(self):
        a = mine(provider="gitlab", repo="g/p", number=1, head="a", base="master")
        b = mine(provider="gitlab", repo="g/p", number=2, head="b", base="a")
        c = mine(provider="gitlab", repo="g/p", number=3, head="c", base="b")
        out = {p.number: (p.parent, p.depth) for p in pr.link_stacks([a, b, c])}
        self.assertEqual(out, {1: (None, 0), 2: (1, 1), 3: (2, 2)})

    def test_script_lookup_takes_the_newest_installed_version(self):
        root = Path(tempfile.mkdtemp())
        for v in ("1.2.0", "1.10.0", "1.9.3"):
            f = root / "mkt" / "pull-request" / v / "skills" / "my-prs-list" / "scripts" / "my_prs_list.py"
            f.parent.mkdir(parents=True)
            f.write_text("")
        self.assertIn("1.10.0", str(pr.find_skill_script("my-prs-list", "my_prs_list.py", root)))
        self.assertIsNone(pr.find_skill_script("other-skill", "my_prs_list.py", root))


class FakeWorld:
    """Stands in for gh, glab and the plugin scripts; records every command."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.my_prs = {"prs": []}
        self.search = []
        self.gl_mine = []
        self.gl_review = []
        self.gl_approvals = {}

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        key = "gh" if cmd[0] == "gh" else "glab" if cmd[0] == "glab" else Path(cmd[1]).name if cmd[0] == "python3" else cmd[0]
        if key in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"{key} exploded\n")
        ok = lambda data: SimpleNamespace(returncode=0, stdout=data if isinstance(data, str) else json.dumps(data), stderr="")
        if key == "my_prs_list.py":
            return ok(self.my_prs)
        if key == "gh":
            return ok("\n".join(json.dumps(i) for i in self.search))
        endpoint = cmd[-1]
        if endpoint == "user":
            return ok({"username": "me"})
        if endpoint.startswith("merge_requests?scope=created_by_me"):
            return ok(self.gl_mine)
        if endpoint.startswith("merge_requests?scope=all"):
            return ok(self.gl_review)
        if endpoint.endswith("/approvals"):
            return ok(self.gl_approvals.get(endpoint, {}))
        return ok({"has_conflicts": False, "detailed_merge_status": "mergeable", "head_pipeline": {"status": "success"}, "blocking_discussions_resolved": True})


def cache_with_scripts():
    root = Path(tempfile.mkdtemp())
    for skill, name in (("my-prs-list", "my_prs_list.py"),):
        f = root / "m" / "pull-request" / "1.2.0" / "skills" / skill / "scripts" / name
        f.parent.mkdir(parents=True)
        f.write_text("")
    return root


def my_json(repo, number, **kw):
    d = {"repo": repo, "number": number, "title": f"PR {number}", "url": f"https://github.com/{repo}/pull/{number}", "decision": None, "merge_state": "CLEAN",
         "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z"}
    d.update(kw)
    return d


def search_item(repo, number, **kw):
    d = {"number": number, "title": f"R{number}", "html_url": f"https://github.com/{repo}/pull/{number}",
         "repository_url": f"https://api.github.com/repos/{repo}", "user": "maria", "created_at": "2026-10-01T00:00:00Z", "draft": False}
    d.update(kw)
    return d


class Fetching(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.cache = cache_with_scripts()
        self.projects = {"/srv/www/live-chat": pr.Remote("github", "octo/live-chat"), "/srv/www/scum-map": pr.Remote("gitlab", "scum-map/scum-map")}

    def fetch(self, **settings):
        return pr.fetch_all(self.projects, settings, self.world, NOW, cache=self.cache, home="/home/x")

    def test_only_repositories_of_known_projects_are_kept(self):
        self.world.my_prs = {"prs": [my_json("octo/live-chat", 1), my_json("Acme/Tools", 2)]}
        self.world.search = [search_item("octo/live-chat", 5), search_item("Acme/Tools", 6)]
        data = self.fetch()
        self.assertEqual([p.number for p in data.mine], [1])
        self.assertEqual([p.number for p in data.review], [5])
        self.assertEqual(data.errors, [])

    def test_github_is_not_queried_when_no_project_is_on_github(self):
        self.projects = {"/srv/www/scum-map": pr.Remote("gitlab", "scum-map/scum-map")}
        self.fetch()
        self.assertFalse(any(c[0] in ("gh", "python3") for c in self.world.calls))

    def test_the_review_queue_never_runs_the_to_review_list_script(self):
        self.world.search = [search_item("octo/live-chat", 5)]
        data = self.fetch(review_teams={"octo": {"include": ["core"], "exclude": ["other"]}})  # left over from older versions: ignored
        self.assertEqual([p.number for p in data.review], [5])
        self.assertFalse(any("to_review_list" in " ".join(c) for c in self.world.calls))

    def test_drafts_are_hidden_from_the_review_queue_by_default(self):
        self.world.search = [search_item("octo/live-chat", 5, draft=True)]
        self.assertEqual(self.fetch().review, [])
        self.assertEqual(len(self.fetch(include_drafts=True).review), 1)

    def test_a_failing_source_is_reported_without_losing_the_others(self):
        self.world.fail.add("my_prs_list.py")
        self.world.search = [search_item("octo/live-chat", 5)]
        data = self.fetch()
        self.assertEqual([p.number for p in data.review], [5])
        self.assertEqual(len(data.errors), 1)
        self.assertIn("your PRs", data.errors[0])
        self.assertIn("exploded", data.errors[0])

    def test_missing_plugin_is_explained(self):
        self.cache = Path(tempfile.mkdtemp())
        self.assertTrue(any("plugin was not found" in e for e in self.fetch().errors))

    def test_gitlab_my_merge_requests_and_review_queue(self):
        mr = lambda iid, **kw: {"iid": iid, "project_id": 7, "title": f"MR {iid}", "web_url": f"https://gitlab.com/scum-map/scum-map/-/merge_requests/{iid}",
                                "references": {"full": f"scum-map/scum-map!{iid}"}, "source_branch": f"b{iid}", "target_branch": "master", "draft": False,
                                "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z", "author": {"username": "dev"}, **kw}
        self.world.gl_mine = [mr(1), {**mr(2), "references": {"full": "someone/else!2"}}]
        self.world.gl_review = [mr(10), mr(11), mr(12, draft=True)]
        self.world.gl_approvals = {"projects/7/merge_requests/11/approvals": {"approved_by": [{"user": {"username": "me"}}]}}
        data = self.fetch()
        self.assertEqual([p.number for p in data.mine if p.provider == "gitlab"], [1])
        self.assertEqual([p.number for p in data.review if p.provider == "gitlab"], [10])  # 11 already approved by you, 12 is a draft
        self.assertTrue(all(c[:4] == ["glab", "api", "--hostname", "gitlab.com"] for c in self.world.calls if c[0] == "glab"))

    def test_gitlab_failure_is_reported(self):
        self.world.fail.add("glab")
        errors = self.fetch().errors
        self.assertTrue(any(e.startswith("GitLab (your MRs)") for e in errors))
        self.assertTrue(any(e.startswith("GitLab (review queue)") for e in errors))


if __name__ == "__main__":
    unittest.main()


class Approving(unittest.TestCase):
    def run_with(self, returncode=0, stderr=""):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

        return seen, run

    def test_a_github_pr_is_approved_with_gh(self):
        seen, run = self.run_with()
        p = pr.ReviewPr("github", "acme/billing-api", 482, "Refactor", "https://github.com/acme/billing-api/pull/482")
        self.assertIsNone(pr.approve(p, run))
        self.assertEqual(seen, [["gh", "pr", "review", "482", "--repo", "acme/billing-api", "--approve"]])

    def test_a_gitlab_mr_is_approved_with_glab(self):
        seen, run = self.run_with()
        p = pr.ReviewPr("gitlab", "scum-map/scum-map", 341, "Map", "https://gitlab.com/scum-map/scum-map/-/merge_requests/341")
        self.assertIsNone(pr.approve(p, run))
        self.assertEqual(seen, [["glab", "mr", "approve", "341", "--repo", f"https://{pr.GITLAB_HOST}/scum-map/scum-map"]])

    def test_a_refusal_comes_back_as_its_first_line(self):
        _, run = self.run_with(1, "failed to create review: Can not approve your own pull request\nmore")
        p = pr.ReviewPr("github", "acme/billing-api", 482, "Refactor", "u")
        self.assertEqual(pr.approve(p, run), "failed to create review: Can not approve your own pull request")

    def test_a_missing_cli_is_reported_not_raised(self):
        def run(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        self.assertEqual(pr.approve(pr.ReviewPr("github", "a/b", 1, "t", "u"), run), "gh is not installed")
