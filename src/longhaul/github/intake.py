"""Repo allowlist and issue intake (LH-303).

Longhaul works only where it was invited. The invitation is `allowlisted: true` on a repo in
longhaul.yaml, and this module is the one place that turns that flag into behavior:

* `sync_repo_settings` copies the YAML repos into the `repo_settings` table, the copy the API
  reads. A repo that leaves the YAML is switched off, not deleted. Leaving its flag at true
  would keep inviting Longhaul to a repo the operator just removed, and deleting the row
  would orphan the tasks and costs recorded against it.
* `is_allowlisted` and `allowlisted_entry` answer from the config. Names are matched the way
  GitHub does, ignoring case, so `Acme/Calc` in the YAML and `acme/calc` from the API are one
  repo. A `local:` path is matched exactly, because a path can be case sensitive.
* `issue_to_spec` turns a GitHub issue into a TaskSpec, or into None with a log line that
  says why. It does no I/O. The caller fetches the issue, its comments and the commenters'
  permissions, so the function can be tested with plain dicts.
* `task_id_for` builds the task id, which is also the Temporal workflow id.

What goes into the spec, and why:

* The body comes from the issue. GitHub sends null for an empty description, so it becomes "".
* A comment is used only when its author has admin or write permission on the repo and the
  comment is not one of ours. Anyone can comment on a public issue, and the comments end up
  in the model's prompt. A person with write access can already push code, so they gain
  nothing by steering the agent. Our own comments carry a hidden marker (LH-304 appends it),
  and they are left out even though a personal token gives them an author with write access.
* A login that is missing from `permissions` counts as no access. Unknown means no.
* The text is cut to MAX_BODY_BYTES and MAX_COMMENTS_BYTES, and the cut says so. This is a
  stopgap. CONTRIBUTING.md rule 5 sends anything over 16 KB across an activity boundary as a
  BlobRef, and the spec is part of the carry state. GitHub allows 65,536 characters per body
  and per comment, so one long thread would break the rule. The real fix is a BlobRef for the full
  text on TaskSpec, which is a contracts change and is listed in docs/architecture.md under
  deferred contract changes. Until then the full text stays on the issue.
* Older comments go first, because a later comment usually corrects an earlier one.

Task ids. `lh-<owner>-<repo>-<n>` in lowercase, where every run of characters other than
a-z and 0-9 becomes one dash, so `Acme/My.Repo_v2` issue 7 is `lh-acme-my-repo-v2-7`. Two
different repos can still give the same id (`acme/my.repo` and `acme/my-repo`), and a start
with an id that is taken is a silent no-op (REJECT_DUPLICATE in durable/client.py). The
allowlist is a closed list, so the sync refuses a config that holds two such repos.
"""

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from longhaul.contracts import RepoSettings, TaskSpec
from longhaul.db.models import RepoSetting
from longhaul.settings import AppConfig, RepoEntry

__all__ = [
    "CUT_NOTE",
    "LOCAL_PREFIX",
    "MAX_BODY_BYTES",
    "MAX_COMMENTS_BYTES",
    "WRITE_PERMISSIONS",
    "allowlisted_entry",
    "is_allowlisted",
    "is_local",
    "issue_to_spec",
    "repo_rows",
    "sync_repo_settings",
    "task_id_for",
]

logger = logging.getLogger(__name__)

LOCAL_PREFIX = "local:"

# GET /collaborators/{login}/permission answers in `permission` with admin, write, read or
# none. The maintain role shows up there as write and triage as read, so these two are every
# role that can push.
WRITE_PERMISSIONS = frozenset({"admin", "write"})

# The hidden marker that every comment of ours ends with, `<!-- longhaul:key=<key> -->`.
# side_effects.py (LH-304) writes it and must keep this shape. Its keys are about 50 characters
# long. The bound on the key keeps a body full of unclosed markers from making the search slow.
_OWN_COMMENT = re.compile(r"<!--\s*longhaul:key=[^>]{0,500}-->")

