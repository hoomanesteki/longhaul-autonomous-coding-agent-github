"""FakeGitHub is the stand-in for GitHub in later tests, so it has to behave like GitHub.

Most tests mount the app through httpx2's ASGI transport: no server and no port. One test
starts the threaded server that e2e runs use.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2
import pytest
import uvicorn
from tests.fakes import fake_github
from tests.fakes.fake_github import FakeGitHub, ManualClock, git, serve_in_thread

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
REPO = "acme/calc"
T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def api(path: str, repo: str = REPO) -> str:
    return f"/repos/{repo}{path}"


def numbers(response: httpx2.Response) -> list[int]:
    assert response.status_code == 200, response.text
    return [item["number"] for item in response.json()]


def label_names(response: httpx2.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [label["name"] for label in response.json()]


def comment_ids(response: httpx2.Response) -> list[int]:
    assert response.status_code == 200, response.text
    return [comment["id"] for comment in response.json()]


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def fake(clock: ManualClock) -> FakeGitHub:
    github = FakeGitHub(clock=clock)
    github.seed_repo(REPO)
    return github


@pytest.fixture
async def gh(fake: FakeGitHub) -> AsyncIterator[httpx2.AsyncClient]:
    async with fake.client() as client:
        yield client


# --- test_endpoints: every endpoint in the story, in the order a task uses them -------------


async def test_endpoints(fake: FakeGitHub, gh: httpx2.AsyncClient, clock: ManualClock) -> None:
    fake.seed_collaborator(REPO, "maria", "write")
    fake.seed_collaborator(REPO, "rey", "read")
    await _check_issues(fake, gh, clock)
    await _check_comments(fake, gh, clock)
    await _check_labels(fake, gh, clock)
    await _check_pulls(fake, gh, clock)
    await _check_permission(gh)
    await _check_not_found(gh)


async def _check_issues(fake: FakeGitHub, gh: httpx2.AsyncClient, clock: ManualClock) -> None:
    bug = fake.seed_issue(
        REPO,
        "add() returns the difference",
        body="Only add is wrong.",
        user="hooman",
        labels=["bug"],
    )
    clock.advance(60)
    plain = fake.seed_issue(REPO, "Docs typo", user="rey")
    clock.advance(60)
    closed = fake.seed_issue(REPO, "Old report", state="closed")
    assert (bug, plain, closed) == (1, 2, 3)

    # GET /issues defaults to state=open and lists the newest first.
    listed = await gh.get(api("/issues"))
    assert numbers(listed) == [2, 1]
    newest, oldest = listed.json()
    assert oldest["title"] == "add() returns the difference"
    assert oldest["body"] == "Only add is wrong."
    assert oldest["state"] == "open"
    assert oldest["user"]["login"] == "hooman"
    assert [label["name"] for label in oldest["labels"]] == ["bug"]
    assert isinstance(oldest["id"], int) and isinstance(oldest["labels"][0]["id"], int)
    assert oldest["html_url"] == "https://github.com/acme/calc/issues/1"
    assert oldest["created_at"] == "2026-03-01T12:00:00Z"
    assert oldest["updated_at"] == "2026-03-01T12:00:00Z"
    assert oldest["comments"] == 0
    assert "pull_request" not in oldest
    # GitHub sends null, not an empty string, for an issue without a description.
    assert newest["body"] is None

    # Labels must all be present. state selects open, closed or all.
    assert numbers(await gh.get(api("/issues"), params={"labels": "bug"})) == [1]
    assert numbers(await gh.get(api("/issues"), params={"labels": "bug,longhaul"})) == []
    assert numbers(await gh.get(api("/issues"), params={"state": "closed"})) == [3]
    assert numbers(await gh.get(api("/issues"), params={"state": "all"})) == [3, 2, 1]

    # GET /issues/{n} returns the same object the list does.
    single = await gh.get(api("/issues/1"))
    assert single.status_code == 200
    assert single.json() == oldest


async def _check_comments(fake: FakeGitHub, gh: httpx2.AsyncClient, clock: ManualClock) -> None:
    marker = "Picked up <!-- longhaul:key=lh-acme-calc-1:picked_up:1 -->"
    posted = await gh.post(api("/issues/1/comments"), json={"body": marker})
    assert posted.status_code == 201
    bot = posted.json()
    assert bot["body"] == marker
    assert bot["user"]["login"] == "longhaul-bot"
    assert bot["created_at"] == bot["updated_at"] == "2026-03-01T12:02:00Z"
    assert bot["html_url"] == f"https://github.com/acme/calc/issues/1#issuecomment-{bot['id']}"

    clock.advance(30)
    human = fake.seed_comment(REPO, 1, "/longhaul status", user="maria")

    listed = await gh.get(api("/issues/1/comments"))
    assert [c["id"] for c in listed.json()] == [bot["id"], human]
    assert listed.json()[1]["user"]["login"] == "maria"

    # since keeps comments updated at or after the time. The edge is inclusive, so a caller
    # that saves the last created_at it saw gets that comment again and has to skip it.
    comments = api("/issues/1/comments")
    at_edge = await gh.get(comments, params={"since": "2026-03-01T12:02:30Z"})
    assert comment_ids(at_edge) == [human]
    from_first = await gh.get(comments, params={"since": "2026-03-01T12:02:00Z"})
    assert comment_ids(from_first) == [bot["id"], human]
    past_edge = await gh.get(comments, params={"since": "2026-03-01T12:02:31Z"})
    assert comment_ids(past_edge) == []

    # A comment is activity on the issue, and other issues are not mixed in.
    issue = (await gh.get(api("/issues/1"))).json()
    assert issue["comments"] == 2
    assert issue["updated_at"] == "2026-03-01T12:02:30Z"
    assert (await gh.get(api("/issues/2/comments"))).json() == []
    assert fake.count_comments(REPO, 1) == 2
    assert fake.count_comments(REPO) == 2


async def _check_labels(fake: FakeGitHub, gh: httpx2.AsyncClient, clock: ManualClock) -> None:
    clock.advance(60)
    added = await gh.post(
        api("/issues/1/labels"), json={"labels": ["longhaul", "longhaul:working"]}
    )
    assert label_names(added) == ["bug", "longhaul", "longhaul:working"]
    assert (await gh.get(api("/issues/1"))).json()["updated_at"] == "2026-03-01T12:03:30Z"

    # Adding again changes nothing, and label names match without regard to case.
    again = await gh.post(api("/issues/1/labels"), json={"labels": ["LongHaul"]})
    assert label_names(again) == ["bug", "longhaul", "longhaul:working"]

    # Clients percent-encode the colon. The call returns the labels that are left.
    removed = await gh.delete(api("/issues/1/labels/longhaul%3Aworking"))
    assert label_names(removed) == ["bug", "longhaul"]

    # GitHub answers 404 when the label is not on the issue, so a retry has to expect that.
    gone = await gh.delete(api("/issues/1/labels/longhaul%3Aworking"))
    assert gone.status_code == 404
    assert gone.json()["message"] == "Label does not exist"

    assert numbers(await gh.get(api("/issues"), params={"labels": "longhaul"})) == [1]
    assert fake.label_names(REPO, 1) == ["bug", "longhaul"]
    # Only changes that happened are events, so a duplicate add leaves no second event.
    events = [(e.number, e.action, e.label, e.actor) for e in fake.label_events(REPO)]
    assert events == [
        (1, "labeled", "longhaul", "longhaul-bot"),
        (1, "labeled", "longhaul:working", "longhaul-bot"),
        (1, "unlabeled", "longhaul:working", "longhaul-bot"),
    ]


async def _check_pulls(fake: FakeGitHub, gh: httpx2.AsyncClient, clock: ManualClock) -> None:
    clock.advance(60)
    created = await gh.post(
        api("/pulls"),
        json={
            "title": "fix(calc): add returns the sum",
            "head": "longhaul-pr/1",
            "base": "main",
            "body": "Closes #1",
            "draft": True,
        },
    )
    assert created.status_code == 201
    pr = created.json()
    # Issues and pull requests share one number sequence, as on GitHub.
    assert pr["number"] == 4
    assert pr["html_url"] == "https://github.com/acme/calc/pull/4"
    assert (pr["state"], pr["draft"], pr["merged"], pr["merged_at"]) == ("open", True, False, None)
    assert pr["title"] == "fix(calc): add returns the sum"
    assert pr["body"] == "Closes #1"
    assert pr["user"]["login"] == "longhaul-bot"
    assert (pr["head"]["ref"], pr["head"]["label"]) == ("longhaul-pr/1", "acme:longhaul-pr/1")
    assert (pr["base"]["ref"], pr["base"]["label"]) == ("main", "acme:main")
    assert re.fullmatch(r"[0-9a-f]{40}", pr["head"]["sha"])

    # head is filtered the way GitHub does it: owner:branch. A bare branch name matches nothing.
    pulls = api("/pulls")
    assert numbers(await gh.get(pulls, params={"head": "acme:longhaul-pr/1"})) == [4]
    assert numbers(await gh.get(pulls, params={"head": "acme:other"})) == []
    assert numbers(await gh.get(pulls, params={"head": "longhaul-pr/1"})) == []
    assert numbers(await gh.get(pulls, params={"base": "main"})) == [4]
    assert numbers(await gh.get(pulls, params={"base": "dev"})) == []
    assert numbers(await gh.get(pulls, params={"state": "closed"})) == []
    # The list leaves out `merged`. Only GET /pulls/{n} has it, so callers must use that.
    listed = (await gh.get(pulls)).json()[0]
    assert "merged" not in listed and listed["merged_at"] is None

    # A pull request is also an issue. Label calls and comments use the same number.
    as_issue = (await gh.get(api("/issues/4"))).json()
    assert as_issue["pull_request"]["html_url"] == pr["html_url"]
    assert numbers(await gh.get(api("/issues"), params={"state": "all"})) == [4, 3, 2, 1]

    clock.advance(60)
    fake.merge_pr(REPO, 4)
    merged = await gh.get(api("/pulls/4"))
    assert merged.status_code == 200
    body = merged.json()
    assert (body["state"], body["merged"], body["merged_at"]) == (
        "closed",
        True,
        "2026-03-01T12:05:30Z",
    )
    assert body["merged_by"]["login"]
    assert body["closed_at"] == "2026-03-01T12:05:30Z"
    assert numbers(await gh.get(pulls)) == []
    assert numbers(await gh.get(pulls, params={"state": "closed"})) == [4]
    assert numbers(await gh.get(pulls, params={"state": "all"})) == [4]
    assert fake.count_pulls(REPO) == 1

    # Number 1 is an issue, so there is no pull request with it.
    assert (await gh.get(api("/pulls/1"))).status_code == 404


async def _check_permission(gh: httpx2.AsyncClient) -> None:
    write = await gh.get(api("/collaborators/maria/permission"))
    assert write.status_code == 200
    body = write.json()
    assert (body["permission"], body["role_name"], body["user"]["login"]) == (
        "write",
        "write",
        "maria",
    )
    assert body["user"]["permissions"] == {
        "admin": False,
        "maintain": False,
        "push": True,
        "triage": True,
        "pull": True,
    }
    read = (await gh.get(api("/collaborators/rey/permission"))).json()
    assert (read["permission"], read["user"]["permissions"]["push"]) == ("read", False)
    # The repo owner is an admin, and a stranger has no access.
    assert (await gh.get(api("/collaborators/acme/permission"))).json()["permission"] == "admin"
    assert (await gh.get(api("/collaborators/mallory/permission"))).json()["permission"] == "none"


async def _check_not_found(gh: httpx2.AsyncClient) -> None:
    ghost = "/repos/nobody/missing"
    comment, labels = {"body": "x"}, {"labels": ["x"]}
    pull = {"title": "t", "head": "b", "base": "main"}
    cases: list[tuple[str, str, Any]] = [
        ("GET", f"{ghost}/issues", None),
        ("GET", f"{ghost}/issues/1", None),
        ("GET", f"{ghost}/issues/1/comments", None),
        ("POST", f"{ghost}/issues/1/comments", comment),
        ("POST", f"{ghost}/issues/1/labels", labels),
        ("DELETE", f"{ghost}/issues/1/labels/x", None),
        ("GET", f"{ghost}/pulls", None),
        ("POST", f"{ghost}/pulls", pull),
        ("GET", f"{ghost}/pulls/1", None),
        ("GET", f"{ghost}/collaborators/x/permission", None),
        ("GET", api("/issues/99"), None),
        ("GET", api("/issues/99/comments"), None),
        ("POST", api("/issues/99/comments"), comment),
        ("POST", api("/issues/99/labels"), labels),
        ("DELETE", api("/issues/99/labels/bug"), None),
        ("GET", api("/pulls/99"), None),
        ("GET", api("/issues/not-a-number"), None),
    ]
    for method, path, body in cases:
        response = await gh.request(method, path, json=body)
        assert response.status_code == 404, (method, path, response.text)
        assert response.json()["message"] == "Not Found", (method, path)


# --- after_write_fail: the write lands and the caller still sees a failure --------------------


async def test_after_write_fail_mode(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    number = fake.seed_issue(REPO, "Fix add")
    body = "Plan ready <!-- longhaul:key=lh-acme-calc-1:plan_comment:1 -->"
    fake.after_write_fail(r"/issues/\d+/comments$")

    response = await gh.post(api(f"/issues/{number}/comments"), json={"body": body})

    # The caller sees a server error, but the comment is stored.
    assert response.status_code == 502
    assert fake.count_comments(REPO, number) == 1
    assert fake.comment_bodies(REPO, number) == [body]
    assert fake.requests[-1].fault == "after"

    # A retry finds its comment by the marker. A retry that skips the search would post twice.
    found = (await gh.get(api(f"/issues/{number}/comments"))).json()
    assert [c["body"] for c in found] == [body]

    # The mode was for one write. The next write is normal.
    again = await gh.post(api(f"/issues/{number}/comments"), json={"body": "second"})
    assert again.status_code == 201
    assert fake.count_comments(REPO, number) == 2


async def test_after_write_fail_leaves_reads_and_other_paths_alone(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "Fix add")
    fake.after_write_fail(r"/comments$")

    assert (await gh.get(api(f"/issues/{number}/comments"))).status_code == 200  # a read
    labels = await gh.post(api(f"/issues/{number}/labels"), json={"labels": ["a"]})
    assert labels.status_code == 200  # another path
    assert (await gh.post(api(f"/issues/{number}/comments"), json={"body": "x"})).status_code == 502


async def test_after_write_fail_can_cover_several_writes_and_pick_the_status(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "Fix add")
    fake.after_write_fail(r"/comments$", count=2, status=504)

    for _ in range(2):
        assert (
            await gh.post(api(f"/issues/{number}/comments"), json={"body": "x"})
        ).status_code == 504
    assert (await gh.post(api(f"/issues/{number}/comments"), json={"body": "x"})).status_code == 201
    assert fake.count_comments(REPO, number) == 3


async def test_after_write_fail_does_not_hide_a_write_that_github_refuses(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "Fix add")
    url = api(f"/issues/{number}/comments")
    fake.after_write_fail(r"/comments$")

    refused = await gh.post(url, json={"body": " "})
    missing = await gh.post(api("/issues/99/comments"), json={"body": "x"})

    # Nothing was written, so there is nothing to lose track of. The rule is still waiting.
    assert (refused.status_code, missing.status_code) == (422, 404)
    assert [r.fault for r in fake.requests] == [None, None]
    assert (await gh.post(url, json={"body": "x"})).status_code == 502
    assert fake.count_comments(REPO, number) == 1


async def test_a_fail_next_rule_is_used_before_an_after_write_fail_rule(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "Fix add")
    url = api(f"/issues/{number}/comments")
    fake.after_write_fail(r"/comments$", status=502)
    fake.fail_next(r"/comments$", status=503)

    first = await gh.post(url, json={"body": "x"})  # fails before the write
    second = await gh.post(url, json={"body": "y"})  # writes, then fails

    assert (first.status_code, second.status_code) == (503, 502)
    assert fake.comment_bodies(REPO, number) == ["y"]


# What the retry of each kind of write gets from GitHub once the first attempt has landed.
# Each client has to cope with these, which is why the fake copies them.
WRITES: dict[str, tuple[str, str, dict[str, Any] | None, int]] = {
    "comment": ("POST", "/issues/1/comments", {"body": "hi"}, 201),
    "label_add": ("POST", "/issues/1/labels", {"labels": ["longhaul"]}, 200),
    "label_remove": ("DELETE", "/issues/1/labels/longhaul:working", None, 404),
    "pull": ("POST", "/pulls", {"title": "t", "head": "b", "base": "main"}, 422),
}


def _landed(fake: FakeGitHub, kind: str) -> bool:
    return {
        "comment": fake.count_comments(REPO, 1) == 1,
        "label_add": "longhaul" in fake.label_names(REPO, 1),
        "label_remove": "longhaul:working" not in fake.label_names(REPO, 1),
        "pull": fake.count_pulls(REPO) == 1,
    }[kind]


@pytest.mark.parametrize("kind", sorted(WRITES))
async def test_after_write_fail_applies_every_kind_of_write(
    fake: FakeGitHub, gh: httpx2.AsyncClient, kind: str
) -> None:
    fake.seed_issue(REPO, "Fix add", labels=["longhaul:working"])
    method, path, body, retry_status = WRITES[kind]
    fake.after_write_fail()

    first = await gh.request(method, api(path), json=body)
    assert first.status_code == 502
    assert _landed(fake, kind)

    retry = await gh.request(method, api(path), json=body)
    assert retry.status_code == retry_status


# --- fail_next: the request fails and nothing is applied --------------------------------------


async def test_fail_next_fails_without_applying_the_write(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "Fix add")
    fake.fail_next(r"/issues/\d+/comments$", status=503, count=2)

    for _ in range(2):
        response = await gh.post(api(f"/issues/{number}/comments"), json={"body": "x"})
        assert response.status_code == 503
        assert response.json()["message"] == "Service Unavailable"
    assert fake.count_comments(REPO, number) == 0

    ok = await gh.post(api(f"/issues/{number}/comments"), json={"body": "x"})
    assert ok.status_code == 201
    assert fake.count_comments(REPO, number) == 1
    assert [r.fault for r in fake.requests] == ["before", "before", None]


async def test_fail_next_defaults_to_one_500(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    fake.fail_next(r"/issues$")
    assert (await gh.get(api("/issues"))).status_code == 500
    assert (await gh.get(api("/issues"))).status_code == 200


async def test_fail_next_matches_by_path_regex_and_method(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    fake.seed_issue(REPO, "one")
    fake.seed_issue(REPO, "two")
    fake.fail_next(r"/issues/1$", status=500, method="GET")

    assert (await gh.get(api("/issues/2"))).status_code == 200  # other path
    assert (await gh.get(api("/issues"))).status_code == 200  # other path
    assert (await gh.delete(api("/issues/1/labels/x"))).status_code == 404  # other method and path
    assert (await gh.get(api("/issues/1"))).status_code == 500  # the match
    assert (await gh.get(api("/issues/1"))).status_code == 200  # used up


async def test_fail_next_can_set_headers_and_body(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    fake.fail_next(
        r"/issues$",
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1772366400"},
        body={"message": "API rate limit exceeded"},
    )

    response = await gh.get(api("/issues"))

    assert response.status_code == 403
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.headers["x-ratelimit-reset"] == "1772366400"
    assert response.json() == {"message": "API rate limit exceeded"}


async def test_fail_next_rules_are_used_in_the_order_they_were_added(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    fake.fail_next(r"/issues$", status=500)
    fake.fail_next(r"/issues$", status=502)

    codes = [(await gh.get(api("/issues"))).status_code for _ in range(3)]

    assert codes == [500, 502, 200]


async def test_clear_faults_removes_pending_rules(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    fake.fail_next(r"/issues$", count=5)
    fake.after_write_fail()

    fake.clear_faults()

    assert (await gh.get(api("/issues"))).status_code == 200
    fake.seed_issue(REPO, "one")
    assert (await gh.post(api("/issues/1/comments"), json={"body": "x"})).status_code == 201


@pytest.mark.parametrize("count", [0, -1])
def test_fault_counts_must_be_positive(fake: FakeGitHub, count: int) -> None:
    with pytest.raises(ValueError, match="count"):
        fake.fail_next(r".*", count=count)
    with pytest.raises(ValueError, match="count"):
        fake.after_write_fail(count=count)


# --- pagination -------------------------------------------------------------------------------


async def test_pagination(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    for i in range(5):
        fake.seed_issue(REPO, f"issue {i + 1}")

    first = await gh.get(api("/issues"), params={"per_page": 2})
    assert numbers(first) == [5, 4]
    assert set(first.links) == {"next", "last"}
    assert httpx2.URL(first.links["next"]["url"]).params["page"] == "2"
    assert httpx2.URL(first.links["last"]["url"]).params["page"] == "3"

    second = await gh.get(api("/issues"), params={"per_page": 2, "page": 2})
    assert numbers(second) == [3, 2]
    assert set(second.links) == {"prev", "next", "last", "first"}

    third = await gh.get(api("/issues"), params={"per_page": 2, "page": 3})
    assert numbers(third) == [1]
    assert set(third.links) == {"prev", "first"}

    beyond = await gh.get(api("/issues"), params={"per_page": 2, "page": 4})
    assert numbers(beyond) == []


async def test_following_next_links_visits_every_item_once(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    for i in range(7):
        fake.seed_issue(REPO, f"issue {i + 1}", labels=["longhaul"])
    fake.seed_issue(REPO, "other")

    seen: list[int] = []
    url: str | None = api("/issues") + "?labels=longhaul&per_page=3"
    while url:
        response = await gh.get(url)
        seen += numbers(response)
        url = response.links.get("next", {}).get("url")

    # The links keep the filter, so the unlabeled issue never shows up.
    assert seen == [7, 6, 5, 4, 3, 2, 1]


async def test_no_paging_headers_when_everything_fits_on_one_page(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    fake.seed_issue(REPO, "one")

    response = await gh.get(api("/issues"))

    assert numbers(response) == [1]
    assert "link" not in response.headers


async def test_page_size_defaults_to_30_and_is_capped_at_100(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    for i in range(105):
        fake.seed_issue(REPO, f"issue {i + 1}")

    default = await gh.get(api("/issues"))
    assert len(default.json()) == 30
    assert httpx2.URL(default.links["last"]["url"]).params["page"] == "4"

    capped = await gh.get(api("/issues"), params={"per_page": 1000})
    assert len(capped.json()) == 100
    assert httpx2.URL(capped.links["last"]["url"]).params["page"] == "2"


@pytest.mark.parametrize(
    "params", [{"per_page": "abc"}, {"per_page": "0"}, {"page": "0"}, {"page": "x"}, {"page": "-3"}]
)
async def test_bad_paging_values_fall_back_to_the_defaults(
    fake: FakeGitHub, gh: httpx2.AsyncClient, params: dict[str, str]
) -> None:
    fake.seed_issue(REPO, "one")

    assert numbers(await gh.get(api("/issues"), params=params)) == [1]


async def test_comments_and_pulls_page_the_same_way(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "one")
    for i in range(3):
        fake.seed_comment(REPO, number, f"comment {i}")
        fake.seed_pull(REPO, f"branch-{i}")

    comments = await gh.get(api(f"/issues/{number}/comments"), params={"per_page": 2, "page": 2})
    assert [c["body"] for c in comments.json()] == ["comment 2"]
    assert set(comments.links) == {"prev", "first"}

    pulls = await gh.get(api("/pulls"), params={"per_page": 2})
    assert len(pulls.json()) == 2
    assert set(pulls.links) == {"next", "last"}


# --- details that clients trip over ------------------------------------------------------------


async def test_since_accepts_offsets_and_fractions_and_rejects_nonsense(
    fake: FakeGitHub, gh: httpx2.AsyncClient, clock: ManualClock
) -> None:
    number = fake.seed_issue(REPO, "one")
    clock.advance(10)
    comment = fake.seed_comment(REPO, number, "later")
    url = api(f"/issues/{number}/comments")

    for since, expected in [
        ("2026-03-01T12:00:10+00:00", [comment]),
        ("2026-03-01T12:00:10.250Z", []),
        ("2026-03-01T14:00:00+02:00", [comment]),
        ("2026-03-01T12:00:10", [comment]),  # no zone means UTC
    ]:
        assert comment_ids(await gh.get(url, params={"since": since})) == expected, since

    bad = await gh.get(url, params={"since": "yesterday"})
    assert bad.status_code == 422
    assert bad.json()["message"] == "Validation Failed"
    assert bad.json()["errors"][0]["field"] == "since"


async def test_comment_validation(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    number = fake.seed_issue(REPO, "one")
    url = api(f"/issues/{number}/comments")

    blank = await gh.post(url, json={"body": "   "})
    assert blank.status_code == 422
    assert blank.json()["errors"] == [
        {"resource": "IssueComment", "code": "missing_field", "field": "body"}
    ]
    assert (await gh.post(url, json={})).status_code == 422
    assert (await gh.post(url, json={"body": 5})).status_code == 422
    assert (await gh.post(url, json=["body"])).status_code == 422
    broken = await gh.post(url, content=b"{not json", headers={"content-type": "application/json"})
    assert broken.status_code == 400
    assert broken.json()["message"] == "Problems parsing JSON"

    too_long = await gh.post(url, json={"body": "x" * 65537})
    assert too_long.status_code == 422
    assert fake.count_comments(REPO, number) == 0
    assert (await gh.post(url, json={"body": "x" * 65536})).status_code == 201


async def test_label_calls_take_the_shapes_github_takes(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "one")
    url = api(f"/issues/{number}/labels")

    assert label_names(await gh.post(url, json={"labels": ["a", "b"]})) == ["a", "b"]
    assert label_names(await gh.post(url, json=["c"])) == ["a", "b", "c"]  # a bare array
    assert label_names(await gh.post(url, json={"labels": [{"name": "d"}]})) == ["a", "b", "c", "d"]
    assert (await gh.post(url, json={"labels": "a"})).status_code == 422
    assert (await gh.post(url, json={})).status_code == 422
    assert (await gh.post(url, json={"labels": [""]})).status_code == 422

    # A label that does not exist yet is created, and keeps the spelling it was created with.
    assert label_names(await gh.post(url, json={"labels": ["Newish"]}))[-1] == "Newish"
    other = fake.seed_issue(REPO, "two")
    assert label_names(
        await gh.post(api(f"/issues/{other}/labels"), json={"labels": ["newish"]})
    ) == ["Newish"]


@pytest.mark.parametrize("name", ["good first issue", "area/build", "longhaul:needs-human", "bug"])
async def test_labels_with_spaces_and_slashes_can_be_removed(
    fake: FakeGitHub, gh: httpx2.AsyncClient, name: str
) -> None:
    number = fake.seed_issue(REPO, "one", labels=[name, "other"])

    # A client escapes everything, including the slash in "area/build".
    response = await gh.delete(api(f"/issues/{number}/labels/{quote(name, safe='')}"))

    assert label_names(response) == ["other"]


async def test_issues_sort_by_creation_or_update_and_filter_by_since(
    fake: FakeGitHub, gh: httpx2.AsyncClient, clock: ManualClock
) -> None:
    first = fake.seed_issue(REPO, "one")
    clock.advance(10)
    second = fake.seed_issue(REPO, "two")
    clock.advance(10)
    fake.seed_comment(REPO, first, "bump")  # now the first issue was updated last
    url = api("/issues")

    assert numbers(await gh.get(url)) == [second, first]
    assert numbers(await gh.get(url, params={"direction": "asc"})) == [first, second]
    assert numbers(await gh.get(url, params={"sort": "updated"})) == [first, second]
    assert numbers(await gh.get(url, params={"sort": "updated", "direction": "asc"})) == [
        second,
        first,
    ]
    assert numbers(await gh.get(url, params={"since": "2026-03-01T12:00:20Z"})) == [first]
    assert (await gh.get(url, params={"state": "bogus"})).status_code == 422
    assert (await gh.get(api("/pulls"), params={"state": "bogus"})).status_code == 422


async def test_pull_requests_are_issues_too(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    issue = fake.seed_issue(REPO, "the report")
    pull = fake.seed_pull(REPO, "feature")
    assert (issue, pull) == (1, 2)

    # The conversation endpoints work on the pull request's number.
    await gh.post(api(f"/issues/{pull}/comments"), json={"body": "looks good"})
    await gh.post(api(f"/issues/{pull}/labels"), json={"labels": ["reviewed"]})
    assert fake.comment_bodies(REPO, pull) == ["looks good"]
    assert fake.label_names(REPO, pull) == ["reviewed"]

    listed = (await gh.get(api("/issues"))).json()
    assert [("pull_request" in item) for item in listed] == [True, False]


async def test_pull_request_validation(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    url = api("/pulls")
    good = {"title": "t", "head": "longhaul-pr/1", "base": "main"}

    for missing in ("title", "head", "base"):
        sent = {k: v for k, v in good.items() if k != missing}
        response = await gh.post(url, json=sent)
        assert response.status_code == 422, missing
        assert response.json()["errors"][0]["field"] == missing
    assert (await gh.post(url, content=b"nope")).status_code == 400
    assert fake.count_pulls(REPO) == 0

    assert (await gh.post(url, json=good)).status_code == 201
    # GitHub refuses a second open pull request from the same head into the same base.
    duplicate = await gh.post(url, json=good)
    assert duplicate.status_code == 422
    assert duplicate.json()["message"] == "Validation Failed"
    assert (
        duplicate.json()["errors"][0]["message"]
        == "A pull request already exists for acme:longhaul-pr/1."
    )
    assert fake.count_pulls(REPO) == 1

    # A different base is a different pull request, and a closed one does not block a new one.
    assert (await gh.post(url, json={**good, "base": "dev"})).status_code == 201
    fake.close_pr(REPO, 1)
    assert (await gh.post(url, json=good)).status_code == 201


async def test_merge_and_close_helpers(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    issue = fake.seed_issue(REPO, "report")
    merged = fake.seed_pull(REPO, "a")
    closed = fake.seed_pull(REPO, "b")

    fake.merge_pr(REPO, merged)
    fake.close_pr(REPO, closed)

    one = (await gh.get(api(f"/pulls/{merged}"))).json()
    two = (await gh.get(api(f"/pulls/{closed}"))).json()
    assert (one["state"], one["merged"]) == ("closed", True)
    assert (two["state"], two["merged"], two["merged_at"]) == ("closed", False, None)
    assert TIMESTAMP.match(two["closed_at"])

    with pytest.raises(ValueError, match="not open"):
        fake.merge_pr(REPO, merged)
    with pytest.raises(ValueError, match="not open"):
        fake.close_pr(REPO, closed)
    with pytest.raises(LookupError):
        fake.merge_pr(REPO, issue)  # an issue, not a pull request
    with pytest.raises(LookupError):
        fake.merge_pr(REPO, 99)


@pytest.mark.parametrize(
    ("role", "legacy", "granted"),
    [
        ("none", "none", ""),
        ("read", "read", "pull"),
        ("triage", "read", "pull triage"),
        ("write", "write", "pull triage push"),
        ("maintain", "write", "pull triage push maintain"),
        ("admin", "admin", "pull triage push maintain admin"),
    ],
)
async def test_permission_levels(
    fake: FakeGitHub, gh: httpx2.AsyncClient, role: str, legacy: str, granted: str
) -> None:
    # `permission` has the old four levels. `role_name` and the flags tell the roles apart.
    fake.seed_collaborator(REPO, "sam", role)

    body = (await gh.get(api("/collaborators/sam/permission"))).json()

    assert (body["permission"], body["role_name"]) == (legacy, role)
    assert {name for name, on in body["user"]["permissions"].items() if on} == set(granted.split())


async def test_repo_names_ignore_case_like_github(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    fake.seed_issue("ACME/Calc", "one")

    assert numbers(await gh.get("/repos/Acme/CALC/issues")) == [1]
    assert (await gh.get("/repos/acme/calc/collaborators/ACME/permission")).json()[
        "permission"
    ] == "admin"


async def test_writes_are_authored_by_the_token_owner(
    fake: FakeGitHub, gh: httpx2.AsyncClient
) -> None:
    number = fake.seed_issue(REPO, "one")
    fake.seed_token("ghp_maria", "maria")
    url = api(f"/issues/{number}/comments")

    as_maria = await gh.post(url, json={"body": "a"}, headers={"authorization": "Bearer ghp_maria"})
    legacy = await gh.post(url, json={"body": "b"}, headers={"authorization": "token ghp_maria"})
    anonymous = await gh.post(url, json={"body": "c"})
    unknown = await gh.post(url, json={"body": "d"}, headers={"authorization": "Bearer other"})

    authors = [r.json()["user"]["login"] for r in (as_maria, legacy, anonymous, unknown)]
    assert authors == ["maria", "maria", "longhaul-bot", "longhaul-bot"]


async def test_requests_are_recorded(fake: FakeGitHub, gh: httpx2.AsyncClient) -> None:
    number = fake.seed_issue(REPO, "one")
    await gh.post(
        api(f"/issues/{number}/comments"),
        json={"body": "x"},
        headers={"authorization": "Bearer ghp_x", "x-github-api-version": "2022-11-28"},
    )
    await gh.get(api("/issues"), params={"labels": "longhaul"})

    post, get = fake.requests
    assert (post.method, post.path, post.status) == (
        "POST",
        "/repos/acme/calc/issues/1/comments",
        201,
    )
    assert post.headers["authorization"] == "Bearer ghp_x"
    assert post.headers["x-github-api-version"] == "2022-11-28"
    assert (get.method, get.path, get.query, get.status) == (
        "GET",
        "/repos/acme/calc/issues",
        "labels=longhaul",
        200,
    )
    assert post.fault is None


async def test_unlisted_routes_and_methods_are_plain_github_404s(
    gh: httpx2.AsyncClient,
) -> None:
    # Only the endpoints in the story exist. A call to any other one fails loudly.
    for method, path in [
        ("GET", api("")),
        ("GET", api("/branches/main")),
        ("PATCH", api("/issues/1")),
        ("PUT", api("/issues/1/labels")),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/"),
    ]:
        response = await gh.request(method, path)
        assert response.status_code == 404, (method, path)
        body = response.json()
        assert body["message"] == "Not Found"
        assert body["status"] == "404"
        assert body["documentation_url"].startswith("https://docs.github.com")


async def test_timestamps_are_utc_to_the_second_and_follow_the_clock() -> None:
    github = FakeGitHub()  # the system clock
    github.seed_repo(REPO)
    github.seed_issue(REPO, "one")
    async with github.client() as client:
        issue = (await client.get(api("/issues/1"))).json()

    assert TIMESTAMP.match(issue["created_at"])
    created = datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - created) < timedelta(seconds=10)


def test_manual_clock_moves_only_when_told() -> None:
    clock = ManualClock(T0)
    assert clock() == T0
    clock.advance(90)
    assert clock() == T0 + timedelta(seconds=90)
    clock.set(T0)
    assert clock() == T0


def test_seed_helpers_refuse_mistakes(fake: FakeGitHub) -> None:
    with pytest.raises(ValueError, match="already"):
        fake.seed_repo(REPO)
    with pytest.raises(ValueError, match="owner/name"):
        fake.seed_repo("no-slash")
    with pytest.raises(LookupError, match="other/repo"):
        fake.seed_issue("other/repo", "x")
    fake.seed_issue(REPO, "one", number=7)
    with pytest.raises(ValueError, match="7"):
        fake.seed_issue(REPO, "again", number=7)
    assert fake.seed_issue(REPO, "next") == 8
    with pytest.raises(ValueError, match="permission"):
        fake.seed_collaborator(REPO, "x", "owner")
    with pytest.raises(LookupError):
        fake.seed_comment(REPO, 99, "x")
    with pytest.raises(ValueError, match="state"):
        fake.seed_issue(REPO, "x", state="merged")
    with pytest.raises(ValueError, match="source"):
        fake.seed_repo("acme/other", source=Path("somewhere"))


# --- the local bare repo behind a repo -------------------------------------------------------


def _work_repo(path: Path) -> Path:
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    (path / "a.txt").write_text("one\n")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "initial", cwd=path)
    git("tag", "base", cwd=path)
    return path


async def test_refs_pushed_to_the_bare_remote_are_visible(
    fake: FakeGitHub, gh: httpx2.AsyncClient, tmp_path: Path
) -> None:
    work = _work_repo(tmp_path / "work")
    base = git("rev-parse", "HEAD", cwd=work)
    remote = tmp_path / "remote.git"
    fake.seed_repo("acme/widgets", remote=remote, source=work)

    assert fake.clone_url("acme/widgets") == str(remote)
    assert fake.branches("acme/widgets") == {"main": base}  # the tag is not a branch
    assert fake.refs("acme/widgets") == {"refs/heads/main": base, "refs/tags/base": base}

    # The host pushes a branch the way the checkpoint code will. The fake sees it at once.
    (work / "a.txt").write_text("two\n")
    git("commit", "-q", "-am", "fix", cwd=work)
    pushed = git("rev-parse", "HEAD", cwd=work)
    git("push", "-q", fake.clone_url("acme/widgets"), "HEAD:refs/heads/longhaul-pr/7", cwd=work)
    assert fake.branches("acme/widgets") == {"main": base, "longhaul-pr/7": pushed}

    # A pull request must name branches that exist, and reports the real commits.
    url = api("/pulls", "acme/widgets")
    missing = await gh.post(url, json={"title": "t", "head": "longhaul-pr/8", "base": "main"})
    assert missing.status_code == 422
    assert missing.json()["errors"] == [
        {"resource": "PullRequest", "field": "head", "code": "invalid"}
    ]
    no_base = await gh.post(url, json={"title": "t", "head": "longhaul-pr/7", "base": "nope"})
    assert no_base.json()["errors"][0]["field"] == "base"

    created = await gh.post(url, json={"title": "t", "head": "longhaul-pr/7", "base": "main"})
    assert created.status_code == 201
    pr = created.json()
    assert (pr["head"]["sha"], pr["base"]["sha"]) == (pushed, base)
    assert pr["base"]["repo"]["default_branch"] == "main"


def test_a_repo_without_a_remote_has_no_refs(fake: FakeGitHub) -> None:
    with pytest.raises(ValueError, match="remote"):
        fake.refs(REPO)
    with pytest.raises(ValueError, match="remote"):
        fake.clone_url(REPO)


def test_an_empty_remote_has_no_branches(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.seed_repo("acme/empty", remote=tmp_path / "empty.git", default_branch="trunk")

    assert fake.refs("acme/empty") == {}
    assert git("symbolic-ref", "HEAD", cwd=tmp_path / "empty.git") == "refs/heads/trunk"


def test_the_source_must_have_the_default_branch(fake: FakeGitHub, tmp_path: Path) -> None:
    work = _work_repo(tmp_path / "work")

    with pytest.raises(ValueError, match="trunk"):
        fake.seed_repo(
            "acme/widgets", default_branch="trunk", remote=tmp_path / "r.git", source=work
        )


# --- the threaded server and the make target --------------------------------------------------


def test_server_in_thread_serves_real_http(fake: FakeGitHub) -> None:
    fake.seed_issue(REPO, "one")

    with serve_in_thread(fake) as server:
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+", server.url)
        with httpx2.Client(base_url=server.url, trust_env=False) as client:
            listed = client.get(api("/issues"), params={"per_page": 1})
            assert listed.status_code == 200
            assert listed.json()[0]["title"] == "one"

            # State seeded after the start is visible, and faults work over the wire.
            fake.seed_issue(REPO, "two")
            assert [i["number"] for i in client.get(api("/issues")).json()] == [2, 1]
            assert set(client.get(api("/issues"), params={"per_page": 1}).links) == {"next", "last"}
            fake.fail_next(r"/issues$", status=500)
            assert client.get(api("/issues")).status_code == 500

            # A write over HTTP shows up in the helpers.
            client.post(api("/issues/1/comments"), json={"body": "hi"})
            assert fake.count_comments(REPO, 1) == 1

    # Leaving the block stopped the server.
    with pytest.raises(httpx2.TransportError):
        httpx2.get(server.url + api("/issues"), trust_env=False, timeout=2)


def test_concurrent_writes_over_http_are_all_kept(fake: FakeGitHub) -> None:
    number = fake.seed_issue(REPO, "one")

    def post_ten(worker: int) -> list[int]:
        with httpx2.Client(base_url=server.url, trust_env=False) as client:
            replies = [
                client.post(api(f"/issues/{number}/comments"), json={"body": f"{worker}-{i}"})
                for i in range(10)
            ]
        return [reply.json()["id"] for reply in replies]

    with serve_in_thread(fake) as server, ThreadPoolExecutor(max_workers=8) as pool:
        ids = [i for batch in pool.map(post_ten, range(8)) for i in batch]
        assert fake.count_comments(REPO, number) == 80  # the helper reads while the server writes

    assert len(set(ids)) == 80
    assert fake.count_comments(REPO, number) == 80


def test_server_refuses_a_port_that_is_taken(fake: FakeGitHub) -> None:
    with serve_in_thread(fake) as server, pytest.raises(RuntimeError, match="port"):
        serve_in_thread(FakeGitHub(), port=server.port)


def test_main_runs_on_8099_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(app: object, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)

    assert fake_github.main([]) == 0
    assert (calls[0]["host"], calls[0]["port"]) == ("127.0.0.1", 8099)
    assert fake_github.main(["--host", "0.0.0.0", "--port", "9000"]) == 0
    assert (calls[1]["host"], calls[1]["port"]) == ("0.0.0.0", 9000)


def test_the_make_target_runs_it_on_port_8099() -> None:
    makefile = (ROOT / "Makefile").read_text()
    phony = " ".join(re.findall(r"^\.PHONY:(.*)$", makefile, flags=re.MULTILINE))
    assert "fake-github" in phony.split()
    recipe = re.search(r"^fake-github:\n((?:\t.*\n?)+)", makefile, flags=re.MULTILINE)
    assert recipe and "tests/fakes/fake_github.py" in recipe.group(1)
    assert "--port 8099" in recipe.group(1)
