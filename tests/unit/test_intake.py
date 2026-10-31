"""Repo allowlist and issue intake (LH-303).

issue_to_spec does no I/O, so most tests hand it plain dicts shaped like GitHub's JSON. One test
runs the whole path against FakeGitHub through GitHubClient, to check that the shapes the client
really returns (a null body, the raw permission answer) are the ones intake reads.

The repo_settings upsert needs Postgres and is tested in tests/integration/test_intake_sync.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub, ManualClock

from longhaul.contracts import Budgets, RepoSettings, TaskSpec
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.intake import (
    CUT_NOTE,
    MAX_BODY_BYTES,
    MAX_COMMENTS_BYTES,
    allowlisted_entry,
    is_allowlisted,
    issue_to_spec,
    repo_rows,
    sync_repo_settings,
    task_id_for,
)
from longhaul.settings import AppConfig, RepoEntry, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
REPO = "acme/calc"
TOKEN = "ghp_unit_test_token_0123456789abcdef"
LOGGER = "longhaul.github.intake"
MARKER = "<!-- longhaul:key=lh-acme-calc-42:pickup:0 -->"


# --- helpers ----------------------------------------------------------------------------------


def entry(repo: str = REPO, *, allowlisted: bool = True, **fields: Any) -> RepoEntry:
    return RepoEntry(repo=repo, allowlisted=allowlisted, **fields)


def issue(
    number: int = 42,
    title: str = "add() is wrong",
    body: str | None = "It returns a - b.",
    **extra: Any,
) -> dict[str, Any]:
    """An issue as GitHub's REST API sends it, trimmed to what matters."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "user": {"login": "octocat"},
        **extra,
    }


def comment(login: str | None, body: str | None) -> dict[str, Any]:
    user = None if login is None else {"login": login, "type": "User"}
    return {"id": 1, "body": body, "user": user}


def permission(level: str) -> dict[str, Any]:
    """The raw answer of GET /collaborators/{login}/permission, trimmed to what matters."""
    return {"permission": level, "role_name": level}


def config_with(*entries: RepoEntry) -> AppConfig:
    return load_config(EXAMPLE).model_copy(update={"repos": list(entries)})


def steering(comments: list[dict[str, Any]], permissions: dict[str, Any]) -> list[str]:
    spec = issue_to_spec(entry(), issue(), comments, permissions)
    assert spec is not None
    return spec.comments


# --- the allowlist ----------------------------------------------------------------------------


def test_non_allowlisted_ignored(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    comments = [comment("maintainer", "please keep it an int")]
    permissions = {"maintainer": permission("admin")}

    spec = issue_to_spec(entry(allowlisted=False), issue(), comments, permissions)

    assert spec is None
    [record] = [r for r in caplog.records if r.name == LOGGER]
    message = record.getMessage()
    assert "acme/calc" in message
    assert "42" in message
    assert "not allowlisted" in message  # the log line says why


def test_the_same_issue_on_an_allowlisted_repo_does_produce_a_spec() -> None:
    # The control for the test above: only the flag differs.
    assert issue_to_spec(entry(allowlisted=True), issue(), [], {}) is not None


def test_a_repo_is_not_allowlisted_unless_the_config_says_so() -> None:
    # RepoEntry defaults the flag to False. Intake has to honour the default, not assume a yes.
    assert issue_to_spec(RepoEntry(repo=REPO), issue(), [], {}) is None


def test_a_repo_that_is_not_allowlisted_is_skipped_before_the_issue_is_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)

    assert issue_to_spec(entry(allowlisted=False), {}, [], {}) is None

    assert "not allowlisted" in caplog.text


