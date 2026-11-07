"""The single-shot baseline (LH-903): retrieve five files, ask once, apply the diff.

No Docker and no model here. The model is a fake provider or the scripted one, and the diff is
applied with the git and patch of the machine in a throwaway copy of a small repo. The runner
that builds the repo map in a sandbox and grades the patch is in test_eval_run.py and
tests/integration/test_baselines.py.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from eval.baselines import single_shot
from eval.baselines.single_shot import PatchError
from longhaul.contracts import (
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    StopReason,
    TextBlock,
    Usage,
)
from longhaul.providers.scripted import ScriptedProvider

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
GOOD = """\
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a - b
+    return a + b

"""
# The hunk header says 9 lines where the hunk has 3. git apply --recount counts them itself.
WRONG_COUNTS = GOOD.replace("@@ -1,3 +1,3 @@", "@@ -1,9 +1,9 @@")
# The model misremembered the signature: the context line differs, so git refuses the hunk and
# patch takes it with a fuzz of one line.
WRONG_CONTEXT = GOOD.replace(" def add(a, b):", " def add(x, y):")
# Nothing like the file at all.
MAP = "Python files of this repo, most relevant first (1 of 1 shown):\ncalc.py: add, mul"
UNRELATED = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n-import nothing\n+import something\n"
FIXED = CALC.replace("a - b", "a + b")


def make_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    repo = root / "repo"
    for name, body in (files or {"calc.py": CALC}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return repo


def git_apply_check(repo: Path, patch: str) -> subprocess.CompletedProcess[str]:
    """What the grader does with the patch: git apply in a clean copy of the base."""
    return subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=repo,
        input=patch,
        text=True,
        capture_output=True,
    )


# ---------- applying a diff ----------
def test_patch_apply_fallback(tmp_path: Path) -> None:
    """git apply is tried first. When it refuses, patch with a fuzz of 2 gets a try."""
    repo = make_repo(tmp_path)

    # The diff is wrong about a context line, so git apply refuses it.
    refused = git_apply_check(repo, WRONG_CONTEXT)
    assert refused.returncode != 0

    applied = single_shot.apply_diff(repo, WRONG_CONTEXT)

    assert applied.method == "patch"
    # What comes back is a clean git diff of the result, not the diff the model wrote.
    assert applied.patch != WRONG_CONTEXT
    assert "-    return a - b\n+    return a + b\n" in applied.patch
    assert "def add(x, y)" not in applied.patch
    assert git_apply_check(repo, applied.patch).returncode == 0
    # The repo that was given is left as it was.
    assert (repo / "calc.py").read_text(encoding="utf-8") == CALC


def test_a_diff_that_git_takes_is_applied_by_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    applied = single_shot.apply_diff(repo, GOOD)

    assert applied.method == "git"
    assert applied.patch.startswith("diff --git a/calc.py b/calc.py\n")
    assert git_apply_check(repo, applied.patch).returncode == 0


def test_hunk_counts_that_are_wrong_are_recounted(tmp_path: Path) -> None:
    # Models count lines badly. --recount is what makes a wrong header harmless.
    repo = make_repo(tmp_path)

    applied = single_shot.apply_diff(repo, WRONG_COUNTS)

    assert applied.method == "git"
    assert "+    return a + b" in applied.patch


def test_the_patch_is_the_same_whichever_tool_applied_it(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    by_git = single_shot.apply_diff(repo, GOOD)
    by_patch = single_shot.apply_diff(repo, WRONG_CONTEXT)

    assert by_git.method != by_patch.method
    assert by_git.patch == by_patch.patch


def test_a_new_file_and_a_removed_file_are_in_the_patch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"calc.py": CALC, "old.py": "VALUE = 1\n"})
    diff = (
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+def new():\n+    return 1\n"
        "--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-VALUE = 1\n"
    )

    applied = single_shot.apply_diff(repo, diff)

    assert "new file mode" in applied.patch
    assert "+++ b/new.py" in applied.patch
    assert "deleted file mode" in applied.patch
    assert git_apply_check(repo, applied.patch).returncode == 0


def test_a_diff_of_several_files_with_wrong_counts_is_applied_by_git(tmp_path: Path) -> None:
    # --recount takes the header of the next file for a line of the hunk, unless git is given one
    # file at a time.
    repo = make_repo(tmp_path, {"calc.py": CALC, "other.py": "X = 1\n"})
    diff = WRONG_COUNTS + "--- a/other.py\n+++ b/other.py\n@@ -1,7 +1,7 @@\n-X = 1\n+X = 2\n"

    applied = single_shot.apply_diff(repo, diff)

    assert applied.method == "git"
    assert applied.patch.count("diff --git") == 2


def test_a_file_that_the_fallback_empties_is_removed(tmp_path: Path) -> None:
    # BSD patch leaves an empty file where GNU patch removes it. The patch has to say the same
    # whichever one the machine has.
    repo = make_repo(tmp_path, {"calc.py": CALC, "old.py": "VALUE = 1\n"})
    diff = WRONG_CONTEXT + "--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-VALUE = 1\n"

    applied = single_shot.apply_diff(repo, diff)

    assert applied.method == "patch"
    assert "deleted file mode" in applied.patch


def test_a_diff_that_nothing_can_apply_names_both_attempts(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    with pytest.raises(PatchError) as caught:
        single_shot.apply_diff(repo, UNRELATED)

    message = str(caught.value)
    assert "git apply" in message
    assert "patch" in message


def test_a_diff_of_two_files_falls_back_as_a_whole(tmp_path: Path) -> None:
    # git apply takes all of a diff or none of it. The second tool then starts from a fresh
    # copy, so the patch holds both files once.
    repo = make_repo(tmp_path, {"calc.py": CALC, "other.py": "X = 1\n"})
    two_files = WRONG_CONTEXT + "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n"

    applied = single_shot.apply_diff(repo, two_files)

    assert applied.method == "patch"
    assert applied.patch.count("diff --git") == 2


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("a/../outside.py", "b/../outside.py", "outside the repo"),
        ("a/sub/../../outside.py", "b/sub/../../outside.py", "outside the repo"),
        ("/etc/passwd", "/etc/passwd", "outside the repo"),
        ("a/.git/config", "b/.git/config", r"\.git"),
        ("a/calc.py", "b/../../calc.py", "outside the repo"),
    ],
)
def test_a_diff_that_leaves_the_repo_is_refused_before_anything_runs(
    tmp_path: Path, old: str, new: str, why: str
) -> None:
    repo = make_repo(tmp_path)
    diff = f"--- {old}\n+++ {new}\n@@ -1 +1 @@\n-x\n+y\n"

    with pytest.raises(PatchError, match=why):
        single_shot.apply_diff(repo, diff)

    assert not (tmp_path / "outside.py").exists()


def climb(target: Path) -> str:
    """A relative path that reaches `target` from any depth, as `x/../../..` would."""
    return "../" * 30 + str(target).lstrip("/")


NO_FILE = "--- /dev/null\n+++ /dev/null\n@@ -0,0 +1 @@\n+text\n"
# A name in a place other than the --- and +++ lines of a section that git would take. The
# {climb} and {absolute} parts name the file that the test looks for afterwards.
OTHER_NAME_SOURCES = [
    pytest.param("Index: x/{climb}\n" + NO_FILE, id="index-climbing-to-an-absolute-path"),
    pytest.param("Index: /{absolute}\n" + NO_FILE, id="index-absolute"),
    pytest.param("Index: x/../../planted.txt\n" + NO_FILE, id="index-short-climb"),
    pytest.param("Index:\tx/../../planted.txt\n" + NO_FILE, id="index-after-a-tab"),
    pytest.param("Index:x/../../planted.txt\n" + NO_FILE, id="index-without-a-space"),
    pytest.param("*** x/{climb}\n" + NO_FILE, id="context-header-climbing"),
    pytest.param("*** x/../../planted.txt\n" + NO_FILE, id="context-header-short-climb"),
    pytest.param(
        '--- "a/\\056\\056/\\056\\056/planted.txt"\n+++ "b/\\056\\056/planted.txt"\n'
        "@@ -0,0 +1 @@\n+text\n",
        id="quoted-escapes-for-dots",
    ),
]


@pytest.mark.parametrize("attack", OTHER_NAME_SOURCES)
def test_a_name_in_any_header_line_is_checked_before_patch_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    # patch -p1 takes a file name from an Index: line and from a context diff header as well as
    # from the --- and +++ lines, and the patch of macOS follows a .. in it. The first section
    # is one that git refuses, so the second tool runs; the second section is the attack.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    repo = make_repo(tmp_path / "work")
    target = tmp_path / "planted.txt"
    named = attack.format(climb=climb(target), absolute=str(target).lstrip("/"))

    with pytest.raises(PatchError, match="outside the repo|escapes"):
        single_shot.apply_diff(repo, WRONG_CONTEXT + named)

    written = [p for p in tmp_path.rglob("*") if "planted" in p.name]
    assert written == []  # no file, and no .rej or .orig next to one
    assert list(scratch.iterdir()) == []  # the throwaway copy is gone


def test_patch_is_given_only_headers_and_hunks() -> None:
    # Whatever else is in the reply is not text that patch gets to read: not an Index: line, not
    # a context diff header, not an ed command, not prose.
    noise = (
        "Here is the fix:\n"
        "Index: calc.py\n"
        "===================================================================\n"
        "diff --git a/calc.py b/calc.py\n"
        "index 1..2 100644\n"
        "*** a/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
        "\\ No newline at end of file\n"
        "Prereq: 1.0\n"
        "1,2c\n"
        "!echo hello\n"
        "Index: other.py\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -1 +1 @@\n"
        "-X = 1\n"
        "+X = 2\n"
        "\n"
        "Thanks.\n"
    )

    assert single_shot.patch_input(noise) == (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
        "\\ No newline at end of file\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -1 +1 @@\n"
        "-X = 1\n"
        "+X = 2\n"
        "\n"
    )


@pytest.mark.parametrize(
    "line",
    [
        "Index: ../../planted.txt",
        "*** ../../planted.txt",
        "==== //depot/../../planted.txt#1 - ../../planted.txt ====",
        "Prereq: ../../planted.txt",
    ],
)
def test_patch_reads_no_name_from_a_line_that_is_not_a_unified_header(
    tmp_path: Path, line: str
) -> None:
    # The second layer. Were a name source missing from the check of diff_paths, the text that
    # patch gets still has none. This calls the fallback without the check, from a folder two
    # levels down, so that a .. in a name would land inside tmp_path.
    checkout = tmp_path / "scratch" / "copy"
    checkout.mkdir(parents=True)
    diff = f"{line}\n--- /dev/null\n+++ /dev/null\n@@ -0,0 +1 @@\n+text\n"

    single_shot._try_patch(checkout, diff)

    assert [p for p in tmp_path.rglob("*") if "planted" in p.name] == []


def test_a_diff_with_a_harmless_index_line_still_falls_back(tmp_path: Path) -> None:
    # svn and cvs write Index: lines, so a diff that holds one is not a reason to refuse it.
    repo = make_repo(tmp_path)
    diff = "Index: calc.py\n" + "=" * 67 + "\n" + WRONG_CONTEXT

    applied = single_shot.apply_diff(repo, diff)

    assert applied.method == "patch"
    assert "+    return a + b" in applied.patch


def test_a_git_command_that_times_out_in_the_copy_is_a_failed_apply_and_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The copy of a big repo can take a while on a busy machine. The caller has paid for the
    # reply already, so it gets a PatchError that it can write into a result, not a traceback.
    def checkout(_source: Path, _dest: Path) -> None:
        raise subprocess.TimeoutExpired("git add", 60)

    monkeypatch.setattr(single_shot, "_checkout", checkout)

    with pytest.raises(PatchError, match="did not finish"):
        single_shot.apply_diff(make_repo(tmp_path), GOOD)


def test_a_git_diff_that_times_out_after_the_apply_is_a_failed_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = single_shot._git

    def git(cwd: Path, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("diff",):
            raise subprocess.TimeoutExpired("git diff", 60)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(single_shot, "_git", git)

    with pytest.raises(PatchError, match="did not finish"):
        single_shot.apply_diff(make_repo(tmp_path), GOOD)


def test_a_change_beside_a_file_that_is_not_utf8_text_is_a_failed_apply(tmp_path: Path) -> None:
    # git diff prints the context lines of a hunk as they are in the file, so a latin-1 byte next
    # to the change reaches the decoder of the pipe.
    repo = make_repo(tmp_path, {"calc.py": CALC})
    (repo / "legacy.py").write_bytes(b"# caf\xe9\nVALUE = 1\n")
    diff = "--- a/legacy.py\n+++ b/legacy.py\n@@ -2 +2 @@\n-VALUE = 1\n+VALUE = 2\n"

    with pytest.raises(PatchError, match="UTF-8"):
        single_shot.apply_diff(repo, diff)


def test_an_empty_diff_is_an_error_and_not_an_empty_patch(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="no diff"):
        single_shot.apply_diff(make_repo(tmp_path), "")


def test_a_diff_that_changes_nothing_is_an_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    diff = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-def add(a, b):\n+def add(a, b):\n"

    with pytest.raises(PatchError, match="changes nothing"):
        single_shot.apply_diff(repo, diff)


# ---------- finding the diff in a reply ----------
def test_a_fenced_diff_is_taken_out_of_the_prose() -> None:
    reply = f"Here is the fix.\n\n```diff\n{GOOD}```\n\nThis changes add()."
    assert single_shot.extract_diff(reply) == GOOD


def test_a_bare_diff_is_taken_as_it_is() -> None:
    assert single_shot.extract_diff(GOOD) == GOOD


def test_a_diff_with_a_git_header_keeps_the_header() -> None:
    reply = "diff --git a/calc.py b/calc.py\nindex 1..2 100644\n" + GOOD
    assert single_shot.extract_diff(reply).startswith("diff --git a/calc.py b/calc.py\n")


def test_the_diffs_of_several_fenced_blocks_are_joined() -> None:
    other = "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n"
    reply = f"First file:\n```diff\n{GOOD}```\nSecond file:\n```diff\n{other}```\n"

    diff = single_shot.extract_diff(reply)

    assert GOOD in diff
    assert other in diff


def test_a_fence_without_a_language_still_counts() -> None:
    assert single_shot.extract_diff(f"```\n{GOOD}```") == GOOD


def test_a_reply_without_a_diff_gives_an_empty_string() -> None:
    assert single_shot.extract_diff("I could not find the bug.") == ""
    assert single_shot.extract_diff("```python\nprint('hi')\n```") == ""


def test_the_diff_ends_with_a_newline() -> None:
    assert single_shot.extract_diff(GOOD.rstrip("\n")).endswith("\n")


# ---------- retrieving files ----------
def test_the_files_that_share_the_most_words_with_the_problem_come_first(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "src/app/clock.py": "def parse_timestamp(text):\n    return text.strip()\n",
            "src/app/money.py": "def format_price(value):\n    return f'{value:.2f}'\n",
            "src/app/names.py": "def title_case(name):\n    return name.title()\n",
        },
    )

    found = single_shot.retrieve(repo, "parse_timestamp drops the timezone of a timestamp", top=2)

    assert found[0] == "src/app/clock.py"
    assert len(found) == 2


def test_camel_case_and_snake_case_names_are_split_into_words(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "a.py": "class InvoiceTotal:\n    pass\n",
            "b.py": "def unrelated():\n    return 1\n",
            "c.py": "def other_thing():\n    return 2\n",
        },
    )

    assert single_shot.retrieve(repo, "the invoice total is wrong", top=1) == ["a.py"]


def test_at_most_five_files_are_retrieved_by_default(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {f"m{n}.py": f"def f{n}():\n    return {n}\n" for n in range(9)})

    assert len(single_shot.retrieve(repo, "f3 returns the wrong number")) == single_shot.TOP_FILES
    assert single_shot.TOP_FILES == 5


def test_equal_scores_are_ordered_by_path_so_a_run_can_be_repeated(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {f"m{n}.py": "VALUE = 1\n" for n in range(8)})

    first = single_shot.retrieve(repo, "nothing here matches")
    second = single_shot.retrieve(repo, "nothing here matches")

    assert first == second == [f"m{n}.py" for n in range(5)]


def test_a_repo_with_fewer_files_than_the_limit_gives_them_all(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"a.py": "A = 1\n", "b.py": "B = 2\n"})
    assert sorted(single_shot.retrieve(repo, "A")) == ["a.py", "b.py"]


def test_files_that_are_not_text_are_left_out(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"a.py": "def target():\n    return 1\n"})
    (repo / "data.bin").write_bytes(b"\xff\xfe\x00target\x00" * 50)

    assert single_shot.retrieve(repo, "target") == ["a.py"]


def test_a_repo_without_text_files_gives_no_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data.bin").write_bytes(b"\xff\x00" * 10)

    assert single_shot.retrieve(repo, "anything") == []


# ---------- the prompt ----------
def test_the_prompt_holds_the_problem_the_repo_map_and_each_file(tmp_path: Path) -> None:
    prompt = single_shot.build_prompt(
        "add() subtracts\n\nadd(2, 3) returns -1",
        MAP,
        {"calc.py": CALC},
    )

    assert "add() subtracts" in prompt
    assert "add(2, 3) returns -1" in prompt
    assert f"## Repo map\n\n{MAP}\n" in prompt
    assert "### calc.py" in prompt
    assert CALC in prompt


def test_a_file_that_is_too_long_is_cut_and_says_so() -> None:
    body = "x = 1\n" * 20_000

    prompt = single_shot.build_prompt("problem", "", {"big.py": body})

    assert len(prompt) < len(body)
    assert "cut" in prompt


def test_the_system_prompt_asks_for_a_unified_diff_and_says_there_are_no_tools() -> None:
    assert "unified diff" in single_shot.SYSTEM_PROMPT
    assert "no tools" in single_shot.SYSTEM_PROMPT.lower()


# ---------- the one call ----------
class FakeProvider:
    name = "fake"

    def __init__(self, text: str | ProviderError, stop: StopReason = "end_turn") -> None:
        self.text = text
        self.stop = stop
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if isinstance(self.text, ProviderError):
            raise self.text
        return LLMResponse(
            content=[TextBlock(text=self.text)],
            tool_calls=[],
            stop_reason=self.stop,
            usage=Usage(input_tokens=1200, output_tokens=80),
            model=kwargs["model"],
            latency_ms=5,
        )

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


async def solve(provider: Any, repo: Path, **changes: Any) -> single_shot.SingleShotResult:
    values: dict[str, Any] = {
        "model": "claude-sonnet-5-5",
        "repo": repo,
        "problem": "add() subtracts\n\nadd(2, 3) returns -1",
        "repo_map": MAP,
        "max_tokens": 4000,
    }
    return await single_shot.solve(provider, **{**values, **changes})


async def test_one_call_without_tools_gives_a_patch(tmp_path: Path) -> None:
    provider = FakeProvider(f"```diff\n{GOOD}```")

    result = await solve(provider, make_repo(tmp_path))

    (call,) = provider.calls
    assert call["tools"] == []
    assert call["model"] == "claude-sonnet-5-5"
    assert call["max_tokens"] == 4000
    assert call["system"].startswith("Role: coder\n")
    (message,) = call["messages"]
    assert isinstance(message, Message)
    assert message.role == "user"
    block = message.content[0]
    assert isinstance(block, TextBlock)
    prompt = block.text
    assert "add(2, 3) returns -1" in prompt
    assert MAP in prompt
    assert CALC in prompt  # the file was retrieved and shown

    assert result.error is None
    assert result.method == "git"
    assert "+    return a + b" in result.patch
    assert result.files == ["calc.py"]
    assert result.usage == Usage(input_tokens=1200, output_tokens=80)


async def test_a_reply_that_needs_the_fallback_still_gives_a_patch(tmp_path: Path) -> None:
    result = await solve(FakeProvider(WRONG_CONTEXT), make_repo(tmp_path))

    assert (result.method, result.error) == ("patch", None)
    assert "+    return a + b" in result.patch


async def test_a_reply_without_a_diff_is_an_error_with_the_usage_kept(tmp_path: Path) -> None:
    result = await solve(FakeProvider("I cannot fix this."), make_repo(tmp_path))

    assert result.patch == ""
    assert result.error is not None
    assert "no diff" in result.error
    assert result.usage.output_tokens == 80  # the call was paid for all the same


async def test_a_diff_that_does_not_apply_is_an_error(tmp_path: Path) -> None:
    result = await solve(FakeProvider(UNRELATED), make_repo(tmp_path))

    assert result.patch == ""
    assert result.error is not None
    assert "git apply" in result.error


async def test_a_reply_that_was_cut_off_is_not_applied(tmp_path: Path) -> None:
    # A diff that stops in the middle of a hunk could still apply with a fuzz, and would then
    # be a different change than the model meant.
    result = await solve(FakeProvider(GOOD, stop="max_tokens"), make_repo(tmp_path))

    assert result.patch == ""
    assert result.error is not None
    assert "max_tokens" in result.error


async def test_a_provider_error_goes_up_to_the_caller(tmp_path: Path) -> None:
    error = ProviderError(ProviderErrorKind.OVERLOADED, "busy")
    with pytest.raises(ProviderError):
        await solve(FakeProvider(error), make_repo(tmp_path))


async def test_the_reply_is_in_hand_before_anything_is_applied(tmp_path: Path) -> None:
    # The runner books the usage of the call as soon as the provider has answered, so a failure
    # of the apply step cannot lose it. ask() is the half of solve() that costs money.
    provider = FakeProvider(f"```diff\n{GOOD}```")
    repo = make_repo(tmp_path)

    asked = await single_shot.ask(
        provider,
        model="claude-sonnet-5-5",
        repo=repo,
        problem="add() subtracts",
        repo_map=MAP,
        max_tokens=4000,
    )

    assert asked.usage == Usage(input_tokens=1200, output_tokens=80)
    assert asked.files == ["calc.py"]
    assert asked.stop_reason == "end_turn"
    assert GOOD in asked.reply
    assert len(provider.calls) == 1

    applied = await single_shot.apply_reply(repo, asked)

    assert applied.error is None
    assert applied.usage == asked.usage
    assert "+    return a + b" in applied.patch
    assert len(provider.calls) == 1  # applying asks nothing


async def test_the_top_argument_limits_the_files_in_the_prompt(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"calc.py": CALC, "other.py": "X = 1\n", "more.py": "Y = 2\n"})
    provider = FakeProvider(GOOD)

    result = await solve(provider, repo, top=2)

    assert len(result.files) == 2
    (call,) = provider.calls
    prompt = call["messages"][0].content[0].text
    assert sum(f"### {name}\n" in prompt for name in ("calc.py", "other.py", "more.py")) == 2


async def test_the_scripted_provider_gives_the_diff_from_its_script(tmp_path: Path) -> None:
    script = tmp_path / "calc-01.yaml"
    script.write_text(
        yaml.safe_dump({"steps": [{"respond": {"text": f"```diff\n{GOOD}```"}}]}),
        encoding="utf-8",
    )

    result = await solve(ScriptedProvider(script), make_repo(tmp_path))

    assert result.error is None
    assert "+    return a + b" in result.patch
    assert result.usage.input_tokens > 0  # the scripted provider reports tokens, never a price


async def test_the_repo_given_to_solve_is_not_changed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    await solve(FakeProvider(GOOD), repo)

    assert (repo / "calc.py").read_text(encoding="utf-8") == CALC
    assert sorted(p.name for p in repo.iterdir()) == ["calc.py"]


# ---------- the scripts of the dry run ----------
SCRIPTS = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "scripts" / "eval_single_shot"
)
CUSTOM = Path(__file__).resolve().parents[2] / "eval" / "custom"


def changed_lines(patch: str) -> list[str]:
    return [
        line
        for line in patch.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


@pytest.mark.parametrize("task", ["calc-01", "calc-02"])
def test_the_scripted_answers_of_the_dry_run_make_the_change_of_the_reference_fix(
    task: str,
) -> None:
    # The dry run grades these answers on the hidden tests, so they have to apply to the repo
    # and change what the reference patch changes. This checks it without a sandbox.
    steps = yaml.safe_load((SCRIPTS / f"{task}.yaml").read_text(encoding="utf-8"))["steps"]
    (step,) = steps
    assert "tool_calls" not in step["respond"]  # the baseline has no tools to call

    diff = single_shot.extract_diff(step["respond"]["text"])
    applied = single_shot.apply_diff(CUSTOM / "repos" / "calc", diff)

    reference = (CUSTOM / "reference" / f"{task}.patch").read_text(encoding="utf-8")
    assert changed_lines(applied.patch) == changed_lines(reference)
    assert changed_lines(applied.patch)


# ---------- cutting a diff into files ----------
def test_a_diff_without_git_lines_is_cut_at_each_file_header() -> None:
    one = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    two = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"

    assert single_shot.file_sections(one + two) == [one, two]


def test_a_diff_with_git_lines_is_cut_at_each_diff_git_line_and_not_inside_it() -> None:
    one = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    two = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"

    assert single_shot.file_sections(one + two) == [one, two]


def test_the_text_before_the_first_header_is_not_part_of_a_section() -> None:
    body = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert single_shot.file_sections("Here you go:\n" + body) == [body]


def test_a_text_with_no_header_comes_back_whole() -> None:
    assert single_shot.file_sections("nothing here\n") == ["nothing here\n"]
