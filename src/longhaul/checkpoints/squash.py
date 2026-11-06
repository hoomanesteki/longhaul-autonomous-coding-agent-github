"""The pull request branch of a task: what a reviewer reads (LH-306, ADR-0006).

The scratch branch `longhaul/<n>` holds one commit for every tool call that changed a file. It
is a log of the agent's moves, with titles such as `lh: edit_file turn 3 call c1`, and nobody
should have to read it. The branch of the pull request, `longhaul-pr/<n>`, is built from it on
the host: one commit for each group of work, written in the style guide
(`fix(calc): add returns the sum`) and made by the configured author. In Phase 3 there is one
group, titled from the summary the agent gave to `finish`. Phase 5 will pass one group for each
plan step.

How a commit is made. A group is `(title, last_sha)`, where `last_sha` is the checkpoint at the
end of the group. The commit for it is a new commit with the tree of `last_sha`, on top of the
commit for the group before (the base for the first). That is the diff of the group, applied,
and it needs no working tree and no patch to apply: it cannot conflict, and the last commit has
the tree of the last checkpoint to the byte. A group that changes nothing makes no commit.

The commits are the same every time. A retried `deliver` builds the branch again and then pushes
it, and a push is never forced (checkpoints/push.py), so a second build that made a different
commit would be refused after the first had been pushed. The author, the committer and both
dates are therefore fixed: the names come from the setting, and the date is the date of the
checkpoint commit, not the clock.

The repo is the agent's. It can write refs and objects there, so this module reads only what it
needs, with hooks off, and checks every sha it is given. The base is the `base` tag that
init_task wrote. The branch is checked against the default branch with `assert_not_default`
before anything is written (failure mode F15, invariant I5), and the default branch itself is
never touched: the only ref this module writes is `refs/heads/longhaul-pr/<n>`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from longhaul.checkpoints.git_store import (
    BASE_TAG,
    CheckpointError,
    GitCheckpointStore,
    RepoNotFoundError,
    branch_name,
    check_sha,
    check_task_id,
    git,
    git_value,
)
from longhaul.checkpoints.push import assert_not_default, check_branch_name
from longhaul.settings import DEFAULT_GIT_AUTHOR

__all__ = [
    "MAX_SUBJECT_CHARS",
    "PR_BRANCH_PREFIX",
    "Author",
    "FileStat",
    "Group",
    "NothingToDeliverError",
    "PrBranch",
    "build_pr_branch",
    "commit_subject",
    "file_stats",
    "parse_author",
    "pr_branch_name",
    "repo_scope",
]

PR_BRANCH_PREFIX = "longhaul-pr/"
# The style guide says "under 72 characters".
MAX_SUBJECT_CHARS = 71
MAX_SCOPE_CHARS = 24


class NothingToDeliverError(Exception):
    """The work changes no file, so there is nothing to put on a pull request.

    A pull request with no commit is refused by GitHub, and asking again would not change that.
    """


class Group(NamedTuple):
    """The work of one pull request commit: its subject line and the checkpoint that ends it."""

    title: str
    last_sha: str


@dataclass(frozen=True)
class PrBranch:
    branch: str  # the name, without refs/heads/
    head_sha: str  # the last commit of the branch
    base_sha: str  # the commit that the first commit sits on


@dataclass(frozen=True)
class FileStat:
    """One changed file. The counts are None for a binary file, which has no lines to count."""

    path: str
    added: int | None
    deleted: int | None


@dataclass(frozen=True)
class Author:
    name: str
    email: str


# ---------- names ----------
def pr_branch_name(task_id: str) -> str:
    """`longhaul-pr/<n>` for the task of issue n, built like the scratch branch `longhaul/<n>`."""
    return PR_BRANCH_PREFIX + branch_name(task_id).removeprefix("longhaul/")


# Text before an address, and the address between angle brackets. Control characters and the
# brackets themselves cannot be part of a name, because git would write them into the commit.
_NAME = r"[^<>\x00-\x1f\x7f]+?"
_ADDRESS_PART = r"[^<>\s\x00-\x1f\x7f@]+"
_AUTHOR = re.compile(rf"(?P<name>{_NAME})\s*<(?P<email>{_ADDRESS_PART}@{_ADDRESS_PART})>")


def parse_author(text: str) -> Author:
    """Read `Name <address>`, the way git writes an author. Raise ValueError for anything else."""
    found = _AUTHOR.fullmatch(text.strip())
    if found is None:
        raise ValueError(
            f"LONGHAUL_GIT_AUTHOR must be written Name <address>, for example "
            f"{DEFAULT_GIT_AUTHOR!r}, not {text[:80]!r}"
        )
    return Author(found.group("name").strip(), found.group("email"))


# ---------- the subject line ----------
_TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
_CONVENTIONAL = re.compile(
    rf"(?P<type>{_TYPES})(?:\((?P<scope>[A-Za-z0-9._-]+)\))?(?P<bang>!)?:\s*(?P<rest>.+)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BOLD = re.compile(r"\*\*|__")
_LEADING_MARKUP = re.compile(r"^(?:[\s>#*+\-|]|\d+[.)]\s)+")
_HAS_WORD = re.compile(r"[A-Za-z0-9]")
# The way a model usually opens a summary, and the verb a commit subject wants instead.
_IMPERATIVE = {
    "added": "add",
    "adjusted": "adjust",
    "changed": "change",
    "corrected": "correct",
    "created": "create",
    "fixed": "fix",
    "handled": "handle",
    "implemented": "implement",
    "improved": "improve",
    "made": "make",
    "moved": "move",
    "removed": "remove",
    "renamed": "rename",
    "replaced": "replace",
    "resolved": "resolve",
    "returned": "return",
    "switched": "switch",
    "updated": "update",
}


def repo_scope(repo: str) -> str:
    """The name of a repo as the scope of a subject: `acme/Calc` gives `calc`.

    "" when the name has nothing a scope can hold.
    """
    name = repo.removeprefix("local:").rsplit("/", 1)[-1].lower()
    name = re.sub(r"[^a-z0-9._-]", "-", name).lstrip("._-")
    return name[:MAX_SCOPE_CHARS]


def _first_words(summary: str) -> str:
    """The first sentence of the first line of the summary that has words in it.

    A model writes Markdown: headings, bullets, quotes and bold. None of it belongs in a commit
    subject, so a heading or a code fence is skipped and the rest of the markup is stripped.
    """
    for raw in summary.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "```", "~~~")):
            continue
        line = _CONTROL.sub(" ", _BOLD.sub("", stripped))
        line = " ".join(_LEADING_MARKUP.sub("", line).split())
        if _HAS_WORD.search(line):
            return _SENTENCE_END.split(line, maxsplit=1)[0].rstrip(" .!?:;,")
    return ""


def _imperative(words: str) -> str:
    """Lower the first letter and turn a past tense opening into the imperative.

    Only the first letter changes case. The rest of the text may name code, such as parseURL, and
    a lower-cased name is a wrong name.
    """
    if not words:
        return words
    first, _, rest = words.partition(" ")
    first = _IMPERATIVE.get(first.lower(), first[:1].lower() + first[1:])
    return f"{first} {rest}".rstrip()


def commit_subject(summary: str, *, scope: str, fallback: str) -> str:
    """A conventional commit subject from the summary the agent gave to `finish`.

    The type and scope of a summary that is already a conventional subject (`feat(math): add
    pow`) are kept. Any other summary becomes `fix(<scope>): <first sentence>`, with the scope
    given by the caller, the first letter in lower case and a past tense opening ("Fixed")
    turned into the imperative ("fix"). The text is cut at a word to stay under 72 characters.
    `fallback` is the description for a summary with no words in it.
    """
    words = _first_words(summary)
    kind, bang, written_scope = "fix", "", ""
    found = _CONVENTIONAL.fullmatch(words)
    if found is not None:
        kind = found.group("type").lower()
        bang = found.group("bang") or ""
        written_scope = (found.group("scope") or "").lower()
        words = found.group("rest").strip()
    description = _imperative(words) or fallback
    scope = written_scope or scope
    head = f"{kind}({scope}){bang}: " if scope else f"{kind}{bang}: "
    return head + _fit(description, MAX_SUBJECT_CHARS - len(head))


def _fit(text: str, room: int) -> str:
    """The text, or the part of it that fits in `room` characters and ends at a word."""
    if len(text) <= room:
        return text
    cut = text[:room]
    if text[room] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-/(")


# ---------- reading the repo ----------
def _repo_of(task_id: str, store: GitCheckpointStore) -> Path:
    check_task_id(task_id)
    if not store.has_repo(task_id):
        raise RepoNotFoundError(f"task {task_id} has no checkpoint repo under {store.root}")
    return store.repo_path(task_id)


async def _tree(repo: Path, sha: str) -> str:
    return await git("rev-parse", "--verify", f"{sha}^{{tree}}", git_dir=repo)


async def _require_commit(repo: Path, sha: str) -> None:
    found = await git_value("cat-file", "-e", f"{check_sha(sha)}^{{commit}}", git_dir=repo)
    if found is None:
        raise CheckpointError(f"commit {sha} is not in the checkpoint repository")


async def _require_descendant(repo: Path, ancestor: str, sha: str, what: str) -> None:
    found = await git_value("merge-base", "--is-ancestor", ancestor, sha, git_dir=repo)
    if found is None:
        raise CheckpointError(f"checkpoint {sha} does not descend from {what}")


_COMMITTER_TIME = re.compile(r"^committer .* (\d+) [+-]\d{4}$", re.MULTILINE)


async def _committed_at(repo: Path, sha: str) -> str:
    """The date of a commit in the form git takes in GIT_*_DATE: seconds, then a zone."""
    raw = await git("cat-file", "commit", sha, git_dir=repo)
    found = _COMMITTER_TIME.search(raw)
    if found is None:
        raise CheckpointError(f"commit {sha} has no committer date")
    return f"{found.group(1)} +0000"


async def _base_commit(task_id: str, store: GitCheckpointStore) -> str:
    """The commit that the task started from, read from the `base` tag of its bare repo."""
    repo = _repo_of(task_id, store)
    base = await git_value(
        "rev-parse", "--verify", f"refs/tags/{BASE_TAG}^{{commit}}", git_dir=repo
    )
    if base is None:
        raise CheckpointError(f"the repository of task {task_id} has no '{BASE_TAG}' tag")
    return check_sha(base)


# ---------- the branch ----------
async def build_pr_branch(
    task_id: str,
    groups: Sequence[Group],
    *,
    store: GitCheckpointStore,
    default_branch: str,
    author: str = DEFAULT_GIT_AUTHOR,
) -> PrBranch:
    """Build `longhaul-pr/<n>` in the task's bare repo and return it.

    The branch starts at the base commit and gets one commit for each group, in order, as the
    module docstring describes. It is written once, at the end, so a failure leaves any earlier
    branch as it was. The same arguments give the same commits.

    `default_branch` is the repo's default branch as GitHub reports it now. Raises
    ForbiddenBranchError if the branch would be that one, NothingToDeliverError if no group
    changes a file, ValueError for an empty list or an author git would misread, and
    CheckpointError if a checkpoint is missing or does not descend from the base.
    """
    if not groups:
        raise ValueError("a pull request branch needs at least one group of work")
    who = parse_author(author)
    branch = check_branch_name(pr_branch_name(task_id))
    assert_not_default(branch, default_branch)
    repo = _repo_of(task_id, store)
    base = await _base_commit(task_id, store)
    parent_sha, parent_tree, last_end = base, await _tree(repo, base), base
    made = False
    for title, last_sha in groups:
        await _require_commit(repo, last_sha)
        after = "the base" if last_end == base else "the group before it"
        await _require_descendant(repo, last_end, last_sha, after)
        tree = await _tree(repo, last_sha)
        last_end = last_sha
        if tree == parent_tree:
            continue  # nothing of its own to say
        parent_sha = await _commit(repo, tree, parent_sha, title, last_sha, who)
        parent_tree, made = tree, True
    if not made:
        raise NothingToDeliverError(f"the work of task {task_id} changes no file")
    await git("update-ref", f"refs/heads/{branch}", parent_sha, git_dir=repo)
    return PrBranch(branch=branch, head_sha=parent_sha, base_sha=base)


async def _commit(
    repo: Path, tree: str, parent: str, title: str, checkpoint: str, who: Author
) -> str:
    when = await _committed_at(repo, checkpoint)
    env = {
        f"GIT_{role}_{field}": value
        for role in ("AUTHOR", "COMMITTER")
        for field, value in (("NAME", who.name), ("EMAIL", who.email), ("DATE", when))
    }
    sha = await git("commit-tree", tree, "-p", parent, "-m", title, git_dir=repo, env=env)
    return check_sha(sha)


# ---------- the diff stat ----------
async def file_stats(
    task_id: str, base_sha: str, head_sha: str, *, store: GitCheckpointStore
) -> list[FileStat]:
    """The files that differ between two commits, with their added and deleted lines.

    Paths are read from `-z` output, so a name with a tab, a quote or a newline comes back
    whole. Renames are shown as a delete and an add, which is what the lines of the diff say.
    The agent can write the config of this repo, so no external diff program or text
    conversion runs here.
    """
    repo = _repo_of(task_id, store)
    out = await git(
        "diff", "--numstat", "-z", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv",
        check_sha(base_sha), check_sha(head_sha), "--",
        git_dir=repo, strip=False,
    )  # fmt: skip
    stats: list[FileStat] = []
    for record in out.split("\0"):
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        stats.append(FileStat(path, _count(added), _count(deleted)))
    return stats


def _count(text: str) -> int | None:
    return None if text == "-" else int(text)