# Together with the title and the settings, these keep the spec under 16 KB (CONTRIBUTING.md rule
# 5). The limits count UTF-8 bytes, which is what the JSON payload counts.
MAX_BODY_BYTES = 8_000
MAX_COMMENTS_BYTES = 6_000
# Added after the kept text, so it is not part of the limit.
CUT_NOTE = "\n\n[cut here: the rest is on the GitHub issue]"

_NAME_PART = re.compile(r"[A-Za-z0-9._-]+")
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


# ---------- repo names and task ids ----------
def is_local(repo: str) -> bool:
    return repo.startswith(LOCAL_PREFIX)


def _valid_part(part: str) -> bool:
    # An owner or a name is made of letters, digits, dot, underscore and hyphen, and holds at
    # least one letter or digit. That rules out ".." and keeps the task id from coming out empty.
    return _NAME_PART.fullmatch(part) is not None and _HAS_ALNUM.search(part) is not None


def _split_repo(repo: str) -> tuple[str, str]:
    owner, slash, name = repo.partition("/")
    if not slash or not _valid_part(owner) or not _valid_part(name):
        raise ValueError(f"{repo!r} is not a GitHub repo written owner/name")
    return owner, name


def _slug(text: str) -> str:
    return _NOT_ALNUM.sub("-", text.lower()).strip("-")


def _task_prefix(repo: str) -> str:
    owner, name = _split_repo(repo)
    return f"lh-{_slug(f'{owner}/{name}')}"


def task_id_for(repo: str, issue_number: int) -> str:
    """The task id, and workflow id, of an issue: `lh-<owner>-<repo>-<n>` in lowercase."""
    prefix = _task_prefix(repo)
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
        raise ValueError(f"an issue number is a positive integer, got {issue_number!r}")
    return f"{prefix}-{issue_number}"


# ---------- the allowlist ----------
def _match_key(repo: str) -> str:
    # GitHub ignores case in owner and repo names. A local path can be case sensitive.
    return repo if is_local(repo) else repo.lower()


def _by_name(config: AppConfig) -> dict[str, RepoEntry]:
    found: dict[str, RepoEntry] = {}
    for entry in config.repos:
        key = _match_key(entry.repo)
        if key in found:
            raise ValueError(
                f"repos lists the same repo twice, as {found[key].repo!r} and {entry.repo!r}. "
                "GitHub ignores case in repo names, and two flags for one repo could disagree"
            )
        found[key] = entry
    return found


def allowlisted_entry(repo: str, config: AppConfig) -> RepoEntry | None:
    """The config entry for `repo` when it is allowlisted, otherwise None.

    Raises ValueError when the config lists the repo twice, because then the flag has two
    values and neither is safe to pick.
    """
    entry = _by_name(config).get(_match_key(repo))
    return entry if entry is not None and entry.allowlisted else None


def is_allowlisted(repo: str, config: AppConfig) -> bool:
    return allowlisted_entry(repo, config) is not None


# ---------- the repo_settings table ----------
def _check_name(repo: str) -> None:
    if not is_local(repo):
        _split_repo(repo)
    elif not repo[len(LOCAL_PREFIX) :]:
        raise ValueError("a local: repo needs a path after the prefix, as in local:/abs/path")


def _check_task_ids(entries: Iterable[RepoEntry]) -> None:
    taken: dict[str, str] = {}
    for entry in entries:
        if is_local(entry.repo):
            continue  # a local repo has no issues, so it never gets a task id
        prefix = _task_prefix(entry.repo)
        if prefix in taken:
            raise ValueError(
                f"{taken[prefix]!r} and {entry.repo!r} would give the same task id "
                f"({prefix}-<n>), so one of them would never start a task for an issue "
                "number that the other has used. Allowlist only one of them"
            )
        taken[prefix] = entry.repo


