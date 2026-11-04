"""The pull request branch: its name, its commits and the subject line of each (LH-306).

The branch is built on the host from the task's bare repo, with real git on temporary folders.
tests/integration/test_deliver.py builds it from a repo that a FakeGitHub remote cloned, and
pushes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from longhaul.checkpoints.squash import (
    MAX_SUBJECT_CHARS,
    Author,
    Group,
    NothingToDeliverError,
    PrBranch,
    build_pr_branch,
    commit_subject,
    file_stats,
    parse_author,
    pr_branch_name,
    repo_scope,
)
from tests.fakes.fake_github import git

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import BASE_TAG, CheckpointError, GitCheckpointStore
from longhaul.checkpoints.push import ForbiddenBranchError
from longhaul.settings import DEFAULT_GIT_AUTHOR

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

TASK = "lh-acme-calc-7"
AUTHOR = "longhaul <longhaul@users.noreply.github.com>"


# ---------- names and the author ----------
def test_the_branch_is_named_after_the_issue() -> None:
    assert pr_branch_name("lh-acme-calc-42") == "longhaul-pr/42"
    assert pr_branch_name("lh-local-calc-1a2b3c4d") == "longhaul-pr/lh-local-calc-1a2b3c4d"


def test_the_default_author_is_the_documented_one() -> None:
    assert DEFAULT_GIT_AUTHOR == "longhaul <longhaul@users.noreply.github.com>"


def test_an_author_is_a_name_and_an_address() -> None:
    assert parse_author("Ada Lovelace <ada@example.com>") == Author(
        "Ada Lovelace", "ada@example.com"
    )
    assert parse_author(AUTHOR) == Author("longhaul", "longhaul@users.noreply.github.com")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "longhaul",
        "<longhaul@example.com>",
        "longhaul <>",
        "longhaul <not an address>",
        "long\nhaul <a@example.com>",
        "long <haul> <a@example.com>",
        "longhaul <a@example.com> extra",
    ],
)
def test_an_author_that_git_would_misread_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="LONGHAUL_GIT_AUTHOR"):
        parse_author(text)


# ---------- the subject line ----------
@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        # The plan's own example shape: type, the repo as scope, an imperative description.
        ("Fixed add() to return the sum.", "fix(calc): fix add() to return the sum"),
        ("Added a multiply helper", "fix(calc): add a multiply helper"),
        # Only the first sentence, and only the first line.
        (
            "Changed add() to return a + b. All tests pass.",
            "fix(calc): change add() to return a + b",
        ),
        ("Updated the parser\n\nIt now reads UTF-8.", "fix(calc): update the parser"),
        # A summary that is already a conventional subject keeps its type and scope.
        ("feat(math): add pow", "feat(math): add pow"),
        ("Fix: handle empty input", "fix(calc): handle empty input"),
        ("refactor!: drop the old api", "refactor(calc)!: drop the old api"),
        # Markdown from the model does not reach the subject.
        ("## Summary\n\n**Fixed** the `add` bug.", "fix(calc): fix the `add` bug"),
        ("- Fixed the bug\n- Added a test", "fix(calc): fix the bug"),
        ("> Removed dead code", "fix(calc): remove dead code"),
        # The first letter is lowercase and the rest keeps its case, so a name stays a name.
        ("Handle parseURL for empty input", "fix(calc): handle parseURL for empty input"),
        # A trailing full stop is dropped.
        ("make the tests pass.", "fix(calc): make the tests pass"),
    ],
)
def test_the_subject_is_derived_from_the_summary(summary: str, expected: str) -> None:
    assert commit_subject(summary, scope="calc", fallback="resolve the issue") == expected


def test_a_long_summary_is_cut_at_a_word_under_the_limit() -> None:
    words = "the very long description of the work " * 10
    subject = commit_subject(f"Fixed {words}", scope="calc", fallback="resolve the issue")

    assert len(subject) <= MAX_SUBJECT_CHARS < 72
    prefix = "fix(calc): "
    rest = subject.removeprefix(prefix)
    full = f"fix {words}"
    assert subject.startswith(prefix)
    assert full.startswith(rest)
    assert full[len(rest)] == " ", "cut between words, not inside one"
    assert rest == rest.rstrip()


@pytest.mark.parametrize("summary", ["", "   \n\n  ", "...", "```\n```", "**", "-"])
def test_a_summary_with_no_words_gets_the_fallback(summary: str) -> None:
    assert (
        commit_subject(summary, scope="calc", fallback="resolve the issue")
        == "fix(calc): resolve the issue"
    )


def test_the_subject_has_no_scope_when_the_repo_name_gives_none() -> None:
    assert commit_subject("Fixed it", scope="", fallback="x") == "fix: fix it"


def test_the_subject_is_one_line_without_control_characters() -> None:
    subject = commit_subject("Fixed\tthe\x00 bug\r\nand more", scope="calc", fallback="x")
    assert subject == "fix(calc): fix the bug"
    assert subject.isprintable()


@pytest.mark.parametrize(
    ("repo", "scope"),
    [
        ("acme/calc", "calc"),
        ("Acme/My.Repo_v2", "my.repo_v2"),
        ("acme/--odd--", "odd--"),
        ("local:/home/me/calc", "calc"),
        ("acme/", ""),
    ],
)
def test_the_scope_is_the_repo_name_in_lowercase(repo: str, scope: str) -> None:
    assert repo_scope(repo) == scope


# ---------- the branch ----------
class Rig:
    """A task's bare repo with a base commit and a scratch branch that grows."""

    def __init__(self, tmp_path: Path) -> None:
        self.work = tmp_path / "work"
        self.work.mkdir()
        git("init", "-q", "-b", "main", cwd=self.work)
        self.store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
        self.write({"a.txt": "one\n", "b.txt": "keep\n"}, "initial")
        self.base = git("rev-parse", "HEAD", cwd=self.work)

    def write(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            (self.work / name).write_text(text)
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", message, cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    async def start(self) -> None:
        await self.store.init_task(TASK, str(self.work), self.base)

    def publish(self) -> None:
        """Copy the work branch into the task's bare repo, as the sandbox's pushes do."""
        bare = self.store.repo_path(TASK)
        git("push", "-q", "-f", str(bare), "HEAD:refs/heads/longhaul/7", cwd=self.work)

    def bare(self, *args: str) -> str:
        return git(f"--git-dir={self.store.repo_path(TASK)}", *args)

    async def build(self, *groups: Group, default: str = "main") -> PrBranch:
        return await build_pr_branch(
            TASK, list(groups), store=self.store, default_branch=default, author=AUTHOR
        )


@pytest.fixture
async def rig(tmp_path: Path) -> Rig:
    made = Rig(tmp_path)
    await made.start()
    return made


async def test_one_group_is_one_commit_on_top_of_base(rig: Rig) -> None:
    rig.write({"a.txt": "two\n"}, "lh: edit 1")
    head = rig.write({"c.txt": "new\n"}, "lh: write 2")
    rig.publish()

    built = await rig.build(Group("fix(calc): change a", head))

    assert built.branch == "longhaul-pr/7"
    assert rig.bare("rev-list", f"{rig.base}..{built.head_sha}").splitlines() == [built.head_sha]
    assert rig.bare("diff", rig.base, built.head_sha) == rig.bare("diff", rig.base, head)
    assert rig.bare("rev-parse", "refs/heads/longhaul-pr/7") == built.head_sha


async def test_each_group_is_one_commit_with_its_own_diff(rig: Rig) -> None:
    first = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    second = rig.write({"c.txt": "new\n"}, "lh: write 2")
    third = rig.write({"b.txt": "changed\n"}, "lh: edit 3")
    rig.publish()

    built = await rig.build(
        Group("fix(calc): a", first), Group("feat(calc): c", second), Group("fix(calc): b", third)
    )

    commits = rig.bare("rev-list", "--reverse", f"{rig.base}..{built.head_sha}").splitlines()
    assert len(commits) == 3
    subjects = [rig.bare("log", "-1", "--format=%s", sha) for sha in commits]
    assert subjects == ["fix(calc): a", "feat(calc): c", "fix(calc): b"]
    # Commit k holds the work of group k: its diff to the commit before is the diff between the
    # two checkpoints.
    parents = [rig.base, *commits[:-1]]
    ends = [first, second, third]
    starts = [rig.base, first, second]
    for commit, parent, start, end in zip(commits, parents, starts, ends, strict=True):
        assert rig.bare("rev-parse", f"{commit}^") == parent
        assert rig.bare("diff", parent, commit) == rig.bare("diff", start, end)


async def test_the_commits_are_the_same_when_built_again(rig: Rig) -> None:
    head = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()
    group = Group("fix(calc): change a", head)

    first = await rig.build(group)
    second = await rig.build(group)

    assert first == second


async def test_the_commit_dates_come_from_the_checkpoint_not_the_clock(rig: Rig) -> None:
    head = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()

    built = await rig.build(Group("fix(calc): change a", head))

    stamp = rig.bare("log", "-1", "--format=%ct", head)
    assert rig.bare("log", "-1", "--format=%ct", built.head_sha) == stamp
    assert rig.bare("log", "-1", "--format=%at", built.head_sha) == stamp


async def test_the_author_and_committer_are_the_configured_ones(rig: Rig) -> None:
    head = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()

    built = await build_pr_branch(
        TASK,
        [Group("fix(calc): change a", head)],
        store=rig.store,
        default_branch="main",
        author="Ada <ada@example.com>",
    )

    assert rig.bare("log", "-1", "--format=%an|%ae|%cn|%ce", built.head_sha) == (
        "Ada|ada@example.com|Ada|ada@example.com"
    )


async def test_the_author_comes_from_the_setting_by_default(rig: Rig) -> None:
    head = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()

    built = await build_pr_branch(
        TASK, [Group("fix(calc): x", head)], store=rig.store, default_branch="main"
    )

    assert rig.bare("log", "-1", "--format=%an <%ae>", built.head_sha) == DEFAULT_GIT_AUTHOR


async def test_a_branch_that_would_be_the_default_is_refused(rig: Rig) -> None:
    head = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()
    before = rig.bare("for-each-ref", "--format=%(refname) %(objectname)")

    with pytest.raises(ForbiddenBranchError):
        await rig.build(Group("fix(calc): x", head), default="longhaul-pr/7")

    assert rig.bare("for-each-ref", "--format=%(refname) %(objectname)") == before


async def test_work_that_changes_nothing_has_nothing_to_deliver(rig: Rig) -> None:
    with pytest.raises(NothingToDeliverError):
        await rig.build(Group("fix(calc): x", rig.base))

    assert "refs/heads/longhaul-pr/7" not in rig.bare("for-each-ref", "--format=%(refname)")


async def test_a_group_with_no_change_of_its_own_is_skipped(rig: Rig) -> None:
    first = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()

    built = await rig.build(Group("fix(calc): a", first), Group("fix(calc): again", first))

    assert len(rig.bare("rev-list", f"{rig.base}..{built.head_sha}").splitlines()) == 1


async def test_a_checkpoint_that_does_not_descend_from_base_is_refused(rig: Rig) -> None:
    rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()
    git("checkout", "-q", "--orphan", "other", cwd=rig.work)
    stray = rig.write({"z.txt": "z\n"}, "unrelated")
    git("push", "-q", "-f", str(rig.store.repo_path(TASK)), "HEAD:refs/heads/stray", cwd=rig.work)

    with pytest.raises(CheckpointError, match="does not descend from the base"):
        await rig.build(Group("fix(calc): x", stray))


async def test_a_sha_that_is_not_in_the_repo_is_refused(rig: Rig) -> None:
    with pytest.raises(CheckpointError):
        await rig.build(Group("fix(calc): x", "f" * 40))


async def test_an_empty_list_of_groups_is_refused(rig: Rig) -> None:
    with pytest.raises(ValueError, match="at least one group"):
        await rig.build()


async def test_the_base_is_the_tag_of_the_bare_repo(rig: Rig) -> None:
    head = rig.write({"a.txt": "two\n"}, "lh: edit 1")
    rig.publish()

    assert rig.bare("rev-parse", f"refs/tags/{BASE_TAG}") == rig.base
    built = await rig.build(Group("fix(calc): x", head))

    assert built.base_sha == rig.base


# ---------- the diff stat ----------
async def test_the_diff_stat_lists_each_file_with_its_lines(rig: Rig) -> None:
    rig.write({"a.txt": "two\nthree\n", "c.txt": "new\n"}, "lh: edit")
    (rig.work / "b.txt").unlink()
    head = rig.write({}, "lh: remove b")
    rig.publish()

    stats = await file_stats(TASK, rig.base, head, store=rig.store)

    assert [(s.path, s.added, s.deleted) for s in stats] == [
        ("a.txt", 2, 1),
        ("b.txt", 0, 1),
        ("c.txt", 1, 0),
    ]


async def test_a_binary_file_has_no_line_counts(rig: Rig) -> None:
    (rig.work / "img.bin").write_bytes(b"\x00\x01\x02\xff")
    git("add", "-A", cwd=rig.work)
    git("commit", "-q", "-m", "lh: add image", cwd=rig.work)
    head = git("rev-parse", "HEAD", cwd=rig.work)
    rig.publish()

    (stat,) = await file_stats(TASK, rig.base, head, store=rig.store)

    assert (stat.path, stat.added, stat.deleted) == ("img.bin", None, None)


async def test_a_file_name_with_odd_characters_is_read_back_whole(rig: Rig) -> None:
    name = "weird name\twith `ticks` and 'quotes'.txt"
    head = rig.write({name: "x\n"}, "lh: add odd file")
    rig.publish()

    (stat,) = await file_stats(TASK, rig.base, head, store=rig.store)

    assert stat.path == name
