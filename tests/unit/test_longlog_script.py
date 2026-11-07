"""longlog.yaml and compactor.yaml: the scripts of the long-task test (LH-407).

tests/e2e/test_long_task.py plays longlog.yaml on the durable stack, which takes minutes and needs
Docker. These tests keep the shape of the scripts that it depends on, and play the coder once in a
FakeSandbox, to show that the script fixes the fixture and that its steps follow what the model is
shown of a result: a head with the summary line, and a tail, and not the whole output.

The compactor is not asked in a play like this one, because the in-process loop has no tier 2.
Its steps are checked on their own: each request that names a fixed module gets the notes that
list the modules up to it. From the second round on, a step also asks for the previous notes, so
that a run that does not carry the notes from one compaction to the next gets no answer.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.fakes.scripted_runs import CONFIG, SCRIPTS, TEST_COMMAND, seed, spec_for

from longhaul.agent.loop import run_loop
from longhaul.context.compact import NOTES_TOOL, NOTES_TOOL_NAME
from longhaul.contracts import (
    Message,
    OutcomeKind,
    ProgressNotes,
    ProviderError,
    Role,
    TextBlock,
    ToolCall,
    ToolContext,
)
from longhaul.providers.base import format_role_line
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

LONGLOG = SCRIPTS / "longlog.yaml"
COMPACTOR = SCRIPTS / "compactor.yaml"
MODULES = [
    "chunks",
    "clamp",
    "dedupe",
    "initials",
    "leap",
    "ordinal",
    "pages",
    "percent",
    "ranges",
    "slug",
    "stats",
    "words",
]
TURNS = 62
# The modules that the first compaction of the long-task test has fixed to fold (the fixes of
# chunks, clamp and dedupe are in turns 2, 7 and 12). Every later round has notes behind it.
FIRST_ROUND_MODULES = 3
# What the model is allowed to see of one result in the long-task test: a window of 8,000 tokens
# gives 1/32 of it (Context.for_window).
RESULT_CAP_TOKENS = 250


def steps(path: Path, role: str) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [s for s in loaded["steps"] if s["match"]["role"] == role]


def coder_steps() -> list[dict[str, Any]]:
    return steps(LONGLOG, "coder")


# ---------- the shape ----------
def test_both_scripts_load() -> None:
    ScriptedProvider(LONGLOG)
    ScriptedProvider(COMPACTOR)


def test_the_coder_has_a_step_for_each_turn_of_the_run() -> None:
    assert [s["match"]["turn_index"] for s in coder_steps()] == list(range(TURNS))


def test_the_run_ends_with_finish_and_calls_it_once() -> None:
    names = [call["name"] for s in coder_steps() for call in s["respond"]["tool_calls"]]
    assert names[-1] == "finish"
    assert names.count("finish") == 1
    assert {"run_tests", "read_file", "edit_file", "git_diff"} == set(names) - {"finish"}


def test_every_step_but_the_first_waits_for_the_result_before_it() -> None:
    # The first turn has no result to look at. A step that only counted turns would go on
    # after a failing edit, and the run would end with a finish that is not true.
    guards = [s["match"].get("last_tool_result_contains") for s in coder_steps()]
    assert guards[0] is None
    assert all(isinstance(g, str) and "\n" not in g for g in guards[1:])


def test_a_module_takes_five_turns_with_one_edit() -> None:
    names = [s["respond"]["tool_calls"][0]["name"] for s in coder_steps()]
    middle = names[1:-1]
    assert len(middle) == 5 * len(MODULES)
    for module in range(len(MODULES)):
        assert middle[5 * module : 5 * module + 5] == [
            "read_file",
            "edit_file",
            "run_tests",
            "git_diff",
            "run_tests",
        ]


def test_each_module_is_edited_in_its_own_turn_with_a_said_line_that_names_it() -> None:
    edits = [s for s in coder_steps() if s["respond"]["tool_calls"][0]["name"] == "edit_file"]
    assert [s["respond"]["tool_calls"][0]["input"]["path"] for s in edits] == [
        f"src/longlog/{m}.py" for m in MODULES
    ]
    # The compactor reads this line to tell which modules are fixed.
    said = [s["respond"]["text"] for s in edits]
    assert all(text.startswith(f"Fixed {m}: ") for text, m in zip(said, MODULES, strict=True))


def test_the_compactor_steps_of_the_two_files_are_the_same() -> None:
    # A run takes one script, so longlog.yaml has the steps of the compactor too. The second
    # file is for a test that brings its own coder.
    assert steps(LONGLOG, "compactor") == steps(COMPACTOR, "compactor")
    assert len(steps(COMPACTOR, "compactor")) == len(MODULES) + 1
    assert steps(COMPACTOR, "coder") == []


# ---------- the compactor ----------
async def notes_for(request_text: str) -> ProgressNotes:
    provider = ScriptedProvider(COMPACTOR)
    reply = await provider.complete(
        model="compactor-model",
        system=format_role_line(Role.COMPACTOR),
        messages=[Message(role="user", content=[TextBlock(text=request_text)])],
        tools=[NOTES_TOOL],
        max_tokens=1000,
    )
    (call,) = reply.tool_calls
    assert call.name == NOTES_TOOL_NAME
    return ProgressNotes.model_validate(call.input)


def previous_notes_block(notes: ProgressNotes) -> str:
    """The part of the request that carries the notes of the round before.

    It has the shape that compact() gives it: the notes as JSON, so a decision is in quotes. The
    turns to fold show the same decision as a line that the model said, without a quote.
    """
    return f"## Previous notes\n{notes.model_dump_json(indent=2)}\n\n"


def folded_turns(fixed: int) -> str:
    return (
        f"## Transcript to fold into the notes\n[turn 3]\n  said: Fixed {MODULES[fixed - 1]}: it."
    )


async def request_of_round(fixed: int) -> str:
    """The request of a round whose turns to fold end with the fix of the module `fixed`.

    The first round has no previous notes. A later one has the notes that the round before it got.
    """
    if fixed <= FIRST_ROUND_MODULES:
        return folded_turns(fixed)
    before = await notes_for(await request_of_round(fixed - 1))
    return previous_notes_block(before) + folded_turns(fixed)


@pytest.mark.parametrize("fixed", range(1, len(MODULES) + 1))
async def test_the_notes_list_the_modules_fixed_so_far(fixed: int) -> None:
    # The request names the last module that was fixed, in the turns to fold or in the notes of
    # the round before. Modules after it that were only read must not be listed.
    notes = await notes_for(await request_of_round(fixed))

    assert notes.files_touched == [f"src/longlog/{m}.py" for m in MODULES[:fixed]]
    assert [d.split(":")[0] for d in notes.decisions[:fixed]] == [
        f"Fixed {m}" for m in MODULES[:fixed]
    ]
    if fixed < len(MODULES):
        assert notes.failing_tests[0].startswith(f"tests/test_{MODULES[fixed]}.py::")
    else:
        assert notes.failing_tests == []


async def test_notes_with_nothing_fixed_yet_list_no_file() -> None:
    notes = await notes_for("[turn 1]\n  call run_tests {}")
    assert notes.files_touched == []
    assert notes.failing_tests[0].startswith(f"tests/test_{MODULES[0]}.py::")


async def test_the_highest_module_in_the_request_wins() -> None:
    notes = await notes_for("Fixed chunks: a. Fixed dedupe: b. Fixed clamp: c.")
    assert notes.files_touched[-1] == "src/longlog/dedupe.py"


async def test_the_notes_keep_what_was_tried_and_failed() -> None:
    first = await notes_for(await request_of_round(1))
    last = await notes_for(await request_of_round(len(MODULES)))
    assert first.tried_and_failed == last.tried_and_failed != []


# ---------- the compactor needs the previous notes from the second round on ----------
def assert_nothing_is_listed(notes: ProgressNotes) -> None:
    """The notes of the last step, which the script gives to a request that no other step fits."""
    assert notes.files_touched == []
    assert not [d for d in notes.decisions if d.startswith("Fixed ")]


@pytest.mark.parametrize("fixed", range(FIRST_ROUND_MODULES + 1, len(MODULES) + 1))
async def test_a_later_round_with_only_the_turns_to_fold_gets_notes_of_nothing_fixed(
    fixed: int,
) -> None:
    # What a run that drops the notes between two compactions sends: the turns to fold, and no
    # previous notes.
    assert_nothing_is_listed(await notes_for(folded_turns(fixed)))


@pytest.mark.parametrize("fixed", range(FIRST_ROUND_MODULES + 1, len(MODULES) + 1))
async def test_a_said_line_is_not_the_previous_notes(fixed: int) -> None:
    # The first module is in the turns to fold as a line that the model said, without the quote
    # that the notes have in the request. It does not stand in for the notes.
    notes = await notes_for(folded_turns(1) + "\n" + folded_turns(fixed))

    assert notes.files_touched == ["src/longlog/chunks.py"]


async def test_a_later_round_whose_previous_notes_lost_the_first_modules_gets_none_either() -> None:
    # Notes that reached the compactor but lost what was fixed first are as bad as none.
    lost = (await notes_for(await request_of_round(5))).model_copy(
        update={"decisions": ["Fixed ordinal: a"], "files_touched": []}
    )

    assert_nothing_is_listed(await notes_for(previous_notes_block(lost) + folded_turns(6)))


def test_every_step_after_the_first_round_asks_for_the_previous_notes() -> None:
    asked = {
        s["match"]["user_text_contains"]: s["match"].get("request_contains")
        for s in steps(COMPACTOR, "compactor")
        if "user_text_contains" in s["match"]
    }
    for index, module in enumerate(MODULES, start=1):
        wanted = '"Fixed chunks:' if index > FIRST_ROUND_MODULES else None
        assert asked[f"Fixed {module}:"] == wanted, module


# ---------- a play in a fake sandbox ----------
async def git_base(box: FakeSandbox) -> None:
    """Make the sandbox a repo at base, as the workspace setup does, so that git_diff works."""
    done = await box.exec(
        "git init -q -b main && git add -A && "
        "git -c user.name=t -c user.email=t@t -c commit.gpgsign=false "
        "commit -q --no-verify -m base && git tag base",
        timeout_s=60,
    )
    assert done.exit_code == 0, done.stderr


async def play_longlog(script: Path, box: FakeSandbox) -> Any:
    await seed(box, "longlog")
    await git_base(box)
    config = CONFIG.model_copy(
        update={"max_turns": 100, "tool_output_max_tokens": RESULT_CAP_TOKENS}
    )
    registry = default_registry(test_command=TEST_COMMAND)
    return await run_loop(spec_for("longlog"), ScriptedProvider(script), registry, box, config)


@pytest.fixture
async def sbx() -> Any:
    box = FakeSandbox()
    yield box
    await box.destroy()


async def test_the_script_fixes_longlog_while_it_sees_only_the_head_and_tail(
    sbx: FakeSandbox,
) -> None:
    result = await play_longlog(LONGLOG, sbx)

    assert result.outcome.kind is OutcomeKind.PR_OPENED, result.outcome.summary
    assert result.turns == TURNS
    done = await sbx.exec(TEST_COMMAND, timeout_s=60)
    assert done.exit_code == 0, done.stdout
    assert "12 passed" in done.stdout


async def test_the_suite_prints_over_five_thousand_lines_and_its_summary_comes_first(
    sbx: FakeSandbox,
) -> None:
    # The tool returns the whole text, and the loop cuts it to a head, a tail and a handle. The
    # steps of the script read the summary line, which is in the head.
    await seed(sbx, "longlog")
    registry = default_registry(test_command=TEST_COMMAND)
    ctx = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")

    result = await registry.run(ToolCall(id="c", name="run_tests", input={}), sbx, ctx)

    lines = result.output.splitlines()
    assert len(lines) > 5_000
    assert lines[:3] == ["exit code: 1", "summary: 12 failed", "stdout:"]


async def test_the_script_does_not_go_on_after_an_edit_that_does_not_fix_the_module(
    tmp_path: Path, sbx: FakeSandbox
) -> None:
    broken = tmp_path / "broken.yaml"
    text = LONGLOG.read_text(encoding="utf-8")
    # The edit of clamp is still wrong, so its own test fails.
    wrong = text.replace(
        'new_str: "    return max(low, min(value, high))"',
        'new_str: "    return max(low, min(value, high - 2))"',
    )
    assert wrong != text
    broken.write_text(wrong, encoding="utf-8")

    with pytest.raises(ProviderError, match="no scripted step matches"):
        await play_longlog(broken, sbx)
