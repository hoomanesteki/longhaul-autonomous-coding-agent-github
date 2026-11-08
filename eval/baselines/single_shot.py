"""The single-shot baseline: one model call, no tools, no loop (plan section 8.3, LH-903).

It answers one question: what does the harness add over asking a model once? The steps are the
ones a person would take with a chat window and no agent.

1. Retrieve. The five files whose text is closest to the issue by BM25 (`rank_bm25`), and the
   repo map of longhaul.memory.repo_map, which lists the other modules and what is in them.
2. Ask. One call through the project's provider protocol with no tools, for a unified diff.
3. Apply. `git apply --recount`, and when git refuses, `patch -p1 --fuzz=2`. Models count lines
   badly and misquote context, which the two flags forgive. The diff goes into a throwaway
   copy of the repo, and what comes back is `git diff` of the result, so the grader gets a
   clean patch whichever tool took the model's.

There is no second try. A reply with no diff, a diff that neither tool applies and a reply that
was cut off at max_tokens are all failed tasks, and they are paid for like any other call.

The repo map is a parameter and not built here, because it comes from a script that runs in a
sandbox (the repo's code never runs on the host). The runner builds it and calls `solve`. With the
scripted provider the reply is read from the script, so a dry run needs no key and costs nothing.

A diff is untrusted text. Before anything runs, every path in it is checked: no absolute path, no
`..` and nothing in `.git`. The paths are checked wherever a patch tool reads one, which is more
than the `---` and `+++` lines: `Index:` lines and context diff headers name files too, and the
patch of macOS follows a `..` in them. `patch` is then given only the headers and hunks of the
unified diff, so a name in a line that the check does not know about never reaches it. The copy
the diff is applied to is deleted afterwards, and the repo that `solve` is given is only read.

`solve` is `ask` followed by `apply_reply`. The runner calls the two apart: the call is paid for
when the provider answers, and the cost ledger has to hold it whatever the apply step does next.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rank_bm25 import BM25Okapi

from longhaul.agent.workspace import collect_source_files
from longhaul.contracts import CallOptions, LLMProvider, Message, Role, TextBlock, Usage
from longhaul.providers.base import format_role_line

TOP_FILES = 5
# A file longer than this is cut in the prompt, so that five of them cannot fill the window.
MAX_FILE_CHARS = 30_000
# A file bigger than this is not indexed: it is data, a lockfile or a generated file.
MAX_INDEX_BYTES = 200_000
TOOL_TIMEOUT_S = 60
STDERR_CHARS = 300

SYSTEM_PROMPT = "\n".join(
    [
        format_role_line(Role.CODER),
        "You fix a bug or add a feature in a Python repository. You get the issue, a map of",
        "the repo and the full text of the files that look most relevant. You have no tools",
        "and no second try.",
        "",
        "Reply with one unified diff that makes the change, and nothing else. Write paths",
        "relative to the repo root with the a/ and b/ prefixes, as `git diff` does. Copy the",
        "context lines exactly as they are in the files you were shown. Start a new file from",
        "/dev/null. Change the tests only if the issue asks for it.",
    ]
)

Method = Literal["git", "patch"]


class PatchError(ValueError):
    """The reply holds no diff that can be applied. The message says why, for a result file."""


@dataclass(frozen=True)
class AppliedPatch:
    patch: str  # `git diff` of the result: the form the grader applies
    method: Method  # the tool that took the model's diff


@dataclass(frozen=True)
class SingleShotResult:
    files: list[str]  # the files in the prompt, best first
    reply: str  # the text of the reply
    usage: Usage
    stop_reason: str
    patch: str  # empty when there is none
    method: Method | None
    error: str | None  # why there is no patch


# ---------- retrieving files ----------
_WORD = re.compile(r"[A-Za-z]+|\d+")
# A boundary inside a camelCase name: lower|Upper, and the end of an acronym (HTTPServer).
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def words(text: str) -> list[str]:
    """The lowercase words of a text, with snake_case and camelCase names split into theirs."""
    return [word.lower() for word in _WORD.findall(_CAMEL.sub(" ", text))]


def read_files(repo: Path) -> dict[str, str]:
    """The text files of the repo that the agent would see, by path. Binary files are left out."""
    found: dict[str, str] = {}
    for item in collect_source_files(repo):
        path = repo / item.path
        try:
            if path.stat().st_size <= MAX_INDEX_BYTES:
                found[item.path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return found


def rank(files: dict[str, str], problem: str, top: int = TOP_FILES) -> list[str]:
    """The `top` paths whose text is closest to the problem by BM25, best first.

    Files with the same score come in path order, so a run can be repeated.
    """
    paths = sorted(files)
    corpus = [words(files[path]) for path in paths]
    query = words(problem)
    if not query or not any(corpus):  # BM25 divides by the average length of a document
        scores = [0.0] * len(paths)
    else:
        scores = [float(score) for score in BM25Okapi(corpus).get_scores(query)]
    ordered = sorted(zip(paths, scores, strict=True), key=lambda item: (-item[1], item[0]))
    return [path for path, _ in ordered[:top]]


def retrieve(repo: Path, problem: str, top: int = TOP_FILES) -> list[str]:
    """The `top` files of `repo` that are closest to the problem."""
    return rank(read_files(repo), problem, top)


# ---------- the prompt ----------
def _fence_for(body: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def _clip(body: str) -> str:
    if len(body) <= MAX_FILE_CHARS:
        return body
    left = len(body) - MAX_FILE_CHARS
    return f"{body[:MAX_FILE_CHARS]}\n[cut: {left} more characters of this file]\n"


def build_prompt(problem: str, repo_map: str, files: dict[str, str]) -> str:
    """The one user message: the issue, the repo map, and each retrieved file in full."""
    parts = ["## Issue", "", problem.strip(), ""]
    if repo_map.strip():
        parts += ["## Repo map", "", repo_map.strip(), ""]
    parts.append("## Files")
    for path, body in files.items():
        shown = _clip(body)
        fence = _fence_for(shown)
        closing = "" if shown.endswith("\n") else "\n"
        parts += ["", f"### {path}", fence, f"{shown}{closing}{fence}"]
    return "\n".join(parts) + "\n"


# ---------- reading the reply ----------
_FENCED = re.compile(
    r"^(?P<fence>`{3,}|~{3,})[^\n]*\n(?P<body>.*?)^(?P=fence)[ \t]*$", re.MULTILINE | re.DOTALL
)
_FILE_HEADER = re.compile(r"^--- [^\n]+\n\+\+\+ ", re.MULTILINE)
_GIT_HEADER = re.compile(r"^diff --git ", re.MULTILINE)


def _has_diff(text: str) -> bool:
    return bool(_GIT_HEADER.search(text) or _FILE_HEADER.search(text))


def extract_diff(reply: str) -> str:
    """The diff in a reply: the fenced blocks that hold one, else the text from its first header.

    Models wrap a diff in prose and a code fence. The diffs of several fenced blocks are joined,
    for a reply that gives one block per file. An empty string means there is no diff.
    """
    blocks = [m.group("body") for m in _FENCED.finditer(reply) if _has_diff(m.group("body"))]
    if blocks:
        return "".join(block if block.endswith("\n") else f"{block}\n" for block in blocks)
    starts = [m.start() for m in (_GIT_HEADER.search(reply), _FILE_HEADER.search(reply)) if m]
    if not starts:
        return ""
    diff = reply[min(starts) :]
    return diff if diff.endswith("\n") else f"{diff}\n"


# ---------- applying the diff ----------
_PATH_LINE = re.compile(r"^(?:---|\+\+\+) (?P<path>[^\t\n]+)", re.MULTILINE)
_MOVE_LINE = re.compile(r"^(?:rename|copy) (?:from|to) (?P<path>[^\n]+)", re.MULTILINE)
_GIT_LINE = re.compile(r"^diff --git (?P<a>\S+) (?P<b>\S+)", re.MULTILINE)
# Names that patch reads and git does not: svn and cvs write `Index:`, and the old header of a
# context diff is a `*** ` line. They are checked, but never staged, so they are not in diff_paths.
_OTHER_NAME_LINE = re.compile(r"^(?:Index:[ \t]*|\*\*\*[ \t]+)(?P<path>[^\t\n]+)", re.MULTILINE)
# The escapes that git and GNU patch decode inside a quoted name: \056 is a dot.
_ESCAPE = re.compile(r'\\(?:(?P<octal>[0-7]{1,3})|(?P<letter>[abfnrtv\\"]))')
_LETTERS = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def _decoded(quoted: str) -> str:
    """The text of a quoted name with its escapes resolved. Octal escapes are bytes of UTF-8."""
    out = bytearray()
    last = 0
    for match in _ESCAPE.finditer(quoted):
        out += quoted[last : match.start()].encode("utf-8", errors="replace")
        octal = match.group("octal")
        out.append(int(octal, 8) & 0xFF if octal is not None else _LETTERS[match.group("letter")])
        last = match.end()
    out += quoted[last:].encode("utf-8", errors="replace")
    return out.decode("utf-8", errors="replace")


def _unquoted(path: str) -> str:
    path = path.strip()
    if len(path) > 1 and path[0] == path[-1] == '"':
        return _decoded(path[1:-1])
    return path


def _screened(raw: str) -> str:
    """The name without its quotes, or PatchError when it is a path the diff must not touch."""
    name = _unquoted(raw)
    if name == "/dev/null":
        return name
    parts = [part for part in name.split("/") if part]
    if name.startswith("/") or ".." in parts:
        raise PatchError(f"the diff names {name!r}, a path outside the repo")
    if any(part.lower() == ".git" for part in parts):
        raise PatchError(f"the diff names {name!r}, which is inside .git and not allowed")
    return name


def _checked(raw: str) -> str | None:
    """The path as the repo knows it (the first component dropped, as -p1 does), or None."""
    name = _screened(raw)
    if name == "/dev/null":
        return None
    parts = [part for part in name.split("/") if part]
    return "/".join(parts[1:]) or None


def diff_paths(diff: str) -> list[str]:
    """The repo paths a diff changes, in order. Raises PatchError for a path it must not touch."""
    raw: list[str] = [m.group("path") for m in _PATH_LINE.finditer(diff)]
    raw += [m.group("path") for m in _MOVE_LINE.finditer(diff)]
    for m in _GIT_LINE.finditer(diff):
        raw += [m.group("a"), m.group("b")]
    seen: dict[str, None] = {}
    for item in raw:
        path = _checked(item)
        if path is not None:
            seen[path] = None
    for m in _OTHER_NAME_LINE.finditer(diff):
        _screened(m.group("path"))
    return list(seen)


_GIT_SETTINGS = [
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    f"core.excludesFile={os.devnull}",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.quotepath=false",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "diff.noprefix=false",
]


def _git_env() -> dict[str, str]:
    """No user or system config, and nothing from a git hook this process may run in."""
    inherited = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    identity = {
        "GIT_AUTHOR_NAME": "single-shot",
        "GIT_AUTHOR_EMAIL": "single-shot@localhost",
        "GIT_COMMITTER_NAME": "single-shot",
        "GIT_COMMITTER_EMAIL": "single-shot@localhost",
    }
    return {
        **inherited,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **identity,
    }


def _run(
    command: list[str], cwd: Path, stdin: str | None = None, *, errors: str = "replace"
) -> subprocess.CompletedProcess[str]:
    # A new session has no controlling terminal, so patch cannot stop to ask a question.
    # What a tool prints is read for messages, where a mangled character does no harm. The
    # patch is the one output that has to come back exactly, and asks for errors="strict".
    return subprocess.run(
        command,
        cwd=cwd,
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        errors=errors,
        timeout=TOOL_TIMEOUT_S,
        env=_git_env(),
        start_new_session=True,
        check=False,
    )


def _git(
    cwd: Path, *args: str, stdin: str | None = None, errors: str = "replace"
) -> subprocess.CompletedProcess[str]:
    return _run(["git", *_GIT_SETTINGS, *args], cwd, stdin, errors=errors)


def _checkout(source: Path, dest: Path) -> None:
    """A git repo at `dest` with one commit of the files the agent would see in `source`."""
    for item in collect_source_files(source):
        target = dest / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / item.path, target)
    for command in (
        ["init", "-q", "-b", "main", "--template="],
        ["add", "-A"],
        ["commit", "-q", "--no-verify", "-m", "base"],
    ):
        done = _git(dest, *command)
        if done.returncode != 0:
            raise RuntimeError(f"git {command[0]} failed in the throwaway copy: {done.stderr}")


def _problem(label: str, done: subprocess.CompletedProcess[str]) -> str:
    detail = (done.stderr or done.stdout).strip().replace("\n", " ")[:STDERR_CHARS]
    return f"{label} exited with {done.returncode}: {detail}"


def file_sections(diff: str) -> list[str]:
    """The diff cut into one piece per file, without any text before the first header.

    `git apply --recount` counts the lines of a hunk up to the next header, and it takes the
    `--- ` line of a second file in a diff without `diff --git` lines for a removed line of the
    first. One call per file gives it a hunk that ends where the diff ends. A diff with no
    header comes back whole.
    """
    lines = diff.splitlines(keepends=True)
    starts: list[int] = []
    in_git_header = False  # a `diff --git` line was seen and its `---` line was not
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if line.startswith("diff --git "):
            starts.append(index)
            in_git_header = True
        elif line.startswith("--- ") and following.startswith("+++ "):
            if not in_git_header:
                starts.append(index)
            in_git_header = False
    if not starts:
        return [diff]
    ends = [*starts[1:], len(lines)]
    return ["".join(lines[start:end]) for start, end in zip(starts, ends, strict=True)]


def _try_git(checkout: Path, diff: str) -> str | None:
    # git apply takes all of one call or none of it, and a later file that fails fails the
    # attempt, so the fallback starts again from a fresh copy.
    for section in file_sections(diff):
        done = _git(checkout, "apply", "--recount", "--whitespace=nowarn", "-", stdin=section)
        if done.returncode != 0:
            return _problem("git apply --recount", done)
    return None


_HUNK_LINE = ("@@", " ", "+", "-", "\\")


def patch_input(diff: str) -> str:
    """The text that `patch` reads: the `---` and `+++` headers of each file and its hunks.

    Everything else is left out: prose, `Index:` lines, `diff --git` and `index` lines, context
    diff headers, `Prereq:` and the `====` lines of Perforce, and ed commands. Each of those is
    a place where some patch tool reads a file name or runs a command, and the check of
    `diff_paths` cannot know all of them. What remains names its files in the two header
    lines, which the check has read. A diff in the context format has no unified header, so it
    comes back empty, which the system prompt makes unlikely.
    """
    lines = re.findall(r"[^\n]*\n|[^\n]+", diff)  # not splitlines: U+2028 is not a line end
    kept: list[str] = []
    in_file = False
    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if line.startswith("--- ") and following.startswith("+++ "):
            kept += [line, following]
            in_file = True
            index += 2
            continue
        # An empty line is a context line whose space the model left out.
        if in_file and (line.startswith(_HUNK_LINE) or line.strip("\r\n") == ""):
            kept.append(line)
        index += 1
    return "".join(kept)


def _try_patch(checkout: Path, diff: str) -> str | None:
    # -u reads a unified diff and nothing else, which leaves out the ed and normal formats.
    # -t answers every question with its default, -N skips a hunk that is applied already, and
    # -E removes a file that the diff empties, as GNU patch does and BSD patch does not.
    command = ["patch", "-p1", "--fuzz=2", "-u", "-t", "-N", "-E"]
    text = patch_input(diff)
    if not text:
        return "patch -p1 --fuzz=2 found no unified diff in the reply"
    try:
        done = _run(command, checkout, stdin=text)
    except FileNotFoundError:
        return "patch -p1 --fuzz=2 is not installed"
    return None if done.returncode == 0 else _problem("patch -p1 --fuzz=2", done)


def _staged_diff(checkout: Path, paths: list[str]) -> str:
    # Only the files the diff names are staged, so a .orig file that patch leaves next to a
    # fuzzy hunk is not in the result.
    _git(checkout, "add", "-A", "--", *paths)
    try:
        done = _git(checkout, "diff", "--cached", "--no-color", "--no-ext-diff", errors="strict")
    except UnicodeDecodeError:
        # git prints the lines around a change as they are in the file. The grader takes the
        # patch as text, so a file in another encoding cannot go through it unchanged.
        raise PatchError(
            "the diff changes a file that is not UTF-8 text, so its patch cannot be kept as text"
        ) from None
    if not done.stdout.strip():
        raise PatchError("the diff applies but changes nothing")
    return done.stdout


def apply_diff(repo: Path, diff: str) -> AppliedPatch:
    """Apply a diff to a throwaway copy of `repo` and return the `git diff` of the result.

    git apply goes first. When it refuses, patch gets a fresh copy, so nothing that the first
    attempt wrote can show up in the second. Raises PatchError when both refuse, when the diff
    names a path it must not touch, and when it applies but changes nothing. A git command that
    does not finish in time counts as a refusal, so the caller gets a PatchError for it too.
    """
    if not diff.strip():
        raise PatchError("the reply holds no diff")
    paths = diff_paths(diff)
    if not paths:
        raise PatchError("the reply holds no diff: no file header was found")
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lh-single-shot-") as scratch:
        for method, attempt in (("git", _try_git), ("patch", _try_patch)):
            checkout = Path(scratch) / method
            try:
                _checkout(repo, checkout)
                problem = attempt(checkout, diff)
                if problem is None:
                    patch = _staged_diff(checkout, paths)
                    return AppliedPatch(patch, "git" if method == "git" else "patch")
            except subprocess.TimeoutExpired:
                problem = f"{method} did not finish in {TOOL_TIMEOUT_S} s"
            problems.append(problem)
    raise PatchError("the diff did not apply. " + "; ".join(problems))


# ---------- the one call ----------
def _text_of(content: list[object]) -> str:
    return "".join(block.text for block in content if isinstance(block, TextBlock))


@dataclass(frozen=True)
class Asked:
    """The one call, answered: what is in the prompt, and what the model said and cost."""

    files: list[str]  # the files in the prompt, best first
    reply: str  # the text of the reply
    usage: Usage
    stop_reason: str


async def ask(
    provider: LLMProvider,
    *,
    model: str,
    repo: Path,
    problem: str,
    repo_map: str,
    max_tokens: int,
    options: CallOptions | None = None,
    top: int = TOP_FILES,
) -> Asked:
    """Retrieve the files and ask once. A failed provider call raises ProviderError.

    This is the half of the baseline that costs money. Nothing here touches git or patch, so a
    caller can book `usage` before the reply is applied.
    """
    files = await asyncio.to_thread(read_files, repo)
    names = rank(files, problem, top)
    prompt = build_prompt(problem, repo_map, {name: files[name] for name in names})
    reply = await provider.complete(
        model=model,
        system=SYSTEM_PROMPT,
        messages=[Message(role="user", content=[TextBlock(text=prompt)])],
        tools=[],
        max_tokens=max_tokens,
        options=options,
    )
    return Asked(
        files=names,
        reply=_text_of(list(reply.content)),
        usage=reply.usage,
        stop_reason=reply.stop_reason,
    )


async def apply_reply(repo: Path, asked: Asked) -> SingleShotResult:
    """Take the diff out of the reply and apply it to a copy of `repo`.

    A reply with no diff, a diff that does not apply and a reply that was cut off are results
    with `error` set and the usage of the call, which was paid for all the same.
    """

    def result(
        *, patch: str = "", method: Method | None = None, error: str | None = None
    ) -> SingleShotResult:
        return SingleShotResult(
            files=asked.files,
            reply=asked.reply,
            usage=asked.usage,
            stop_reason=asked.stop_reason,
            patch=patch,
            method=method,
            error=error,
        )

    if asked.stop_reason == "max_tokens":
        # A diff that stops in the middle of a hunk can still apply with a fuzz, and would
        # then be a different change than the model meant.
        return result(error="the reply was cut off at max_tokens, so its diff is not complete")
    diff = extract_diff(asked.reply)
    try:
        applied = await asyncio.to_thread(apply_diff, repo, diff)
    except PatchError as exc:
        return result(error=str(exc))
    return result(patch=applied.patch, method=applied.method)


async def solve(
    provider: LLMProvider,
    *,
    model: str,
    repo: Path,
    problem: str,
    repo_map: str,
    max_tokens: int,
    options: CallOptions | None = None,
    top: int = TOP_FILES,
) -> SingleShotResult:
    """Retrieve, ask once and apply. A failed provider call raises ProviderError.

    Any other failure, such as a reply with no diff or one that does not apply, is a result with
    `error` set and the usage of the call, which was paid for all the same.
    """
    asked = await ask(
        provider,
        model=model,
        repo=repo,
        problem=problem,
        repo_map=repo_map,
        max_tokens=max_tokens,
        options=options,
        top=top,
    )
    return await apply_reply(repo, asked)