def test_a_pull_request_is_not_a_task(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    pull = issue(number=7, pull_request={"url": "https://api.github.com/repos/acme/calc/pulls/7"})

    assert issue_to_spec(entry(), pull, [], {}) is None

    assert "pull request" in caplog.text
    assert "7" in caplog.text


def test_a_local_repo_has_no_github_issues(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)

    assert issue_to_spec(entry("local:/work/calc"), issue(), [], {}) is None

    assert "local" in caplog.text


def test_is_allowlisted_follows_the_flag_and_ignores_case() -> None:
    config = config_with(entry("Acme/Calc"), entry("acme/off", allowlisted=False))

    assert is_allowlisted("acme/calc", config)
    assert is_allowlisted("ACME/CALC", config)
    assert not is_allowlisted("acme/off", config)
    assert not is_allowlisted("acme/other", config)
    assert allowlisted_entry("acme/calc", config) is config.repos[0]
    assert allowlisted_entry("acme/off", config) is None


def test_a_local_repo_matches_its_path_exactly() -> None:
    # A path can be case sensitive, unlike a GitHub name.
    config = config_with(entry("local:/work/Calc"))

    assert is_allowlisted("local:/work/Calc", config)
    assert not is_allowlisted("local:/work/calc", config)


def test_a_repo_listed_twice_is_a_config_error_not_a_guess() -> None:
    # GitHub ignores case, so these name one repo, and the two flags disagree.
    config = config_with(entry("acme/calc"), entry("Acme/Calc", allowlisted=False))

    with pytest.raises(ValueError, match="twice"):
        is_allowlisted("acme/calc", config)


# --- the task id ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "number", "expected"),
    [
        ("acme/calc", 42, "lh-acme-calc-42"),
        ("hoomanesteki/lh-demo-calc", 1, "lh-hoomanesteki-lh-demo-calc-1"),
        ("Acme/My.Repo_v2", 7, "lh-acme-my-repo-v2-7"),
        ("acme/my--repo", 1, "lh-acme-my-repo-1"),
        ("Some-Org/.github", 3, "lh-some-org-github-3"),
        ("acme/Repo_", 5, "lh-acme-repo-5"),
        ("ACME/CALC", 42, "lh-acme-calc-42"),
    ],
)
def test_task_id_format(repo: str, number: int, expected: str) -> None:
    assert task_id_for(repo, number) == expected
    assert task_id_for(repo, number) == expected  # stable: same input, same id