def repo_rows(config: AppConfig) -> list[dict[str, Any]]:
    """The rows `sync_repo_settings` writes, sorted by repo.

    `settings` holds every field of the entry except the name and the flag, so
    `RepoSettings(repo=row["repo"], **row["settings"])` rebuilds what a task runs with.

    Raises ValueError for a config that cannot be a safe allowlist: a name that is neither
    owner/name nor local:<path>, a repo listed twice, or two repos with the same task ids.
    """
    entries = sorted(_by_name(config).values(), key=lambda entry: entry.repo)
    for entry in entries:
        _check_name(entry.repo)
    _check_task_ids(entries)
    return [
        {
            "repo": entry.repo,
            "allowlisted": entry.allowlisted,
            "settings": entry.model_dump(mode="json", exclude={"repo", "allowlisted"}),
        }
        for entry in entries
    ]


async def _write_rows(session: AsyncSession, rows: list[dict[str, Any]]) -> list[str]:
    """Upsert the rows and switch off the rest. Returns the repos that were switched off."""
    if rows:
        upsert = insert(RepoSetting).values(rows)
        await session.execute(
            upsert.on_conflict_do_update(
                index_elements=[RepoSetting.repo],
                # created_at is left out on purpose: it stays the time of the first insert.
                set_={
                    "allowlisted": upsert.excluded.allowlisted,
                    "settings": upsert.excluded.settings,
                },
            )
        )
    switched_off = await session.execute(
        update(RepoSetting)
        .where(RepoSetting.repo.not_in([row["repo"] for row in rows]))
        .where(RepoSetting.allowlisted.is_(True))
        .values(allowlisted=False)
        .returning(RepoSetting.repo)
    )
    return sorted(switched_off.scalars())


async def sync_repo_settings(config: AppConfig, session: AsyncSession) -> None:
    """Make the `repo_settings` table match the repos in the config.

    Each repo is inserted, or updated in place when it is already there. A repo that is in
    the table and no longer in the config has its flag set to false and keeps its row.
    Running it twice changes nothing.

    The config is checked first (see `repo_rows`), and a bad config raises ValueError before
    anything is written. The writes are one transaction. When the session has none open, this
    function begins one and commits it. When the caller has begun one, the writes join it and
    the caller commits or rolls back. Rows are written in name order, so workers that start
    together with the same config take their row locks in the same order and do not deadlock.
    """
    rows = repo_rows(config)
    if session.in_transaction():
        switched_off = await _write_rows(session, rows)
    else:
        async with session.begin():
            switched_off = await _write_rows(session, rows)
    allowlisted = sum(bool(row["allowlisted"]) for row in rows)
    logger.info(
        "repo_settings synced: %d repos from the config, %d allowlisted", len(rows), allowlisted
    )
    if switched_off:
        logger.info("no longer allowlisted, they left the config: %s", ", ".join(switched_off))


