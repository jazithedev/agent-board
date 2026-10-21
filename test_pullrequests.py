import json
import time
import unittest
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
        self.assertEqual(mine(draft=True).group, "draft")
        self.assertEqual(mine(draft=True, merge="conflict", decision="approved").group, "draft")

    def test_waiting_notes(self):
        self.assertIn("nobody asked", mine().notes)
        self.assertNotIn("nobody asked", mine(requested=("maria",)).notes)
        self.assertNotIn("nobody asked", mine(draft=True).notes)
        self.assertNotIn("draft", mine(draft=True).notes)  # the Draft section already says so
        self.assertIn("behind base", mine(decision="approved", merge="behind").notes)


class Normalising(unittest.TestCase):
    def test_github_my_pr_node(self):
        node = mine_node("o/r", 7, headRefName="web-1-x", baseRefName="main", reviewDecision="CHANGES_REQUESTED", mergeStateStatus="DIRTY",
                         reviewRequests={"nodes": [{"requestedReviewer": {"login": "tom"}}, {"requestedReviewer": {"name": "Core"}}]},
                         latestReviews={"nodes": [{"state": "CHANGES_REQUESTED", "author": {"__typename": "User", "login": "maria"}}]},
                         commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]})
        p = pr.github_my_pr(node)
        self.assertEqual((p.repo, p.number, p.base, p.head), ("o/r", 7, "main", "web-1-x"))
        self.assertEqual((p.decision, p.merge, p.checks, p.checks_stale, p.ticket, p.reviewed, p.requested),
                         ("changes", "conflict", "fail", True, "WEB-1", True, ("tom", "Core")))
        self.assertEqual(p.attention, ["conflicts", "changes requested"])  # failing checks behind a conflict are an old result

    def test_ticket_comes_from_the_branch_then_the_title(self):
        self.assertEqual(pr.ticket_of("bi-5008-split", "BI-1 elsewhere"), "BI-5008")
        self.assertEqual(pr.ticket_of("fix-it", "BI-1234 fix it"), "BI-1234")
        self.assertEqual(pr.ticket_of("fix-it", "fix it"), "")

    def test_a_pr_only_a_bot_reviewed_counts_as_not_reviewed(self):
        node = mine_node("o/r", 1, latestReviews={"nodes": [{"state": "COMMENTED", "author": {"__typename": "Bot", "login": "copilot-pull-request-reviewer"}}]})
        self.assertFalse(pr.github_my_pr(node).reviewed)
        self.assertIn("nobody asked", pr.github_my_pr(node).notes)


class ReviewFlags(unittest.TestCase):
    def flags(self, requested=(), **reviews):
        node = review_node("o/r", 1, author="dev", requested=requested, reviews=reviews)
        return pr.github_review_pr(node, "me").flags

    def test_new_when_nobody_reviewed(self):
        self.assertEqual(self.flags(requested=("me",)), ("new",))

    def test_re_requested_after_you_reviewed(self):
        self.assertEqual(self.flags(requested=("me",), me="COMMENTED"), ("re-requested",))

    def test_a_team_request_after_your_review_is_not_a_re_request(self):
        self.assertEqual(self.flags(requested=("Core",), me="APPROVED"), ("new",))

    def test_teammates_approving_and_asking_for_changes(self):
        self.assertEqual(self.flags(requested=("me",), maria="APPROVED", tom="CHANGES_REQUESTED"), ("teammate-changes-requested", "teammate-approved"))

    def test_the_author_and_bots_are_not_teammates(self):
        node = review_node("o/r", 1, author="dev", requested=("me",), reviews={"dev": "COMMENTED"})
        node["latestReviews"]["nodes"].append({"state": "APPROVED", "author": {"__typename": "Bot", "login": "copilot-pull-request-reviewer"}})
        node["latestReviews"]["nodes"].append({"state": "APPROVED", "author": {"__typename": "User", "login": "renovate[bot]"}})
        self.assertEqual(pr.github_review_pr(node, "me").flags, ("new",))

    def test_review_pr_fields(self):
        p = pr.github_review_pr(review_node("o/r", 5, author="maria", isDraft=True), "me")
        self.assertEqual((p.provider, p.repo, p.number, p.author, p.draft, p.url), ("github", "o/r", 5, "maria", True, "https://github.com/o/r/pull/5"))


class MoreNormalising(unittest.TestCase):
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