def test_task_id_format_is_safe_for_blob_keys_and_workflow_ids() -> None:
    # ADR-0005: each part of a blob key starts with a letter or digit and holds only letters,
    # digits, dot, underscore and hyphen. The task id is one such part.
    task_id = task_id_for("Weird-Org/__Weird..Name__", 123)

    assert task_id == "lh-weird-org-weird-name-123"
    assert task_id.isascii()
    assert task_id == task_id.lower()
    assert "--" not in task_id


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "acme",
        "acme/",
        "/calc",
        "acme/calc/extra",
        "local:/abs/path",
        "acme/ca lc",
        "acme/..",
        "-/_",
        "acme/calc?x=1",
    ],
)
def test_task_id_refuses_a_name_that_is_not_owner_slash_name(repo: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        task_id_for(repo, 1)


@pytest.mark.parametrize("number", [0, -1, True, "7"])
def test_task_id_refuses_a_number_that_is_not_a_positive_integer(number: Any) -> None:
    with pytest.raises(ValueError, match="positive"):
        task_id_for(REPO, number)


def test_the_spec_uses_the_same_id() -> None:
    spec = issue_to_spec(entry("Acme/My.Repo_v2"), issue(number=9), [], {})

    assert spec is not None
    assert spec.task_id == "lh-acme-my-repo-v2-9"
    assert spec.repo == "Acme/My.Repo_v2"  # the name as the config wrote it


# --- the spec ---------------------------------------------------------------------------------


def test_the_spec_carries_the_issue_and_the_repo_settings() -> None:
    settings = entry(
        plan_approval_required=True,
        setup_command="pip install -e .",
        test_command="pytest -q",
        budgets={"task_usd": 2.5},
    )

    spec = issue_to_spec(settings, issue(), [], {})

    assert spec == TaskSpec(
        task_id="lh-acme-calc-42",
        repo="acme/calc",
        issue_number=42,
        title="add() is wrong",
        body="It returns a - b.",
        comments=[],
        settings=RepoSettings(
            repo="acme/calc",
            plan_approval_required=True,
            setup_command="pip install -e .",
            test_command="pytest -q",
            budgets=Budgets(task_usd=2.5),
        ),
    )
    assert spec is not None
    # Plain contract types. A RepoEntry inside a TaskSpec would not equal itself after a trip
    # through Temporal's JSON, which returns the plain classes.
    assert type(spec.settings) is RepoSettings
    assert type(spec.settings.budgets) is Budgets


def test_the_spec_survives_a_json_round_trip() -> None:
    spec = issue_to_spec(entry(), issue(), [comment("alice", "hi")], {"alice": permission("write")})

    assert spec is not None
    assert TaskSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_body_becomes_an_empty_string(empty: str | None) -> None:
    # GitHub sends null for an issue without a description, and TaskSpec.body is a string.
    spec = issue_to_spec(entry(), issue(body=empty), [], {})

    assert spec is not None
    assert spec.body == ""


def test_the_base_branch_is_left_for_the_workspace_step() -> None:
    # Intake does not call the API. The default branch and its sha are resolved in LH-305.
    spec = issue_to_spec(entry(), issue(), [], {})

    assert spec is not None
    assert (spec.base_branch, spec.base_sha) == ("main", None)


@pytest.mark.parametrize(
    "broken",
    [
        {"title": "no number"},
        {"number": 0, "title": "zero"},
        {"number": -3, "title": "negative"},
        {"number": True, "title": "a bool is not a number"},
        {"number": "42", "title": "a string is not a number"},
        {"number": 1},
        {"number": 1, "title": None},
        {"number": 1, "title": "t", "body": 5},
    ],
)
def test_a_malformed_issue_is_an_error_and_not_a_quiet_skip(broken: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="issue"):
        issue_to_spec(entry(), broken, [], {})


def test_text_that_postgres_or_json_would_refuse_is_cleaned() -> None:
    # A NUL cannot go into a Postgres text column, and a lone surrogate cannot be written as
    # JSON. Either one would fail the same insert on every poll of the same issue.
    spec = issue_to_spec(
        entry(),
        issue(title="add\x00() is wrong", body="a\x00b \ud800 c"),
        [comment("alice", "x\x00y")],
        {"alice": permission("write")},
    )

    assert spec is not None
    assert spec.title == "add() is wrong"
    assert spec.body == "ab ? c"
    assert spec.comments == ["xy"]
    assert spec.model_dump_json()  # would raise on a lone surrogate


# --- comments ---------------------------------------------------------------------------------


def test_comment_permission_filter() -> None:
    comments = [
        comment("alice", "admins may steer"),
        comment("carol", "readers may not"),
        comment("bob", "writers may steer"),
        comment("dave", "people with no access may not"),
        comment("erin", "people nobody looked up may not"),
    ]
    permissions = {
        "alice": permission("admin"),
        "bob": permission("write"),
        "carol": permission("read"),
        "dave": permission("none"),
    }

    assert steering(comments, permissions) == ["admins may steer", "writers may steer"]


def test_without_any_permissions_no_comment_is_used() -> None:
    # Unknown means no. A caller that forgot to look the users up gets no comments, not all.
    assert steering([comment("alice", "hello")], {}) == []


@pytest.mark.parametrize(
    ("answer", "counts"),
    [
        (permission("admin"), True),
        (permission("write"), True),
        (permission("read"), False),
        (permission("none"), False),
        # The legacy field decides. The maintain role shows up as write, triage as read.
        ({"permission": "write", "role_name": "maintain"}, True),
        ({"permission": "read", "role_name": "triage"}, False),
        ({"permission": None}, False),
        ({"permission": ["write"]}, False),  # not a string, and must not raise
        ({}, False),
        (None, False),  # a failed lookup that a caller stored as None
        (42, False),
        ("admin", True),
        ("write", True),
        ("read", False),
        ("", False),
    ],
)
def test_which_permission_answers_count_as_write_access(answer: Any, counts: bool) -> None:
    used = steering([comment("alice", "hello")], {"alice": answer})

    assert used == (["hello"] if counts else [])


def test_logins_match_without_regard_to_case() -> None:
    # GitHub logins ignore case, and the caller may build its keys from either side.
    assert steering([comment("Alice", "one")], {"alice": permission("write")}) == ["one"]
    assert steering([comment("alice", "two")], {"ALICE": permission("admin")}) == ["two"]


@pytest.mark.parametrize(
    "body",
    [
        f"Picked up.\n\n{MARKER}",
        "<!--longhaul:key=abc-->",
        "<!--   longhaul:key=lh-acme-calc-42:pr:1   -->",
        "text first <!-- longhaul:key=k --> text after",
        "<!-- longhaul:key=spans\ntwo lines -->",
    ],
)
def test_our_own_comments_are_left_out_even_from_a_writer(body: str) -> None:
    # With a personal token, Longhaul's comments come from an account that has write access.
    comments = [comment("longhaul-bot", body), comment("alice", "a real instruction")]
    permissions = {"longhaul-bot": permission("write"), "alice": permission("write")}

    assert steering(comments, permissions) == ["a real instruction"]


@pytest.mark.parametrize(
    "body",
    [
        "Longhaul, please use the longhaul label",
        "<!-- a note for reviewers -->",
        "longhaul:key=not-a-comment-marker",
    ],
)
def test_a_comment_that_only_mentions_longhaul_is_kept(body: str) -> None:
    assert steering([comment("alice", body)], {"alice": permission("write")}) == [body]


def test_unclosed_markers_are_not_our_marker() -> None:
    # A marker has to close. A long body of openers is somebody's text, kept and then cut to
    # the budget, and the search for a closing mark must not make it slow.
    body = "<!-- longhaul:key=" * 3_000

    used = steering([comment("alice", body)], {"alice": permission("write")})

    assert used == [body[:MAX_COMMENTS_BYTES] + CUT_NOTE]


def test_blank_and_ownerless_comments_are_dropped() -> None:
    comments = [
        comment("alice", None),
        comment("alice", ""),
        comment("alice", " \n\t "),
        comment(None, "from a deleted account"),
        comment("alice", "the only real one"),
    ]

    assert steering(comments, {"alice": permission("write")}) == ["the only real one"]


def test_a_comment_keeps_its_text_as_written() -> None:
    # The prompt builder trims and normalizes line endings. Intake passes the text through.
    text = "  line one\r\nline two\r\n"

    assert steering([comment("alice", text)], {"alice": permission("write")}) == [text]


# --- size limits ------------------------------------------------------------------------------


def test_a_long_body_is_cut_and_says_so() -> None:
    body = "a" * (MAX_BODY_BYTES + 500)

    spec = issue_to_spec(entry(), issue(body=body), [], {})

    assert spec is not None
    assert spec.body == "a" * MAX_BODY_BYTES + CUT_NOTE


def test_a_body_at_the_limit_is_kept_whole() -> None:
    body = "a" * MAX_BODY_BYTES

    spec = issue_to_spec(entry(), issue(body=body), [], {})

    assert spec is not None
    assert spec.body == body


def test_the_cut_never_splits_a_character() -> None:
    # Three bytes each, so the byte limit falls inside the 2667th character.
    body = "€" * MAX_BODY_BYTES

    spec = issue_to_spec(entry(), issue(body=body), [], {})

    assert spec is not None
    kept = MAX_BODY_BYTES // 3
    assert spec.body == "€" * kept + CUT_NOTE
    assert "�" not in spec.body


def test_old_comments_go_first_when_the_thread_is_long() -> None:
    # Four comments of this size fill the budget exactly (the constant is a multiple of four).
    size = MAX_COMMENTS_BYTES // 4
    texts = [f"comment {n:02d} ".ljust(size, "x") for n in range(12)]
    comments = [comment("alice", text) for text in texts]

    used = steering(comments, {"alice": permission("write")})

    assert used[0] == "[8 earlier comments left out because the thread is long]"
    assert used[1:] == texts[-4:]  # the newest, in the order they were written


def test_one_left_out_comment_is_counted_in_the_singular() -> None:
    size = MAX_COMMENTS_BYTES // 4
    texts = [f"comment {n}".ljust(size, "x") for n in range(5)]

    used = steering([comment("alice", t) for t in texts], {"alice": permission("write")})

    assert used[0] == "[1 earlier comment left out because the thread is long]"


def test_one_huge_comment_is_cut_to_the_room_there_is() -> None:
    huge = "y" * (MAX_COMMENTS_BYTES * 3)

    used = steering([comment("alice", huge)], {"alice": permission("write")})

    assert used == ["y" * MAX_COMMENTS_BYTES + CUT_NOTE]


def test_text_at_every_limit_still_fits_the_16_kb_rule() -> None:
    # CONTRIBUTING.md rule 5: anything over 16 KB that crosses an activity boundary goes to the blob
    # store. The spec is part of the carry state, so its text has to stay under that.
    big = "€" * 50_000
    title = "t" * 256
    comments = [comment("alice", big) for _ in range(5)]

    spec = issue_to_spec(entry(), issue(title=title, body=big), comments, {"alice": "write"})

    assert spec is not None
    assert len(spec.model_dump_json().encode()) < 16_000


# --- repo_settings rows -----------------------------------------------------------------------


def test_repo_rows_keep_the_flag_and_the_settings_apart_from_the_name() -> None:
    config = config_with(
        entry("hoomanesteki/lh-demo-textkit", allowlisted=False, test_command="pytest"),
        entry(
            "hoomanesteki/lh-demo-calc",
            plan_approval_required=True,
            setup_command="make setup",
            test_command="pytest -q",
            budgets={"task_usd": 2.5},
        ),
        entry("local:/work/calc"),
    )

    rows = repo_rows(config)

    assert [row["repo"] for row in rows] == [  # sorted, so two writers lock rows in one order
        "hoomanesteki/lh-demo-calc",
        "hoomanesteki/lh-demo-textkit",
        "local:/work/calc",
    ]
    calc, textkit, local = rows
    assert calc == {
        "repo": "hoomanesteki/lh-demo-calc",
        "allowlisted": True,
        "settings": {
            "plan_approval_required": True,
            "setup_command": "make setup",
            "test_command": "pytest -q",
            "budgets": {
                "task_usd": 2.5,
                "task_turns": 150,
                "task_wall_minutes": 240,
                "daily_repo_usd": 25.0,
            },
        },
    }
    assert textkit["allowlisted"] is False
    assert local["allowlisted"] is True


def test_a_stored_row_rebuilds_the_settings_the_spec_carries() -> None:
    settings = entry(test_command="pytest -q", budgets={"daily_repo_usd": 10})
    [row] = repo_rows(config_with(settings))

    rebuilt = RepoSettings(repo=row["repo"], **row["settings"])

    spec = issue_to_spec(settings, issue(), [], {})
    assert spec is not None
    assert rebuilt == spec.settings


@pytest.mark.parametrize(
    "repo", ["acme/calc", "My-Org/My.Repo_v2", "local:/abs/path", "local:calc"]
)
def test_repo_settings_accept_owner_name_and_the_local_prefix(repo: str) -> None:
    assert RepoSettings(repo=repo).repo == repo
    assert RepoEntry(repo=repo).repo == repo
    [row] = repo_rows(config_with(entry(repo)))
    assert row["repo"] == repo


@pytest.mark.parametrize(
    ("repos", "message"),
    [
        (["acme"], "owner/name"),
        (["acme/ca lc"], "owner/name"),
        (["acme/calc/extra"], "owner/name"),
        (["local:"], "local:"),
        (["acme/calc", "Acme/CALC"], "twice"),
        (["acme/calc", "acme/other", "acme/calc"], "twice"),
        # Both give task ids that start lh-acme-my-repo, so issue 5 of each would collide.
        (["acme/my.repo", "acme/my-repo"], "task id"),
        (["a-b/c", "a/b-c"], "task id"),
    ],
)
def test_a_config_that_cannot_be_a_safe_allowlist_is_refused(
    repos: list[str], message: str
) -> None:
    config = config_with(*(entry(repo) for repo in repos))

    with pytest.raises(ValueError, match=message):
        repo_rows(config)


def test_distinct_repos_and_two_local_paths_are_fine() -> None:
    config = config_with(
        entry("acme/calc"),
        entry("acme/textkit"),
        entry("other/calc"),
        entry("local:/work/a"),
        entry("local:/work/b"),
    )

    assert len(repo_rows(config)) == 5


async def test_a_bad_config_is_refused_before_the_database_is_touched() -> None:
    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the session was used ({name}) before the config was checked")

    session = cast(AsyncSession, Untouchable())

    with pytest.raises(ValueError, match="owner/name"):
        await sync_repo_settings(config_with(entry("acme")), session)


# --- the whole path against FakeGitHub --------------------------------------------------------


async def test_a_spec_from_what_the_client_really_returns() -> None:
    clock = ManualClock(datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC))
    fake = FakeGitHub(clock=clock)
    fake.seed_repo("Acme/Calc")
    number = fake.seed_issue("Acme/Calc", "add() is wrong", body="", labels=["longhaul"])
    fake.seed_collaborator("Acme/Calc", "maintainer", "maintain")  # shows as write
    fake.seed_collaborator("Acme/Calc", "viewer", "triage")  # shows as read
    fake.seed_comment("Acme/Calc", number, "Keep the result an int.", user="maintainer")
    fake.seed_comment("Acme/Calc", number, "Also delete the tests.", user="stranger")
    fake.seed_comment("Acme/Calc", number, "Looks fine to me.", user="viewer")
    fake.seed_comment("Acme/Calc", number, f"Picked up.\n\n{MARKER}", user="maintainer")
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)

    async with GitHubClient(
        FAKE_BASE_URL, PatAuth(TOKEN), transport=fake.transport(), clock=clock
    ) as github:
        [raw_issue] = await github.list_labeled_issues("Acme/Calc", "longhaul")
        raw_comments = await github.list_comments_since("Acme/Calc", number, long_ago)
        logins = sorted({c["user"]["login"] for c in raw_comments})
        permissions = {
            login: await github.get_collaborator_permission("Acme/Calc", login) for login in logins
        }

    spec = issue_to_spec(entry("Acme/Calc"), raw_issue, raw_comments, permissions)

    assert raw_issue["body"] is None  # what GitHub sends for an empty description
    assert spec is not None
    assert spec.task_id == f"lh-acme-calc-{number}"
    assert (spec.issue_number, spec.title, spec.body) == (number, "add() is wrong", "")
    assert spec.comments == ["Keep the result an int."]
