"""The GitHub that one chaos run delivers to (LH-904).

A world is a FakeGitHub served on a port of its own. Its repo has the base commit of a custom
task as its remote, a bare repo in a folder, and its issue has the text of the task. The worker is
told where all of it is through two settings (`LONGHAUL_GITHUB_API_URL` and
`LONGHAUL_GITHUB_CLONE_BASE`), so it clones, comments, labels, pushes and opens its pull request
the way it does on github.com, and what it wrote can be counted from the fake afterwards.

Each run has a world of its own. Runs share nothing, so a write of one run cannot be taken for a
write of another.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from tests.fakes.fake_github import FakeGitHub, RunningServer, serve_in_thread

from eval import custom_tasks
from eval.chaos import audit
from eval.chaos.results import SideEffects
from longhaul.agent.workspace import DEFAULT_TEST_COMMAND
from longhaul.contracts import Budgets, RepoSettings, TaskSpec

OWNER = "chaos"
# The caps of the eval runner (eval/run.py), so a chaos run is bounded like an eval run.
BUDGETS = Budgets(task_usd=2.0, task_turns=150, task_wall_minutes=120)


@dataclass
class World:
    fake: FakeGitHub
    server: RunningServer
    remotes: Path  # <remotes>/<owner>/<name>.git is the bare repo of each repo
    repo: str  # owner/name
    issue: int
    token: str
    task: custom_tasks.CustomTask

    @classmethod
    def open(cls, root: Path, task: custom_tasks.CustomTask) -> World:
        """Seed the repo and the issue of `task` under `root` and serve them."""
        folder = custom_tasks.REPOS_DIR / task.repo
        custom_tasks.check_agent_view(folder)
        source = root / "source"
        actual = custom_tasks.build_base_repo(folder, source)
        if actual != task.base_sha:
            raise ValueError(
                f"{task.id}: base_sha is out of date, the repo folder is at {actual} "
                "(python eval/custom_tasks.py prints the current ones)"
            )
        repo = f"{OWNER}/{task.repo}"
        remotes = root / "remotes"
        fake = FakeGitHub()
        fake.seed_repo(repo, remote=remotes / f"{repo}.git", source=source)
        issue = fake.seed_issue(repo, task.issue_title, body=task.issue_body, labels=["longhaul"])
        token = f"ghp_chaos{uuid.uuid4().hex}"
        fake.seed_token(token, fake.bot_login)
        return cls(fake, serve_in_thread(fake), remotes, repo, issue, token, task)

    def env(self) -> dict[str, str]:
        """The settings that point a worker at this world."""
        return {
            "LONGHAUL_GITHUB_TOKEN": self.token,
            "LONGHAUL_GITHUB_API_URL": self.server.url,
            "LONGHAUL_GITHUB_CLONE_BASE": str(self.remotes),
        }

    def spec(self, task_id: str) -> TaskSpec:
        """The task of the issue, as the workflow gets it. The plan gate is off: nobody answers."""
        return TaskSpec(
            task_id=task_id,
            repo=self.repo,
            issue_number=self.issue,
            title=self.task.issue_title,
            body=self.task.issue_body,
            settings=RepoSettings(
                repo=self.repo,
                plan_approval_required=False,
                test_command=DEFAULT_TEST_COMMAND,
                budgets=BUDGETS,
            ),
        )

    def side_effects(self) -> SideEffects:
        return audit.count_side_effects(self.fake, self.repo, self.issue)

    def close(self) -> None:
        self.server.stop()