class FakeWorld:
    """Stands in for gh and glab; records every command."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.mine = []
        self.review = []
        self.merge_unknown_for = 0  # how many GitHub answers still report UNKNOWN merge states
        self.gl_mine = []
        self.gl_review = []
        self.gl_approvals = {}
        self.teams = []  # "org/slug" lines from gh api user/teams
        self.team = []  # the team search's PRs, with their review-request history

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        key = cmd[0]
        if key in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"{key} exploded\n")
        ok = lambda data: SimpleNamespace(returncode=0, stdout=data if isinstance(data, str) else json.dumps(data), stderr="")
        if key == "gh" and cmd[2] == "user/teams":
            return ok("\n".join(self.teams) + ("\n" if self.teams else ""))
        if key == "gh":
            mine = self.mine
            if self.merge_unknown_for:
                self.merge_unknown_for -= 1
                mine = [{**n, "mergeStateStatus": "UNKNOWN"} for n in mine]
            return ok({"data": {"viewer": {"login": "me"}, "mine": {"nodes": mine}, "review": {"nodes": self.review}, "team": {"nodes": self.team}}})
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


def mine_node(repo, number, **kw):
    d = {"number": number, "title": f"PR {number}", "url": f"https://github.com/{repo}/pull/{number}", "repository": {"nameWithOwner": repo},
         "author": {"login": "me"}, "isDraft": False, "baseRefName": "main", "headRefName": f"b{number}", "reviewDecision": None,
         "mergeStateStatus": "CLEAN", "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z"}
    d.update(kw)
    return d


def review_node(repo, number, author="maria", requested=("me",), reviews=None, **kw):
    d = {"number": number, "title": f"R{number}", "url": f"https://github.com/{repo}/pull/{number}", "repository": {"nameWithOwner": repo},
         "author": {"login": author}, "isDraft": False, "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z",
         "reviewRequests": {"nodes": [{"requestedReviewer": {"login": w}} for w in requested]},
         "latestReviews": {"nodes": [{"state": st, "author": {"__typename": "User", "login": w}} for w, st in (reviews or {}).items()]}}
    d.update(kw)
    return d


class Fetching(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.slept = []
        self.projects = {"/srv/www/live-chat": pr.Remote("github", "octo/live-chat"), "/srv/www/scum-map": pr.Remote("gitlab", "scum-map/scum-map")}

    def fetch(self, **settings):
        return pr.fetch_all(self.projects, settings, self.world, NOW, home="/home/x", sleep=self.slept.append)

    def test_only_repositories_of_known_projects_are_kept(self):
        self.world.mine = [mine_node("octo/live-chat", 1), mine_node("Acme/Tools", 2)]
        self.world.review = [review_node("octo/live-chat", 5), review_node("Acme/Tools", 6)]
        data = self.fetch()
        self.assertEqual([p.number for p in data.mine], [1])
        self.assertEqual([p.number for p in data.review], [5])
        self.assertEqual(data.errors, [])

    def test_one_github_request_carries_both_lists_and_no_plugin_script_runs(self):
        self.world.mine = [mine_node("octo/live-chat", 1)]
        self.world.review = [review_node("octo/live-chat", 5)]
        self.fetch()
        github = [c for c in self.world.calls if c[:3] == ["gh", "api", "graphql"]]
        self.assertEqual(len(github), 1)
        self.assertEqual(github[0][:3], ["gh", "api", "graphql"])
        self.assertFalse(any(c[0] == "python3" for c in self.world.calls))

    def test_stacks_are_linked_by_branch(self):
        self.world.mine = [mine_node("octo/live-chat", 1, headRefName="a"), mine_node("octo/live-chat", 2, headRefName="b", baseRefName="a")]
        self.assertEqual({p.number: p.parent for p in self.fetch().mine}, {1: None, 2: 1})

    def test_unknown_merge_states_are_asked_again(self):
        self.world.mine = [mine_node("octo/live-chat", 1)]
        self.world.merge_unknown_for = 1
        data = self.fetch()
        self.assertEqual(data.mine[0].merge, "clean")
        self.assertEqual(len([c for c in self.world.calls if c[:3] == ["gh", "api", "graphql"]]), 2)
        self.assertEqual(self.slept, [1.0])

    def test_merge_states_still_unknown_after_three_tries_are_shown_as_unknown(self):
        self.world.mine = [mine_node("octo/live-chat", 1)]
        self.world.merge_unknown_for = 5
        data = self.fetch()
        self.assertEqual(data.mine[0].merge, "unknown")
        self.assertEqual(len([c for c in self.world.calls if c[:3] == ["gh", "api", "graphql"]]), 3)

    def test_github_is_not_queried_when_no_project_is_on_github(self):
        self.projects = {"/srv/www/scum-map": pr.Remote("gitlab", "scum-map/scum-map")}
        self.fetch()
        self.assertFalse(any(c[0] in ("gh", "python3") for c in self.world.calls))

    def test_leftover_review_team_settings_are_ignored(self):
        self.world.review = [review_node("octo/live-chat", 5)]
        data = self.fetch(review_teams={"octo": {"include": ["core"], "exclude": ["other"]}})
        self.assertEqual([p.number for p in data.review], [5])

    def test_drafts_are_hidden_from_the_review_queue_by_default(self):
        self.world.review = [review_node("octo/live-chat", 5, isDraft=True)]
        self.assertEqual(self.fetch().review, [])
        self.assertEqual(len(self.fetch(include_drafts=True).review), 1)

    def test_a_failing_source_is_reported_without_losing_the_others(self):
        self.world.fail.add("gh")
        self.world.gl_mine = [{"iid": 1, "project_id": 7, "title": "MR", "web_url": "u", "references": {"full": "scum-map/scum-map!1"}}]
        data = self.fetch()
        self.assertEqual([p.number for p in data.mine], [1])
        self.assertEqual(len(data.errors), 1)
        self.assertIn("GitHub (pull requests)", data.errors[0])
        self.assertIn("exploded", data.errors[0])

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


class MergeCommand(unittest.TestCase):
    def test_github_uses_the_chosen_method_and_deletes_the_branch(self):
        p = pr.MyPr("github", "acme/api", 7, "t", "u")
        self.assertEqual(pr.merge_command(p, "squash"), ["gh", "pr", "merge", "7", "--repo", "acme/api", "--squash", "--delete-branch"])

    def test_gitlab_squashes_or_rebases_by_flag_and_removes_the_source_branch(self):
        p = pr.MyPr("gitlab", "g/p", 3, "t", "u")
        self.assertEqual(pr.merge_command(p, "squash")[-2:], ["--remove-source-branch", "--squash"])
        self.assertEqual(pr.merge_command(p, "rebase")[-1], "--rebase")
        self.assertEqual(pr.merge_command(p, "merge")[-1], "--remove-source-branch")

    def test_merge_reports_the_first_line_of_a_failure(self):
        run = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="X Pull request is not mergeable\nmore")
        self.assertEqual(pr.merge(pr.MyPr("github", "a/b", 1, "t", "u"), "merge", run), "X Pull request is not mergeable")


class ApprovedByMe(unittest.TestCase):
    def test_your_standing_approval_is_read_from_your_latest_review(self):
        self.assertTrue(pr.github_review_pr(review_node("o/r", 1, reviews={"me": "APPROVED"}), "me").approved_by_me)
        self.assertFalse(pr.github_review_pr(review_node("o/r", 2, reviews={"me": "DISMISSED", "tom": "APPROVED"}), "me").approved_by_me)
        self.assertFalse(pr.github_review_pr(review_node("o/r", 3), "me").approved_by_me)


class NeedsReapproval(unittest.TestCase):
    def node(self, n, latest=None, approved_before=True, requested=("me",)):
        return review_node("o/r", n, requested=requested, reviews=latest or {}, headRefOid=f"sha{n}",
                           approvals={"nodes": [{"author": {"login": "me"}}] if approved_before else [{"author": {"login": "tom"}}]})

    def test_an_approval_dismissed_by_new_commits_or_a_re_request_needs_you_again(self):
        dismissed = pr.github_review_pr(self.node(1, {"me": "DISMISSED"}), "me")
        self.assertEqual((dismissed.approved_before, dismissed.head_sha, dismissed.needs_reapproval), (True, "sha1", True))
        self.assertTrue(pr.github_review_pr(self.node(2, {"me": "APPROVED"}), "me").needs_reapproval)  # re-requested while it stands

    def test_an_approval_github_dismissed_still_counts_as_approved_before(self):
        node = self.node(5, approved_before=False)  # the dismissed approval is no longer an APPROVED review...
        node["dismissals"] = {"nodes": [{"previousReviewState": "APPROVED", "review": {"author": {"login": "me"}}},
                                        {"previousReviewState": "CHANGES_REQUESTED", "review": {"author": {"login": "tom"}}}]}
        self.assertTrue(pr.github_review_pr(node, "me").needs_reapproval)  # ...only the dismissal event remembers it
        self.assertEqual(pr.approvers(node), {"me", "tom"})  # tom approved outright in the fixture; his dismissed change request adds nothing

    def test_a_dismissed_change_request_is_not_an_approval(self):
        node = self.node(6, approved_before=False)
        node["dismissals"] = {"nodes": [{"previousReviewState": "CHANGES_REQUESTED", "review": {"author": {"login": "me"}}}]}
        self.assertFalse(pr.github_review_pr(node, "me").approved_before)

    def test_a_standing_approval_or_no_earlier_approval_does_not(self):
        self.assertFalse(pr.github_review_pr(self.node(3, {"me": "APPROVED"}, requested=("team",)), "me").needs_reapproval)
        self.assertFalse(pr.github_review_pr(self.node(4, approved_before=False), "me").needs_reapproval)


class ViewerApproved(unittest.TestCase):
    def answer(self, review, head="h1"):
        body = json.dumps({"data": {"repository": {"pullRequest": {"headRefOid": head, "viewerLatestReview": review}}}})
        return lambda *a, **k: SimpleNamespace(returncode=0, stdout=body, stderr="")

    def test_only_an_approval_on_the_current_head_counts(self):
        self.assertTrue(pr.viewer_approved("o/r", 1, self.answer({"state": "APPROVED", "commit": {"oid": "h1"}})))
        self.assertFalse(pr.viewer_approved("o/r", 1, self.answer({"state": "APPROVED", "commit": {"oid": "old"}})))
        self.assertFalse(pr.viewer_approved("o/r", 1, self.answer({"state": "COMMENTED", "commit": {"oid": "h1"}})))
        self.assertFalse(pr.viewer_approved("o/r", 1, self.answer(None)))

    def test_no_answer_is_none(self):
        self.assertIsNone(pr.viewer_approved("o/r", 1, lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502")))


class TeamReviews(unittest.TestCase):
    """A PR asked of your team leaves review-requested:@me once a teammate reviews it; the board keeps it in the queue."""

    def setUp(self):
        self.world = FakeWorld()
        self.world.teams = ["Octo/BE-Team"]
        self.projects = {"/srv/www/live-chat": pr.Remote("github", "octo/live-chat")}

    def team_node(self, n, asked="be-team", reviews=None, author="maria"):
        who = {"login": asked} if asked == "me" else {"slug": asked, "organization": {"login": "octo"}}
        return review_node("octo/live-chat", n, author=author, requested=(), reviews=reviews or {"tom": "APPROVED"},
                           requests={"nodes": [{"requestedReviewer": who}]})

    def fetch(self, **settings):
        return pr.fetch_all(self.projects, settings, self.world, NOW, home="/home/x", sleep=lambda s: None)

    def test_a_pr_asked_of_your_team_stays_after_a_teammate_approves(self):
        self.world.team = [self.team_node(7)]
        [p] = self.fetch().review
        self.assertEqual((p.number, p.flags), (7, ("teammate-approved",)))
        query = next(c for c in self.world.calls if c[:3] == ["gh", "api", "graphql"])[-1]
        self.assertIn("team: search(", query)
        self.assertIn("repo:octo/live-chat", query)
        self.assertIn("-reviewed-by:@me", query)
        self.assertIn(f"created:>={time.strftime('%Y-%m-%d', time.gmtime(NOW - 30 * 86400))}", query)

    def test_one_asked_of_you_by_name_stays_too(self):
        self.world.team = [self.team_node(8, asked="me")]
        self.assertEqual([p.number for p in self.fetch().review], [8])

    def test_other_teams_your_own_reviews_and_duplicates_are_left_out(self):
        self.world.team = [self.team_node(9, asked="frontend"), self.team_node(10, reviews={"me": "COMMENTED"}), self.team_node(5)]
        self.world.review = [review_node("octo/live-chat", 5)]
        self.assertEqual([p.number for p in self.fetch().review], [5])

    def test_the_setting_off_or_no_teams_means_no_team_search(self):
        self.world.team = [self.team_node(7)]
        self.assertEqual(self.fetch(keep_team_reviews=False).review, [])
        self.assertFalse(any(c[:3] == ["gh", "api", "user/teams"] for c in self.world.calls))
        self.world.teams = []
        self.world.calls.clear()
        self.assertEqual(self.fetch().review, [])
        self.assertNotIn("team: search(", next(c for c in self.world.calls if c[:3] == ["gh", "api", "graphql"])[-1])

    def test_showing_every_pr_drops_the_age_bound(self):
        self.fetch(review_max_age_days=0)
        query = next(c for c in self.world.calls if c[:3] == ["gh", "api", "graphql"])[-1]
        self.assertIn("team: search(", query)
        self.assertNotIn("created:>=", query)