# ---------- issues ----------
def _text(value: str | None) -> str:
    """Text from GitHub as a string that Postgres and JSON both accept.

    A NUL cannot go into a Postgres text column, and a lone surrogate cannot be written as
    JSON. Either would fail the same insert on every poll of the same issue.
    """
    if value is None:
        return ""
    return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def _clip(text: str, limit: int) -> str:
    """At most `limit` bytes of `text`, cut on a character boundary, then CUT_NOTE."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    # "ignore" drops the half of a character that the cut may have left at the end.
    return raw[:limit].decode("utf-8", "ignore") + CUT_NOTE


def _level(answer: object) -> str | None:
    """The `permission` string from a raw permission answer, or from a plain string."""
    if isinstance(answer, str):
        return answer
    if isinstance(answer, Mapping):
        level = answer.get("permission")
        return level if isinstance(level, str) else None
    return None


def _writers(permissions: Mapping[str, Mapping[str, Any] | str]) -> frozenset[str]:
    return frozenset(
        login.lower()
        for login, answer in permissions.items()
        if login and _level(answer) in WRITE_PERMISSIONS
    )


def _author(comment: Mapping[str, Any]) -> str:
    user = comment.get("user")  # null for an account that was deleted
    login = user.get("login") if isinstance(user, Mapping) else None
    return login.lower() if isinstance(login, str) else ""


def _steering_comments(
    comments: Sequence[Mapping[str, Any]], permissions: Mapping[str, Mapping[str, Any] | str]
) -> list[str]:
    writers = _writers(permissions)
    texts: list[str] = []
    for comment in comments:
        if _author(comment) not in writers:
            continue  # first, so that text from a stranger is never searched or kept
        raw = comment.get("body")
        body = _text(raw) if isinstance(raw, str) else ""
        if body.strip() and not _OWN_COMMENT.search(body):
            texts.append(body)
    return texts


def _fit_comments(texts: Sequence[str], limit: int) -> list[str]:
    """The newest comments that fit in `limit` bytes, oldest first, with a note on the rest."""
    kept: list[str] = []
    room = limit
    for text in reversed(texts):
        if room <= 0:
            break
        kept.append(_clip(text, room))
        room -= len(text.encode("utf-8"))
    kept.reverse()
    left_out = len(texts) - len(kept)
    if left_out:
        noun = "comment" if left_out == 1 else "comments"
        kept.insert(0, f"[{left_out} earlier {noun} left out because the thread is long]")
    return kept


def _read_issue(issue: Mapping[str, Any]) -> tuple[int, str, str]:
    number, title, body = issue.get("number"), issue.get("title"), issue.get("body")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"issue has no usable number: {number!r}")
    if not isinstance(title, str):
        raise ValueError(f"issue #{number} has no title")
    if body is not None and not isinstance(body, str):
        raise ValueError(f"issue #{number} has a body that is not text")
    return number, _text(title), _text(body)


def _why_not_a_task(entry: RepoEntry, issue: Mapping[str, Any]) -> str | None:
    if not entry.allowlisted:
        return "the repo is not allowlisted"
    if is_local(entry.repo):
        return "a local repo has no GitHub issues"
    if "pull_request" in issue:  # GitHub lists pull requests as issues
        return "it is a pull request, not an issue"
    return None


def _contract_settings(entry: RepoEntry) -> RepoSettings:
    # The plain contract class. A RepoEntry inside a TaskSpec would come back from Temporal's
    # JSON as a RepoSettings and no longer equal the spec it was built into.
    return RepoSettings.model_validate(entry.model_dump(exclude={"allowlisted"}))


def issue_to_spec(
    repo_settings: RepoEntry,
    issue: Mapping[str, Any],
    comments: Sequence[Mapping[str, Any]],
    permissions: Mapping[str, Mapping[str, Any] | str],
) -> TaskSpec | None:
    """The TaskSpec for an issue, or None when Longhaul should not work on it.

    `issue` and `comments` are GitHub's JSON. `permissions` maps a login to the answer of
    `GitHubClient.get_collaborator_permission` for that user (a plain permission string works
    too), and has to cover every login in `comments`. None comes back, with a log line that
    says why, for a repo that is not allowlisted, a local repo, and a pull request.

    Raises ValueError for an issue without a usable number or title, which means the client
    or the API changed, and a quiet skip would hide it.
    """
    reason = _why_not_a_task(repo_settings, issue)
    if reason is not None:
        logger.info("ignoring #%s of %s: %s", issue.get("number"), repo_settings.repo, reason)
        return None
    number, title, body = _read_issue(issue)
    return TaskSpec(
        task_id=task_id_for(repo_settings.repo, number),
        repo=repo_settings.repo,
        issue_number=number,
        title=title,
        body=_clip(body, MAX_BODY_BYTES),
        comments=_fit_comments(_steering_comments(comments, permissions), MAX_COMMENTS_BYTES),
        settings=_contract_settings(repo_settings),
    )
