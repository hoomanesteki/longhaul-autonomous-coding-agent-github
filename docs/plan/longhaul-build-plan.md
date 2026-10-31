# Longhaul: Build Plan

**A background coding agent that takes a GitHub issue, works on it for hours in a sandbox, survives crashes, resumes exactly where it stopped, and asks a person only at checkpoints. It ends with a pull request, or with an honest report of where it got stuck.**

| Field | Value |
|---|---|
| Owner | Hooman Esteki (product owner, final reviewer) |
| Repo | `longhaul-background-agent` |
| Status | Planned. Not started. |
| Plan version | 1.0 (2026-10-21) |
| Target release | v1.0.0 at the end of Phase 9 |
| Sister project | Keystone (MCP security gateway). Optional link in Phase 9 / post-v1. |

## 0. How to use this document

This file is the single source of truth for the build. Copy it into the repo as `docs/plan/longhaul-build-plan.md` in Phase 0. Every story, test name and contract below is meant to be followed literally unless an ADR changes it.

| Reader | What to read | What to do |
|---|---|---|
| Hooman (product owner) | Sections 1, 2, 8, 10, 11 and every "Defend it" block | Approve scope, review each phase gate report, merge `dev` into `main`, run the paid eval runs, record the demo. |
| Developer | Section 3 (contracts), Section 5 (operating model), then one story at a time from Section 7 | Implement exactly one story per branch, tests first, open a PR into `dev`, merge when CI is green. Do not start a story whose dependencies are not merged. |
| Reviewer | Sections 3, 4, 5 and the phase being closed | Run `make gate PHASE=N`, check every acceptance criterion and invariant, write `docs/gates/phase-N.md`, open fix stories when something fails, then open the `dev` to `main` PR. |

Rules of thumb for the developer:

- One story, one branch, one PR. Never mix stories.
- If a story says "Spike", the output is a short ADR plus a throwaway script, not production code.
- If a contract in Section 3 is wrong or missing something, stop and write a one-paragraph note in the PR. Do not quietly change a shared contract inside an unrelated story.
- Never weaken or delete a test to make CI pass.

---

## 1. Product brief

### 1.1 Problem

Teams have a long tail of small, well-specified issues: a bug with a clear repro, a missing flag, a failing test after a dependency bump. Coding agents can fix many of these, but in practice they are run interactively. Someone sits in a terminal, watches the agent, restarts it when it crashes, and copies the result into a PR.

That doesn't scale past a handful of issues a day, and it fails in predictable ways:

1. **Runs die.** A laptop sleeps, a container is killed, the API returns 529 for ten minutes. An interactive agent loses its state and starts over, paying for the same tokens twice.
2. **Context fills up.** Long tasks overflow the context window. The agent forgets what it already tried and loops.
3. **Nobody knows it is stuck.** The agent keeps editing the same three lines for forty minutes. Cost climbs and nobody is told.
4. **Side effects repeat.** A retry posts the same comment twice or opens a second PR.
5. **Cost is a surprise.** There is no per-task budget and no ledger, so the bill is the first signal.

### 1.2 Why now (from our job data)

We sampled 401 recent AI Engineer, ML Engineer, Data Scientist, ML Platform and FDE postings in Seattle and the Bay Area (October 2026).

- 55% ask for agentic systems. 48% ask for LLM or agent evaluation. 36% ask for both in the same post.
- 19% name at least one of: long-running or autonomous agents, coding agents, agent memory, context engineering, computer use. This is the fastest-moving slice of the agentic pool at SF startups and labs.
- SF posts ask for context engineering far more than Seattle (10% vs 2%). Posts at SF agent startups use phrases like "agents that use tools and computers, retain knowledge, and run long-running workflows."
- Hiring writers keep asking for the same proof: a baseline, a fixed eval set, named failure cases, and cost and latency numbers. Longhaul is designed around producing exactly those numbers.

The portfolio already shows evals (TraceGym), routing (ToolSmith), RAG (Skein), multi-agent analytics (OmniAgent), MLOps (Real-Time ML Platform) and human-in-the-loop (Symbiote Lite). It does not show **autonomy over time**: an agent that runs for hours, keeps its state through failures, and manages its own context. Longhaul fills that gap.

### 1.3 Personas

| Persona | Role | What they need from Longhaul |
|---|---|---|
| **Maya** | Staff engineer, tech lead on a 40-person SaaS team | Label an issue before lunch, review a tested PR after lunch. She will not merge anything without passing tests and a readable explanation. |
| **Raj** | Platform / developer-experience engineer | Run agents safely: sandboxed, no credentials inside the sandbox, hard budgets, an audit trail of every command, and dashboards he can put on a wall. |
| **Elena** | Engineering manager | A predictable cost per resolved issue, and a clear signal when an agent is stuck so a person can step in early. |
| **Sam** | Open-source maintainer | Triage relief on "good first issue" bugs, without the agent ever pushing to `main` or spamming the issue thread. |

### 1.4 Jobs to be done

1. When I label an issue, I want an agent to pick it up within a minute, so I don't have to start anything by hand.
2. When the agent runs for hours, I want it to survive crashes and API outages without losing work or paying twice.
3. When the agent is stuck, I want a short summary of what it tried and a way to give it a hint, so I can unblock it in one comment.
4. When the agent finishes, I want a PR with a clear description, passing tests and a run report (time, tokens, cost, interventions).
5. When I review cost, I want a per-task and per-repo ledger, so I can decide whether the agent is worth it.

### 1.5 Goals

- **G1 Durable:** a task survives worker crashes, container loss and provider outages, and resumes from the last checkpoint with no duplicate side effects.
- **G2 Long-horizon:** a task can run past the model's context window through truncation handles and structured compaction.
- **G3 Self-aware:** the agent detects when it is stuck, pauses, and asks a specific question instead of looping.
- **G4 Governed:** hard budgets per task and per repo. The agent never pushes to a default branch. A person merges every PR.
- **G5 Measured:** every claim in the README is backed by a reproducible eval with baselines.

### 1.6 Non-goals (v1)

- Beating the top SWE-bench leaderboard entries. The goal is a sound durable harness with honest numbers, not a model race.
- Multi-language repo support beyond Python. The repo map and test detection are Python-first. Other languages are post-v1.
- Real-time chat with the agent. Interaction is through issue comments, the console and Temporal signals.
- Public webhooks, multi-tenant SaaS and billing.
- Training or fine-tuning models.

### 1.7 Success and proof metrics

These are targets to aim for. Section 8 has the method. The README publishes whatever the real numbers turn out to be.

| Metric | Definition | v1 target |
|---|---|---|
| Crash recovery | Chaos runs that complete after random worker kills / runs | 20/20 (100%) |
| Duplicate side effects | Extra comments, labels or PRs caused by retries, across all chaos runs | 0 |
| Resume latency | Worker restart to next activity started, p95 | < 60 s |
| Resolve rate gain over single-shot | Full Longhaul minus single-shot, same model, SWE-bench Verified subset (n=40) | +20 points or more |
| Resolve rate gain over naive loop | Full Longhaul minus naive loop (no compaction, memory or verifier), same model | +5 points or more |
| Cost per resolved task | Total spend / resolved, full vs naive | Full ≤ naive |
| Compaction savings | 1 - (input tokens with compaction / without) on the long-task set | ≥ 30% savings with ≤ 2 points resolve drop |
| No silent failures | Unresolved tasks that end with a PR, a stuck report or a budget pause / unresolved tasks | 100% |
| Human intervention rate | Tasks needing a hint, budget top-up or stuck unblock / tasks (custom set) | ≤ 25% |
| Durability overhead | Wall time with Temporal vs in-process loop on the same scripted task | < 5% |

### 1.8 Competitive landscape

| Product | What it is | How Longhaul differs |
|---|---|---|
| Devin (Cognition) | Hosted autonomous software engineer | Longhaul is open and self-hosted. Its durability, budgets and stuck handling are inspectable code with published chaos results. |
| OpenAI Codex (cloud tasks) | Runs coding tasks in cloud sandboxes and proposes PRs | Same shape of product. Longhaul shows how the harness works underneath and measures it. |
| GitHub Copilot coding agent | Assign an issue to Copilot; it works in Actions and opens a draft PR | Longhaul runs outside CI minutes, keeps state across hours, and has explicit per-task budgets and a stuck protocol. |
| Claude Code GitHub Actions | `@claude` in issues or PRs runs Claude Code inside an Actions job | An Actions job has a time limit and no durable resume. Longhaul is built for multi-hour runs. |
| Factory (Droids) | Commercial agent platform for software tasks | Enterprise product. Longhaul is a reference implementation with open evals. |
| Sweep | Early issue-to-PR bot | Similar trigger model. Longhaul adds durable execution, compaction and memory. |
| OpenHands | Open-source coding agent platform | Closest open peer. Longhaul's focus is narrower: durable long runs, budget governance, chaos-tested resume and a per-PR run report. |

**Positioning line:** Longhaul is not trying to be a smarter coder than these products. It is a durable, observable harness around a coding agent, with numbers that show what durability, compaction and verification actually buy.

---

## 2. Scope

### 2.1 v1 features (MoSCoW)

| Priority | Feature | Phase |
|---|---|---|
| Must | Own agent loop on the Anthropic Messages API, with a ScriptedProvider for CI | 1 |
| Must | Docker sandbox per task, non-root, resource limits, network off after setup, no credentials inside | 1, 2 |
| Must | Tool set: list_dir, read_file, search, edit_file, write_file, run_command, run_tests, git_diff, read_output | 1, 4 |
| Must | Temporal TaskWorkflow: turns and tool calls as activities, payloads by reference, Continue-As-New | 2 |
| Must | Git-based workspace checkpoints and retry-safe tool execution | 2 |
| Must | GitHub intake through a Temporal Schedule poller, plus a CLI trigger | 3 |
| Must | Idempotent side effects (comments, labels, PRs) | 3 |
| Must | Tool output handles, structured compaction, token budget manager | 4 |
| Must | Planner, Coder and Verifier roles, with an optional human plan gate | 5 |
| Must | Stuck detection, pause and hint protocol | 5 |
| Must | Per-task and per-repo budgets with pause-and-ask | 7 |
| Must | Cost ledger, OTel traces, Prometheus metrics, run report in the PR | 7 |
| Must | Eval harness: SWE-bench Verified subset, custom tasks, baselines, chaos suite | 9 |
| Should | OpenAI-compatible provider | 1 |
| Should | Per-repo long-term memory with a safe write policy | 6 |
| Should | Console (React + Vite + TypeScript) | 8 |
| Should | GitHub App auth (a PAT is enough for local dev) | 3 |
| Should | Git bundle backup of checkpoints to blob storage, for cross-host resume | 2 |
| Could | Terraform for AWS (ECS Fargate workers, RDS, S3) | 9 |
| Could | Route tool calls through Keystone for policy and audit | 9 |
| Could | Allowlisted package-index proxy during the agent phase | post-v1 |
| Won't (v1) | Webhooks, non-Python repo maps, multi-tenant SaaS, model training | none |

### 2.2 Out of scope

- Any automatic merge. A person always merges.
- Writing to repos that are not on the allowlist.
- Running untrusted code outside the sandbox.
- Browser or desktop computer use. The agent only works with code and shell.

### 2.3 Post-v1 backlog

- GitHub webhook intake through a GitHub App, replacing polling.
- Repo maps for TypeScript and Go via tree-sitter.
- Keystone as the default tool path.
- Multi-agent parallel attempts (best-of-N with the Verifier as judge).
- PR review loop: read reviewer comments on the PR and push follow-up commits.
- Slack notifications for stuck and budget events.
- Temporal Cloud deployment profile.

---

## 3. Architecture

### 3.1 System context

```mermaid
flowchart LR
  subgraph GitHub
    ISS[Issue labeled 'longhaul']
    PR[Pull request]
    CMT[Issue comments: /longhaul approve, hint, budget, cancel]
  end

  subgraph Longhaul
    POLL[PollerWorkflow<br/>Temporal Schedule, every 60s]
    WF[TaskWorkflow<br/>one per issue]
    W[Worker process<br/>activities]
    API[FastAPI<br/>console API, health, metrics]
    CON[Console<br/>React + Vite + TS]
    CLI[longhaul CLI]
  end

  subgraph Infra
    T[(Temporal server)]
    PG[(Postgres + pgvector)]
    S3[(MinIO / S3 blobs)]
    SB[[Docker sandbox<br/>per task]]
    CK[(Bare checkpoint repo<br/>Docker volume)]
    OT[OTel collector -> Jaeger]
    PM[Prometheus -> Grafana]
  end

  LLM[(Anthropic Messages API<br/>or OpenAI-compatible)]

  ISS --> POLL
  CMT --> POLL
  POLL -->|start / signal| WF
  CLI -->|start| WF
  WF <--> T
  T <--> W
  W --> LLM
  W --> SB
  SB <--> CK
  W --> S3
  W --> PG
  W -->|idempotent writes| PR
  W -->|idempotent writes| CMT
  CON --> API
  API --> PG
  API -->|signals / queries| T
  W --> OT
  W --> PM
```

### 3.2 Task lifecycle

```mermaid
sequenceDiagram
  autonumber
  participant GH as GitHub
  participant P as PollerWorkflow
  participant WF as TaskWorkflow
  participant A as Activities (worker)
  participant SB as Sandbox
  participant L as LLM

  P->>GH: list issues with label 'longhaul' (allowlisted repos)
  P->>WF: start workflow id lh-owner-repo-42 (reject duplicate)
  WF->>A: prepare_workspace (clone to bare repo, create sandbox, run setup, cut network)
  A->>SB: create container, checkout base
  WF->>A: load_memory (repo map, commands, approved lessons)
  WF->>A: llm_turn(role=planner) ... until plan submitted
  A->>L: messages.create
  WF->>A: post_comment(plan) [idempotent]
  opt plan_approval_required
    WF-->>WF: wait_condition(approved or cancelled)
    GH->>P: /longhaul approve
    P->>WF: signal approve_plan
  end
  loop each turn (coder, then verifier rounds)
    WF->>A: check_budget
    WF->>A: llm_turn(role) -> tool_calls (refs)
    WF->>A: tool_exec(call) -> result ref, pre_sha, post_sha
    A->>SB: ensure_at(pre_sha), run, commit if mutated
    WF->>WF: stuck detector, compaction trigger, continue-as-new every N turns
  end
  alt verified
    WF->>A: build_pr_branch (squash per plan step), push, create PR [idempotent]
  else stuck or budget
    WF->>A: post_comment(stuck report / budget ask) and wait for signal
  end
  WF->>A: write_run_report, extract_lessons (candidates only)
```

### 3.3 Components

| Component | Package | Responsibility |
|---|---|---|
| Contracts | `longhaul.contracts` | Pydantic models and Protocols shared by every module. No I/O. |
| Providers | `longhaul.providers` | `AnthropicProvider`, `OpenAICompatProvider`, `ScriptedProvider`, `ReplayProvider`. Map vendor errors to `ProviderError` kinds. |
| Agent core | `longhaul.agent` | Prompt assembly, role definitions, the in-process loop (Phase 1), turn logic reused by activities. |
| Tools | `longhaul.tools` | Tool specs and implementations that run inside a `Sandbox`. |
| Sandbox | `longhaul.sandbox` | Docker lifecycle, exec, file IO, limits, network cut, reattach. |
| Checkpoints | `longhaul.checkpoints` | Bare repo per task, micro-commits, restore, git bundle backup, PR branch squash. |
| Blob store | `longhaul.blobs` | Content-addressed put/get on MinIO/S3. |
| Durable layer | `longhaul.durable` | `workflows/` (pure, deterministic) and `activities/` (all I/O). Worker entry point. |
| GitHub | `longhaul.github` | Read client, idempotent side-effect activities, command parser, poller logic. |
| Context | `longhaul.context` | Token counting, truncation handles, two-tier eviction, compaction. |
| Roles | `longhaul.roles` | Planner, Coder, Verifier prompts, tool subsets and output schemas. Stuck detector. |
| Memory | `longhaul.memory` | Repo map builder, embeddings, memory store, retrieval, lesson extraction, promotion. |
| Cost and budgets | `longhaul.cost` | Pricing table, ledger, budget checks. |
| Telemetry | `longhaul.telemetry` | OTel setup, metrics registry, structured logs, redaction. |
| API | `longhaul.api` | FastAPI app for console, health and metrics. |
| Console | `console/` | React app. |
| Eval | `eval/` | SWE-bench adapter, custom tasks, baselines, chaos, reports. |

### 3.4 Architecture decisions (ADR summaries)

Each ADR becomes a file in `docs/adr/NNNN-title.md` (template: Context, Decision, Consequences, Alternatives).

- **ADR-0001 Record decisions as ADRs.** Every non-obvious choice gets a short ADR in the same PR as the code.
- **ADR-0002 Pin dependencies.** Pin the latest stable versions at project start with `uv lock`. Record the key versions (temporalio, anthropic, docker, fastapi, sqlalchemy, pgvector, opentelemetry) in the ADR. Upgrades get their own `chore/` PR.
- **ADR-0003 Own agent loop on the Messages API.** We write our own loop instead of running Claude Agent SDK's loop or a LangGraph graph as the core. Hooman has shipped with both; they are good tools for interactive and graph-shaped agents. Here the unit of durability is one model turn and one tool call: each must be a separate Temporal activity with its own retry policy, timeout, heartbeat and checkpoint. The Agent SDK owns its loop and session transcript, so we can't checkpoint or retry between its internal steps. LangGraph with a Postgres checkpointer could checkpoint per node, but we would still need a job runner, durable timers (24 h hint waits), schedules, signals and a history UI, and Temporal gives us all of those. A thin `LLMProvider` protocol keeps the loop vendor-neutral.
- **ADR-0004 Temporal for durable execution.** Alternatives considered: Celery/RQ (no durable workflow state), a DIY Postgres state machine (we would rebuild retries, timers and signals), and LangGraph checkpointer (see ADR-0003). Temporal provides deterministic replay, activity retries, heartbeats, signals, queries, schedules and Continue-As-New. Cost: determinism rules in workflow code, which invariant I1 enforces.
- **ADR-0005 Payloads by reference.** Transcripts, tool args, tool outputs and diffs go to the blob store. Workflows and activities pass `BlobRef`s and small summaries only, with a hard cap of 256 KB per payload (invariant I2). Continue-As-New runs every 40 turns, or earlier when Temporal suggests it.
- **ADR-0006 Git checkpoints.** Each task has a bare repo in a Docker volume. Every mutating tool call is followed by a micro-commit on scratch branch `longhaul/<issue>`, so each tool call has a `pre_sha` and `post_sha`. Before any retry, the workspace is hard-reset to `pre_sha`. At PR time we build a separate branch `longhaul-pr/<issue>` with one squashed commit per completed plan step. A git bundle is uploaded to blob storage at each Continue-As-New, so a different host can resume (Should).
- **ADR-0007 Roles as phases inside one workflow.** Planner, Coder and Verifier are role prompts with tool subsets inside the same TaskWorkflow, sharing one sandbox and one transcript store. Child workflows were rejected: they need separate transcripts, separate sandboxes or sandbox handoff, and more signal plumbing, for no gain at our scale.
- **ADR-0008 Poll, don't webhook (v1).** A Temporal Schedule runs `PollerWorkflow` every 60 s. No public endpoint, no tunnel, no webhook secrets. A latency of up to 60 s is acceptable for background work.
- **ADR-0009 Sandbox model.** One Docker container per task, non-root user, `--cap-drop ALL`, `no-new-privileges`, pids/memory/cpu limits, read-only root filesystem except `/workspace` and `/tmp`. The network is enabled only for the setup command, then disconnected. No GitHub token or LLM key ever enters the container: the worker clones into the bare repo on the host and pushes from the host side.
- **ADR-0010 Memory write policy.** Memory items used in prompts must be `approved`. Approval comes only from the default branch (repo map, verified commands), a merged Longhaul PR (lessons), or an explicit human approval. Unmerged work can only create `candidate` items, which prevents memory poisoning.
- **ADR-0011 Console stack.** React + Vite + TypeScript, with API types generated from FastAPI's OpenAPI schema. The timeline view is interactive enough to justify React, Hooman already knows it, and TypeScript is asked for in 15.5% of our sampled posts (19% in SF).
- **ADR-0012 GitHub auth.** A fine-grained PAT for local development (Must). A GitHub App with short-lived installation tokens for any shared deployment (Should). Tokens live in the worker environment only.
- **ADR-0013 Two-tier context strategy.** Tier 1: tool outputs older than K turns are elided to a one-line stub with their handle. Tier 2: past the threshold, older turns become structured `ProgressNotes`. The cut point is always a turn boundary where every `tool_use` has its `tool_result`.

### 3.5 Data model (Postgres)

All tables carry `created_at timestamptz default now()`. IDs are text unless noted.

| Table | Key fields | Notes |
|---|---|---|
| `repo_settings` | `repo` (pk, `owner/name`), `allowlisted bool`, `settings jsonb` | Loaded from `longhaul.yaml` at startup; the DB copy is what the API reads. |
| `tasks` | `id` (pk, workflow id `lh-owner-repo-42`), `repo`, `issue_number int`, `title`, `status`, `phase`, `outcome`, `pr_url`, `model_coder`, `cost_usd numeric(10,4)`, `turns int`, `started_at`, `ended_at`, `settings jsonb` | One row per task. Updated by activities. |
| `turns` | `task_id`, `turn_index int`, `attempt int`, `role`, `request_ref`, `response_ref`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `stop_reason`, `latency_ms`, `started_at`, `ended_at` | pk (`task_id`, `turn_index`, `attempt`). |
| `tool_calls` | `id` (pk, tool_use id), `task_id`, `turn_index`, `name`, `args_ref`, `ok bool`, `exit_code int null`, `output_ref`, `duration_ms`, `pre_sha`, `post_sha null`, `attempt int` | |
| `checkpoints` | `id serial`, `task_id`, `sha`, `kind` (`tool`, `step`, `compaction`, `bundle`), `step_id null`, `turn_index`, `bundle_ref null` | |
| `plans` | `task_id`, `version int`, `plan jsonb`, `status` (`proposed`, `approved`, `rejected`, `superseded`), `decided_by null`, `decided_at null` | pk (`task_id`, `version`). |
| `events` | `id serial`, `task_id`, `ts`, `type`, `payload jsonb` | Types: `phase_changed`, `paused`, `resumed`, `hint`, `budget_added`, `stuck`, `compaction`, `verdict`, `continue_as_new`, `sandbox_recreated`, `cancelled`. |
| `side_effects` | `key` (pk), `task_id`, `kind` (`comment`, `label_add`, `label_remove`, `pr`, `push`), `external_id`, `payload_hash` | The idempotency ledger. |
| `cost_ledger` | `id serial`, `task_id`, `repo`, `model`, `turn_index`, `attempt`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `usd numeric(10,6)`, `ts` | Every provider call writes one row, including retries and compaction calls. |
| `memory_items` | `id uuid`, `repo`, `kind` (`repo_map`, `command`, `convention`, `pitfall`, `lesson`), `content text`, `embedding vector(384)`, `status` (`candidate`, `approved`, `rejected`), `source` (`default_branch`, `merged_pr`, `human`, `task`), `source_ref`, `approved_by null`, `uses int`, `last_used_at` | HNSW or ivfflat index on `embedding`. |

Migrations use Alembic. Each story that adds a table adds exactly one migration.

### 3.6 Contracts (code every story builds against)

File: `src/longhaul/contracts.py`. This module has no I/O and imports only pydantic, enum and typing. Changing it requires a note in the PR and a reviewer check.

```python
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from pydantic import BaseModel, Field

# ---------- basic records ----------
class Role(StrEnum):
    PLANNER = "planner"
    CODER = "coder"
    VERIFIER = "verifier"
    COMPACTOR = "compactor"

class BlobRef(BaseModel):
    key: str            # e.g. "tasks/lh-o-r-42/turns/0007/response.json"
    sha256: str
    size: int

class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]

class ToolCall(BaseModel):
    id: str             # provider tool_use id
    name: str
    input: dict[str, Any]

class ToolResult(BaseModel):
    call_id: str
    ok: bool
    output: str                       # what the model sees (possibly truncated)
    output_ref: BlobRef | None = None # full output when truncated
    exit_code: int | None = None
    duration_ms: int = 0
    pre_sha: str | None = None
    post_sha: str | None = None       # set when the call mutated the workspace

class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]

class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False

Block = TextBlock | ToolUseBlock | ToolResultBlock

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[Block]

class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

class LLMResponse(BaseModel):
    content: list[Block]
    tool_calls: list[ToolCall]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "other"]
    usage: Usage
    model: str
    latency_ms: int

class ProviderErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"      # retryable
    OVERLOADED = "overloaded"      # retryable
    SERVER = "server"              # retryable
    TIMEOUT = "timeout"            # retryable
    CONTEXT_TOO_LONG = "context_too_long"  # not retryable, triggers forced compaction
    INVALID_REQUEST = "invalid_request"    # not retryable
    AUTH = "auth"                  # not retryable

class ProviderError(Exception):
    def __init__(self, kind: ProviderErrorKind, message: str, retry_after_s: float | None = None): ...

# ---------- task and settings ----------
class Budgets(BaseModel):
    task_usd: float = 5.0
    task_turns: int = 150
    task_wall_minutes: int = 240
    daily_repo_usd: float = 25.0

class RepoSettings(BaseModel):
    repo: str                          # "owner/name" or "local:/abs/path"
    plan_approval_required: bool = False
    setup_command: str | None = None   # runs with network, before the agent phase
    test_command: str | None = None    # e.g. "pytest -q"
    budgets: Budgets = Budgets()

class TaskSpec(BaseModel):
    task_id: str                       # "lh-owner-repo-42"
    repo: str
    issue_number: int | None
    title: str
    body: str
    comments: list[str] = []
    base_branch: str = "main"
    base_sha: str | None = None
    settings: RepoSettings

# ---------- plan, verdicts, notes ----------
class PlanStep(BaseModel):
    id: str                            # "s1", "s2", ...
    description: str
    files: list[str] = []
    done_when: str
    status: Literal["todo", "doing", "done", "skipped"] = "todo"

class Plan(BaseModel):
    summary: str
    steps: list[PlanStep]
    acceptance_checks: list[str]       # commands or observable checks
    risks: list[str] = []

class VerifierVerdict(BaseModel):
    passed: bool
    tests_run: str                     # command executed
    tests_summary: str                 # "41 passed, 1 failed"
    issues: list[str] = []
    send_back_step: str | None = None

class ProgressNotes(BaseModel):
    goal: str
    plan_status: list[PlanStep]
    files_touched: list[str]
    decisions: list[str]
    failing_tests: list[str]
    open_hypotheses: list[str]
    tried_and_failed: list[str]
    covers_turns_up_to: int

class StuckReason(StrEnum):
    NO_PROGRESS = "no_progress"
    REPEATED_FAILURE = "repeated_failure"
    EDIT_CHURN = "edit_churn"
    TOOL_ERRORS = "tool_errors"
    AGENT_ASKED = "agent_asked"

class StuckSignal(BaseModel):
    reason: StuckReason
    evidence: list[str]

class Phase(StrEnum):
    QUEUED = "queued"; PREPARING = "preparing"; PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"; CODING = "coding"
    VERIFYING = "verifying"; PAUSED = "paused"; DELIVERING = "delivering"
    DONE = "done"; FAILED = "failed"; CANCELLED = "cancelled"

class OutcomeKind(StrEnum):
    PR_OPENED = "pr_opened"; STUCK = "stuck"; BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"; ERROR = "error"

class Outcome(BaseModel):
    kind: OutcomeKind
    pr_url: str | None = None
    summary: str

class TaskStatus(BaseModel):
    task_id: str
    phase: Phase
    turn_index: int
    cost_usd: float
    last_checkpoint: str | None
    plan_version: int | None
    paused_reason: str | None = None
    outcome: Outcome | None = None

# ---------- workflow carry state (Continue-As-New) ----------
class CarryState(BaseModel):
    spec: TaskSpec
    phase: Phase
    turn_index: int = 0
    transcript_ref: BlobRef | None = None   # list[Message] json
    notes: ProgressNotes | None = None
    plan: Plan | None = None
    plan_version: int = 0
    plan_approved: bool = False
    head_sha: str | None = None
    sandbox_id: str | None = None
    verify_rounds: int = 0
    cost_usd: float = 0.0
    extra_budget_usd: float = 0.0
    extra_turns: int = 0
    started_at: datetime
    hints: list[str] = []
    stuck_count: int = 0

# ---------- protocols ----------
class LLMProvider(Protocol):
    name: str
    async def complete(self, *, model: str, system: str, messages: list[Message],
                       tools: list[ToolSpec], max_tokens: int,
                       cache_system: bool = True) -> LLMResponse: ...
    async def count_tokens(self, *, model: str, system: str, messages: list[Message],
                           tools: list[ToolSpec]) -> int: ...

class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

class Sandbox(Protocol):
    id: str
    workdir: str                       # "/workspace"
    async def exec(self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None) -> ExecResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def destroy(self) -> None: ...

class SandboxLimits(BaseModel):
    cpus: float = 2.0
    memory_mb: int = 4096
    pids: int = 512
    disk_note: str = "bounded by volume"

class SandboxManager(Protocol):
    async def create(self, task_id: str, image: str, limits: SandboxLimits, network: bool) -> Sandbox: ...
    async def get(self, sandbox_id: str) -> Sandbox | None: ...
    async def set_network(self, sandbox_id: str, enabled: bool) -> None: ...

class ToolContext(BaseModel):
    task_id: str
    turn_index: int
    blob_prefix: str
    output_max_tokens: int = 4000

class Tool(Protocol):
    spec: ToolSpec
    mutates: bool
    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

class BlobStore(Protocol):
    async def put(self, data: bytes, *, key: str) -> BlobRef: ...
    async def get(self, ref: BlobRef) -> bytes: ...

class CheckpointStore(Protocol):
    async def init_task(self, task_id: str, clone_url: str, base_sha: str) -> str: ...
    async def commit(self, sandbox: Sandbox, message: str) -> str | None: ...  # None if clean
    async def restore(self, sandbox: Sandbox, sha: str) -> None: ...
    async def bundle(self, task_id: str) -> BlobRef: ...

class MemoryItem(BaseModel):
    id: str
    repo: str
    kind: Literal["repo_map", "command", "convention", "pitfall", "lesson"]
    content: str
    status: Literal["candidate", "approved", "rejected"]
    source: Literal["default_branch", "merged_pr", "human", "task"]

class MemoryStore(Protocol):
    async def search(self, repo: str, query: str, k: int, approved_only: bool = True) -> list[MemoryItem]: ...
    async def add_candidate(self, item: MemoryItem) -> str: ...
    async def promote(self, item_id: str, by: str) -> None: ...
    async def reject(self, item_id: str, by: str) -> None: ...

class Embedder(Protocol):
    dim: int
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class GitHubGateway(Protocol):
    async def list_labeled_issues(self, repo: str, label: str) -> list[dict[str, Any]]: ...
    async def get_issue(self, repo: str, number: int) -> dict[str, Any]: ...
    async def list_comments_since(self, repo: str, number: int, since: datetime) -> list[dict[str, Any]]: ...
    async def post_comment(self, repo: str, number: int, body: str, *, idempotency_key: str) -> str: ...
    async def add_label(self, repo: str, number: int, label: str, *, idempotency_key: str) -> None: ...
    async def remove_label(self, repo: str, number: int, label: str, *, idempotency_key: str) -> None: ...
    async def create_pr(self, repo: str, head: str, base: str, title: str, body: str,
                        *, draft: bool, idempotency_key: str) -> str: ...
    async def pr_merged(self, repo: str, pr_number: int) -> bool: ...
```

### 3.7 Workflow and activity contracts

File: `src/longhaul/durable/workflows/task.py` (pure) and `src/longhaul/durable/activities/*.py` (I/O).

| Activity | Input | Output | Timeouts and retry |
|---|---|---|---|
| `prepare_workspace` | `TaskSpec` | `{sandbox_id, base_sha, head_sha}` | start-to-close 15 min, heartbeat 60 s, 3 attempts |
| `ensure_sandbox` | `task_id, head_sha, sandbox_id?` | `sandbox_id` | 10 min, 5 attempts |
| `load_memory` | `repo, query` | `BlobRef` (rendered memory block) | 2 min, 3 attempts |
| `llm_turn` | `LlmTurnInput{task_id, turn_index, role, transcript_ref, notes, plan, memory_ref, hints}` | `LlmTurnOutput{transcript_ref, tool_calls_ref, tool_call_summaries, stop_reason, usage, cost_usd, input_tokens_total}` | 10 min, heartbeat 30 s, max 8 attempts, backoff 2 s to 120 s, non-retryable: `invalid_request`, `auth`, `context_too_long` |
| `tool_exec` | `ToolExecInput{task_id, turn_index, call: ToolCall (args ref if > 16 KB), pre_sha, sandbox_id}` | `ToolResult` (output truncated, full by ref) | tool timeout + 60 s, heartbeat 15 s, 3 attempts, always restores `pre_sha` first |
| `compact` | `task_id, transcript_ref, notes` | `{transcript_ref, notes}` | 10 min, 5 attempts |
| `check_budget` | `task_id, repo, limits` | `{ok, reason, spent_task, spent_repo_today}` | 30 s, 3 attempts |
| `record_event` | `task_id, type, payload` | none | local activity, 10 s |
| `post_comment` / `add_label` / `remove_label` | as gateway, plus `idempotency_key` | external id | 2 min, 10 attempts, backoff to 5 min |
| `build_pr_branch` | `task_id, plan` | `{branch, head_sha}` | 5 min, 3 attempts |
| `create_pr` | as gateway | `pr_url` | 2 min, 10 attempts |
| `write_run_report` | `task_id` | `BlobRef` (markdown) | 2 min, 3 attempts |
| `extract_lessons` | `task_id` | `list[str]` candidate ids | 5 min, 2 attempts |
| `bundle_checkpoint` | `task_id` | `BlobRef` | 5 min, 3 attempts |

Signals on `TaskWorkflow`: `approve_plan(by: str)`, `reject_plan(by: str, feedback: str)`, `hint(by: str, text: str)`, `add_budget(by: str, usd: float, turns: int)`, `cancel(by: str, reason: str)`.
Query: `status() -> TaskStatus`.

Idempotency key format: `{workflow_id}:{purpose}:{seq}`, e.g. `lh-acme-calc-42:plan_comment:1`. Comments carry a hidden marker `<!-- longhaul:key=... -->` so the activity can find an existing comment even if the DB row was lost.

### 3.8 Configuration

Environment variables (secrets only through env):

| Variable | Purpose |
|---|---|
| `LONGHAUL_DATABASE_URL` | Postgres DSN |
| `LONGHAUL_TEMPORAL_ADDRESS`, `LONGHAUL_TEMPORAL_NAMESPACE`, `LONGHAUL_TASK_QUEUE` | Temporal connection (`localhost:7233`, `default`, `longhaul`) |
| `LONGHAUL_BLOB_ENDPOINT`, `LONGHAUL_BLOB_BUCKET`, `LONGHAUL_BLOB_ACCESS_KEY`, `LONGHAUL_BLOB_SECRET_KEY` | MinIO / S3 |
| `LONGHAUL_PROVIDER` | `anthropic`, `openai`, `scripted`, `replay` |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Provider credentials (worker only) |
| `LONGHAUL_GITHUB_TOKEN` | Fine-grained PAT for dev |
| `LONGHAUL_GITHUB_APP_ID`, `LONGHAUL_GITHUB_APP_KEY_PATH` | GitHub App (Should) |
| `LONGHAUL_ADMIN_TOKEN` | Console/API bearer token |
| `LONGHAUL_CONFIG` | Path to `longhaul.yaml` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel collector |

`longhaul.yaml` (checked in as `config/longhaul.example.yaml`):

```yaml
poller:
  interval_seconds: 60
  label: longhaul
repos:
  - repo: hoomanesteki/lh-demo-calc
    allowlisted: true
    plan_approval_required: true
    setup_command: "pip install -e '.[dev]'"
    test_command: "pytest -q"
    budgets: { task_usd: 5, task_turns: 150, task_wall_minutes: 240, daily_repo_usd: 25 }
models:                 # set to current model ids at project start
  planner: "<sonnet-class model id>"
  coder: "<sonnet-class model id>"
  verifier: "<sonnet-class model id>"
  compactor: "<haiku-class model id>"
sandbox:
  image: "longhaul-sandbox:py312"
  limits: { cpus: 2, memory_mb: 4096, pids: 512 }
  command_timeout_s: 600
context:
  window_tokens: 200000
  compact_at: 0.60
  hard_limit: 0.85
  keep_recent_turns: 6
  elide_tool_outputs_after_turns: 4
  tool_output_max_tokens: 4000
loop:
  max_verify_rounds: 3
  continue_as_new_every_turns: 40
  max_tokens_per_turn: 8192
stuck:
  no_progress_turns: 15
  repeated_failure_count: 3
  churn_edits: 4
  consecutive_tool_errors: 5
  hint_wait_hours: 24
pricing_file: config/pricing.yaml   # fill from the provider's pricing page; never hardcode in code
```

### 3.9 Repo layout

```
longhaul-background-agent/
  CONTRIBUTING.md
  README.md
  Makefile
  pyproject.toml / uv.lock
  docker-compose.yml               # postgres+pgvector, temporal dev, temporal-ui, minio, otel-collector, jaeger, prometheus, grafana
  config/
    longhaul.example.yaml
    pricing.yaml
  docker/
    sandbox/Dockerfile             # python 3.12, git, ripgrep, non-root 'agent'
    worker/Dockerfile
    api/Dockerfile
  src/longhaul/
    contracts.py
    settings.py
    cli.py                         # typer: run, run-local, worker, poller, api, memory, eval
    providers/{base,anthropic,openai_compat,scripted,replay}.py
    agent/{prompts,loop,render}.py
    tools/{registry,files,search,exec,git,output}.py
    sandbox/{docker_manager,local_manager}.py
    checkpoints/{git_store,squash}.py
    blobs/s3.py
    context/{tokens,truncate,evict,compact}.py
    roles/{planner,coder,verifier,stuck}.py
    memory/{repo_map,embedder,store,retrieve,lessons}.py
    cost/{pricing,ledger,budget}.py
    github/{client,side_effects,commands,intake}.py
    durable/
      workflows/{task,poller}.py   # deterministic, no I/O imports
      activities/{workspace,llm,tools,context,github,cost,report,memory}.py
      worker.py
      client.py
    telemetry/{otel,metrics,logging}.py
    api/{app,routes_tasks,routes_memory,deps}.py
    db/{models,session}.py
    db/migrations/                 # alembic
  console/                         # vite + react + ts
  eval/
    swebench/{subset.txt,adapter.py,run.py}
    custom/{tasks.yaml,repos/,hidden_tests/}
    baselines/{single_shot.py,naive.py}
    chaos/{chaos.py,recordings/}
    report/{build_report.py}
  tests/
    unit/ integration/ e2e/ chaos/
    fixtures/repos/{calc,textkit,inventory}/
    fakes/{fake_github.py,fake_sandbox.py}
  docs/
    plan/longhaul-build-plan.md
    adr/ gates/ progress.md architecture.md runbook.md results.md
  scripts/{import_backlog.py,chaos_kill.sh,seed_demo.sh}
  .github/{workflows/ci.yml,pull_request_template.md}
```

### 3.10 Tech stack

| Tech | Why |
|---|---|
| Python 3.12, uv | Fast, reproducible installs. Hooman's main language. |
| Temporal (Python SDK) | Durable workflows, retries, timers, signals, schedules, and a UI for free (ADR-0004). |
| Anthropic Messages API | Primary model provider with tool use and prompt caching. |
| OpenAI-compatible client | Second provider. Also works with local or self-hosted endpoints. |
| Docker SDK for Python | Sandbox lifecycle without shelling out. |
| Git (CLI inside sandbox, pygit2 not needed) | Checkpoints are plain commits, easy to inspect. |
| Postgres + pgvector | One database for state, ledger and memory vectors. |
| MinIO (S3 API) | Local blob store with the same API as S3 in AWS. |
| FastAPI | Console API, health and metrics. |
| SQLAlchemy 2.0 + Alembic | Typed models and migrations. |
| OpenTelemetry, Jaeger | Traces per turn and tool call, using GenAI semantic conventions. |
| Prometheus, Grafana | Metrics and a dashboard. |
| React + Vite + TypeScript | Console (ADR-0011). |
| pytest, ruff, mypy, Playwright | Tests, lint, types, console e2e. |
| GitHub Actions | CI. |
| swebench (official harness) | Grading on SWE-bench Verified. |
| Terraform (Could) | AWS deploy. |

---

## 4. Reliability and failure model

### 4.1 Failure modes

| # | Failure | Detection | Handling | Test |
|---|---|---|---|---|
| F1 | Worker process crashes mid-activity | Temporal heartbeat timeout (15 to 60 s) or start-to-close timeout | Temporal retries the activity on any live worker. `tool_exec` restores `pre_sha` before running again. `llm_turn` re-sends the same request (one extra ledger row, marked `attempt=2`). | `tests/chaos/test_kill_worker.py::test_kill_during_tool_exec_resumes`, `::test_kill_during_llm_turn_resumes` |
| F2 | Worker crashes between activities | Workflow task timeout | Workflow replays from history on another worker. No activity runs twice, because completed results are in history. | `tests/chaos/test_kill_worker.py::test_kill_between_activities` |
| F3 | Sandbox container dies or is removed | `ensure_sandbox` finds no container, or `exec` raises `NotFound` | Recreate the container from the image, clone from the bare repo, check out `head_sha`, re-run setup, record a `sandbox_recreated` event. | `tests/integration/test_sandbox_recovery.py::test_container_removed_is_recreated_at_head` |
| F4 | Worker host lost (bare repo volume gone) | `init_task` finds no bare repo for a running task | Restore from the latest git bundle in blob storage (Should). If there is none, restart from `base_sha`, keep the transcript, and add a note telling the agent to redo its edits. Recorded as an event. | `tests/integration/test_checkpoints.py::test_restore_from_bundle` |
| F5 | LLM 429 / 529 / 5xx / timeout | `ProviderError` kind `rate_limit`, `overloaded`, `server` or `timeout` | Activity retry with exponential backoff (2 s to 120 s, honours `retry_after_s`), up to 8 attempts. If attempts run out, the workflow pauses with reason `provider_unavailable` and retries after 10 min (up to 6 times) before failing with an error outcome. | `tests/unit/test_providers.py::test_error_mapping`, `tests/integration/test_llm_activity.py::test_retries_on_overloaded` |
| F6 | Context too long | Pre-flight token estimate over `hard_limit`, or `ProviderError(CONTEXT_TOO_LONG)` | Force compaction, then retry the turn once. If it is still too long, keep only the most recent 2 turns plus the notes. | `tests/integration/test_compaction.py::test_forced_compaction_on_overflow` |
| F7 | Tool command hangs | Timeout inside `Sandbox.exec` | Kill the process group and return `timed_out=true` to the model with a short hint. Counted toward the consecutive tool-error rule. | `tests/integration/test_tools_exec.py::test_run_command_timeout_kills_process` |
| F8 | Agent loops or makes no progress | `StuckDetector` rules (Section 7, LH-505) | Pause, post a stuck summary, and wait for `hint` or `cancel` up to 24 h. On timeout, end with outcome `stuck` and a report. | `tests/unit/test_stuck.py::*`, `tests/e2e/test_stuck_flow.py::test_stuck_pause_hint_resume` |
| F9 | Budget exhausted | `check_budget` before each turn: spent + projected next turn > limit | Pause, comment with numbers, and wait for `add_budget` or `cancel`. On timeout, end with outcome `budget_exhausted`. | `tests/e2e/test_budget_flow.py::test_pause_and_top_up` |
| F10 | GitHub API failure or rate limit | HTTP 5xx, 403 with rate-limit headers, network errors | Activity retries with backoff up to 5 min. Reads are retried freely; writes go through idempotent activities. | `tests/integration/test_github_side_effects.py::test_retry_after_5xx_no_duplicate` |
| F11 | Duplicate side effects on retry | `side_effects` row with the same key, or a hidden marker found in existing comments or PRs | Return the existing external id and do not write again. | `tests/integration/test_github_side_effects.py::test_comment_idempotent_after_db_loss` |
| F12 | Temporal history too large | Turn count, or `is_continue_as_new_suggested()` (verify in spike LH-201) | Continue-As-New with `CarryState`. | `tests/integration/test_task_workflow.py::test_continue_as_new_preserves_state` |
| F13 | Non-determinism after a code change | Temporal replay error in tests | Workflow versioning (`workflow.patched`) for any change to a deployed workflow. A replay test runs against recorded histories. | `tests/integration/test_replay.py::test_recorded_histories_replay` |
| F14 | Malformed model output (bad JSON for plan or verdict) | Pydantic validation error | Send one repair turn with the validation error. If that fails, fall back to text and record an event. | `tests/unit/test_roles.py::test_plan_repair_turn` |
| F15 | Agent tries to touch the default branch | Branch guard in `build_pr_branch` and in push helpers | Raise `ForbiddenBranchError`. This is never retryable and is recorded as an event. | `tests/unit/test_branch_guard.py::test_refuses_default_branch` |

### 4.2 Security notes

- **Credentials:** the GitHub token and LLM keys live only in the worker process environment. The sandbox environment is built from an explicit allowlist (`PATH`, `HOME`, `LANG`, `PYTHONUNBUFFERED`), and invariant I3 has a test for it.
- **Code execution:** all repo code runs inside the sandbox. The worker never imports or executes repo code.
- **Container hardening:**
  - user `agent` (uid 10001), `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`, `pids_limit`, `mem_limit`, `nano_cpus`
  - read-only root filesystem with tmpfs `/tmp`
  - `/workspace` is a per-task volume
  - optional gVisor runtime (`runsc`) documented in the runbook
- **Network:** on during `setup_command` only, then disconnected with `network.disconnect`. An allowlisted package proxy is post-v1.
- **Prompt injection from repo content:** issue bodies and repo files are untrusted. The agent's tools can't reach the network, can't read worker secrets, and can't push. The worst case is a bad diff, which a person reviews. Routing tools through Keystone (Could) adds policy checks and audit.
- **Logs:** a redaction filter removes tokens matching common key patterns (`sk-`, `ghp_`, `github_pat_`, `AKIA`) from logs, traces and run reports.

### 4.3 Invariants (checked by tests and at every phase gate)

| ID | Invariant | Enforced by |
|---|---|---|
| I1 | Workflow modules import no I/O libraries (httpx, httpx2, httpcore2, docker, sqlalchemy, boto3, anthropic, openai, subprocess) | `tests/unit/test_invariants.py::test_workflows_have_no_io_imports` (AST scan) plus Temporal's sandbox |
| I2 | No activity input or output serializes above 256 KB | `tests/unit/test_invariants.py::test_payload_size_guard` plus a runtime guard in the payload converter |
| I3 | The sandbox environment contains no secrets | `tests/integration/test_sandbox.py::test_no_secrets_in_container_env` |
| I4 | Every GitHub write goes through `github/side_effects.py` | `tests/unit/test_invariants.py::test_github_writes_only_in_side_effects` (AST scan for write methods) |
| I5 | No push to the default branch, ever | `tests/unit/test_branch_guard.py` |
| I6 | Every provider call writes a `cost_ledger` row | `tests/integration/test_cost_ledger.py::test_every_call_ledgered` |
| I7 | Prompts include only `approved` memory items | `tests/unit/test_memory_retrieve.py::test_only_approved_in_prompt` |
| I8 | CI needs no API keys | the CI job runs with `LONGHAUL_PROVIDER=scripted` and no `ANTHROPIC_API_KEY` set; the gate verifies this |
| I9 | Every earlier phase's integration tests still pass | `make gate PHASE=N` runs the cumulative markers |

---

## 5. Delivery operating model

### 5.1 Roles

| Role | Who | Responsibilities |
|---|---|---|
| Product owner | Hooman | Owns scope and priorities. Reviews gate reports, merges `dev` into `main`, tags releases, runs paid evals, keeps `.env` secrets. |
| Developer | Contributor | Implements one story at a time from this plan, tests first. |
| Reviewer | Maintainer | Runs phase gates, reviews the cumulative diff against acceptance criteria and invariants, writes gate reports, files fix stories. Also signs off stories marked "Reviewer sign-off" (shared contracts, workflow determinism). |

### 5.2 Branching and releases

```
main   ●────────────────●──────────────────●────── ...   (tags v0.0.1, v0.1.0, ... v1.0.0)
        \              / \                /
dev      ●──●──●──●──●    ●──●──●──●──●──●      ...     (integration)
            \  /  \  /       \  /
feat/lh-101  ●     ●  feat/lh-102 ...                   (one story each, squash-merged)
```

- `main` is protected. Merges into it come only from phase-gate PRs (`dev` into `main`, merge commit, not squash). Each gate merge is tagged: Phase 0 gets `v0.0.1`, Phases 1 to 8 get `v0.N.0`, and Phase 9 gets `v1.0.0`.
- `dev` is protected and requires CI. Story PRs squash-merge into `dev`.
- Branch names:
  - `feat/lh-<id>-<slug>` for stories
  - `spike/lh-<id>-<slug>` for spikes
  - `fix/lh-<id>-<slug>` for fix stories
  - `chore/<slug>` for upkeep
- Hotfix on `main`: branch `fix/` from `main`, PR to `main`, then merge `main` back into `dev` the same day.
- GitHub settings (Phase 0 story LH-003): require PRs on `main` and `dev`, require the `ci` status check, and disallow force pushes.

### 5.3 Story workflow, exact steps

```bash
# 0. pick the next story whose "Depends on" are all merged (see docs/progress.md)
git switch dev && git pull --ff-only
git switch -c feat/lh-203-task-workflow-skeleton

# 1. read the story block in docs/plan/longhaul-build-plan.md and the contracts it references
# 2. write the tests named in the story first; run them and confirm they fail
uv run pytest tests/unit/test_task_workflow.py -q   # expect failures

# 3. implement until the story's tests pass
make check          # ruff + ruff format --check + mypy + unit tests
make up             # local stack (postgres, temporal, minio, ...) if the story has integration tests
make itest          # integration tests

# 4. update docs: progress.md row, ADR if the story says so, README section if user-facing
# 5. commit in small natural steps (see 5.9), then push
git push -u origin HEAD

# 6. open the PR into dev
gh pr create --base dev --title "LH-203: task workflow skeleton" --body-file .github/pull_request_template.md
#    (edit the body: what, why, how tested, "Closes #<issue>")
gh pr checks --watch

# 7. merge when green
gh pr merge --squash --delete-branch
```

If a story can't be finished:
- Push the branch.
- Open the PR as draft with a short "Blocked:" note.
- Move on to a story that does not depend on it.

### 5.4 Phase gate protocol

```bash
git switch dev && git pull --ff-only
make up
make gate PHASE=3      # lint, types, unit, integration + e2e for markers phase0..phase3, chaos-lite if PHASE>=2
git log --oneline v0.2.0..dev
git diff --stat v0.2.0..dev
```

Reviewer checklist:

1. Every story in the phase is merged and its acceptance criteria hold. Check each criterion against a test or a manual command.
2. `make gate PHASE=N` is green. Paste the summary.
3. Invariants I1 to I9 hold.
4. The manual smoke script in "Integration checkpoint N" runs clean. Paste the output tail.
5. Shared contracts changed only in stories that flagged it.
6. Docs updated: `docs/progress.md`, ADRs listed in the phase, README "Status" line.
7. No TODO without an issue link. No skipped tests without a linked issue.

Write `docs/gates/phase-N.md` with these sections: Result (pass/fail), Test summary, Acceptance criteria table, Invariants, Smoke output, Issues found, Fix stories created.

On fail:
- Create fix stories `LH-N91`, `LH-N92` and so on, in the same story-block format, and append them to the phase in `docs/plan/longhaul-build-plan.md`.
- Work through them like any other story.
- Re-run the gate.

On pass:

```bash
gh pr create --base main --head dev --title "Phase 3 gate: GitHub integration" --body-file docs/gates/phase-3.md
gh pr merge --merge
git switch main && git pull --ff-only
git tag -a v0.3.0 -m "Phase 3: GitHub integration"
git push origin v0.3.0
git switch dev && git merge --ff-only main || true
```

### 5.5 Definition of Ready and Done

**Ready**
- The story block has a user story, Build notes, acceptance criteria, test names and files.
- Every dependency is merged into `dev`.
- The referenced contracts exist in `contracts.py`, or the story creates them.
- The story is 8 points or fewer.

**Done**
- Every acceptance criterion is covered by a passing test or a documented manual check.
- `make check` and `make itest` pass locally, and CI passes on the PR.
- No new mypy errors. ruff is clean.
- The `docs/progress.md` row is updated with the PR link.
- The ADR is written if the story calls for one.
- The PR is squash-merged into `dev` and CI on `dev` is green afterwards.

### 5.6 CI pipeline (`.github/workflows/ci.yml`)

| Job | Trigger | Steps |
|---|---|---|
| `lint` | PR, push | `uv sync`, `ruff check`, `ruff format --check` |
| `types` | PR, push | `mypy src` (strict on `contracts`, `durable`, `context`, `cost`) |
| `unit` | PR, push | `pytest tests/unit -q --cov=longhaul --cov-fail-under=80` |
| `integration` | PR, push | services: `pgvector/pgvector` image, MinIO. Temporal via `temporal server start-dev` (CLI installed in the job) or the SDK's local test server. Docker is available on ubuntu runners. Build the sandbox image (cached), then `pytest -m "integration" -q` |
| `e2e` | PR into `dev`, push | `docker compose up -d`, `LONGHAUL_PROVIDER=scripted pytest tests/e2e -q` |
| `console` | PR, push (Phase 8+) | `npm ci`, `npm run lint`, `npm run test`, `npm run build`. Playwright runs on push to `dev` only. |
| `security` | PR, push | `gitleaks detect`, `pip-audit` |
| `chaos-lite` | nightly, gate | `pytest tests/chaos -m chaos_lite` (replay provider, 5 kills) |

There are no secrets in CI (I8). Paid evals run only on Hooman's machine or a VM.

### 5.7 Testing strategy

| Layer | Scope | Tools | Rules |
|---|---|---|---|
| Unit | Pure logic: contracts, prompt rendering, token math, stuck rules, budget math, command parsing, squash planning | pytest, `FakeSandbox` (temp dir + subprocess), `ScriptedProvider` | Fast (< 30 s total). No Docker, network or DB. |
| Integration | Real Docker sandbox, Postgres, MinIO, Temporal test server, `FakeGitHub` | pytest markers `integration`, `phaseN`, Temporal `WorkflowEnvironment` (time-skipping for timers) | Each test cleans up its containers and buckets. |
| E2E | Full stack via docker compose, CLI to PR on `FakeGitHub`, scripted provider | pytest `e2e` | Seeded toy repos in `tests/fixtures/repos/`. |
| Chaos | Random kills of worker, sandbox and Temporal frontend during replayed runs | `tests/chaos`, `scripts/chaos_kill.sh`, `ReplayProvider` | Deterministic final diff compared with a no-kill reference. |
| Eval | Real models, SWE-bench and custom tasks | `eval/` | Manual, paid, and never in CI. |

Test doubles:

- `ScriptedProvider`: built from a YAML script of responses, keyed by `(role, turn_index)` or by a matcher on the last tool result. It lets e2e tests drive the agent through a known sequence: read, edit, run tests, finish.
- `ReplayProvider`: replays a recorded real run. Requests are matched by turn index and a hash of the request. Used for chaos runs, so they are deterministic and free.
- `FakeGitHub`: an in-memory FastAPI app that implements the handful of REST endpoints we use (issues, comments, labels, pulls, git refs), mounted through `httpx.MockTransport` in unit tests and run as a compose service in e2e.

Pytest markers: `unit`, `integration`, `e2e`, `chaos`, `chaos_lite`, `phase0` ... `phase9`. Each test carries the marker of the phase that introduced it. `make gate PHASE=N` runs `-m "phase0 or ... or phaseN"`.

### 5.8 `CONTRIBUTING.md` (put this in the repo root in Phase 0)

````markdown
# Contributing to Longhaul

## What this is
Background coding agent on Temporal. Plan: docs/plan/longhaul-build-plan.md. Progress: docs/progress.md.

## Commands
- make setup        # uv sync, pre-commit install
- make up / make down   # local stack via docker compose
- make check        # ruff, format check, mypy, unit tests
- make itest        # integration tests (needs make up)
- make e2e          # end-to-end with scripted provider
- make gate PHASE=N # everything for phases 0..N

## Working rules
1. One story per branch: feat/lh-<id>-<slug> from dev. PR into dev. Squash merge.
2. Write the story's listed tests first. Make them fail. Then implement.
3. Do not edit src/longhaul/contracts.py unless the story says so. If you must, explain why in the PR.
4. Workflow code (src/longhaul/durable/workflows/) must stay deterministic: no I/O, no datetime.now(), no random, no imports of httpx/httpx2/httpcore2/docker/sqlalchemy/boto3/anthropic/openai/subprocess. Use workflow.now() and activities.
5. Anything bigger than 16 KB crossing an activity boundary goes to the blob store as a BlobRef.
6. All GitHub writes go through src/longhaul/github/side_effects.py with an idempotency key.
7. Never put secrets into the sandbox environment. Never push to a default branch.
8. Never delete or weaken a test to get green. Never mark a test skip without an issue link.
9. CI has no API keys. Use ScriptedProvider or ReplayProvider in tests.
10. Keep functions small and typed. mypy must stay clean.

## Style
- Plain English in docs, comments, commits and PRs. No hype words. No emoji.
- Commit messages: conventional commits, lowercase, imperative, under 72 chars. Body only when the why is not obvious.
- Comments explain why, not what.

## When unsure
Stop and leave a short note in the PR description under "Questions". Do not guess at shared contracts.
````

### 5.9 Checklists

**Story checklist**

1. Read `CONTRIBUTING.md`, sections 3.6 to 3.8 (contracts, activities, config), the story block in section 7, and any listed files that already exist.
2. Create the story branch from an up-to-date `dev`.
3. Write the tests listed under "Tests" first and confirm they fail.
4. Implement until every listed test passes and every acceptance criterion holds.
5. Run `make check`, and `make itest` when the story has integration tests.
6. Update `docs/progress.md` (status Done, PR link) and any ADR the story asks for.
7. Commit in small steps with conventional-commit messages.
8. Push, open a PR into `dev` with what changed, why, how it was tested and "Closes #<issue>", and squash-merge when CI is green.
9. Stay inside the story's files. If a dependency is missing or a contract is wrong, stop and write it under "Questions" in the PR.

**Gate checklist**

1. Read `CONTRIBUTING.md`, section 4 (failure model, invariants), section 5.4 (gate protocol) and the section of the phase being closed.
2. `git switch dev && git pull`, then `make up` and `make gate PHASE=N`.
3. Check every acceptance criterion of every story in the phase against a test or a command, and record pass or fail with the evidence.
4. Check invariants I1 to I9 and run the phase's integration checkpoint smoke script exactly as written.
5. Review `git diff v<previous tag>..dev` for shared contract changes, determinism problems in workflows, secrets, dead code, missing tests and the style rules in `CONTRIBUTING.md`.
6. Write `docs/gates/phase-N.md` (Result, Test summary, AC table, Invariants, Smoke output, Issues, Fix stories).
7. If anything fails, add fix stories LH-N91, LH-N92 and so on in the standard story format, commit them with the report on a `chore/` branch, open a PR into `dev`, and stop.
8. If everything passes, open the `dev` to `main` PR titled "Phase N gate: <phase name>" with the report as the body, merge it with a merge commit, then tag the release and push the tag.

Be strict. A gate that passes broken work costs more later than a gate that fails today.

### 5.10 Voice and style guide (README, docs, comments, commits, PRs)

The repo should read like one careful engineer wrote it.

- **README:** lead with the problem in one sentence and one real number. Then the architecture diagram, quickstart (three commands), results table, and design decisions with links to ADRs. Keep the top screen short.
- **Docs:** short paragraphs. Active voice. Say what the thing does and why. Prefer tables for facts and code blocks for commands.
- **Comments:** explain why something is done when it isn't obvious ("reset to pre_sha first so a retried command never runs on half-applied edits"). Don't narrate code.
- **Commit messages:** conventional commits with a scope, lowercase, imperative, under 72 characters. Use a body only when the reason isn't obvious.
  - Good:
    - `fix(sandbox): reset to pre_sha before retrying a tool call`
    - `feat(context): elide tool outputs older than four turns`
    - `test(chaos): kill worker during run_tests and check final diff`
  - Avoid:
    - `feat: Implement comprehensive robust checkpoint system` (plus a rocket emoji)
    - `Update files`
    - `WIP`
- **PR descriptions:** three short sections (What, Why, How I tested it), plus `Closes #N`. Three to ten lines is normal.
- **Banned phrases:**
  - robust, seamless, seamlessly, leverage, delve, comprehensive, cutting-edge, state-of-the-art (unless citing a paper)
  - game-changer, unlock, empower, elevate, harness the power, supercharge, in today's fast-paced, it's worth noting
  - furthermore, moreover, plethora, a testament to, navigate the complexities, best-in-class
  - em dashes, emoji
- **Ownership:** Hooman reads every phase gate report and every "Defend it" block. If he can't explain a decision in an interview, it isn't done.

### 5.11 Progress tracking

`docs/progress.md` holds one table per phase:

```markdown
## Phase 3: GitHub integration (target v0.3.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-301 | GitHub client wrapper | 3 | Done | #41 | 2026-11-04 |
| LH-302 | Repo allowlist and issue intake | 3 | In progress | #42 | |
```

Status values are Todo, In progress, Blocked, Done. Story LH-006 imports every story into GitHub Issues with labels and milestones, so the GitHub project board mirrors this file.

---

## 6. Backlog structure

### 6.1 Themes

| ID | Theme | Outcome |
|---|---|---|
| T1 | Agent core | The agent can read, edit, run and finish a task in a sandbox. |
| T2 | Durability | Work survives crashes and resumes from checkpoints, with no duplicate side effects. |
| T3 | GitHub delivery | Issues come in, PRs go out, and humans steer with comments. |
| T4 | Intelligence | Context, planning, verification, stuck handling and memory make long tasks work. |
| T5 | Control and operability | Budgets, telemetry, reports and the console keep humans in charge. |
| T6 | Proof and launch | Evals, chaos results, docs and the release. |

### 6.2 Epics

| Epic | Name | Theme | Phases | Points |
|---|---|---|---|---|
| E0 | Repo foundation | T5 | 0 | 21 |
| E1 | LLM providers | T1 | 1 | 13 |
| E2 | Agent loop and tools | T1 | 1 | 19 |
| E3 | Sandbox | T1 | 1 | 7 |
| E4 | Durable workflow | T2 | 2 | 21 |
| E5 | Workspace checkpoints and recovery | T2 | 2 | 15 |
| E6 | GitHub intake and delivery | T3 | 3 | 34 |
| E7 | Context engineering | T4 | 4 | 22 |
| E8 | Planning and verification | T4 | 5 | 18 |
| E9 | Human gates and stuck handling | T5 | 5 | 11 |
| E10 | Memory | T4 | 6 | 22 |
| E11 | Budgets and cost | T5 | 7 | 8 |
| E12 | Observability and run reports | T5 | 7 | 13 |
| E13 | Console | T5 | 8 | 27 |
| E14 | Evaluation and chaos | T6 | 9 | 26 |
| E15 | Release, hardening and integrations | T6 | 9 | 14 |

### 6.3 Phase summary

| Phase | Name | Stories | Points | Tag |
|---|---|---|---|---|
| 0 | Foundation and backlog import | 8 | 21 | v0.0.1 |
| 1 | Agent core without durability | 11 | 39 | v0.1.0 |
| 2 | Temporal durability and checkpoints | 9 | 36 | v0.2.0 |
| 3 | GitHub integration | 8 | 34 | v0.3.0 |
| 4 | Context engineering | 7 | 22 | v0.4.0 |
| 5 | Planner, verifier, stuck handling | 7 | 29 | v0.5.0 |
| 6 | Memory | 7 | 22 | v0.6.0 |
| 7 | Budgets, cost and observability | 6 | 21 | v0.7.0 |
| 8 | Console | 8 | 27 | v0.8.0 |
| 9 | Evaluation, chaos, hardening, release | 9 | 40 | v1.0.0 |
| | **Total** | **80** | **291** | |

The Could stories (LH-908 Terraform, LH-909 Keystone link) are worth 8 points together. Cutting them gives a 283-point v1.

---

## 7. Phases and stories

Story block format (parsed by `scripts/import_backlog.py`): a heading `#### LH-<phase><nn> · <Title>`, then bullet fields in this order: Epic / Points / Branch / Depends on, Story, Build, Acceptance criteria, Tests, Files.

---

## Phase 0: Foundation and backlog import (tag v0.0.1)

**Goal:** an empty but real project: commands work, the local stack starts, CI is green, the rules are written down, and the backlog lives in GitHub.
**Demo at the end:** `make setup && make up && make check` passes on a fresh clone. The GitHub board shows 80 issues across 10 milestones.
**Entry criteria:** GitHub repo `longhaul-background-agent` created (private until v1.0.0), Docker installed, `uv` installed, `gh` authenticated.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-001 | Repo scaffold and tooling | 3 | E0 |
| LH-002 | Local stack with docker compose | 3 | E0 |
| LH-003 | CI pipeline and branch protection | 3 | E0 |
| LH-004 | CONTRIBUTING.md, ADRs and contribution docs | 3 | E0 |
| LH-005 | Settings and config loading | 2 | E0 |
| LH-006 | Backlog import into GitHub issues | 3 | E0 |
| LH-007 | Toy repo fixtures | 2 | E0 |
| LH-008 | Database baseline and migrations | 2 | E0 |

#### LH-001 · Repo scaffold and tooling
- **Epic:** E0 · **Points:** 3 · **Branch:** bootstrap commit on `main`, then create `dev` from `main` · **Depends on:** none
- **Story:** As a developer, I want a working Python project skeleton, so that every later story uses the same commands and layout.
- **Build:**
  - `uv init --package longhaul`. Python 3.12. Use the `src/longhaul/` layout from section 3.9 with empty `__init__.py` files for each package.
  - Dev dependencies: pytest, pytest-asyncio, pytest-cov, ruff, mypy, pre-commit.
  - `pyproject.toml`:
    - ruff (line length 100, rules E, F, I, B, UP, SIM)
    - mypy (strict for `longhaul.contracts`, `longhaul.durable`, `longhaul.context`, `longhaul.cost`)
    - pytest markers `unit, integration, e2e, chaos, chaos_lite, phase0..phase9`, with `asyncio_mode = auto`
  - `Makefile` targets: `setup`, `check`, `itest`, `e2e`, `up`, `down`, `gate`. For now `gate` runs `check` plus `pytest -m "<phase markers up to $(PHASE)>"`. Build the marker expression in a tiny script, `scripts/markers.py N`.
  - `.pre-commit-config.yaml` with ruff and ruff-format.
  - `.gitignore`, `LICENSE` (MIT) and `README.md` with a one-line pitch and "Status: under construction".
  - Bootstrap exception: this one story is committed directly to `main`. Then run `git switch -c dev && git push -u origin dev`.
- **Acceptance criteria:**
  - [ ] Given a fresh clone, when `make setup && make check` runs, then it exits 0.
  - [ ] `python scripts/markers.py 2` prints `phase0 or phase1 or phase2`.
  - [ ] `dev` and `main` both exist on the remote.
- **Tests:** `tests/unit/test_smoke.py::test_package_imports`, `tests/unit/test_markers.py::test_marker_expression`
- **Files:** `pyproject.toml`, `uv.lock`, `Makefile`, `scripts/markers.py`, `.pre-commit-config.yaml`, `src/longhaul/**/__init__.py`, `tests/unit/test_smoke.py`, `tests/unit/test_markers.py`

#### LH-002 · Local stack with docker compose
- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-002-local-stack` · **Depends on:** LH-001
- **Story:** As a developer, I want one command to start every backing service, so that integration tests and demos run the same way everywhere.
- **Build:**
  - `docker-compose.yml` services:
    - `postgres`: `pgvector/pgvector` image, db `longhaul`, port 5432, healthcheck
    - `temporal`: Temporal dev server via the official `temporalio/temporal` CLI image running `server start-dev --ip 0.0.0.0 --db-filename /data/temporal.db`, with `/data` on a named volume so a restart keeps workflow state. Ports 7233 and 8233 (UI). If the image name differs at project start, use the documented dev-server option and note it in ADR-0002.
    - `minio`: ports 9000 and 9001, plus a `minio-init` one-shot (mc) that creates bucket `longhaul`
    - `otel-collector`: config in `docker/otel/config.yaml`, exporting to `jaeger` (16686)
    - `prometheus`: scrapes `host.docker.internal:9464` (worker) and `api:8000/metrics`
    - `grafana`: port 3000, anonymous viewer, provisioning folder
  - `make up` runs `docker compose up -d --wait`. `make down` runs `docker compose down -v`.
  - `.env.example` lists every variable from section 3.8 with safe local defaults.
- **Acceptance criteria:**
  - [ ] `make up` returns only after every healthcheck passes.
  - [ ] The Temporal UI answers on `http://localhost:8233`, MinIO bucket `longhaul` exists, and Postgres has the `vector` extension available.
  - [ ] `make down` removes the volumes.
- **Tests:** `tests/integration/test_stack.py::test_services_reachable` (marker `integration, phase0`). It connects to Postgres, runs `CREATE EXTENSION IF NOT EXISTS vector`, connects to Temporal with `Client.connect`, and lists MinIO buckets.
- **Files:** `docker-compose.yml`, `docker/otel/config.yaml`, `docker/prometheus/prometheus.yml`, `docker/grafana/provisioning/**`, `.env.example`, `tests/integration/test_stack.py`

#### LH-003 · CI pipeline and branch protection
- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-003-ci` · **Depends on:** LH-001, LH-002
- **Story:** As the product owner, I want CI on every PR and protected branches, so that `dev` and `main` are always green.
- **Build:**
  - `.github/workflows/ci.yml` with jobs `lint`, `types`, `unit`, `integration` and `security`, as in section 5.6. `e2e`, `console` and `chaos-lite` are added later by their stories.
  - Integration job: Postgres (pgvector image) and MinIO as service containers. Install the Temporal CLI and start the dev server in the background, then wait on port 7233.
  - Cache uv and Docker layers.
  - `scripts/protect_branches.sh` uses `gh api` to require PRs and the `ci` check on `main` and `dev`, and to block force pushes. It is run once by Hooman (it needs admin rights).
  - Add a `ci` aggregate job that needs all other jobs, so branch protection points at one name.
- **Acceptance criteria:**
  - [ ] A PR into `dev` runs all jobs, and `ci` is the required check.
  - [ ] A direct push to `dev` is rejected after the script runs.
  - [ ] No secrets are referenced in the workflow file (checked by grep in the test).
- **Tests:** `tests/unit/test_ci_config.py::test_no_secrets_in_ci`, `::test_ci_has_required_jobs` (parse YAML)
- **Files:** `.github/workflows/ci.yml`, `scripts/protect_branches.sh`, `tests/unit/test_ci_config.py`

#### LH-004 · CONTRIBUTING.md, ADRs and contribution docs
- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-004-contribution-docs` · **Depends on:** LH-001
- **Story:** As a contributor or reviewer, I want the working rules in the repo, so that every change follows the same process.
- **Build:**
  - Copy this plan to `docs/plan/longhaul-build-plan.md`.
  - `CONTRIBUTING.md` exactly as in section 5.8.
  - `docs/adr/0000-template.md`, `0001-record-decisions.md` and `0002-pin-dependencies.md`. ADR-0002 lists the pinned versions from `uv.lock` for temporalio, anthropic, docker, fastapi, sqlalchemy, alembic, pgvector, opentelemetry-sdk and boto3.
  - `.github/pull_request_template.md` with What / Why / How I tested it / Closes #.
  - `docs/style.md` with the voice guide from section 5.10.
  - `docs/progress.md` with every phase table pre-filled as Todo.
- **Acceptance criteria:**
  - [ ] Every file above exists. `docs/progress.md` lists 80 stories.
  - [ ] A scan of `docs/` and `README.md` finds no banned phrases and no em or en dashes. `docs/style.md` and `docs/plan/` are excluded from the banned-phrase check only, because they list the phrases.
- **Tests:** `tests/unit/test_docs_style.py::test_no_banned_phrases`, `::test_no_long_dashes`, `tests/unit/test_progress.py::test_progress_lists_all_stories` (parses the plan headings and compares them with the progress rows)
- **Files:** `CONTRIBUTING.md`, `docs/plan/longhaul-build-plan.md`, `docs/adr/*`, `docs/style.md`, `docs/progress.md`, `docs/runbook.md`, `.github/pull_request_template.md`, tests

#### LH-005 · Settings and config loading
- **Epic:** E0 · **Points:** 2 · **Branch:** `feat/lh-005-settings` · **Depends on:** LH-001
- **Story:** As a developer, I want typed settings from env and YAML, so that every module reads config the same way.
- **Build:**
  - `src/longhaul/settings.py`: `EnvSettings(BaseSettings)` for the env vars in section 3.8, plus `AppConfig` (pydantic) mirroring `longhaul.yaml`. Also `load_config(path) -> AppConfig` and `get_settings()`, cached with `functools.lru_cache`.
  - Write `config/longhaul.example.yaml` and `config/pricing.yaml` with placeholder model ids and a `# fill from provider pricing page` comment. Prices are `null` until filled; the cost module refuses to run paid providers with null prices.
  - `RepoSettings` comes from `contracts.py`. If LH-101 is not merged yet, define it in `settings.py` with a `TODO(LH-101): move to contracts` and an issue link. LH-101 moves it.
- **Acceptance criteria:**
  - [ ] Loading the example YAML gives a valid `AppConfig`.
  - [ ] An unknown key in YAML raises a clear validation error that names the key.
  - [ ] Env vars override YAML for secrets only. YAML never contains secrets, and the loader rejects keys named `*token*`, `*key*` or `*secret*` in YAML.
- **Tests:** `tests/unit/test_settings.py::test_example_config_loads`, `::test_unknown_key_rejected`, `::test_secret_keys_rejected_in_yaml`
- **Files:** `src/longhaul/settings.py`, `config/*.yaml`, `tests/unit/test_settings.py`

#### LH-006 · Backlog import into GitHub issues
- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-006-backlog-import` · **Depends on:** LH-004
- **Story:** As the product owner, I want every story as a GitHub issue with labels and milestones, so that the board mirrors the plan.
- **Build:**
  - `scripts/import_backlog.py PLAN_PATH [--dry-run] [--repo owner/name]`:
    - Parse phase headings with `^## Phase (\d): (.+) \(tag (v[\d.]+)\)$` and story headings with `^#### (LH-\d{3}) · (.+)$`. Skip everything inside fenced code blocks, because the plan contains example markdown with headings.
    - Read Epic, Points and Depends on from the next line.
    - The issue body is the full story block.
  - Create (idempotently, via `gh api`):
    - milestones "Phase N: Name"
    - labels `phase:N`, `epic:EN`, `pts:N`, `theme:TN`, `type:story|spike|fix`, `could` (for Could stories)
    - issues titled `LH-xxx: Title`
  - Skip an issue if one with the same `LH-xxx:` title prefix exists. Update its body if the plan changed (`--update`).
  - Write the issue numbers back into `docs/progress.md` (column PR stays empty).
- **Acceptance criteria:**
  - [ ] `--dry-run` prints 80 stories and 10 milestones and makes no API calls.
  - [ ] Running the import twice creates nothing new on the second run.
  - [ ] The parser fails loudly on a malformed story heading.
- **Tests:** `tests/unit/test_import_backlog.py::test_parses_all_stories` (runs against `docs/plan/longhaul-build-plan.md`), `::test_idempotent_plan` (a fake gh runner records calls), `::test_malformed_heading_raises`
- **Files:** `scripts/import_backlog.py`, `tests/unit/test_import_backlog.py`

#### LH-007 · Toy repo fixtures
- **Epic:** E0 · **Points:** 2 · **Branch:** `feat/lh-007-fixture-repos` · **Depends on:** LH-001
- **Story:** As a test author, I want small known repos with planted issues, so that e2e tests have stable targets.
- **Build:** create three tiny Python packages under `tests/fixtures/repos/`, each with `pyproject.toml`, source, tests and an `ISSUE.md` (the issue text):
  - `calc`: `add()` subtracts. One failing test. Single-file fix.
  - `textkit`: `slugify()` mishandles unicode. Fixing it needs two files and a new test.
  - `inventory`: `Inventory.remove()` allows negative stock. Fixing it needs a new exception class and a test, with ~6 files in the repo.
  - `scripts/make_fixture_git.py` turns each folder into a git repo on the fly (init, commit "initial", tag `base`), so fixtures stay plain folders in this repo.
- **Acceptance criteria:**
  - [ ] Each fixture has exactly the failing tests described in `ISSUE.md` on `base`.
  - [ ] A reference fix patch in `tests/fixtures/repos/<name>/REFERENCE.patch` makes all of its tests pass.
- **Tests:** `tests/unit/test_fixtures.py::test_fixture_fails_at_base[calc|textkit|inventory]`, `::test_reference_patch_passes[...]` (run pytest in a temp copy via subprocess)
- **Files:** `tests/fixtures/repos/**`, `scripts/make_fixture_git.py`, `tests/unit/test_fixtures.py`

#### LH-008 · Database baseline and migrations
- **Epic:** E0 · **Points:** 2 · **Branch:** `feat/lh-008-db-baseline` · **Depends on:** LH-002, LH-005
- **Story:** As a developer, I want SQLAlchemy models and Alembic wired up, so that later stories add tables with one migration each.
- **Build:**
  - `src/longhaul/db/session.py`: async engine (`asyncpg`), `get_session()` context manager.
  - `src/longhaul/db/models.py`: a declarative base with naming conventions. First migration: the `vector` extension, plus the `repo_settings` and `tasks` tables (section 3.5).
  - `alembic.ini` and `src/longhaul/db/migrations/`. `make migrate` runs `alembic upgrade head`.
- **Acceptance criteria:**
  - [ ] `make up && make migrate` creates both tables, and running it again is a no-op.
  - [ ] Downgrade to base and upgrade again both succeed.
- **Tests:** `tests/integration/test_migrations.py::test_upgrade_downgrade_roundtrip` (marker `integration, phase0`)
- **Files:** `src/longhaul/db/*`, `alembic.ini`, `tests/integration/test_migrations.py`

### Integration checkpoint 0

- **Cumulative tests:** `make gate PHASE=0`, which covers unit plus integration tests with marker `phase0`.
- **Manual smoke:**
  ```bash
  git clone git@github.com:hoomanesteki/longhaul-background-agent.git && cd longhaul-background-agent
  make setup && make up && make migrate && make check
  python scripts/import_backlog.py docs/plan/longhaul-build-plan.md --dry-run | tail -3
  open http://localhost:8233   # Temporal UI
  ```

### Phase gate 0

- [ ] All 8 stories Done, CI green on `dev`.
- [ ] Branch protection active on `main` and `dev`.
- [ ] 80 issues and 10 milestones exist on GitHub.
- [ ] ADR-0001 and ADR-0002 merged.
- [ ] Tag `v0.0.1`.

### Defend it

1. **Why a written plan with story IDs instead of just prompting an agent?** Small stories with tests keep each change reviewable and let a cheaper model do most of the work. The gate catches integration problems every phase, not at the end.
2. **Why `dev` and `main`?** `dev` is where stories integrate. `main` only moves after a gate passes, so every tag is a known-good, demoable state.
3. **Why reject secrets in YAML?** Config files get committed. Secrets come only from the environment, which keeps them out of git and out of the sandbox.
4. **Why fixture repos instead of real repos for tests?** Determinism. Real repos change, and network calls make CI flaky. Real repos are used in evals, not tests.

---

## Phase 1: Agent core without durability (tag v0.1.0)

**Goal:** an in-process agent that fixes the `calc` fixture end to end inside a Docker sandbox, using the real Anthropic provider locally and the scripted provider in CI.
**Demo at the end:** `longhaul run-local --path tests/fixtures/repos/calc --issue-file ISSUE.md` prints each turn and tool call, then shows a diff that makes the tests pass.
**Entry criteria:** Phase 0 gate passed.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-101 | Shared contracts module | 3 | E2 |
| LH-102 | Spike: Messages API tool use, caching and token counting | 2 | E1 |
| LH-103 | Provider protocol and ScriptedProvider | 3 | E1 |
| LH-104 | AnthropicProvider | 5 | E1 |
| LH-105 | OpenAI-compatible provider (Should) | 3 | E1 |
| LH-106 | Sandbox image | 2 | E3 |
| LH-107 | Docker sandbox manager | 5 | E3 |
| LH-108 | Tool registry and file tools | 5 | E2 |
| LH-109 | Exec and git tools | 3 | E2 |
| LH-110 | Turn functions and in-process loop | 5 | E2 |
| LH-111 | run-local CLI and first e2e | 3 | E2 |

#### LH-101 · Shared contracts module
- **Epic:** E2 · **Points:** 3 · **Branch:** `feat/lh-101-contracts` · **Depends on:** LH-005 · **Reviewer sign-off**
- **Story:** As every later story, I want one module of shared types, so that pieces built by different sessions fit together.
- **Build:**
  - Create `src/longhaul/contracts.py` exactly as in section 3.6. Move `RepoSettings` and `Budgets` out of `settings.py` if LH-005 put them there.
  - Add `to_anthropic()` / `from_anthropic()` helpers for `Message` in `src/longhaul/agent/render.py`, not in contracts, so contracts stay vendor-free.
  - Add `contracts_version = "1"` and a `CHANGELOG` section in `docs/architecture.md` for contract changes.
- **Acceptance criteria:**
  - [ ] Every model round-trips through `model_dump_json()` and `model_validate_json()`.
  - [ ] `contracts.py` imports only stdlib, pydantic and typing (AST test).
  - [ ] mypy strict passes on the module.
- **Tests:** `tests/unit/test_contracts.py::test_roundtrip_all_models`, `::test_contracts_imports_are_pure`
- **Files:** `src/longhaul/contracts.py`, `src/longhaul/agent/render.py`, `docs/architecture.md`, tests

#### LH-102 · Spike: Messages API tool use, caching and token counting
- **Epic:** E1 · **Points:** 2 · **Branch:** `spike/lh-102-messages-api` · **Depends on:** LH-101
- **Story:** As the developer of LH-104, I want verified facts about the current Anthropic SDK, so that the provider is built on what the API does today.
- **Build:** this is a timeboxed throwaway script (2 hours max), `spikes/messages_api.py`, run by Hooman with a real key. Verify and record in `docs/adr/0003-own-agent-loop.md` (appendix "API facts"):
  1. Tool definitions and `tool_use` / `tool_result` block shapes.
  2. Which error classes and status codes map to rate limit (429), overloaded (529) and context too long. Write down the exact exception class names.
  3. Prompt caching: `cache_control` placement on system and tools, and the usage fields for cache read and write tokens.
  4. Whether `client.messages.count_tokens` exists and what it returns.
  5. Whether the response `usage.input_tokens` excludes cached tokens.
- **Fallback:** if `count_tokens` is unavailable, LH-401 estimates tokens as the last response's `input_tokens` plus `len(new_text)/3.5` and recalibrates after each real response.
- **Acceptance criteria:**
  - [ ] The ADR-0003 appendix lists all five facts with the SDK version.
  - [ ] The script is under `spikes/` and is excluded from mypy and the package.
- **Tests:** none (spike). A reviewer reads the ADR.
- **Files:** `spikes/messages_api.py`, `docs/adr/0003-own-agent-loop.md`

#### LH-103 · Provider protocol and ScriptedProvider
- **Epic:** E1 · **Points:** 3 · **Branch:** `feat/lh-103-scripted-provider` · **Depends on:** LH-101
- **Story:** As a test author, I want a deterministic provider driven by a script, so that the agent can be tested without API keys.
- **Build:**
  - `src/longhaul/providers/base.py`: re-export `LLMProvider` and `ProviderError`. Add `get_provider(settings) -> LLMProvider` (factory by `LONGHAUL_PROVIDER`).
  - `src/longhaul/providers/scripted.py`:
    - `ScriptedProvider(script_path)`. The script is YAML: a list of steps, each with optional `match` (`role`, `turn_index`, `last_tool_result_contains`) and a `respond` that holds text and/or tool calls `{name, input}`.
    - Usage numbers are synthetic and deterministic: input = sum of chars / 4, output = 50 per tool call.
    - Raises `ProviderError(INVALID_REQUEST)` when no step matches, with a message that shows the last tool result. That makes broken scripts easy to debug.
  - `count_tokens` uses the same chars / 4 rule.
  - Example script: `tests/fixtures/scripts/calc_fix.yaml` (list_dir, read_file, edit_file, run_tests, finish).
- **Acceptance criteria:**
  - [ ] Given `calc_fix.yaml`, when `complete()` is called five times with growing transcripts, then the five expected tool calls come back in order.
  - [ ] A non-matching request raises with a helpful message.
- **Tests:** `tests/unit/test_scripted_provider.py::test_sequence`, `::test_match_on_tool_result`, `::test_no_match_raises`
- **Files:** `src/longhaul/providers/{base,scripted}.py`, `tests/fixtures/scripts/calc_fix.yaml`, tests

#### LH-104 · AnthropicProvider
- **Epic:** E1 · **Points:** 5 · **Branch:** `feat/lh-104-anthropic-provider` · **Depends on:** LH-102, LH-103
- **Story:** As the agent, I want a provider for the Anthropic Messages API, with errors mapped to retry semantics, so that the durable layer knows what to retry.
- **Build:**
  - `src/longhaul/providers/anthropic.py`:
    - Use `anthropic.AsyncAnthropic(max_retries=0)`. The SDK's internal retries are off; Temporal owns retries.
    - Convert contracts to SDK params via `render.py`. When `cache_system=True`, put `cache_control: {"type": "ephemeral"}` on the system prompt's last block and on the last tool definition.
    - Parse the response into `LLMResponse`. Map `stop_reason`.
    - Map the error classes from the spike to `ProviderErrorKind`, and parse `retry-after` when present.
    - Measure `latency_ms`.
  - `count_tokens`: use the SDK endpoint if the spike confirmed it, else the fallback estimator.
  - The model id comes from config, never a constant.
- **Acceptance criteria:**
  - [ ] Unit tests with `httpx.MockTransport` (or the SDK's `http_client` injection) cover success, tool use, 429, 529, 500, timeout, context-too-long and 401.
  - [ ] Each error maps to the right `ProviderErrorKind`, and `retry_after_s` is parsed.
  - [ ] An opt-in live test (`-m live`, skipped without a key) does one round trip with a tool call.
- **Tests:** `tests/unit/test_anthropic_provider.py::test_tool_use_parsed`, `::test_error_mapping[429|529|500|timeout|context|401]`, `::test_cache_control_placed`, `tests/live/test_anthropic_live.py::test_round_trip`
- **Files:** `src/longhaul/providers/anthropic.py`, `src/longhaul/agent/render.py`, tests

#### LH-105 · OpenAI-compatible provider (Should)
- **Epic:** E1 · **Points:** 3 · **Branch:** `feat/lh-105-openai-provider` · **Depends on:** LH-103
- **Story:** As an operator, I want to point Longhaul at any OpenAI-compatible endpoint, so that the loop isn't tied to one vendor.
- **Build:**
  - `src/longhaul/providers/openai_compat.py` using the `openai` SDK with `base_url` from env.
  - Convert our `Message` blocks to chat-completions `tool_calls` / `tool` messages and back.
  - Map errors the same way as LH-104.
  - `count_tokens` uses the estimator.
- **Acceptance criteria:**
  - [ ] Mocked tests cover a tool round trip and error mapping.
  - [ ] A transcript built with this provider renders back into our contracts without loss of tool ids.
- **Tests:** `tests/unit/test_openai_provider.py::test_tool_roundtrip`, `::test_error_mapping`
- **Files:** `src/longhaul/providers/openai_compat.py`, tests

#### LH-106 · Sandbox image
- **Epic:** E3 · **Points:** 2 · **Branch:** `feat/lh-106-sandbox-image` · **Depends on:** LH-001
- **Story:** As the sandbox manager, I want a minimal image with the tools the agent needs, so that every task starts from the same base.
- **Build:**
  - `docker/sandbox/Dockerfile`:
    - FROM `python:3.12-slim`
    - install `git`, `ripgrep`, `patch`, `procps`
    - create user `agent` uid 10001, `WORKDIR /workspace`
    - `git config --system user.name "longhaul"` and an email such as `longhaul@localhost`
    - `ENTRYPOINT ["sleep", "infinity"]`
  - `make sandbox-image` builds `longhaul-sandbox:py312`.
- **Acceptance criteria:**
  - [ ] The image builds in CI. `docker run --rm longhaul-sandbox:py312 id -u` prints 10001.
  - [ ] `rg --version` and `git --version` work as user `agent`.
- **Tests:** `tests/integration/test_sandbox_image.py::test_image_tools_present` (marker `integration, phase1`)
- **Files:** `docker/sandbox/Dockerfile`, `Makefile`, tests

#### LH-107 · Docker sandbox manager
- **Epic:** E3 · **Points:** 5 · **Branch:** `feat/lh-107-sandbox-manager` · **Depends on:** LH-101, LH-106
- **Story:** As the agent runtime, I want to create, exec in, and destroy isolated containers, so that repo code never runs on the worker.
- **Build:**
  - `src/longhaul/sandbox/docker_manager.py`, implementing `SandboxManager` and `Sandbox` with the `docker` SDK. Blocking SDK calls run through `asyncio.to_thread`.
  - `create()`:
    - container name `lh-sbx-<task_id>-<short uuid>`
    - labels `longhaul.task_id`
    - `user="agent"`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`
    - `pids_limit`, `mem_limit`, `nano_cpus`
    - `read_only=True` with `tmpfs={"/tmp": "size=512m"}`
    - named volume `lh-ws-<task_id>` mounted at `/workspace`
    - network `bridge` if `network=True` else `none`
    - `environment` built only from the allowlist in section 4.2
  - `exec(cmd, timeout_s)`:
    - run `bash -lc` with `timeout --signal=KILL {timeout_s}` wrapped around it, so the process group dies
    - capture stdout and stderr separately (exec_run with `demux=True`)
    - cap each at 1 MB, and set `timed_out` when the exit code is 137 and the elapsed time is at least the timeout
  - `read_file` / `write_file`: use `get_archive` / `put_archive` (tar), so binary files work and nothing goes through a shell.
  - `set_network(enabled)`: connect or disconnect `bridge`.
  - `get()`: find the container by id and return None if it's gone.
  - `cleanup_task(task_id)`: remove the container and the volume.
  - `src/longhaul/sandbox/local_manager.py`: `FakeSandbox` (temp dir + subprocess) for unit tests only.
- **Acceptance criteria:**
  - [ ] Given a new sandbox, when `exec("id -u")` runs, then stdout is `10001`.
  - [ ] Given `network=False`, when `exec("python -c 'import urllib.request as u; u.urlopen(\"https://pypi.org\")'")` runs, then it fails.
  - [ ] When `exec("sleep 30", timeout_s=2)` runs, then `timed_out` is true and the call returns in under 5 s.
  - [ ] The container env has no variable outside the allowlist (I3).
  - [ ] `cleanup_task` removes the container and the volume.
- **Tests:** `tests/integration/test_sandbox.py::test_runs_as_non_root`, `::test_no_network_when_disabled`, `::test_timeout_kills`, `::test_no_secrets_in_container_env`, `::test_file_roundtrip_binary`, `::test_cleanup`
- **Files:** `src/longhaul/sandbox/{docker_manager,local_manager}.py`, tests

#### LH-108 · Tool registry and file tools
- **Epic:** E2 · **Points:** 5 · **Branch:** `feat/lh-108-file-tools` · **Depends on:** LH-107
- **Story:** As the agent, I want precise file tools, so that I can read and change code without rewriting whole files.
- **Build:**
  - `src/longhaul/tools/registry.py`:
    - `ToolRegistry` with `register(tool)`, `specs(names: list[str] | None)` and `get(name)`
    - `async run(call, sandbox, ctx) -> ToolResult`, which validates input against `input_schema` with `jsonschema` and turns validation errors into `ok=False` results instead of raising
  - `src/longhaul/tools/files.py`:
    - `list_dir(path=".", depth=2)`: tree output, ignores `.git`, `__pycache__`, `.venv`, `node_modules`, max 400 entries.
    - `read_file(path, start_line=1, end_line=None)`: returns numbered lines and refuses paths outside `/workspace`. Max 800 lines per call, with a note to page further.
    - `edit_file(path, old_str, new_str)`: `old_str` must occur exactly once (else error "found N matches"). The tool writes the file back and returns a short unified diff of the change. `mutates=True`.
    - `write_file(path, content)`: create or overwrite. Refuses paths outside `/workspace`. `mutates=True`.
  - `src/longhaul/tools/search.py`: `search(pattern, path=".", glob=None, max_results=200)` via `rg --json`, returning `file:line: text` lines.
  - Path safety: normalize and reject `..` escapes and symlinks that point outside `/workspace` (`realpath` inside the container).
  - Every tool description says plainly when to use it. Keep them short; they cost tokens on every turn.
- **Acceptance criteria:**
  - [ ] `edit_file` with 0 or more than 1 match returns `ok=False` with the match count, and the file is unchanged.
  - [ ] `read_file("../../etc/passwd")` returns `ok=False`.
  - [ ] Invalid input (missing `path`) returns `ok=False` with the schema error, not an exception.
  - [ ] `search` finds a known symbol in the `calc` fixture.
- **Tests:** `tests/integration/test_file_tools.py::test_edit_exactly_once`, `::test_edit_multiple_matches_rejected`, `::test_path_escape_rejected`, `::test_read_line_range`, `::test_search_finds_symbol`, `tests/unit/test_registry.py::test_schema_errors_are_results`
- **Files:** `src/longhaul/tools/{registry,files,search}.py`, tests

#### LH-109 · Exec and git tools
- **Epic:** E2 · **Points:** 3 · **Branch:** `feat/lh-109-exec-tools` · **Depends on:** LH-108
- **Story:** As the agent, I want to run commands and tests and see my diff, so that I can check my own work.
- **Build:**
  - `src/longhaul/tools/exec.py`:
    - `run_command(command, timeout_s=300)` (max from config) returns the exit code, plus stdout and stderr each capped at 20,000 chars (head 8,000 + tail 12,000 with a marker). Phase 4 replaces the cap with handles.
    - `run_tests(target=None)` runs `settings.test_command` (plus target if given) and returns the same shape with a parsed summary line when pytest output is detected (`N passed, M failed`).
  - `src/longhaul/tools/git.py`: `git_diff(staged=False)` returns `git diff` against `base` (the base ref is stored when the workspace is prepared).
  - `src/longhaul/tools/control.py`: `finish(summary)` (a control tool, handled by the loop and never executed in the sandbox).
- **Acceptance criteria:**
  - [ ] `run_tests` on `calc` at base reports `1 failed`.
  - [ ] Output over the cap is trimmed with a visible marker that says how many chars were cut.
  - [ ] `git_diff` is empty at base and shows the change after an `edit_file`.
- **Tests:** `tests/integration/test_exec_tools.py::test_run_tests_summary`, `::test_output_cap_marker`, `::test_git_diff_after_edit`, `tests/unit/test_pytest_summary.py::test_parse_summary`
- **Files:** `src/longhaul/tools/{exec,git,control}.py`, tests

#### LH-110 · Turn functions and in-process loop
- **Epic:** E2 · **Points:** 5 · **Branch:** `feat/lh-110-agent-loop` · **Depends on:** LH-103, LH-109
- **Story:** As the durable layer in Phase 2, I want the agent's turn logic as plain async functions, so that the same code runs in-process now and inside activities later.
- **Build:**
  - `src/longhaul/agent/prompts.py`: `build_system_prompt(role, spec, memory_block=None, notes=None, plan=None) -> str`. Sections in a fixed order (role, rules, task, repo facts, notes, plan), so the cache prefix stays stable. The coder rules ask for small edits, running tests after changes, and calling `finish` only when tests pass.
  - `src/longhaul/agent/turns.py`:
    - `async def llm_step(provider, model, system, transcript: list[Message], tools: list[ToolSpec], max_tokens) -> LLMResponse`
    - `async def tool_step(registry, sandbox, call: ToolCall, ctx) -> ToolResult`
    - `def append_assistant(transcript, resp) -> list[Message]`
    - `def append_tool_results(transcript, results: list[ToolResult]) -> list[Message]`, which keeps every `tool_result` paired with its `tool_use` in the next user message
  - `src/longhaul/agent/loop.py`:
    - `async def run_in_process(spec, provider, registry, sandbox, cfg) -> Outcome`
    - The loop runs until `finish` is called, `max_turns` is reached, or the model ends a turn without tool calls twice in a row (it nudges once with "Use tools or call finish").
    - It emits a `TurnEvent` callback for printing.
- **Acceptance criteria:**
  - [ ] Given `FakeSandbox` + `ScriptedProvider(calc_fix.yaml)`, when the loop runs, then the outcome is `pr_opened`-equivalent (`finish` called) and the tests pass in the workspace.
  - [ ] `tool_use` and `tool_result` stay paired in the transcript (validated by a helper used in tests).
  - [ ] When `max_turns` is hit, the outcome is `ERROR` with summary "turn limit".
- **Tests:** `tests/unit/test_loop.py::test_scripted_calc_fix`, `::test_pairs_valid`, `::test_turn_limit`, `tests/unit/test_prompts.py::test_section_order_stable`
- **Files:** `src/longhaul/agent/{prompts,turns,loop}.py`, tests

#### LH-111 · run-local CLI and first e2e
- **Epic:** E2 · **Points:** 3 · **Branch:** `feat/lh-111-run-local` · **Depends on:** LH-107, LH-110
- **Story:** As a developer, I want one command to run the agent on a local repo, so that I can see it work before any infrastructure exists.
- **Build:**
  - `src/longhaul/cli.py` (Typer): `longhaul run-local --path DIR --issue-file FILE [--provider scripted|anthropic|openai] [--script YAML] [--model ID]`.
  - The command:
    1. Copies the folder into a fresh Docker sandbox volume.
    2. Runs `setup_command` with the network on, then cuts the network.
    3. Runs the loop and prints each turn compactly (`turn 3 coder: edit_file calc/ops.py (+1 -1)`).
    4. Prints the final `git_diff`.
  - Exit code 0 on finish, 2 on error.
  - Add an `e2e` job to CI: `make sandbox-image` then `pytest -m e2e`.
- **Acceptance criteria:**
  - [ ] The e2e test runs `run-local` on `calc` with the scripted provider. Exit code is 0, and the printed diff matches `REFERENCE.patch` semantically (tests pass).
  - [ ] Manually, with a real key, `run-local` fixes `calc` and `textkit` (Hooman runs this once and pastes the output into the PR).
- **Tests:** `tests/e2e/test_run_local.py::test_calc_scripted` (marker `e2e, phase1`)
- **Files:** `src/longhaul/cli.py`, `.github/workflows/ci.yml`, tests

### Phase 1 fix stories (from the pre-gate review)

The reviewer ran an adversarial review of everything merged during Phase 0 and Phase 1 before the Phase 1 gate. Each finding below was reproduced with a failing test before it was fixed. Findings that did not reproduce are recorded in the gate report.

#### LH-191 · Sandbox write and exec safety
- **Epic:** E3 · **Points:** 5 · **Branch:** `fix/lh-191-sandbox-safety` · **Depends on:** LH-107, LH-108
- **Story:** As the agent runtime, I want sandbox writes and commands to behave like a normal filesystem and shell, so that edits take effect, data is never destroyed silently, and one noisy command cannot take down the worker.
- **Build:**
  - `write_file` sets a real mtime, keeps an existing file mode, and raises `IsADirectoryError`, `NotADirectoryError` or `FileExistsError` on type conflicts instead of replacing nodes.
  - `PYTHONDONTWRITEBYTECODE=1` is part of the fixed sandbox environment, so stale bytecode never runs after an edit.
  - `read_file` is confined to the writable roots.
  - `exec` streams output and keeps a bounded head and tail per stream; `timeout_s` must be positive; processes a command leaves in the background are killed when it returns.
  - Containers run with an init that reaps children (`init=True`, `CMD` instead of `ENTRYPOINT`) and never inherit Docker proxy settings (`use_config_proxy=False`).
  - No-network tests assert that no network is attached instead of relying on an outbound request failing.
- **Acceptance criteria:**
  - [ ] A same-size `edit_file` followed by a test run executes the new code.
  - [ ] Writing onto a directory, or below a regular file, fails and leaves the tree unchanged.
  - [ ] A command that prints 100 MB returns within its timeout and the worker memory stays bounded.
  - [ ] A background `sleep` started by a command is gone after `exec` returns.
- **Tests:** `tests/integration/test_sandbox.py` (new cases for each criterion), `tests/unit/test_fake_sandbox.py`
- **Files:** `src/longhaul/sandbox/*`, `docker/sandbox/Dockerfile`, tests

#### LH-192 · Tool input and output hardening
- **Epic:** E2 · **Points:** 5 · **Branch:** `fix/lh-192-tool-hardening` · **Depends on:** LH-108, LH-109
- **Story:** As the agent, I want every tool to reject unsafe input and return results I can act on, so that untrusted issue text cannot escape the workspace and I never act on stale or misleading output.
- **Build:**
  - No argument can inject options or escape `/workspace` through any tool, including dash-leading paths and symlinks; tools use the resolved in-workspace path.
  - The registry turns bad-input errors into `ok=False` results and lets infrastructure errors propagate, so later stories can recreate a lost sandbox.
  - `read_file` numbers lines on newlines only, handles CRLF files consistently with `edit_file`, stops at a budget derived from `output_max_tokens`, and rejects bad ranges. `edit_file` counts overlapping matches and rejects no-op edits.
  - `search` returns results in a stable order and keeps partial results when `rg` reports unreadable files.
  - Fixture repos run their tests in place and carry a `.gitignore`.
- **Acceptance criteria:**
  - [ ] Path-escape tests pass for every registered tool.
  - [ ] Two identical `search` calls return identical output.
  - [ ] A Docker `NotFound` error from a tool propagates out of the registry.
- **Tests:** `tests/unit/test_registry.py`, `tests/unit/test_tool_helpers.py`, `tests/integration/test_file_tools.py`, `tests/integration/test_exec_tools.py`
- **Files:** `src/longhaul/tools/*`, `tests/fixtures/repos/*`, tests

#### LH-193 · OpenAI-compatible provider mapping fixes
- **Epic:** E1 · **Points:** 3 · **Branch:** `fix/lh-193-openai-provider` · **Depends on:** LH-105
- **Story:** As the cost owner and the retry policy, I want the OpenAI-compatible provider to report usage and errors with the same meaning as the Anthropic provider, so that cost and retries are right whichever provider runs.
- **Build:**
  - `Usage.input_tokens` excludes cached prompt tokens.
  - Errors map to the right kind: `insufficient_quota` and other non-recoverable 429s are not retried, 408 is a timeout, context overflow is detected across common server wordings, and `retry_after_s` parses every documented form.
  - The max tokens parameter name is configurable per endpoint; refusals and tool calls with `finish_reason: stop` are surfaced correctly; unexpected shapes raise `ProviderError`.
- **Acceptance criteria:**
  - [ ] A response with cached tokens yields disjoint `input_tokens` and `cache_read_tokens`.
  - [ ] Error-mapping tests count transport calls and prove the SDK never retries.
- **Tests:** `tests/unit/test_openai_provider.py`
- **Files:** `src/longhaul/providers/openai_compat.py`, tests

#### LH-194 · Settings, env and database safety
- **Epic:** E0 · **Points:** 3 · **Branch:** `fix/lh-194-config-db` · **Depends on:** LH-005, LH-008
- **Story:** As a developer, I want config to load the way the README says and tests that never touch real data, so that local runs are safe and predictable.
- **Build:**
  - `EnvSettings` reads `.env` and treats blank values as unset; `LONGHAUL_CONFIG` points at a file that exists; `LONGHAUL_SCRIPT` selects the scripted provider script.
  - Compose publishes ports on `127.0.0.1` only and takes the Postgres password from the environment.
  - The migration test runs in a throwaway database and compares the migrated schema with the models.
  - The engine can be disposed between event loops; the YAML secret scanner handles camelCase names.
- **Acceptance criteria:**
  - [ ] Copying `.env.example` to `.env` gives a working `EnvSettings()`.
  - [ ] The migration roundtrip never touches the application database.
- **Tests:** `tests/unit/test_settings.py`, `tests/unit/test_db_session.py`, `tests/integration/test_migrations.py`
- **Files:** `src/longhaul/settings.py`, `src/longhaul/db/*`, `.env.example`, `docker-compose.yml`, tests

#### LH-195 · CI, gate and test collection integrity
- **Epic:** E0 · **Points:** 3 · **Branch:** `fix/lh-195-ci-gate-integrity` · **Depends on:** LH-001, LH-003
- **Story:** As the reviewer, I want every test to run where it should and every gate to fail loudly, so that green means what it says.
- **Build:**
  - Test files with the same name in different layers collect; a collection hook requires a phase marker on every test and derives the layer marker from the directory.
  - `make gate` fails when the marker script fails and excludes opt-in layers; the mypy strict override applies only to the strict modules.
  - The fixture repo builder is deterministic; CI pins the Temporal CLI; Dependabot targets `dev`; the CI config tests check the aggregate job, every secrets reference and the branch protection body.
- **Acceptance criteria:**
  - [ ] A test without a phase marker fails collection.
  - [ ] Two fixture builds give the same commit.
- **Tests:** `tests/unit/test_ci_config.py`, `tests/unit/test_markers.py`, `tests/unit/test_fixtures.py`, `tests/unit/test_docs_style.py`
- **Files:** `pyproject.toml`, `Makefile`, `tests/conftest.py`, `.github/*`, `scripts/*`, tests

#### LH-196 · Backlog import safety
- **Epic:** E0 · **Points:** 2 · **Branch:** `fix/lh-196-backlog-import` · **Depends on:** LH-006
- **Story:** As the product owner, I want the backlog import to validate the plan and behave like real `gh`, so that a run against GitHub cannot leave half-created or mislabelled issues.
- **Build:**
  - Story counts come from the plan, so appending fix stories keeps CI green.
  - The fake `gh` is strict like the real one; the plan is validated before any write; closed milestones and empty label lists are handled; `--update` syncs title, labels and milestone.
- **Acceptance criteria:**
  - [ ] Appending a fix story to the plan keeps the unit suite green.
  - [ ] A mistyped phase heading fails before any GitHub call.
- **Tests:** `tests/unit/test_import_backlog.py`, `tests/unit/test_progress.py`
- **Files:** `scripts/import_backlog.py`, tests

#### LH-197 · Scripted provider determinism
- **Epic:** E1 · **Points:** 3 · **Branch:** `fix/lh-197-scripted-provider` · **Depends on:** LH-103
- **Story:** As a test author, I want scripted runs to stay deterministic through retries and compaction, so that e2e and chaos tests mean what they say.
- **Build:**
  - Tool ids are unique within a transcript and stable for a retried identical request; the matched turn survives compaction through a documented marker; matchers can see user text; raise steps can be bounded.
  - Scripted calls to tools that were not offered fail; role matching fails loudly; scripts are validated strictly; `calc_fix.yaml` finishes only after a passing test result.
- **Acceptance criteria:**
  - [ ] No two `tool_use` blocks in one transcript share an id.
  - [ ] A script keyed on turn 5 still matches after a compaction rewrites the transcript.
- **Tests:** `tests/unit/test_scripted_provider.py`
- **Files:** `src/longhaul/providers/scripted.py`, `src/longhaul/providers/base.py`, `tests/fixtures/scripts/*`, tests

#### LH-198 · Contracts v2 from the API research
- **Epic:** E2 · **Points:** 5 · **Branch:** `feat/lh-198-contracts-v2` · **Depends on:** LH-102, LH-101 · **Reviewer sign-off**
- **Story:** As every provider and the loop, I want the shared contracts to carry what the current Messages API returns and accepts, so that refusals, effort, forced output and billed failures are handled without guessing.
- **Build:**
  - `LLMResponse.stop_reason` adds `refusal`, `pause_turn` and `model_context_window_exceeded`, plus `raw_stop_reason`, `stop_details`, `request_id` and `dropped_thinking_blocks`.
  - `LLMProvider.complete()` and `count_tokens()` take `options: CallOptions | None` with `effort`, `force_tool` and `output_schema`. `ToolSpec` gains `strict`. `ProviderError` can carry the `usage` of a billed response.
  - `CarryState.started_at` is timezone aware; block type tags always serialize; render never sends empty blocks or messages.
  - Config: model ids `claude-sonnet-5-5` and `claude-haiku-5-5`, an `effort` section per role, `loop.max_tokens_retry`, and prices in `config/pricing.yaml` taken from the bundled docs.
  - `contracts_version = "2"` with a changelog entry.
- **Acceptance criteria:**
  - [ ] Every contract model round-trips with non-default values for every field.
  - [ ] A naive `started_at` is rejected.
  - [ ] The example config loads with the effort section.
- **Tests:** `tests/unit/test_contracts.py`, `tests/unit/test_render.py`, `tests/unit/test_settings.py`
- **Files:** `src/longhaul/contracts.py`, `src/longhaul/agent/render.py`, `src/longhaul/settings.py`, `config/*.yaml`, `docs/architecture.md`, tests

### Integration checkpoint 1

- **Cumulative tests:** `make gate PHASE=1`, which runs phase 0 and 1 markers including e2e `test_calc_scripted`.
- **Manual smoke:**
  ```bash
  make up && make sandbox-image
  uv run longhaul run-local --path tests/fixtures/repos/calc --issue-file tests/fixtures/repos/calc/ISSUE.md --provider scripted --script tests/fixtures/scripts/calc_fix.yaml
  ANTHROPIC_API_KEY=... uv run longhaul run-local --path tests/fixtures/repos/textkit --issue-file tests/fixtures/repos/textkit/ISSUE.md --provider anthropic
  docker ps -a --filter label=longhaul.task_id   # expect nothing left running
  ```

### Phase gate 1

- [ ] All 11 stories Done. ADR-0003 merged with the API facts appendix.
- [ ] I3 and I8 hold.
- [ ] A real-model run fixed `calc` and `textkit`, with the output in the gate report.
- [ ] Tag `v0.1.0`.

### Defend it

1. **Why `edit_file` with exact-match `old_str` instead of line numbers?** Line numbers go stale after any earlier edit. Exact-match replacement fails loudly when the model's view is out of date, which is what we want.
2. **Why turn off the SDK's own retries?** Retries live in one place, Temporal, with one policy and one ledger. Two retry layers multiply attempts and hide cost.
3. **Why does the sandbox lose network after setup?** Dependencies need the network once. After that, network access is the main path for exfiltration or for the agent pulling in random code. Cutting it removes both.
4. **Why are turn functions separate from the loop?** Phase 2 wraps `llm_step` and `tool_step` as Temporal activities without rewriting them. The in-process loop becomes a test harness and a baseline.
5. **What does the scripted provider prove?** That the plumbing works: tools, pairing, finish. It says nothing about model quality. That's what the evals in Phase 9 are for.

---

## Phase 2: Temporal durability and checkpoints (tag v0.2.0)

**Goal:** the same task runs as a Temporal workflow. Every turn and tool call is an activity, every workspace change is a git checkpoint, and killing the worker at any point resumes the run with the same final diff.
**Demo at the end:**
1. Start `longhaul run --path tests/fixtures/repos/inventory`.
2. `kill -9` the worker mid-run.
3. Start the worker again.
4. Temporal UI shows the retried activity, and the task finishes with the reference diff.

**Entry criteria:** Phase 1 gate passed.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-201 | Spike: Temporal Python SDK patterns | 2 | E4 |
| LH-202 | Blob store | 3 | E4 |
| LH-203 | Task workflow skeleton | 5 | E4 |
| LH-204 | LLM and tool activities | 5 | E4 |
| LH-205 | Git checkpoint store | 5 | E5 |
| LH-206 | Sandbox recovery and retry-safe tool execution | 5 | E5 |
| LH-207 | Continue-As-New | 3 | E4 |
| LH-208 | Worker process and run CLI | 3 | E4 |
| LH-209 | Chaos test v1 | 5 | E5 |

#### LH-201 · Spike: Temporal Python SDK patterns
- **Epic:** E4 · **Points:** 2 · **Branch:** `spike/lh-201-temporal-patterns` · **Depends on:** LH-002
- **Story:** As the developer of Phase 2, I want the exact SDK patterns verified, so that the workflow code is right the first time.
- **Build:** a timeboxed (2 h) throwaway workflow in `spikes/temporal_patterns.py`. Verify and record in `docs/adr/0004-temporal.md` (appendix "SDK facts"):
  1. Workflow sandbox import rules and `workflow.unsafe.imports_passed_through()` for pydantic models.
  2. The pydantic data converter (`temporalio.contrib.pydantic`), if available in the pinned version. Otherwise write a custom converter.
  3. Activity heartbeats and `heartbeat_timeout` behaviour when the worker is killed.
  4. `workflow.info().is_continue_as_new_suggested()` and `get_current_history_length()`.
  5. `WorkflowEnvironment.start_time_skipping()` for tests with long timers.
  6. Starting a workflow with an id that already exists: which error is raised and which id reuse policy to use.
  7. Schedules API for the poller.
- **Fallback:** if the pydantic converter is missing, serialize with `model_dump(mode="json")` at boundaries and reconstruct in a small helper. If `is_continue_as_new_suggested` is missing, use a turn count only.
- **Acceptance criteria:**
  - [ ] The ADR-0004 appendix answers all seven points with code snippets.
- **Tests:** none (spike).
- **Files:** `spikes/temporal_patterns.py`, `docs/adr/0004-temporal.md`

#### LH-202 · Blob store
- **Epic:** E4 · **Points:** 3 · **Branch:** `feat/lh-202-blob-store` · **Depends on:** LH-101, LH-002
- **Story:** As activities, I want to put large payloads in object storage and pass references, so that Temporal history stays small.
- **Build:**
  - `src/longhaul/blobs/s3.py`: `S3BlobStore(endpoint, bucket, keys)` using `aioboto3`, or boto3 in `to_thread`. Pick one and record it in ADR-0005.
  - `put` computes sha256, writes to `key`, and returns a `BlobRef`. `get` verifies the sha256 and raises `BlobIntegrityError` on mismatch.
  - Helpers: `put_json(model)`, `get_json(ref, Model)`.
  - Key helper `blob_key(task_id, kind, turn_index, name)` produces e.g. `tasks/lh-x/turns/0007/response.json`.
  - `MemoryBlobStore` for unit tests.
  - Write `docs/adr/0005-payloads-by-reference.md` including the 256 KB cap.
  - Add `src/longhaul/durable/payload_guard.py`: `assert_small(obj, limit=256_000)`, used in activity wrappers (I2).
- **Acceptance criteria:**
  - [ ] Put/get round-trips a 5 MB payload against MinIO.
  - [ ] A tampered object raises `BlobIntegrityError`.
  - [ ] The payload guard raises on a 300 KB object.
- **Tests:** `tests/integration/test_blobs.py::test_roundtrip_large`, `::test_integrity_check`, `tests/unit/test_invariants.py::test_payload_size_guard`
- **Files:** `src/longhaul/blobs/s3.py`, `src/longhaul/durable/payload_guard.py`, `docs/adr/0005-payloads-by-reference.md`, tests

#### LH-203 · Task workflow skeleton
- **Epic:** E4 · **Points:** 5 · **Branch:** `feat/lh-203-task-workflow-skeleton` · **Depends on:** LH-201, LH-202 · **Reviewer sign-off**
- **Story:** As the system, I want a TaskWorkflow with state, signals and a status query, so that each task has one durable owner.
- **Build:**
  - `src/longhaul/durable/workflows/task.py`:
    - `@workflow.defn class TaskWorkflow` with `run(state: CarryState) -> Outcome`. It holds `self.state`.
    - Signals: `cancel`, `hint` and `add_budget`. Stubs for `approve_plan` and `reject_plan` store the flag and are used in Phase 5.
    - Query: `status() -> TaskStatus`.
    - Phases for now: `PREPARING`, then `CODING` (loop), then `DONE`. The loop calls activities by name through a small `acts` module that holds activity stubs, so imports stay clean.
    - Uses `workflow.now()` only.
  - `src/longhaul/durable/client.py`: `async def start_task(client, spec) -> WorkflowHandle` with workflow id `spec.task_id` and an id reuse policy that rejects duplicates while one is running (from the spike).
  - Add the I1 AST test: `src/longhaul/durable/workflows/*.py` may not import the forbidden modules listed in section 4.3.
- **Acceptance criteria:**
  - [ ] Given a time-skipping test env and stub activities, when the workflow starts, then `status()` reports `PREPARING` then `CODING`.
  - [ ] Given a running workflow, when `cancel` is signalled, then the outcome is `CANCELLED` and a cleanup activity is called.
  - [ ] Starting a second workflow with the same `task_id` while the first is running fails with a clear error.
  - [ ] The I1 test passes.
- **Tests:** `tests/integration/test_task_workflow.py::test_status_query_phases`, `::test_cancel_signal`, `::test_duplicate_start_rejected`, `tests/unit/test_invariants.py::test_workflows_have_no_io_imports`
- **Files:** `src/longhaul/durable/workflows/task.py`, `src/longhaul/durable/client.py`, `src/longhaul/durable/acts.py`, tests

#### LH-204 · LLM and tool activities
- **Epic:** E4 · **Points:** 5 · **Branch:** `feat/lh-204-activities` · **Depends on:** LH-203, LH-110
- **Story:** As the workflow, I want each model turn and each tool call to be its own activity, so that each one has its own timeout, retry and heartbeat.
- **Build:**
  - `src/longhaul/durable/activities/llm.py`: `@activity.defn async def llm_turn(inp: LlmTurnInput) -> LlmTurnOutput`.
    1. Load the transcript from blob and call `turns.llm_step`.
    2. Append to the transcript, store the new transcript and the response JSON in blob.
    3. Return refs, a small list of `ToolCall` summaries (name plus args ref when args are over 16 KB), usage and cost (cost is 0.0 until Phase 7).
    4. Heartbeat every 10 s while waiting, using a background task.
    5. Raise `ApplicationError(non_retryable=True)` for the non-retryable `ProviderErrorKind`s.
  - `src/longhaul/durable/activities/tools.py`: `@activity.defn async def tool_exec(inp: ToolExecInput) -> ToolResult`. It gets the sandbox, runs `turns.tool_step`, stores the full output in blob, heartbeats during long commands, and writes a `tool_calls` row. The checkpoint logic is added in LH-205 and LH-206, behind a `CheckpointStore` parameter that is a no-op for now.
  - The workflow loop:
    1. Call `llm_turn` with the retry policy from section 3.7.
    2. Call `tool_exec` for each tool call, sequentially and in order.
    3. Append results via a third small activity `append_results` that takes the transcript ref and result refs and returns a new transcript ref.
    4. Stop on `finish`.
  - Migrations: the `turns` and `tool_calls` tables.
- **Acceptance criteria:**
  - [ ] With the scripted provider and a real Docker sandbox, the workflow fixes `calc` end to end through activities.
  - [ ] Temporal history contains no payload over 256 KB (checked by scanning history in the test).
  - [ ] A `ProviderError(OVERLOADED)` raised twice by a flaky test provider is retried, and the run completes.
  - [ ] `INVALID_REQUEST` fails the activity without retry.
- **Tests:** `tests/integration/test_activities.py::test_workflow_fixes_calc_via_activities`, `::test_history_payloads_small`, `::test_retries_on_overloaded`, `::test_invalid_request_not_retried`
- **Files:** `src/longhaul/durable/activities/{llm,tools}.py`, `src/longhaul/durable/workflows/task.py`, migration, tests

#### LH-205 · Git checkpoint store
- **Epic:** E5 · **Points:** 5 · **Branch:** `feat/lh-205-checkpoints` · **Depends on:** LH-107, LH-202
- **Story:** As the recovery logic, I want every workspace change saved as a commit outside the container, so that any state can be restored on a new container.
- **Build:**
  - `src/longhaul/checkpoints/git_store.py`, `GitCheckpointStore` implementing `CheckpointStore`:
    - `init_task(task_id, clone_url, base_sha)`:
      - on the worker host, create bare repo `/var/lib/longhaul/ckpt/<task_id>.git` (a Docker volume when the worker runs in a container) by `git clone --bare`
      - create branch `longhaul/<issue>` at `base_sha`
      - mount the bare repo read-write into the sandbox at `/ckpt`
      - inside the sandbox, `git clone /ckpt /workspace`, check out the branch, and tag `base`
    - `commit(sandbox, message)`: inside the sandbox, `git add -A`. If there are changes, commit and `git push /ckpt HEAD:longhaul/<issue>`. Return the new sha, or None if the tree is clean.
    - `restore(sandbox, sha)`: `git fetch /ckpt`, `git reset --hard sha`, `git clean -fdx -e .venv`.
    - `bundle(task_id)`: `git bundle create` on the host and upload to blob as `tasks/<id>/ckpt/<sha>.bundle` (Should).
  - Write `docs/adr/0006-git-checkpoints.md`.
  - Migration: the `checkpoints` table.
- **Acceptance criteria:**
  - [ ] After `edit_file` + `commit`, the bare repo branch head equals the returned sha.
  - [ ] `restore(base)` brings the workspace back byte-for-byte (test with `git status --porcelain` empty and file hashes).
  - [ ] `commit` on a clean tree returns None and creates no commit.
  - [ ] A bundle restored into an empty dir has the same head.
- **Tests:** `tests/integration/test_checkpoints.py::test_commit_and_head`, `::test_restore_exact`, `::test_clean_tree_no_commit`, `::test_restore_from_bundle`
- **Files:** `src/longhaul/checkpoints/git_store.py`, migration, ADR, tests

#### LH-206 · Sandbox recovery and retry-safe tool execution
- **Epic:** E5 · **Points:** 5 · **Branch:** `feat/lh-206-retry-safe-tools` · **Depends on:** LH-204, LH-205
- **Story:** As the workflow, I want every tool call to start from a known commit and the sandbox to come back if it disappears, so that retries never run on a half-changed workspace.
- **Build:**
  - Activity `ensure_sandbox(task_id, head_sha, sandbox_id)` (sketched after this list):
    - if the container exists and is running, return its id
    - if not, create a new one (same limits, network off)
    - clone from `/ckpt`, check out `head_sha`, re-run `setup_command` with the network on, cut the network
    - record a `sandbox_recreated` event and return the new id
  - `tool_exec` changes:
    1. Call `restore(pre_sha)` if the workspace HEAD isn't `pre_sha` or the tree is dirty.
    2. Run the tool.
    3. If `tool.mutates` (or `run_command` / `run_tests` dirtied the tree), `commit` with the message `lh: <tool> turn <n> call <id>` and return `post_sha`.
  - The workflow keeps `state.head_sha` equal to the latest `post_sha`, and passes `pre_sha=state.head_sha` into each `tool_exec`.
  - The workflow calls `ensure_sandbox` before the first turn and again whenever `tool_exec` fails with `SandboxGoneError` (raised as a retryable application error after `ensure_sandbox`).

    ```python
    # sketch only: activity, so I/O is fine here
    @activity.defn
    async def ensure_sandbox(task_id: str, head_sha: str, sandbox_id: str | None) -> str:
        sb = await manager.get(sandbox_id) if sandbox_id else None
        if sb is not None:
            return sb.id
        sb = await manager.create(task_id, cfg.image, cfg.limits, network=True)
        await ckpt.attach(sb, task_id)          # mounts /ckpt, clones, checks out head_sha
        await ckpt.restore(sb, head_sha)
        if spec.settings.setup_command:
            await sb.exec(spec.settings.setup_command, timeout_s=900)
        await manager.set_network(sb.id, False)
        await events.record(task_id, "sandbox_recreated", {"head_sha": head_sha})
        return sb.id
    ```
- **Acceptance criteria:**
  - [ ] Given a running task, when the sandbox container is removed with `docker rm -f`, then the next tool call recreates it at `head_sha` and the task finishes with the reference diff.
  - [ ] Given a `tool_exec` that was killed after editing but before returning, when it is retried, then the edit is applied exactly once (the final file has one change, not two).
  - [ ] Every mutating tool call has a `post_sha` row in `checkpoints`.
- **Tests:** `tests/integration/test_sandbox_recovery.py::test_container_removed_is_recreated_at_head`, `::test_retry_after_partial_edit_applies_once`, `tests/integration/test_checkpoints.py::test_every_mutation_checkpointed`
- **Files:** `src/longhaul/durable/activities/{workspace,tools}.py`, `src/longhaul/durable/workflows/task.py`, tests

#### LH-207 · Continue-As-New
- **Epic:** E4 · **Points:** 3 · **Branch:** `feat/lh-207-continue-as-new` · **Depends on:** LH-206
- **Story:** As a long task, I want my workflow history reset every N turns, so that I never hit Temporal's history limits.
- **Build:**
  - In the loop, after each turn: if `turn_index % cfg.continue_as_new_every_turns == 0` or `workflow.info().is_continue_as_new_suggested()` (if available), then:
    1. Call `bundle_checkpoint` (Should).
    2. Record a `continue_as_new` event.
    3. `workflow.continue_as_new(self.state)`.
  - Signals that arrive during the handoff must not be lost. Drain any pending signal state into `CarryState` first: hints go into `state.hints` and budget additions into `state.extra_budget_usd`.
- **Acceptance criteria:**
  - [ ] With `continue_as_new_every_turns=3` and a 10-turn scripted task, the run produces 4 runs in the same workflow id and finishes with the reference diff.
  - [ ] `turn_index`, `head_sha`, `cost_usd` and `hints` carry over unchanged.
  - [ ] A hint signalled right before the handoff shows up in the next run's prompt.
- **Tests:** `tests/integration/test_task_workflow.py::test_continue_as_new_preserves_state`, `::test_signal_not_lost_across_can`
- **Files:** `src/longhaul/durable/workflows/task.py`, tests

#### LH-208 · Worker process and run CLI
- **Epic:** E4 · **Points:** 3 · **Branch:** `feat/lh-208-worker-cli` · **Depends on:** LH-204
- **Story:** As an operator, I want a worker command and a run command, so that I can run tasks against the durable stack.
- **Build:**
  - `src/longhaul/durable/worker.py`: `async def main()` builds the providers, sandbox manager, blob store, checkpoint store and DB session, then registers all workflows and activities on task queue `longhaul`. It handles SIGTERM with a graceful shutdown.
  - CLI:
    - `longhaul worker`
    - `longhaul run --path DIR --issue-file FILE` (local repo source; Phase 3 adds `--repo/--issue`) starts the workflow and tails `status()` every 2 s until done
    - `longhaul status TASK_ID`
    - `longhaul cancel TASK_ID`
  - `docker/worker/Dockerfile`. The compose service `worker` is optional (profile `full`), because it needs the Docker socket mounted. That decision is documented in the runbook with its security note.
- **Acceptance criteria:**
  - [ ] `longhaul worker` in one shell and `longhaul run ...` in another fixes `calc` with the scripted provider.
  - [ ] Ctrl-C on the worker during a turn, then restarting it, completes the run.
- **Tests:** `tests/e2e/test_durable_run.py::test_run_calc_durable` (starts the worker as a subprocess)
- **Files:** `src/longhaul/durable/worker.py`, `src/longhaul/cli.py`, `docker/worker/Dockerfile`, `docs/runbook.md`, tests

#### LH-209 · Chaos test v1
- **Epic:** E5 · **Points:** 5 · **Branch:** `feat/lh-209-chaos-v1` · **Depends on:** LH-207, LH-208
- **Story:** As the product owner, I want an automated test that kills the worker at random points, so that "survives crashes" is proven, not claimed.
- **Build:**
  - `src/longhaul/providers/replay.py`: `ReplayProvider(recording_dir)`. Responses are keyed by `(turn_index, sha256(canonical request))`. `--record` mode wraps another provider and saves pairs. If no matching request is found, it falls back to the turn index only and logs a warning.
  - Record one real run of `inventory` (Hooman, with a key) into `eval/chaos/recordings/inventory/`. Also produce a scripted equivalent for CI: `tests/fixtures/scripts/inventory_fix.yaml`.
  - `tests/chaos/test_kill_worker.py`:
    1. Start the worker as a subprocess and start the task.
    2. Pick a random delay (seeded) in `[0.5 s, expected duration]` and send `SIGKILL`.
    3. Restart the worker. Repeat k times per run.
    4. Assert that the outcome is `finish`, the final `git_diff` equals the no-kill reference, and each `tool_calls` row's `post_sha` chain is linear (no forks).
  - Markers: `chaos_lite` (5 runs, 1 kill each, scripted) runs in CI nightly and at gates. `chaos` (20 runs, 1 to 3 kills, replay) runs locally in Phase 9.
  - `scripts/chaos_kill.sh` for the manual demo.
- **Acceptance criteria:**
  - [ ] `pytest -m chaos_lite` passes 5/5 locally and in CI.
  - [ ] The test fails if `restore(pre_sha)` is removed from `tool_exec` (mutation check: the reviewer temporarily comments it out and confirms the failure, then restores it).
  - [ ] Kills during `llm_turn`, during `tool_exec` and between activities are each hit at least once across the seeds (the test logs the kill point from the worker's last log line).
- **Tests:** `tests/chaos/test_kill_worker.py::test_kill_during_tool_exec_resumes`, `::test_kill_during_llm_turn_resumes`, `::test_kill_between_activities`
- **Files:** `src/longhaul/providers/replay.py`, `tests/chaos/*`, `eval/chaos/recordings/**`, `scripts/chaos_kill.sh`, `.github/workflows/ci.yml` (nightly)

### Integration checkpoint 2

- **Cumulative tests:** `make gate PHASE=2`. That covers phases 0 to 2, including Phase 1's `test_calc_scripted` (the in-process loop still works) and `chaos_lite`.
- **Manual smoke:**
  ```bash
  make up && make sandbox-image
  uv run longhaul worker &           # shell 1
  WORKER=$!
  uv run longhaul run --path tests/fixtures/repos/inventory --issue-file tests/fixtures/repos/inventory/ISSUE.md --provider scripted --script tests/fixtures/scripts/inventory_fix.yaml &
  sleep 4 && kill -9 $WORKER && sleep 2 && uv run longhaul worker &
  wait
  # Temporal UI: workflow shows a retried activity; final status DONE
  docker rm -f $(docker ps -q --filter label=longhaul.task_id) 2>/dev/null; echo "no leftovers"
  ```

### Phase gate 2

- [ ] All 9 stories Done. ADR-0004, 0005 and 0006 merged.
- [ ] I1, I2 and I3 hold. `chaos_lite` 5/5.
- [ ] Mutation check on `restore(pre_sha)` done and noted in the gate report.
- [ ] Phase 1 e2e still green.
- [ ] Tag `v0.2.0`.

### Defend it

1. **What exactly happens if the worker dies while `pytest` is running in the sandbox?** The heartbeat stops. After the heartbeat timeout, Temporal schedules the activity again. The new attempt resets the workspace to `pre_sha`, which drops anything the killed command wrote, and runs `pytest` again. The workflow never sees the dead attempt.
2. **Why micro-commits per tool call instead of per turn?** A turn can hold several tool calls. If a retry happens after the second of three edits, we need the exact state before that call. Commits are cheap, and we squash them for the PR.
3. **Why pass references instead of the transcript?** Temporal stores every input and output in history. A 150 KB transcript per turn would blow past history limits in a few dozen turns and slow replay.
4. **Is an LLM turn idempotent?** No. A retry costs a second call and may return a different answer. We accept that: the ledger records both attempts, and only the completed attempt's result enters history, so the workflow stays consistent.
5. **How do you know the chaos test is meaningful?** It fails when the recovery code is removed (mutation check), and it asserts the final diff matches a no-kill reference, not just that something finished.

---

## Phase 3: GitHub integration (tag v0.3.0)

**Goal:** labeling an issue on an allowlisted repo starts a task within a minute. The agent works, pushes a branch and opens a PR. People steer with comments, and no side effect ever happens twice.
**Demo at the end:**
1. On the test repo `hoomanesteki/lh-demo-calc`, label issue #1 `longhaul`.
2. Within 60 s the issue gets a "Picked up" comment and a `longhaul:working` label.
3. A few minutes later a PR appears that links the issue.

**Entry criteria:**
- Phase 2 gate passed.
- Three demo repos created from the fixtures (`lh-demo-calc`, `lh-demo-textkit`, `lh-demo-inventory`), each with an open issue copied from `ISSUE.md`.
- A fine-grained PAT with contents, issues and pull requests read/write on those repos only.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-301 | FakeGitHub test server | 3 | E6 |
| LH-302 | GitHub client and auth | 5 | E6 |
| LH-303 | Repo allowlist and issue intake | 3 | E6 |
| LH-304 | Idempotent side-effect activities | 5 | E6 |
| LH-305 | Clone from GitHub and push from the host | 5 | E6 |
| LH-306 | PR creation and label lifecycle | 3 | E6 |
| LH-307 | Poller workflow on a Temporal Schedule | 5 | E6 |
| LH-308 | Comment commands and issue-to-PR e2e | 5 | E6 |

#### LH-301 · FakeGitHub test server
- **Epic:** E6 · **Points:** 3 · **Branch:** `feat/lh-301-fake-github` · **Depends on:** LH-001
- **Story:** As a test author, I want an in-memory GitHub that speaks the REST endpoints we use, so that GitHub logic is tested without the network.
- **Build:**
  - `tests/fakes/fake_github.py`: a FastAPI app with in-memory state for repos, issues, comments, labels, pulls and collaborators. It also records refs pushed through a local bare repo path.
  - Endpoints (only these):
    - `GET /repos/{o}/{r}/issues?labels=&state=open`
    - `GET /repos/{o}/{r}/issues/{n}`
    - `GET/POST /repos/{o}/{r}/issues/{n}/comments` (with `since`)
    - `POST /repos/{o}/{r}/issues/{n}/labels`
    - `DELETE /repos/{o}/{r}/issues/{n}/labels/{name}`
    - `GET/POST /repos/{o}/{r}/pulls` (filter `head`)
    - `GET /repos/{o}/{r}/pulls/{n}`
    - `GET /repos/{o}/{r}/collaborators/{u}/permission`
  - Fault injection: `fail_next(path_regex, status, count)`, and an `after_write_fail` mode that applies a write, then returns 502. That simulates the "did my write land?" case.
  - Helpers: `seed_repo()`, `seed_issue()`, `count_comments()`.
  - `make fake-github` runs it on port 8099 for e2e.
- **Acceptance criteria:**
  - [ ] Each endpoint has a unit test.
  - [ ] `after_write_fail` stores the comment and still returns 502.
- **Tests:** `tests/unit/test_fake_github.py::test_endpoints`, `::test_after_write_fail_mode`
- **Files:** `tests/fakes/fake_github.py`, tests

#### LH-302 · GitHub client and auth
- **Epic:** E6 · **Points:** 5 · **Branch:** `feat/lh-302-github-client` · **Depends on:** LH-301
- **Story:** As the worker, I want one GitHub client with auth, retries and rate-limit handling, so that every GitHub call behaves the same way.
- **Build:**
  - `src/longhaul/github/client.py`: `GitHubClient(base_url, auth)` on `httpx.AsyncClient`.
    - Read methods for the `GitHubGateway` read side.
    - Low-level `_request()` that raises `GitHubRetryable` (5xx, network, 403/429 with rate-limit headers, honouring `retry-after` / `x-ratelimit-reset`) and `GitHubFatal` (other 4xx).
  - Auth:
    - `PatAuth(token)`: Must.
    - `AppAuth(app_id, key_path, installation_id)`: Should. It mints a JWT, exchanges it for an installation token, caches it until 5 min before expiry, and uses PyJWT.
    - Write `docs/adr/0012-github-auth.md`.
  - `git_auth_header(token) -> list[str]` returns the `-c http.https://github.com/.extraheader=AUTHORIZATION: basic <b64>` args, so tokens never land in git config or remote URLs.
- **Acceptance criteria:**
  - [ ] Against FakeGitHub, the read methods return parsed dicts.
  - [ ] A 502 maps to `GitHubRetryable`, a 404 to `GitHubFatal`, and a 403 with `x-ratelimit-remaining: 0` to `GitHubRetryable` with the right `retry_after_s`.
  - [ ] AppAuth caches the token (two calls, one exchange). Tested with a mocked token endpoint.
- **Tests:** `tests/unit/test_github_client.py::test_reads`, `::test_error_mapping`, `::test_rate_limit_retry_after`, `::test_app_token_cached`, `::test_git_auth_header_not_in_url`
- **Files:** `src/longhaul/github/client.py`, `docs/adr/0012-github-auth.md`, tests

#### LH-303 · Repo allowlist and issue intake
- **Epic:** E6 · **Points:** 3 · **Branch:** `feat/lh-303-intake` · **Depends on:** LH-302, LH-008
- **Story:** As the system, I want issues turned into a `TaskSpec` only for allowlisted repos, so that Longhaul never works where it wasn't invited.
- **Build:**
  - `src/longhaul/github/intake.py`:
    - `sync_repo_settings(config, db)` upserts `repo_settings` from YAML.
    - `issue_to_spec(repo_settings, issue, comments) -> TaskSpec` sets `task_id = lh-{owner}-{repo}-{n}` (lowercase, with non-alnum replaced by `-`) and the body from the issue. Comments are included only from users with write permission, and bot comments carrying our marker are excluded.
    - `is_allowlisted(repo)`.
  - `RepoSettings.repo` now accepts `owner/name`. Local paths keep working with the `local:` prefix.
- **Acceptance criteria:**
  - [ ] An issue on a non-allowlisted repo produces no spec, and a log line says why.
  - [ ] Comments from users without write permission are excluded from the spec.
  - [ ] The `task_id` format is stable for weird repo names (`My.Repo_v2`).
- **Tests:** `tests/unit/test_intake.py::test_non_allowlisted_ignored`, `::test_comment_permission_filter`, `::test_task_id_format`
- **Files:** `src/longhaul/github/intake.py`, tests

#### LH-304 · Idempotent side-effect activities
- **Epic:** E6 · **Points:** 5 · **Branch:** `feat/lh-304-side-effects` · **Depends on:** LH-302 · **Reviewer sign-off**
- **Story:** As the workflow, I want every GitHub write to happen exactly once no matter how many retries, so that issue threads stay clean.
- **Build:**
  - Migration: the `side_effects` table.
  - `src/longhaul/github/side_effects.py`. Activities `post_comment`, `add_label`, `remove_label` and `create_pr`. Each takes an `idempotency_key` and does this:
    1. If a `side_effects` row exists for the key, return its `external_id`.
    2. Otherwise, for comments, search existing comments on the issue for marker `<!-- longhaul:key=<key> -->`. For PRs, search open PRs with the same `head`. If found, insert the row and return the id.
    3. Otherwise, write (the comment body gets the marker appended), insert the row, and return the id.
  - Label add/remove are naturally idempotent on GitHub, but they still record rows for the audit trail.
  - Key helper: `effect_key(workflow_id, purpose, seq)`.
  - I4 AST test: the client's write methods (`_post`, `_delete`, `_patch`) are called only from `side_effects.py`.
- **Acceptance criteria:**
  - [ ] Given `after_write_fail` on the comments endpoint, when `post_comment` is retried, then exactly one comment exists.
  - [ ] Given the `side_effects` table is truncated (DB loss), when `post_comment` runs again with the same key, then no new comment appears (marker search).
  - [ ] Given an existing open PR for the head branch, `create_pr` returns its URL without creating another.
- **Tests:** `tests/integration/test_github_side_effects.py::test_retry_after_5xx_no_duplicate`, `::test_comment_idempotent_after_db_loss`, `::test_pr_idempotent_by_head`, `tests/unit/test_invariants.py::test_github_writes_only_in_side_effects`
- **Files:** `src/longhaul/github/side_effects.py`, migration, tests

#### LH-305 · Clone from GitHub and push from the host
- **Epic:** E6 · **Points:** 5 · **Branch:** `feat/lh-305-clone-push` · **Depends on:** LH-205, LH-302
- **Story:** As the workspace preparer, I want to clone the real repo into the checkpoint store and push branches from the host, so that the sandbox never holds a token.
- **Build:**
  - `prepare_workspace` activity, now for `owner/name` repos:
    - Resolve `base_branch` (the repo default) and `base_sha` via the API.
    - `git clone --bare` on the host using `git_auth_header`.
    - Create branch `longhaul/<n>` at `base_sha`, then continue as in LH-205/LH-206.
  - `src/longhaul/checkpoints/push.py`: `push_branch(task_id, local_ref, remote_branch)` runs on the host from the bare repo with the auth header. It calls `assert_not_default(remote_branch, default_branch)`, which raises `ForbiddenBranchError` (F15, I5) for the default branch or anything matching `main|master|develop|release/*`.
  - Write `docs/adr/0009-sandbox-model.md`, covering why the clone and push happen on the host.
- **Acceptance criteria:**
  - [ ] Against FakeGitHub (backed by a local bare "remote"), `prepare_workspace` produces a sandbox at `base_sha` with no token in its env, its git config or `/ckpt/config`.
  - [ ] `push_branch(..., "main")` raises `ForbiddenBranchError`, and no push happens.
  - [ ] A pushed branch appears in the fake remote.
- **Tests:** `tests/integration/test_clone_push.py::test_prepare_from_remote`, `::test_no_token_in_sandbox_or_ckpt_config`, `tests/unit/test_branch_guard.py::test_refuses_default_branch`, `::test_refuses_protected_patterns`
- **Files:** `src/longhaul/durable/activities/workspace.py`, `src/longhaul/checkpoints/push.py`, ADR, tests

#### LH-306 · PR creation and label lifecycle
- **Epic:** E6 · **Points:** 3 · **Branch:** `feat/lh-306-pr-labels` · **Depends on:** LH-304, LH-305
- **Story:** As a reviewer, I want a clean PR branch and clear labels on the issue, so that I can see the task state at a glance.
- **Build:**
  - `src/longhaul/checkpoints/squash.py`: `build_pr_branch(task_id, groups)`.
    - `groups` is a list of `(title, last_sha)`. For Phase 3 it holds one group, titled with the finish summary.
    - On the host, create `longhaul-pr/<n>` from `base_sha` and apply the diff of each group as one commit, using commit messages written in the style guide (`fix(calc): add returns the sum`).
    - Commit author is configurable (`LONGHAUL_GIT_AUTHOR`, default `longhaul <longhaul@users.noreply.github.com>`).
  - Activity `deliver`:
    1. Push `longhaul-pr/<n>`.
    2. `create_pr` (draft until Phase 5 marks verified PRs ready). The body comes from `templates/pr_body.md.j2`: summary, changes per file, how it was tested (last `run_tests` output summary) and `Closes #n`.
  - Label transitions as idempotent activities:
    - on pickup: add `longhaul:working`
    - on PR: remove `longhaul:working`, add `longhaul:pr-open`
    - on pause: add `longhaul:needs-human`
    - on resume: remove it
- **Acceptance criteria:**
  - [ ] The PR branch has exactly one commit on top of base for Phase 3, and its diff equals the scratch head diff.
  - [ ] The PR body contains `Closes #<n>` and the test summary.
  - [ ] Labels follow the transitions above in FakeGitHub.
- **Tests:** `tests/integration/test_deliver.py::test_pr_branch_single_commit_equals_scratch`, `::test_pr_body_fields`, `::test_label_transitions`
- **Files:** `src/longhaul/checkpoints/squash.py`, `src/longhaul/durable/activities/github.py`, `templates/pr_body.md.j2`, tests

#### LH-307 · Poller workflow on a Temporal Schedule
- **Epic:** E6 · **Points:** 5 · **Branch:** `feat/lh-307-poller` · **Depends on:** LH-303, LH-208
- **Story:** As the system, I want to find labeled issues every minute and start one task per issue, so that nobody has to start tasks by hand.
- **Build:**
  - `src/longhaul/durable/workflows/poller.py`: `PollerWorkflow.run()` calls activity `poll_once()` and returns. It is kept trivial and deterministic.
  - Activity `poll_once()`:
    1. For each allowlisted repo, list open issues with the label.
    2. For each one with no active task row, build the spec and `start_task` (handling already-started as a no-op).
    3. Insert or update the `tasks` row.
    4. Post the "Picked up" comment through the task workflow's first activity, not the poller. Only the task owns its side effects.
  - `longhaul poller install` creates or updates a Temporal Schedule `longhaul-poller` with interval `poller.interval_seconds` and overlap policy SKIP (from the spike). `longhaul poller pause|resume|trigger` manage it.
  - Write `docs/adr/0008-polling.md`.
- **Acceptance criteria:**
  - [ ] Given two labeled issues and one unlabeled, when `poll_once` runs twice, then exactly two TaskWorkflows exist.
  - [ ] Given an issue on a non-allowlisted repo with the label, then no task starts.
  - [ ] The schedule exists after `poller install`, and running install again changes nothing.
- **Tests:** `tests/integration/test_poller.py::test_starts_one_task_per_issue`, `::test_ignores_non_allowlisted`, `::test_schedule_install_idempotent`
- **Files:** `src/longhaul/durable/workflows/poller.py`, `src/longhaul/durable/activities/github.py`, `src/longhaul/cli.py`, ADR, tests

#### LH-308 · Comment commands and issue-to-PR e2e
- **Epic:** E6 · **Points:** 5 · **Branch:** `feat/lh-308-commands-e2e` · **Depends on:** LH-306, LH-307
- **Story:** As a maintainer, I want to steer a task with slash commands in the issue, so that I never need another tool to approve, hint or cancel.
- **Build:**
  - `src/longhaul/github/commands.py`: `parse_command(body) -> Command | None` for:
    - `/longhaul approve`
    - `/longhaul reject <feedback>`
    - `/longhaul hint <text>`
    - `/longhaul budget +<usd> [turns +<n>]`
    - `/longhaul cancel`
    - `/longhaul status`
    - `/longhaul retry`
  - Only the first line counts. The parser is case-insensitive and ignores code blocks.
  - In `poll_once`:
    - For each active task, fetch comments since `tasks.last_comment_at`.
    - Drop bot comments (marker) and users without write permission. Those users get a single polite idempotent reply per user per task: "Only maintainers can steer this task".
    - Map each command to a signal. `status` posts a status comment via the task (signal `report_status`).
    - `retry` on a finished task starts `lh-...-a2` (attempt suffix).
  - The e2e test uses the full stack with FakeGitHub on port 8099 and the scripted provider:
    1. Label the issue.
    2. Run the poller.
    3. The task runs.
    4. The PR appears.
    5. Assert exactly 1 PR, the expected comments and the expected labels.
- **Acceptance criteria:**
  - [ ] Each command parses, and junk returns None.
  - [ ] A `cancel` comment from a read-only user is ignored, and the task keeps running.
  - [ ] e2e: labeled issue to PR with one "Picked up" comment, one PR and `longhaul:pr-open`, in under 3 minutes in CI.
- **Tests:** `tests/unit/test_commands.py::test_parse_all`, `::test_ignores_code_blocks`, `tests/integration/test_poller.py::test_unauthorized_cancel_ignored`, `tests/e2e/test_issue_to_pr.py::test_label_to_pr_fake_github`
- **Files:** `src/longhaul/github/commands.py`, `src/longhaul/durable/activities/github.py`, `src/longhaul/durable/workflows/task.py`, tests

### Integration checkpoint 3

- **Cumulative tests:** `make gate PHASE=3`. That covers Phase 1 in-process e2e, Phase 2 durable e2e plus `chaos_lite`, and Phase 3 `test_label_to_pr_fake_github`.
- **Manual smoke (real GitHub, real model):**
  ```bash
  make up && uv run longhaul worker &
  uv run longhaul poller install
  gh issue edit 1 --repo hoomanesteki/lh-demo-calc --add-label longhaul
  sleep 90 && gh issue view 1 --repo hoomanesteki/lh-demo-calc --comments | tail -20
  gh pr list --repo hoomanesteki/lh-demo-calc
  gh issue comment 1 --repo hoomanesteki/lh-demo-calc --body "/longhaul status"
  ```

### Phase gate 3

- [ ] All 8 stories Done. ADR-0008, 0009 and 0012 merged.
- [ ] I4 and I5 hold. Idempotency tests pass, including DB-loss.
- [ ] Real-GitHub smoke produced exactly one PR on `lh-demo-calc`, with a screenshot in the gate report.
- [ ] Tag `v0.3.0`.

### Defend it

1. **Why the hidden marker in comments if you already have a `side_effects` table?** The table can be lost or rolled back. GitHub is the source of truth for whether the comment exists, so the marker lets a retry find it there.
2. **Why push from the host instead of the sandbox?** The sandbox runs untrusted repo code. If it had a token, any test file could exfiltrate it. The host pushes a branch it built itself.
3. **How do you stop strangers from steering the agent?** Commands are honoured only from users with write permission, checked through the collaborator permission API on every command.
4. **Why does the poller not post comments itself?** Each task workflow owns its own side effects and idempotency keys. If the poller posted too, the two could race and double-post.

---

## Phase 4: Context engineering (tag v0.4.0)

**Goal:** tasks run far past the context window without losing the thread. Large outputs become handles, old outputs are elided, and older turns become structured progress notes. Tokens and cache use are measured.
**Demo at the end:** the `longlog` fixture runs 60+ turns with an 8k-token test window. The trace shows 3+ compactions, the transcript never crosses the hard limit, and the task finishes.
**Entry criteria:** Phase 3 gate passed.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-401 | Token meter and context budget | 3 | E7 |
| LH-402 | Output handles and read_output | 3 | E7 |
| LH-403 | Structured compaction | 5 | E7 |
| LH-404 | Eviction and compaction in the workflow | 3 | E7 |
| LH-405 | Prompt layout and cache breakpoints | 3 | E7 |
| LH-406 | Context metrics and ablation flags | 2 | E7 |
| LH-407 | Long-task test | 3 | E7 |

#### LH-401 · Token meter and context budget
- **Epic:** E7 · **Points:** 3 · **Branch:** `feat/lh-401-token-meter` · **Depends on:** LH-104
- **Story:** As the loop, I want a cheap and fairly accurate token count before each call, so that I compact before the provider rejects the request.
- **Build:**
  - `src/longhaul/context/tokens.py`:
    - `estimate_tokens(text) -> int` returns `ceil(len(text) / 3.5)`.
    - `TokenMeter`:
      - `observe(usage: Usage, request_chars: int)` calibrates a chars-per-token ratio from real responses, using an EMA with alpha 0.3.
      - `projected(system, messages, tools) -> int`.
      - `fraction(window) -> float`.
    - `ContextBudget(window, compact_at, hard_limit)`: `decide(projected) -> Literal["ok", "compact", "force"]`.
  - If ADR-0003 confirmed `count_tokens`, call it only when `fraction` is between `compact_at - 0.05` and `hard_limit`, where precision matters.
- **Acceptance criteria:**
  - [ ] After 3 observations, the projection is within 10% of the true count on recorded Anthropic usage samples (fixtures from the spike).
  - [ ] `decide` returns compact at 0.61 and force at 0.86.
- **Tests:** `tests/unit/test_tokens.py::test_calibration_within_10pct`, `::test_budget_decisions`
- **Files:** `src/longhaul/context/tokens.py`, `tests/fixtures/usage_samples.json`, tests

#### LH-402 · Output handles and read_output
- **Epic:** E7 · **Points:** 3 · **Branch:** `feat/lh-402-output-handles` · **Depends on:** LH-204
- **Story:** As the agent, I want huge outputs replaced by a head, a tail and a handle I can page through, so that one noisy test run doesn't eat my context.
- **Build:**
  - `src/longhaul/context/truncate.py`: `shape_output(full: str, max_tokens, handle) -> str`. It keeps the head (60 lines) and tail (60 lines), and adds the marker `[output truncated: <lines> lines, <bytes>. Full text: handle <handle>. Use read_output to page or filter.]`.
  - `tool_exec` stores every full output in blob. If the output is over `tool_output_max_tokens`, the model sees the shaped version and `ToolResult.output_ref` is set. The handle is `out_<call_id>`.
  - New tool `read_output(handle, start_line=1, end_line=None, pattern=None)` in `src/longhaul/tools/output.py`. It reads from the blob store, not the sandbox, returns at most 400 lines, and with `pattern` returns matching lines with line numbers. It is registered for all roles.
  - The Phase 1 20k char cap is replaced by this.
- **Acceptance criteria:**
  - [ ] A 10,000-line output becomes a shaped result under the token cap, with a valid handle.
  - [ ] `read_output(handle, pattern="FAILED")` returns exactly the failing lines.
  - [ ] An unknown handle returns `ok=False`.
- **Tests:** `tests/unit/test_truncate.py::test_shape_large_output`, `tests/integration/test_read_output.py::test_page_and_filter`, `::test_unknown_handle`
- **Files:** `src/longhaul/context/truncate.py`, `src/longhaul/tools/output.py`, `src/longhaul/durable/activities/tools.py`, tests

#### LH-403 · Structured compaction
- **Epic:** E7 · **Points:** 5 · **Branch:** `feat/lh-403-compaction` · **Depends on:** LH-401
- **Story:** As the agent, I want older turns summarized into structured notes that keep what matters, so that I remember what I tried after the raw turns are gone.
- **Build:**
  - `src/longhaul/context/compact.py`:
    - `find_cut(messages, keep_recent_turns) -> int` returns an index such that `messages[cut]` is an assistant message and every `tool_use` before the cut has its `tool_result` before the cut.
    - `render_for_compaction(messages[:cut]) -> str` is a compact text form: tool calls as one line each, outputs as their first 20 lines or the shaped version.
    - `async compact(provider, model, notes_prev, rendered) -> ProgressNotes`. The compactor model is forced to call tool `record_progress_notes` whose `input_schema` is the `ProgressNotes` JSON schema. The prompt tells it to:
      - keep exact file paths, test names, error messages and commands
      - merge with the previous notes
      - never drop a "tried_and_failed" entry
    - `apply(messages, cut, notes) -> list[Message]` returns `[user: task statement + rendered notes] + messages[cut:]`.
  - Validation failures get one repair attempt (F14). After that, fall back to keeping the previous notes and cutting half as much.
- **Acceptance criteria:**
  - [ ] `find_cut` never splits a `tool_use` from its `tool_result` (property test over random transcripts with hypothesis).
  - [ ] Given scripted compactor output, `apply` yields a transcript that passes the pairing validator and starts with a user message.
  - [ ] Notes from round 1 survive round 2 (merge test: a `tried_and_failed` item persists).
- **Tests:** `tests/unit/test_compact.py::test_find_cut_property`, `::test_apply_valid_transcript`, `::test_notes_merge_keeps_failures`, `::test_repair_then_fallback`
- **Files:** `src/longhaul/context/compact.py`, tests

#### LH-404 · Eviction and compaction in the workflow
- **Epic:** E7 · **Points:** 3 · **Branch:** `feat/lh-404-context-in-workflow` · **Depends on:** LH-402, LH-403
- **Story:** As the workflow, I want the two context tiers applied automatically, so that long tasks never hit the provider limit.
- **Build:**
  - Keep two blobs per task: `log` (append-only full transcript, for reports) and `view` (what the model sees). `llm_turn` appends to both.
  - Tier 1, in `context/evict.py`: before each `llm_turn`, inside the activity, replace `tool_result` content older than `elide_tool_outputs_after_turns` with `[elided: <tool> output, <n> lines, handle <h>]`. This is deterministic and cheap.
  - Tier 2: if `ContextBudget.decide` says compact, the workflow calls the `compact` activity, which updates `view` and `state.notes` and records a `compaction` event with tokens before and after. On force, or on `ProviderError(CONTEXT_TOO_LONG)`, it compacts with `keep_recent_turns=2` and retries the turn once (F6).
  - Write `docs/adr/0013-context-strategy.md`.
- **Acceptance criteria:**
  - [ ] With window 8,000 tokens, no `llm_turn` request exceeds `hard_limit * window` in the long-task test (assert on recorded projections).
  - [ ] A forced `CONTEXT_TOO_LONG` from the scripted provider triggers compaction and one retry, and the run continues.
  - [ ] The `log` blob holds every turn, and `view` holds the elided and compacted form.
- **Tests:** `tests/integration/test_compaction.py::test_never_exceeds_hard_limit`, `::test_forced_compaction_on_overflow`, `::test_log_and_view_split`
- **Files:** `src/longhaul/context/evict.py`, `src/longhaul/durable/activities/{llm,context}.py`, `src/longhaul/durable/workflows/task.py`, ADR, tests

#### LH-405 · Prompt layout and cache breakpoints
- **Epic:** E7 · **Points:** 3 · **Branch:** `feat/lh-405-prompt-cache` · **Depends on:** LH-404
- **Story:** As the cost owner, I want a stable prompt prefix with cache breakpoints, so that repeated turns re-use cached tokens.
- **Build:**
  - The system prompt is built from fixed blocks in a fixed order:
    1. role and rules (static per role)
    2. tool usage guide (static)
    3. task statement (static per task)
    4. memory block (static per task, filled in Phase 6)
    5. plan (changes on step updates)
    6. progress notes (change on compaction)
  - Breakpoints:
    - after block 4 (stable for the whole task)
    - on the last tool definition
    - on the last content block of the previous assistant turn (incremental conversation caching)
  - Use at most 4 breakpoints, the API limit confirmed in the spike.
  - Add `cache_ratio = cache_read / (input + cache_read)` to the turn record.
- **Acceptance criteria:**
  - [ ] A rendering test confirms the block order and that the first four blocks are byte-identical across turns of the same task.
  - [ ] With a live key (opt-in), turn 3+ of a task shows `cache_read_tokens > 0`.
- **Tests:** `tests/unit/test_prompt_layout.py::test_stable_prefix`, `::test_breakpoints_count_le_4`, `tests/live/test_cache_live.py::test_cache_hits_after_turn_2`
- **Files:** `src/longhaul/agent/prompts.py`, `src/longhaul/providers/anthropic.py`, tests

#### LH-406 · Context metrics and ablation flags
- **Epic:** E7 · **Points:** 2 · **Branch:** `feat/lh-406-context-flags` · **Depends on:** LH-404
- **Story:** As the evaluator, I want to switch context features off per run and see token numbers per task, so that Phase 9 can measure what compaction buys.
- **Build:**
  - Config `context.mode` takes `full` (both tiers), `no_compaction` (tier 1 only) or `none` (no elision, no compaction, hard limit still enforced by failing the task with a clear outcome). It can be overridden per run (`longhaul run --context-mode`).
  - Per task, write totals into `tasks`: `input_tokens_total`, `output_tokens_total`, `cache_read_total`, `compactions`, `max_fraction`.
- **Acceptance criteria:**
  - [ ] Each mode runs the `calc` scripted task, and the totals are written.
  - [ ] `none` on the long-task fixture ends with outcome `error`, summary "context limit", and does not crash.
- **Tests:** `tests/integration/test_context_modes.py::test_modes_run`, `::test_none_mode_hits_limit_cleanly`
- **Files:** `src/longhaul/settings.py`, `src/longhaul/durable/workflows/task.py`, migration, tests

#### LH-407 · Long-task test
- **Epic:** E7 · **Points:** 3 · **Branch:** `feat/lh-407-long-task` · **Depends on:** LH-404, LH-405
- **Story:** As the reviewer, I want one test that forces many compactions, so that the context system is proven under pressure.
- **Build:**
  - Fixture repo `tests/fixtures/repos/longlog`:
    - 12 small modules, each with a planted bug.
    - The test suite prints 5,000 lines of noise.
    - The scripted task fixes one module per ~5 turns, about 60 turns total.
  - Script `tests/fixtures/scripts/longlog.yaml` drives the coder.
  - `tests/fixtures/scripts/compactor.yaml` returns notes that list fixed modules so far. The scripted coder steps use `match: last_tool_result_contains` so they react to elided outputs and handles.
  - Config for the test: window 8,000, `compact_at` 0.6, `keep_recent_turns` 4.
- **Acceptance criteria:**
  - [ ] The task finishes with all 12 modules fixed.
  - [ ] At least 3 compactions ran, and `max_fraction < 0.85`.
  - [ ] The notes after the last compaction still list modules fixed in the first 10 turns.
- **Tests:** `tests/e2e/test_long_task.py::test_longlog_many_compactions` (marker `e2e, phase4`)
- **Files:** `tests/fixtures/repos/longlog/**`, `tests/fixtures/scripts/{longlog,compactor}.yaml`, tests

### Integration checkpoint 4

- **Cumulative tests:** `make gate PHASE=4`. That covers phases 0 to 4, including `test_label_to_pr_fake_github`, `chaos_lite` and `test_longlog_many_compactions`.
- **Manual smoke:**
  ```bash
  uv run longhaul run --path tests/fixtures/repos/longlog --issue-file tests/fixtures/repos/longlog/ISSUE.md \
      --provider scripted --script tests/fixtures/scripts/longlog.yaml --window 8000
  uv run longhaul status <task_id>     # shows compactions and max_fraction
  # real model, real repo:
  gh issue edit 1 --repo hoomanesteki/lh-demo-inventory --add-label longhaul
  ```

### Phase gate 4

- [ ] All 7 stories Done. ADR-0013 merged.
- [ ] The long-task e2e passes, and Phase 3 e2e still passes.
- [ ] The live cache test was run once by Hooman, with `cache_ratio` noted in the gate report.
- [ ] Tag `v0.4.0`.

### Defend it

1. **Why structured notes instead of a free-text summary?** Free text drifts and drops specifics. A schema forces the compactor to keep file paths, failing tests and what already failed, and merging is mechanical.
2. **Why two tiers?** Eliding old tool outputs is free and removes most of the bulk, because tool output dominates tokens in coding tasks. LLM compaction costs a call and can lose detail, so it runs only when tier 1 isn't enough.
3. **What breaks if you cut in the wrong place?** The Messages API rejects a `tool_result` without its `tool_use`, and vice versa. `find_cut` is property-tested so that never happens.
4. **Doesn't compaction hurt caching?** It invalidates the part of the prefix after the notes once per compaction. The static blocks stay cached. We measure `cache_ratio` and tokens per task to show the net effect.

---

## Phase 5: Planner, verifier and stuck handling (tag v0.5.0)

**Goal:** the agent plans before coding, checks its own work before delivering, notices when it is stuck, and asks a specific question. Every task ends in a verified PR, a stuck report or a budget pause, never silence.
**Demo at the end:**
1. On `lh-demo-inventory` (plan approval on), the agent posts a plan.
2. Hooman replies `/longhaul approve`, and the agent codes.
3. The Verifier runs the tests and sends the work back once.
4. The agent fixes it, and the PR opens as ready for review.

**Entry criteria:** Phase 4 gate passed.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-501 | Planner role and plan schema | 5 | E8 |
| LH-502 | Human plan gate | 3 | E9 |
| LH-503 | Coder role with step tracking | 5 | E8 |
| LH-504 | Verifier role and send-back loop | 5 | E8 |
| LH-505 | Stuck detector | 5 | E9 |
| LH-506 | Pause, ask and hint flow | 3 | E9 |
| LH-507 | Final outcomes and stuck report | 3 | E8 |

#### LH-501 · Planner role and plan schema
- **Epic:** E8 · **Points:** 5 · **Branch:** `feat/lh-501-planner` · **Depends on:** LH-404 · **Reviewer sign-off**
- **Story:** As a reviewer, I want the agent to write a plan with steps and checks before touching code, so that I can catch a wrong approach early.
- **Build:**
  - `src/longhaul/roles/planner.py`:
    - The role prompt tells the planner to read the issue, explore, reproduce the failure if possible, and produce 2 to 8 steps, each with `done_when`, plus `acceptance_checks` (runnable commands first).
    - Tool subset: `list_dir`, `read_file`, `search`, `run_tests`, `run_command`, `read_output`. Mutations are disallowed: edit/write are not offered, and any tree change after a planner turn is reset to `head_sha`.
    - Control tool `submit_plan` with input schema = `Plan`. The planner gets at most `planner_max_turns: 20` turns.
  - Workflow:
    1. Phase `PLANNING` runs before `CODING`.
    2. Store the plan in the `plans` table (version 1) and in `state.plan`.
    3. Post the plan comment (key `plan_comment:v<version>`), rendered as a checklist.
  - Write `docs/adr/0007-roles-in-one-workflow.md`.
- **Acceptance criteria:**
  - [ ] With the scripted planner, the plan is stored, posted once and visible in `status()`.
  - [ ] A planner `run_command` that writes a file leaves the tree clean before coding starts.
  - [ ] An invalid plan triggers one repair turn, and a second failure ends with an `error` outcome and a clear summary.
- **Tests:** `tests/integration/test_planner.py::test_plan_stored_and_posted`, `::test_planner_cannot_mutate`, `tests/unit/test_roles.py::test_plan_repair_turn`
- **Files:** `src/longhaul/roles/planner.py`, `src/longhaul/durable/workflows/task.py`, `templates/plan_comment.md.j2`, ADR, migration, tests

#### LH-502 · Human plan gate
- **Epic:** E9 · **Points:** 3 · **Branch:** `feat/lh-502-plan-gate` · **Depends on:** LH-501, LH-308
- **Story:** As a maintainer, I want to approve or reject the plan on repos where I care, so that the agent doesn't spend an hour on the wrong idea.
- **Build:**
  - If `plan_approval_required`:
    1. Set phase `AWAITING_PLAN_APPROVAL` and add the label `longhaul:needs-human`.
    2. `await workflow.wait_condition(lambda: s.plan_approved or s.plan_rejected or s.cancelled, timeout=hint_wait_hours)`.
  - `reject_plan(feedback)`:
    - The planner runs again with the feedback added as a user message, producing version + 1. At most 3 versions.
    - After that, end with outcome `stuck` ("plan not approved").
  - Approval records `decided_by` in `plans`. On timeout, end with outcome `stuck`.
- **Acceptance criteria:**
  - [ ] In the time-skipping env, approve after 2 h proceeds to coding, reject then approve yields plan v2, and no signal for 24 h ends with outcome `stuck`.
  - [ ] Repos with the gate off go straight to coding.
- **Tests:** `tests/integration/test_plan_gate.py::test_approve_proceeds`, `::test_reject_revises`, `::test_timeout_stuck`, `::test_gate_off_skips`
- **Files:** `src/longhaul/durable/workflows/task.py`, tests

#### LH-503 · Coder role with step tracking
- **Epic:** E8 · **Points:** 5 · **Branch:** `feat/lh-503-coder-steps` · **Depends on:** LH-501
- **Story:** As the agent, I want to work the plan step by step and record progress, so that compaction, the Verifier and the PR all know which step is which.
- **Build:**
  - `src/longhaul/roles/coder.py`:
    - The role prompt includes the plan with statuses.
    - Control tools:
      - `update_step(step_id, status, note)`
      - `submit_for_review(summary)`, which replaces `finish` for the coder
      - `ask_human(question)`, which pauses (handled in LH-506)
  - On `update_step(..., "done")`, record a `kind=step` checkpoint at the current `head_sha` with that `step_id`.
  - `build_pr_branch` now groups commits by step: one squashed commit per done step, titled from the step description in commit style. Steps with no changes are skipped.
- **Acceptance criteria:**
  - [ ] A 3-step scripted run produces 3 step checkpoints and a PR branch with 3 commits whose combined diff equals the scratch head diff.
  - [ ] Plan statuses show in the system prompt and in `status()`.
- **Tests:** `tests/integration/test_coder_steps.py::test_step_checkpoints`, `::test_pr_commits_per_step`, `tests/unit/test_squash.py::test_group_by_step`
- **Files:** `src/longhaul/roles/coder.py`, `src/longhaul/checkpoints/squash.py`, tests

#### LH-504 · Verifier role and send-back loop
- **Epic:** E8 · **Points:** 5 · **Branch:** `feat/lh-504-verifier` · **Depends on:** LH-503
- **Story:** As a reviewer, I want an independent check before any PR is marked ready, so that "tests pass" is verified, not claimed.
- **Build:**
  - On `submit_for_review`, the phase becomes `VERIFYING`:
    1. Run `test_command` through `tool_exec` (a deterministic pre-check) and run every acceptance check that is a command.
    2. Start a fresh verifier transcript with the plan, the diff (`git_diff`), the pre-check results, and read-only tools plus `run_tests`.
    3. Control tool `submit_verdict` with schema `VerifierVerdict`.
  - The work passes only if the pre-check passed and `verdict.passed` is true.
  - On fail:
    - `verify_rounds += 1`. Inject a user message to the coder: "Review failed: <issues>. Fix step <send_back_step>." Return to `CODING`.
    - At `max_verify_rounds`, end with outcome `stuck`, with reason and evidence.
  - A verified PR is created as ready, not draft. Include "Verified: <tests_summary>" in the PR body.
- **Acceptance criteria:**
  - [ ] Scripted scenario: the first review fails (planted missing edge case), the coder fixes it, the second review passes, and the PR is ready, not draft.
  - [ ] If the pre-check fails, a verdict of `passed=true` from the model still counts as fail.
  - [ ] Three failed rounds end with `stuck`.
- **Tests:** `tests/integration/test_verifier.py::test_send_back_then_pass`, `::test_precheck_overrides_model`, `::test_max_rounds_stuck`
- **Files:** `src/longhaul/roles/verifier.py`, `src/longhaul/durable/workflows/task.py`, `templates/pr_body.md.j2`, tests

#### LH-505 · Stuck detector
- **Epic:** E9 · **Points:** 5 · **Branch:** `feat/lh-505-stuck-detector` · **Depends on:** LH-503
- **Story:** As an engineering manager, I want the agent to notice when it is going in circles, so that a person is pulled in after minutes, not hours.
- **Build:**
  - `src/longhaul/roles/stuck.py`. It is pure Python with no I/O, so it is safe to import in the workflow.
    - `TurnSignal` holds: `turn_index`, `tests_passed: int | None`, `steps_done: int`, `failed_cmd_hash: str | None` (sha1 of the command, exit code and the last 20 lines of stderr), `edited_regions: list[(path, start, end)]` and `tool_errors: int`. Activities return these small fields.
    - `StuckDetector(cfg)` keeps a bounded window of the last 30 signals in `CarryState` and has `observe(sig) -> StuckSignal | None`.
  - Rules:
    - `NO_PROGRESS`: no increase in `tests_passed` and no new step done in `no_progress_turns`.
    - `REPEATED_FAILURE`: the same `failed_cmd_hash` `repeated_failure_count` times in a row.
    - `EDIT_CHURN`: the same file region (overlap of 50% or more) edited `churn_edits` times in the last 10 turns with no test change.
    - `TOOL_ERRORS`: `consecutive_tool_errors` in a row.
  - Evidence strings are short and specific ("pytest tests/test_inv.py failed 3 times with the same KeyError at inv.py:42").
- **Acceptance criteria:**
  - [ ] Each rule has a positive and a negative unit test.
  - [ ] The detector state serializes to under 8 KB at a full window.
  - [ ] It doesn't fire during the planning phase.
- **Tests:** `tests/unit/test_stuck.py::test_no_progress[pos|neg]`, `::test_repeated_failure[pos|neg]`, `::test_edit_churn[pos|neg]`, `::test_tool_errors[pos|neg]`, `::test_state_size_bound`
- **Files:** `src/longhaul/roles/stuck.py`, `src/longhaul/durable/activities/tools.py` (emit signals), tests

#### LH-506 · Pause, ask and hint flow
- **Epic:** E9 · **Points:** 3 · **Branch:** `feat/lh-506-pause-hint` · **Depends on:** LH-505, LH-308
- **Story:** As a maintainer, I want one clear question when the agent is stuck and a way to answer in one comment, so that unblocking takes seconds.
- **Build:**
  - On a `StuckSignal` or `ask_human`:
    1. Call activity `summarize_for_human`, a compactor-model call that produces: what I'm trying to do, what I tried (3 to 5 bullets), my current guess, and one specific question.
    2. Post the comment (key `stuck:<n>`) and add `longhaul:needs-human`.
    3. Set phase `PAUSED` and wait for `hint`, `add_budget` or `cancel` with a timeout.
  - On `hint`:
    1. Append a user message "Hint from @<user>: <text>".
    2. Reset the detector window and remove the label.
    3. Record a `resumed` event. `stuck_count += 1`.
  - After 3 pauses, or on timeout, end with outcome `stuck`.
- **Acceptance criteria:**
  - [ ] e2e on the scripted provider: a planted loop triggers `REPEATED_FAILURE`, the comment is posted once, the hint is signalled via `/longhaul hint`, the task resumes and finishes.
  - [ ] The comment has all four sections and is under 250 words.
- **Tests:** `tests/e2e/test_stuck_flow.py::test_stuck_pause_hint_resume`, `tests/unit/test_templates.py::test_stuck_comment_sections`
- **Files:** `src/longhaul/durable/workflows/task.py`, `src/longhaul/durable/activities/context.py`, `templates/stuck_comment.md.j2`, tests

#### LH-507 · Final outcomes and stuck report
- **Epic:** E8 · **Points:** 3 · **Branch:** `feat/lh-507-outcomes` · **Depends on:** LH-504, LH-506
- **Story:** As the product owner, I want every task to end in one of a few explicit outcomes with a report, so that there are no silent failures.
- **Build:**
  - The workflow wraps the main body in a `try` / `finally`-style structure that always:
    1. Sets `Outcome`.
    2. Posts the final comment: the PR link, or the stuck report with what was done, what is left and the diff stats. If `open_draft_on_stuck` is set and the diff is non-empty, it also opens a draft PR with the partial work.
    3. Sets the final labels.
    4. Destroys the sandbox.
    5. Schedules bare repo cleanup after 7 days (a janitor activity on the poller schedule).
  - Unexpected exceptions become outcome `error` with a short summary, and the full trace goes to the `events` table.
- **Acceptance criteria:**
  - [ ] For each of `pr_opened`, `stuck`, `cancelled` and `error`, the scripted tests show exactly one final comment and the right labels.
  - [ ] No sandbox container remains after any outcome.
- **Tests:** `tests/integration/test_outcomes.py::test_each_outcome_reports_once[pr_opened|stuck|cancelled|error]`, `::test_no_leftover_containers`
- **Files:** `src/longhaul/durable/workflows/task.py`, `templates/final_comment.md.j2`, tests

### Integration checkpoint 5

- **Cumulative tests:** `make gate PHASE=5`. That covers phases 0 to 5, including the issue-to-PR e2e (now through planner, coder and verifier), the long task, `chaos_lite` and the stuck flow e2e.
- **Manual smoke (real model):**
  ```bash
  # lh-demo-inventory has plan_approval_required: true
  gh issue edit 1 --repo hoomanesteki/lh-demo-inventory --add-label longhaul
  # wait for plan comment, then:
  gh issue comment 1 --repo hoomanesteki/lh-demo-inventory --body "/longhaul approve"
  # watch Temporal UI; expect VERIFYING, maybe one send-back, then a ready PR
  gh pr view --repo hoomanesteki/lh-demo-inventory --json isDraft,title,body | head -40
  ```

### Phase gate 5

- [ ] All 7 stories Done. ADR-0007 merged.
- [ ] Every outcome path is tested, with no leftover containers.
- [ ] The real-model smoke produced a plan comment, an approval, a verified ready PR, with links in the gate report.
- [ ] Tag `v0.5.0`.

### Defend it

1. **Why a fresh transcript for the Verifier?** If it reads the coder's whole history, it inherits the coder's assumptions. A fresh look at the plan, diff and test results catches more.
2. **Why does a failing pre-check override the model's verdict?** Tests are ground truth. The model reviews code quality and plan coverage; it doesn't get to decide that failing tests are fine.
3. **How do you avoid false "stuck" alarms?** Each rule needs specific evidence (the same failing command hash, the same edited region, no test movement), and thresholds are config. Phase 9 reports the stuck rate and how often a hint actually unblocked the task.
4. **Why roles in one workflow and not separate agents?** They share one sandbox and one workspace history. Separate child workflows would add handoffs and transcript plumbing without a benefit at this scale.

---

## Phase 6: Memory (tag v0.6.0)

**Goal:** the agent starts each task already knowing the repo: a map of modules and symbols, the commands that work, and lessons from past merged work. Nothing from unmerged or unreviewed work can enter that memory.
**Demo at the end:**
1. Run two tasks on `lh-demo-inventory`.
2. The first PR is merged, and its lesson ("use `InventoryError`, not `ValueError`") is promoted.
3. The second task's prompt shows the lesson, and the agent follows it.

**Entry criteria:** Phase 5 gate passed.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-601 | Embedder and memory schema | 3 | E10 |
| LH-602 | Repo map builder | 5 | E10 |
| LH-603 | Memory store API and verified commands | 3 | E10 |
| LH-604 | Retrieval into the prompt | 3 | E10 |
| LH-605 | Lesson extraction | 3 | E10 |
| LH-606 | Promotion on merged PRs and human approval | 3 | E10 |
| LH-607 | Memory poisoning test and ablation flag | 2 | E10 |

#### LH-601 · Embedder and memory schema
- **Epic:** E10 · **Points:** 3 · **Branch:** `feat/lh-601-memory-schema` · **Depends on:** LH-008
- **Story:** As the memory store, I want a table with vectors and a local embedder, so that retrieval works offline and in CI.
- **Build:**
  - Migration: the `memory_items` table from section 3.5, with `embedding vector(384)` and an HNSW index (`vector_cosine_ops`). If the pinned pgvector lacks HNSW, use ivfflat and note it in ADR-0002.
  - `src/longhaul/memory/embedder.py`:
    - `LocalEmbedder`: a small 384-dim model such as `BAAI/bge-small-en-v1.5` via `fastembed` or `sentence-transformers`. Pick one, record it in ADR-0010, and load lazily.
    - `HashEmbedder(dim=384)`: deterministic hashing trick, for unit tests and CI.
  - Write `docs/adr/0010-memory-write-policy.md` (the rules in section 3.4).
- **Acceptance criteria:**
  - [ ] Inserting and nearest-neighbour querying work in Postgres with `HashEmbedder`.
  - [ ] `LocalEmbedder` returns 384-dim unit vectors (opt-in test, marker `slow`).
- **Tests:** `tests/integration/test_memory_schema.py::test_insert_and_knn`, `tests/unit/test_embedder.py::test_hash_embedder_deterministic`, `tests/slow/test_local_embedder.py::test_dim`
- **Files:** migration, `src/longhaul/memory/embedder.py`, ADR, tests

#### LH-602 · Repo map builder
- **Epic:** E10 · **Points:** 5 · **Branch:** `feat/lh-602-repo-map` · **Depends on:** LH-206
- **Story:** As the agent, I want a short map of the repo's modules and key symbols, so that I spend fewer turns exploring.
- **Build:**
  - `src/longhaul/memory/repo_map_script.py` is a standalone script that is copied into the sandbox and run there, because it parses repo code.
    - Parse every `*.py` under 200 KB with `ast`, collecting the module path, top-level classes and functions with signatures, and the first docstring line.
    - Build an import graph and rank modules by in-degree plus a boost for name overlap with the issue text.
    - Output JSON.
  - `src/longhaul/memory/repo_map.py`:
    - `build_repo_map(sandbox, issue_text, max_tokens=3000) -> str` renders the top-ranked modules until the budget is reached (aider-style).
    - The map of the default branch is cached as an `approved` memory item, `kind=repo_map`, `source=default_branch`, `source_ref=<sha>`, and rebuilt only when the default branch sha changes.
  - Call it in `prepare_workspace` at `base_sha`, before any agent edit.
- **Acceptance criteria:**
  - [ ] On the `inventory` fixture, the map lists `Inventory.remove` with its signature and stays under 3,000 tokens.
  - [ ] A second task on the same base sha reuses the cached map (no rebuild, checked via a counter).
  - [ ] Syntax errors in one file don't break the map (the file is skipped and logged).
- **Tests:** `tests/integration/test_repo_map.py::test_inventory_map`, `::test_cached_by_sha`, `::test_syntax_error_skipped`
- **Files:** `src/longhaul/memory/{repo_map,repo_map_script}.py`, `src/longhaul/durable/activities/workspace.py`, tests

#### LH-603 · Memory store API and verified commands
- **Epic:** E10 · **Points:** 3 · **Branch:** `feat/lh-603-memory-store` · **Depends on:** LH-601
- **Story:** As the system, I want one API for memory and auto-verified build and test commands, so that the agent never guesses how to run the tests.
- **Build:**
  - `src/longhaul/memory/store.py`: `PgMemoryStore` implementing `MemoryStore`. It de-duplicates on `(repo, kind, normalized content hash)`, and `uses` / `last_used_at` are updated on retrieval.
  - Command discovery in `prepare_workspace`:
    1. Detect candidates from `pyproject.toml` (pytest config), `tox.ini`, `Makefile` targets (`test`, `lint`) and `.github/workflows/*.yml` steps containing `pytest`, `ruff` or `tox`.
    2. Run each candidate once at `base_sha` in the sandbox. "Works" means it ran and printed a recognizable summary. Tests may fail, since the issue may be a failing test.
    3. Store the ones that work as `approved`, `kind=command`, `source=default_branch`.
  - CLI: `longhaul memory list --repo`, `longhaul memory approve|reject ID`.
- **Acceptance criteria:**
  - [ ] On the `calc` fixture, `pytest -q` is discovered and stored as approved, and a bogus `make test` with no Makefile is not stored.
  - [ ] Duplicates are not inserted twice.
- **Tests:** `tests/integration/test_memory_store.py::test_dedupe`, `tests/integration/test_command_discovery.py::test_calc_commands`
- **Files:** `src/longhaul/memory/store.py`, `src/longhaul/durable/activities/workspace.py`, `src/longhaul/cli.py`, tests

#### LH-604 · Retrieval into the prompt
- **Epic:** E10 · **Points:** 3 · **Branch:** `feat/lh-604-memory-retrieval` · **Depends on:** LH-602, LH-603, LH-405
- **Story:** As the agent, I want relevant repo knowledge in my prompt from the first turn, so that I don't repeat past mistakes.
- **Build:**
  - `src/longhaul/memory/retrieve.py`: `render_memory_block(repo, issue_text, budget_tokens=4000) -> str`. It includes:
    - the repo map
    - approved commands
    - the top 5 approved `lesson` / `pitfall` / `convention` items by cosine similarity to the issue title and body
  - The block is wrapped with the header "Notes from past work on this repo. Treat them as hints and verify before relying on them." The content goes inside a fenced block, so it reads as data, not instructions.
  - Activity `load_memory` returns a `BlobRef`. The prompt builder puts it in block 4 (cached).
  - `test_command` falls back to the first approved test command if repo settings don't set one.
- **Acceptance criteria:**
  - [ ] The block never includes `candidate` or `rejected` items (I7).
  - [ ] The block stays under budget with 50 approved items present.
  - [ ] The system prompt has the memory block in position 4, so the cached prefix stays stable.
- **Tests:** `tests/unit/test_memory_retrieve.py::test_only_approved_in_prompt`, `::test_budget_respected`, `tests/unit/test_prompt_layout.py::test_memory_block_position`
- **Files:** `src/longhaul/memory/retrieve.py`, `src/longhaul/durable/activities/memory.py`, `src/longhaul/agent/prompts.py`, tests

#### LH-605 · Lesson extraction
- **Epic:** E10 · **Points:** 3 · **Branch:** `feat/lh-605-lessons` · **Depends on:** LH-603, LH-507
- **Story:** As the system, I want short lessons drafted from each finished task, so that useful knowledge can be kept once a person confirms the work.
- **Build:**
  - Activity `extract_lessons(task_id)` runs after the outcome is set:
    1. Call the compactor model with the plan, the final notes, the stuck events and the verifier issues.
    2. Ask for at most 5 items, each under 300 chars, typed `lesson|pitfall|convention`, specific to this repo. The prompt states that generic advice is not allowed.
    3. Store them as `candidate` with `source=task`, `source_ref=<pr number or task id>`.
  - Write the lesson list into the run report.
- **Acceptance criteria:**
  - [ ] Scripted output produces candidates linked to the PR number.
  - [ ] Items over 300 chars are trimmed, and generic items matching a small blocklist ("write tests", "read the code") are dropped.
- **Tests:** `tests/integration/test_lessons.py::test_candidates_created`, `tests/unit/test_lessons.py::test_generic_filtered`
- **Files:** `src/longhaul/memory/lessons.py`, `src/longhaul/durable/activities/memory.py`, tests

#### LH-606 · Promotion on merged PRs and human approval
- **Epic:** E10 · **Points:** 3 · **Branch:** `feat/lh-606-memory-promotion` · **Depends on:** LH-605, LH-307
- **Story:** As the product owner, I want lessons promoted only when the work was accepted, so that memory reflects what reviewers approved.
- **Build:**
  - In `poll_once`, for tasks with `candidate` lessons:
    - Check `pr_merged`. If merged, promote with `approved_by="merged_pr#<n>"`.
    - If the PR is closed and unmerged, reject.
    - Stuck tasks without a PR keep their lessons as candidates until a person approves them in the CLI or console.
  - Record events `memory_promoted` and `memory_rejected`.
- **Acceptance criteria:**
  - [ ] In FakeGitHub, merging the PR promotes all its candidates on the next poll, and closing it unmerged rejects them.
  - [ ] Human approval via the CLI works for candidates without a PR.
- **Tests:** `tests/integration/test_memory_promotion.py::test_merged_promotes`, `::test_closed_rejects`, `::test_cli_approve`
- **Files:** `src/longhaul/durable/activities/github.py`, `src/longhaul/memory/store.py`, tests

#### LH-607 · Memory poisoning test and ablation flag
- **Epic:** E10 · **Points:** 2 · **Branch:** `feat/lh-607-memory-safety` · **Depends on:** LH-604, LH-606
- **Story:** As a security reviewer, I want proof that bad lessons can't reach the prompt, and a switch to turn memory off, so that memory is safe and measurable.
- **Build:**
  - Poisoning e2e:
    1. Task A (scripted) produces a lesson "Always delete failing tests".
    2. Its PR is closed unmerged.
    3. Task B on the same repo runs.
    4. Assert that task B's system prompt (from the `view` blob) doesn't contain the lesson.
  - Config `memory.mode: on|off`. With `off`, `load_memory` returns an empty block but verified commands still set `test_command`.
- **Acceptance criteria:**
  - [ ] The poisoning e2e passes.
  - [ ] `memory.mode=off` produces a system prompt with no memory block.
- **Tests:** `tests/e2e/test_memory_poisoning.py::test_unmerged_lesson_never_in_prompt`, `tests/integration/test_memory_modes.py::test_off_mode`
- **Files:** tests, `src/longhaul/settings.py`

### Integration checkpoint 6

- **Cumulative tests:** `make gate PHASE=6`. That covers all earlier e2e tests (issue to PR, long task, stuck flow) and `chaos_lite`, plus the memory poisoning e2e.
- **Manual smoke:**
  ```bash
  uv run longhaul memory list --repo hoomanesteki/lh-demo-inventory   # repo_map + commands approved
  # run task 1, merge its PR, wait one poll, then:
  uv run longhaul memory list --repo hoomanesteki/lh-demo-inventory --status approved --kind lesson
  # open a second issue, label it, check the memory block in the Temporal UI input or run report
  ```

### Phase gate 6

- [ ] All 7 stories Done. ADR-0010 merged.
- [ ] I7 holds, and the poisoning e2e passes.
- [ ] Earlier e2e tests still pass with memory on.
- [ ] Tag `v0.6.0`.

### Defend it

1. **How do you stop memory poisoning?** Only three sources can approve memory: the default branch, a merged PR, or a person. A task can only propose. The prompt also labels memory as hints to verify.
2. **Why a repo map instead of embedding the whole codebase?** For one repo, a ranked symbol map gives better orientation per token than chunk retrieval. The agent can still search and read files.
3. **Why verify commands by running them?** Detected commands are often wrong (a stale Makefile, a missing tox env). Running them once at base removes a whole class of early-turn failures.
4. **How will you know memory helps?** Phase 9 runs the custom set with memory on and off, and compares turns to first passing test, cost and resolve rate.

---

## Phase 7: Budgets, cost and observability (tag v0.7.0)

**Goal:** every dollar is ledgered, every task has hard limits that pause and ask, and every turn and tool call is visible in traces, metrics and a run report attached to the PR.
**Demo at the end:**
1. A task with a $0.50 budget pauses with a precise comment.
2. `/longhaul budget +1` resumes it.
3. Grafana shows cost per repo, and Jaeger shows the turn spans.
4. The PR body includes a collapsible run report.

**Entry criteria:** Phase 6 gate passed. `config/pricing.yaml` filled in from the provider's current pricing page by Hooman.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-701 | Pricing table and cost ledger | 3 | E11 |
| LH-702 | Budget enforcement and top-ups | 5 | E11 |
| LH-703 | OpenTelemetry traces | 5 | E12 |
| LH-704 | Prometheus metrics and Grafana dashboard | 3 | E12 |
| LH-705 | Run report | 3 | E12 |
| LH-706 | Structured logs and redaction | 2 | E12 |

#### LH-701 · Pricing table and cost ledger
- **Epic:** E11 · **Points:** 3 · **Branch:** `feat/lh-701-cost-ledger` · **Depends on:** LH-204
- **Story:** As an engineering manager, I want every model call priced and recorded, so that cost per task and per repo is exact.
- **Build:**
  - `src/longhaul/cost/pricing.py`: `Pricing.load(path)`. Each model has `input_per_mtok`, `output_per_mtok`, `cache_write_per_mtok` and `cache_read_per_mtok`.
    - `cost(usage, model) -> Decimal` returns `(in*pi + out*po + cw*pcw + cr*pcr) / 1e6`.
    - Worker startup fails if a configured paid model has no price. Scripted and replay providers cost 0.
  - `src/longhaul/cost/ledger.py`: `record(task_id, repo, model, turn_index, attempt, usage) -> Decimal`. It is called inside every activity that calls a provider (llm_turn, compact, summarize_for_human, extract_lessons), even when the attempt later fails after the call returned. It updates `tasks.cost_usd`.
  - Migration: the `cost_ledger` table.
- **Acceptance criteria:**
  - [ ] Cost math matches hand-computed values for 3 usage samples.
  - [ ] Every provider call in the scripted e2e has a ledger row (I6), including compaction calls.
  - [ ] A retried `llm_turn` produces two ledger rows with attempts 1 and 2.
- **Tests:** `tests/unit/test_pricing.py::test_cost_math`, `::test_missing_price_fails_startup`, `tests/integration/test_cost_ledger.py::test_every_call_ledgered`, `::test_retry_ledgered_twice`
- **Files:** `src/longhaul/cost/{pricing,ledger}.py`, migration, tests

#### LH-702 · Budget enforcement and top-ups
- **Epic:** E11 · **Points:** 5 · **Branch:** `feat/lh-702-budgets` · **Depends on:** LH-701, LH-506
- **Story:** As a platform engineer, I want hard limits that pause the task and ask, so that no task can run up a surprise bill.
- **Build:**
  - Activity `check_budget(task_id, repo, limits, projected_input_tokens, max_tokens, model)`. It computes `projected = in_tokens*pi + max_tokens*po` (worst case) and returns `{ok, reason, numbers}`. Reasons:
    - `task_usd`: `spent_task + projected > task_usd + extra_usd`
    - `task_turns`: `turn_index >= task_turns + extra_turns`
    - `wall_clock`: `workflow.now() - started_at > task_wall_minutes`. This one is computed in the workflow, not the activity.
    - `daily_repo_usd`: ledger sum for the repo since 00:00 UTC, plus projected
  - The workflow calls it before every `llm_turn`. If it is not ok:
    1. Set phase `PAUSED` and post a budget comment (key `budget:<n>`) with the numbers and exact commands (`/longhaul budget +2`, `/longhaul cancel`).
    2. For task limits: wait for `add_budget` or `cancel` up to `hint_wait_hours`, then end with outcome `budget_exhausted`.
    3. For the daily repo limit: sleep on a durable timer until 00:00 UTC, then re-check. The comment says so.
- **Acceptance criteria:**
  - [ ] Each reason triggers a pause with a correct comment (time-skipping env).
  - [ ] `add_budget(usd=1)` resumes, and the new limit is used.
  - [ ] The daily limit waits until midnight UTC and then continues without a signal.
  - [ ] No `llm_turn` starts while paused (assert from history).
- **Tests:** `tests/integration/test_budgets.py::test_task_usd_pause_and_topup`, `::test_turn_limit_pause`, `::test_wall_clock_pause`, `::test_daily_waits_until_midnight`, `tests/e2e/test_budget_flow.py::test_pause_and_top_up`
- **Files:** `src/longhaul/cost/budget.py`, `src/longhaul/durable/activities/cost.py`, `src/longhaul/durable/workflows/task.py`, `templates/budget_comment.md.j2`, tests

#### LH-703 · OpenTelemetry traces
- **Epic:** E12 · **Points:** 5 · **Branch:** `feat/lh-703-otel` · **Depends on:** LH-204
- **Story:** As an operator, I want one trace per task with a span per turn and tool call, so that I can see where time and tokens went.
- **Build:**
  - `src/longhaul/telemetry/otel.py`:
    - Configure the tracer provider with an OTLP exporter to the collector.
    - Register Temporal's OpenTelemetry interceptor (`temporalio.contrib.opentelemetry`) on the client and worker, so workflow and activity spans link.
  - Inside activities, add spans following the OpenTelemetry GenAI semantic conventions. Check the current attribute names at project start and record them in `docs/architecture.md`.
    - Model calls: span `chat <model>` with `gen_ai.operation.name=chat`, provider name, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and finish reasons.
    - Tool calls: span `execute_tool <name>` with `gen_ai.tool.name` and `gen_ai.tool.call.id`.
    - Custom attributes: `longhaul.task_id`, `longhaul.turn_index`, `longhaul.role`, `longhaul.cost_usd`, `longhaul.cache_ratio`.
  - Prompt and output content are not captured by default. `telemetry.capture_content: false`.
- **Acceptance criteria:**
  - [ ] With an in-memory span exporter, a scripted run produces 1 workflow span tree with N chat spans and M tool spans that match the turn and tool counts.
  - [ ] No span attribute contains prompt text when `capture_content` is false.
  - [ ] Jaeger shows the trace in the manual smoke.
- **Tests:** `tests/integration/test_tracing.py::test_span_counts_match`, `::test_no_content_by_default`
- **Files:** `src/longhaul/telemetry/otel.py`, `src/longhaul/durable/activities/{llm,tools}.py`, tests

#### LH-704 · Prometheus metrics and Grafana dashboard
- **Epic:** E12 · **Points:** 3 · **Branch:** `feat/lh-704-metrics` · **Depends on:** LH-701
- **Story:** As an operator, I want a dashboard with the numbers that matter, so that I can watch the fleet at a glance.
- **Build:**
  - `src/longhaul/telemetry/metrics.py` with `prometheus_client`, served on worker port 9464:
    - `longhaul_tasks_total{outcome,repo}`
    - `longhaul_active_tasks`
    - `longhaul_turns_total{role}`
    - `longhaul_llm_cost_usd_total{model,repo}`
    - `longhaul_llm_errors_total{kind}`
    - `longhaul_tool_duration_seconds{tool}` (histogram)
    - `longhaul_compactions_total`
    - `longhaul_stuck_total{reason}`
    - `longhaul_sandbox_recreated_total`
    - `longhaul_activity_retries_total{activity}`
    - `longhaul_cache_ratio` (histogram)
  - `docker/grafana/dashboards/longhaul.json` is provisioned with panels for each metric, a cost per repo per day panel, and outcome rates.
- **Acceptance criteria:**
  - [ ] After a scripted e2e run, `/metrics` exposes every metric listed, with non-zero values where expected.
  - [ ] The dashboard JSON loads in Grafana without errors (provisioning test via the Grafana API in compose).
- **Tests:** `tests/integration/test_metrics.py::test_metrics_exposed_after_run`, `tests/integration/test_grafana.py::test_dashboard_provisioned`
- **Files:** `src/longhaul/telemetry/metrics.py`, `docker/grafana/dashboards/longhaul.json`, tests

#### LH-705 · Run report
- **Epic:** E12 · **Points:** 3 · **Branch:** `feat/lh-705-run-report` · **Depends on:** LH-701, LH-507
- **Story:** As a reviewer, I want a short report in the PR that shows how the agent got here, so that I can judge the work and its cost.
- **Build:**
  - Activity `write_run_report(task_id)` builds markdown from the DB:
    - header: issue, outcome, wall time, turns, cost, tokens, cache ratio
    - phase timeline: planning, approval wait, coding, verify rounds, pauses, compactions, recoveries
    - tool usage: count, errors and total time per tool
    - the plan with statuses
    - interventions: who, what and when
    - candidate lessons
  - Store it in blob.
  - The PR body embeds it inside `<details><summary>Run report</summary>...</details>`, capped so the whole body stays under 60,000 chars.
  - `longhaul report TASK_ID` prints it.
- **Acceptance criteria:**
  - [ ] The report for the scripted inventory run contains every section, and the numbers match the DB.
  - [ ] The PR body stays under 60,000 chars even with 500 turns (the timeline is truncated with a note).
- **Tests:** `tests/integration/test_run_report.py::test_sections_and_numbers`, `::test_body_cap`
- **Files:** `src/longhaul/durable/activities/report.py`, `templates/run_report.md.j2`, `src/longhaul/cli.py`, tests

#### LH-706 · Structured logs and redaction
- **Epic:** E12 · **Points:** 2 · **Branch:** `feat/lh-706-logging` · **Depends on:** LH-703
- **Story:** As an operator, I want JSON logs with task context and no secrets, so that logs are searchable and safe to share.
- **Build:**
  - `src/longhaul/telemetry/logging.py`: `structlog` configured for JSON, with `task_id`, `turn_index`, `activity` and `trace_id` bound from the context.
  - A redaction processor replaces matches of `sk-[A-Za-z0-9_-]{20,}`, `ghp_[A-Za-z0-9]{30,}`, `github_pat_[A-Za-z0-9_]{30,}`, `AKIA[0-9A-Z]{16}` and `-----BEGIN [A-Z ]*PRIVATE KEY-----` with `[redacted]`. The same function is applied to run reports and to stored tool outputs before they are shown in the console.
- **Acceptance criteria:**
  - [ ] Each pattern is redacted in logs and in report output.
  - [ ] Log lines carry `task_id` and `trace_id` inside activities.
- **Tests:** `tests/unit/test_redaction.py::test_patterns[...]`, `tests/integration/test_logging.py::test_context_bound`
- **Files:** `src/longhaul/telemetry/logging.py`, tests

### Integration checkpoint 7

- **Cumulative tests:** `make gate PHASE=7`. That covers every earlier e2e and `chaos_lite`, plus the budget e2e, the tracing span-count test and the metrics test.
- **Manual smoke:**
  ```bash
  # set budgets.task_usd: 0.50 for lh-demo-textkit, label an issue
  gh issue view 1 --repo hoomanesteki/lh-demo-textkit --comments | tail -15   # budget pause comment
  gh issue comment 1 --repo hoomanesteki/lh-demo-textkit --body "/longhaul budget +1"
  open http://localhost:3000   # Grafana: Longhaul dashboard
  open http://localhost:16686  # Jaeger: search service longhaul-worker
  uv run longhaul report lh-hoomanesteki-lh-demo-textkit-1
  ```

### Phase gate 7

- [ ] All 6 stories Done. I6 holds.
- [ ] The budget pause and resume shown on real GitHub, with a screenshot in the gate report.
- [ ] Dashboard and trace screenshots in the gate report (used later in the README).
- [ ] Tag `v0.7.0`.

### Defend it

1. **Why project worst-case output tokens in the budget check?** Better to pause a little early than to overshoot. The check runs before the call, and the actual cost is ledgered after it.
2. **Why ledger failed attempts?** Retries cost real money. Hiding them would make the cost-per-resolved number look better than it is.
3. **Why not capture prompts in traces?** Prompts contain code and maybe secrets. Content capture is opt-in. Token counts and timings are enough for operations.
4. **What is in the run report that a reviewer can't see in the diff?** How long it took, how much it cost, whether the Verifier sent it back, whether a person had to step in, and what the agent believes it learned.

---

## Phase 8: Console (tag v0.8.0)

**Goal:** a small web console to see every task, read its timeline, act on it (approve, hint, top up, cancel) and review memory candidates, without opening GitHub or the Temporal UI.
**Demo at the end:**
1. Open the console and find the paused task.
2. Read its timeline down to the failing command.
3. Send a hint from the console and watch it resume.
4. Approve a lesson.

**Entry criteria:** Phase 7 gate passed.

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-801 | Console API | 5 | E13 |
| LH-802 | Console scaffold and typed client | 3 | E13 |
| LH-803 | Tasks list page | 3 | E13 |
| LH-804 | Task timeline page | 5 | E13 |
| LH-805 | Task actions | 3 | E13 |
| LH-806 | Memory viewer | 3 | E13 |
| LH-807 | Console auth | 2 | E13 |
| LH-808 | Console e2e with Playwright | 3 | E13 |

#### LH-801 · Console API
- **Epic:** E13 · **Points:** 5 · **Branch:** `feat/lh-801-console-api` · **Depends on:** LH-705
- **Story:** As the console, I want a typed REST API over tasks, timelines, actions and memory, so that the UI never talks to Temporal or the DB directly.
- **Build:**
  - `src/longhaul/api/app.py` (FastAPI) with these endpoints:
    - `GET /api/tasks?status=&repo=&limit=50&cursor=`
    - `GET /api/tasks/{id}`: the DB row plus a live `status()` query if running, plus the plan
    - `GET /api/tasks/{id}/timeline?cursor=`: events, turns and tool calls merged by time
    - `GET /api/tasks/{id}/turns/{n}`: rendered, redacted request and response from blobs
    - `GET /api/tasks/{id}/tool-calls/{call_id}/output?start=&end=`
    - `POST /api/tasks/{id}/approve-plan|reject-plan|hint|budget|cancel`: these send signals with `by="console"`
    - `GET /api/memory?repo=&status=&kind=`
    - `POST /api/memory/{id}/approve|reject`
    - `GET /api/repos`
    - `GET /healthz`
    - `GET /metrics`
  - Pydantic response models in `api/schemas.py`. `make api-types` dumps the OpenAPI schema to `console/openapi.json`.
  - Write `docs/adr/0011-console-stack.md`.
- **Acceptance criteria:**
  - [ ] Every endpoint has a test against a seeded DB and a time-skipping Temporal env.
  - [ ] The POST actions deliver the right signal (asserted via workflow state).
  - [ ] Turn and output endpoints apply redaction.
- **Tests:** `tests/integration/test_api.py::test_tasks_list_filters`, `::test_task_detail_live_status`, `::test_timeline_order`, `::test_actions_signal`, `::test_outputs_redacted`
- **Files:** `src/longhaul/api/*`, ADR, tests

#### LH-802 · Console scaffold and typed client
- **Epic:** E13 · **Points:** 3 · **Branch:** `feat/lh-802-console-scaffold` · **Depends on:** LH-801
- **Story:** As a frontend developer, I want a Vite + React + TypeScript app with types generated from the API, so that the UI breaks at build time when the API changes.
- **Build:**
  - `console/` from `npm create vite@latest` (react-ts template), plus React Router, TanStack Query, Tailwind, Vitest and Testing Library.
  - `openapi-typescript console/openapi.json -o console/src/api/schema.ts`. A thin `api/client.ts` with `fetch` and the bearer token from localStorage, with access wrapped in try/catch.
  - Layout: left nav (Tasks, Memory, Repos), header with the API health dot.
  - The `console` CI job runs lint, typecheck, test and build.
  - FastAPI serves the built `console/dist` at `/` in the compose `full` profile.
- **Acceptance criteria:**
  - [ ] `npm run build` passes in CI. A type change in `openapi.json` that breaks a call fails `npm run typecheck`.
- **Tests:** `console/src/__tests__/client.test.ts::adds bearer header`, CI typecheck
- **Files:** `console/**`, `.github/workflows/ci.yml`

#### LH-803 · Tasks list page
- **Epic:** E13 · **Points:** 3 · **Branch:** `feat/lh-803-tasks-list` · **Depends on:** LH-802
- **Story:** As Maya, I want all tasks in one table with status and cost, so that I know what needs me.
- **Build:**
  - `/tasks` shows a table with columns: repo, issue (link), title, phase badge, outcome, turns, cost, age and "needs you" (paused or awaiting approval).
  - Filters by status and repo, a "needs you" toggle, and 10 s polling via TanStack Query.
  - Empty and error states.
- **Acceptance criteria:**
  - [ ] Component tests cover rendering, filters and the "needs you" toggle with mocked data.
  - [ ] It works at 375 px width (horizontal scroll inside the table only).
- **Tests:** `console/src/pages/__tests__/TasksPage.test.tsx`
- **Files:** `console/src/pages/TasksPage.tsx`, components

#### LH-804 · Task timeline page
- **Epic:** E13 · **Points:** 5 · **Branch:** `feat/lh-804-timeline` · **Depends on:** LH-803
- **Story:** As Raj, I want to see every turn, tool call, checkpoint and pause in order, so that I can understand a run in minutes.
- **Build:**
  - `/tasks/:id`:
    - Header: phase, cost, turns, tokens, cache ratio, PR link and plan with step statuses.
    - Timeline: a virtualized list grouped by turn. Each turn shows the role, model, tokens, cost and the tool calls (name, args summary, ok/error, duration, `post_sha` short).
    - Expanders load the turn request/response and tool outputs on demand (paged).
    - Event rows are coloured by type (compaction, pause, hint, verdict, recovery).
    - A small cost-over-turns sparkline.
- **Acceptance criteria:**
  - [ ] It renders a 300-turn fixture without jank (virtualized; test asserts fewer than 100 DOM rows).
  - [ ] Expanding a tool output fetches only that output.
- **Tests:** `console/src/pages/__tests__/TaskPage.test.tsx`
- **Files:** `console/src/pages/TaskPage.tsx`, `console/src/components/timeline/*`

#### LH-805 · Task actions
- **Epic:** E13 · **Points:** 3 · **Branch:** `feat/lh-805-actions` · **Depends on:** LH-804
- **Story:** As a maintainer, I want to approve, hint, top up or cancel from the console, so that I can act where I'm reading.
- **Build:**
  - Buttons shown only when the action is valid for the phase:
    - Approve / Reject plan (with a feedback box)
    - Send hint (textarea)
    - Add budget (USD and turns inputs)
    - Cancel (typed confirmation: the task id)
  - Each action calls the API, shows a toast, then refetches. Actions are also mirrored to the issue as a short comment by the workflow ("Hint from console: ...") for the audit trail.
- **Acceptance criteria:**
  - [ ] Component tests: buttons appear only for valid phases, and the right endpoint is called with the right body.
  - [ ] The workflow posts the mirror comment (integration test through the API).
- **Tests:** `console/src/components/__tests__/TaskActions.test.tsx`, `tests/integration/test_api.py::test_console_hint_mirrored_to_issue`
- **Files:** `console/src/components/TaskActions.tsx`, `src/longhaul/durable/workflows/task.py`

#### LH-806 · Memory viewer
- **Epic:** E13 · **Points:** 3 · **Branch:** `feat/lh-806-memory-viewer` · **Depends on:** LH-802
- **Story:** As the product owner, I want to review memory candidates and see what the agent knows per repo, so that memory stays clean.
- **Build:**
  - `/memory` has a repo selector and tabs: Approved, Candidates, Rejected.
  - Each item shows its kind, content, source link (PR or task), uses and last used. Candidates get Approve / Reject.
  - The repo map renders as monospace text inside a collapsible panel.
- **Acceptance criteria:**
  - [ ] Approve and reject call the API and move the item between tabs.
  - [ ] The source links resolve to the PR or task page.
- **Tests:** `console/src/pages/__tests__/MemoryPage.test.tsx`
- **Files:** `console/src/pages/MemoryPage.tsx`

#### LH-807 · Console auth
- **Epic:** E13 · **Points:** 2 · **Branch:** `feat/lh-807-console-auth` · **Depends on:** LH-801
- **Story:** As an operator, I want the API locked behind a token, so that nobody on the network can cancel tasks or read outputs.
- **Build:**
  - A FastAPI dependency checks `Authorization: Bearer` against `LONGHAUL_ADMIN_TOKEN` with `hmac.compare_digest`. `/healthz` is public. `/metrics` is public only when bound to localhost, otherwise it needs the token.
  - The console has a login screen that stores the token, with an in-memory fallback if storage throws.
  - The API rate-limits POST actions to 30 per minute per token (simple in-memory bucket, noted as single-instance).
- **Acceptance criteria:**
  - [ ] Missing or wrong token returns 401 on every `/api` route.
  - [ ] Rate limiting returns 429 after 30 POSTs per minute.
- **Tests:** `tests/integration/test_api_auth.py::test_requires_token`, `::test_rate_limit_actions`
- **Files:** `src/longhaul/api/deps.py`, `console/src/pages/Login.tsx`

#### LH-808 · Console e2e with Playwright
- **Epic:** E13 · **Points:** 3 · **Branch:** `feat/lh-808-console-e2e` · **Depends on:** LH-805, LH-806, LH-807
- **Story:** As the reviewer, I want one browser test of the main path, so that the console keeps working as the backend changes.
- **Build:**
  - `scripts/seed_demo.sh` runs:
    - one scripted task to `pr_opened`
    - one to a stuck pause
    - one awaiting plan approval
    - two lesson candidates
  - Playwright test:
    1. Log in and see 3 tasks.
    2. Open the stuck task and send a hint, then see the phase change to coding within 15 s.
    3. Approve a plan.
    4. Approve a lesson.
  - Runs in CI on push to `dev` with the compose stack.
- **Acceptance criteria:**
  - [ ] The Playwright test passes locally and in CI on `dev`.
- **Tests:** `console/e2e/main-path.spec.ts`
- **Files:** `console/e2e/*`, `scripts/seed_demo.sh`, `.github/workflows/ci.yml`

### Integration checkpoint 8

- **Cumulative tests:** `make gate PHASE=8`, which covers all backend e2e, `chaos_lite`, console unit tests and Playwright main path.
- **Manual smoke:**
  ```bash
  make up && docker compose --profile full up -d api worker
  ./scripts/seed_demo.sh
  open http://localhost:8000    # log in with LONGHAUL_ADMIN_TOKEN, walk the main path
  ```

### Phase gate 8

- [ ] All 8 stories Done. ADR-0011 merged.
- [ ] Playwright green. Console screenshots in the gate report.
- [ ] Tag `v0.8.0`.

### Defend it

1. **Why a console at all if GitHub comments work?** Comments are good for steering one task. The console is for seeing many tasks, reading long timelines and reviewing memory, which GitHub threads are bad at.
2. **Why generated API types?** The backend and frontend are built by different sessions. Generated types turn an API drift into a build error instead of a runtime bug.
3. **Why mirror console actions to the issue?** One audit trail. Anyone reading the issue sees every human intervention, wherever it came from.

---

## Phase 9: Evaluation, chaos, hardening and release (tag v1.0.0)

**Goal:** publish honest numbers with baselines, prove crash recovery at scale, harden the sandbox and ship v1.0.0 with docs and a demo.
**Demo at the end:** the README results table (filled with real numbers), the chaos table, a 2-minute video, and the esteki.ca case study draft.
**Entry criteria:**
- Phase 8 gate passed.
- An x86_64 VM with Docker and about 250 GB of disk for SWE-bench images.
- An eval budget approved by Hooman (section 10).

| ID | Title | Pts | Epic |
|---|---|---|---|
| LH-901 | Eval task sets and runner | 5 | E14 |
| LH-902 | SWE-bench Verified adapter and grading | 8 | E14 |
| LH-903 | Baselines: single-shot and naive loop | 5 | E14 |
| LH-904 | Full chaos suite | 5 | E14 |
| LH-905 | Results report | 3 | E14 |
| LH-906 | Security hardening pass | 3 | E15 |
| LH-907 | Docs, README and demo | 3 | E15 |
| LH-908 | Terraform for AWS (Could) | 5 | E15 |
| LH-909 | Route tool calls through Keystone (Could) | 3 | E15 |

#### LH-901 · Eval task sets and runner
- **Epic:** E14 · **Points:** 5 · **Branch:** `feat/lh-901-eval-runner` · **Depends on:** LH-706
- **Story:** As the evaluator, I want one runner for every task set and config, so that every number in the README comes from the same code path.
- **Build:**
  - `eval/custom/tasks.yaml` lists 15 tasks on 3 small repos Hooman owns, 5 per repo. Fields: `id`, `repo`, `base_sha`, `issue_title`, `issue_body`, `category` (`bugfix`, `feature`, `refactor`, `dependency`, `flaky`) and `hidden_tests` (a path in `eval/custom/hidden_tests/`, never visible to the agent).
  - `eval/run.py --set custom|swebench --config full|naive|single_shot [--model ID] [--limit N] [--concurrency K] [--memory on|off] [--context-mode ...] --run-id NAME`. It:
    1. Runs tasks in local mode, with no GitHub writes, plan gate off and hints off. A pause counts as `intervention_needed` and ends the task after a 1-minute timeout.
    2. Writes `eval/results/<run_id>/<task_id>.json` with: outcome, `resolved` (filled by the grader), cost, tokens, cache ratio, wall time, turns, compactions, verify rounds, stuck reasons and `intervention_needed`.
    3. Writes `summary.json`.
  - The custom grader starts a fresh sandbox at `base_sha`, applies the agent patch, copies in the hidden tests, and runs the repo tests plus the hidden tests. `resolved` means everything passes.
- **Acceptance criteria:**
  - [ ] A scripted dry run (`--config full --set custom --limit 2 --provider scripted`) writes valid result files.
  - [ ] The grader marks a known-good patch resolved and a known-bad patch unresolved.
  - [ ] The agent's sandbox never contains `hidden_tests` (asserted).
- **Tests:** `tests/integration/test_eval_runner.py::test_dry_run_writes_results`, `::test_grader_good_bad`, `::test_hidden_tests_not_visible`
- **Files:** `eval/run.py`, `eval/custom/**`, `eval/grade_custom.py`, tests

#### LH-902 · SWE-bench Verified adapter and grading
- **Epic:** E14 · **Points:** 8 · **Branch:** `feat/lh-902-swebench` · **Depends on:** LH-901
- **Story:** As the evaluator, I want Longhaul run on a fixed SWE-bench Verified subset and graded by the official harness, so that results are comparable and credible.
- **Build:**
  - Spike first (part of this story, timeboxed 2 h, notes in `eval/swebench/README.md`). Check against the current `swebench` package:
    - how to build per-instance environment images without running evaluation
    - the image naming
    - the workdir (`/testbed`) and environment activation
    - where the per-repo test command lives in the package constants
  - `eval/swebench/select_subset.py` loads `princeton-nlp/SWE-bench_Verified` via `datasets` and samples 40 ids stratified by the dataset's difficulty annotation with seed 42: 15 easy ("<15 min fix"), 20 medium ("15 min - 1 hour"), 5 hard ("1-4 hours"). It writes `eval/swebench/subset.txt`, which is committed and never changed after the first headline run.
  - `eval/swebench/adapter.py`:
    1. Build a `TaskSpec` from `problem_statement` with repo `local:` mode.
    2. The sandbox image is the instance's environment image, workdir `/testbed`, `base_sha` = the instance `base_commit`. Network stays off after setup.
    3. `test_command` comes from the harness's per-repo spec, as a hint the agent can use.
    4. After the run, `model_patch = git diff base_commit`, excluding any file touched by the instance's `test_patch` (documented, so the official test patch applies cleanly).
    5. Write `predictions.jsonl` (`instance_id`, `model_name_or_path`, `model_patch`).
  - `eval/swebench/grade.sh` runs the official `python -m swebench.harness.run_evaluation` with dataset, predictions, `--max_workers` and `--run_id`, then parses the report into the `resolved` field of each result file.
- **Acceptance criteria:**
  - [ ] On 2 instances with the gold patch as the "agent output" (a sanity mode), the grader reports 2/2 resolved.
  - [ ] On the same 2 with an empty patch, it reports 0/2.
  - [ ] `subset.txt` has 40 ids with the stated difficulty split, generated from the seed.
  - [ ] A real 3-instance smoke run with the agent completes end to end (Hooman runs it, and its output goes in the PR).
- **Tests:** `tests/integration/test_swebench_adapter.py::test_gold_patch_sanity` (marker `slow`, needs Docker and images, run on the eval VM), `tests/unit/test_subset.py::test_stratified_counts`, `tests/unit/test_patch_filter.py::test_excludes_test_patch_files`
- **Files:** `eval/swebench/*`, tests

#### LH-903 · Baselines: single-shot and naive loop
- **Epic:** E14 · **Points:** 5 · **Branch:** `feat/lh-903-baselines` · **Depends on:** LH-901
- **Story:** As the evaluator, I want two fair baselines on the same model and tasks, so that the gains from the harness are measured, not assumed.
- **Build:**
  - `eval/baselines/single_shot.py`:
    1. Retrieve the top 5 files by BM25 (`rank_bm25`) between the problem statement and file contents, plus the repo map.
    2. Make one model call asking for a unified diff.
    3. Apply it with `git apply --recount` (or a fuzzy fallback via `patch -p1 --fuzz=2`).
    4. No tools.
  - `naive` config, the same durable stack with:
    - planner off, verifier off, memory off, stuck detector off
    - `context.mode=none`, replaced by a sliding window that drops the oldest whole turns at the hard limit (the common naive approach)
    - same tools, same turn and cost limits
  - Each config is defined in `eval/configs/*.yaml`, and the config file hash is stored in each result file.
- **Acceptance criteria:**
  - [ ] All three configs run on the scripted custom subset in a dry run.
  - [ ] Result files record the config hash.
  - [ ] Same model id across configs is enforced by the runner unless `--allow-mixed-models` is passed.
- **Tests:** `tests/integration/test_baselines.py::test_three_configs_dry_run`, `::test_config_hash_recorded`, `tests/unit/test_single_shot.py::test_patch_apply_fallback`
- **Files:** `eval/baselines/*`, `eval/configs/*.yaml`, tests

#### LH-904 · Full chaos suite
- **Epic:** E14 · **Points:** 5 · **Branch:** `feat/lh-904-chaos-full` · **Depends on:** LH-209, LH-901
- **Story:** As the product owner, I want 20 crash runs with several kill types and a side-effect audit, so that "survives crashes" has a number behind it.
- **Build:**
  - Recordings: 4 custom tasks recorded with the real model (`ReplayProvider --record`) on the full config.
  - Temporal dev server persistence: compose runs it with `--db-filename` on a volume. Verify this and add it to compose if LH-002 didn't, because without it a server restart loses all workflows.
  - `eval/chaos/chaos.py --runs 20 --seed 7` runs each of 5 runs per recorded task with 1 to 3 kills, picked at random times, of these types:
    - worker SIGKILL
    - sandbox `docker kill`
    - Temporal server restart (`docker compose restart temporal`)
    - Postgres restart
  - It uses FakeGitHub for delivery, so side effects can be counted.
  - Per run, it records:
    - completed
    - final diff equals the reference
    - extra ledger rows from retries and their cost
    - resume latency: kill time to the next activity start, from Temporal history timestamps
    - side-effect counts (comments, PRs, labels) compared with the expected counts
  - Output: `eval/results/chaos-<date>/summary.json`.
- **Acceptance criteria:**
  - [ ] Running the suite produces the summary with all fields.
  - [ ] Duplicate side-effect count is computed from FakeGitHub state, not from our own DB.
  - [ ] Each kill type is hit at least 4 times across the 20 runs (the seed is chosen to make this true, and asserted).
- **Tests:** `tests/chaos/test_chaos_full.py::test_suite_summary_fields` (marker `chaos`), `tests/unit/test_chaos_plan.py::test_kill_type_coverage`
- **Files:** `eval/chaos/*`, `docker-compose.yml`, tests

#### LH-905 · Results report
- **Epic:** E14 · **Points:** 3 · **Branch:** `feat/lh-905-results-report` · **Depends on:** LH-902, LH-903, LH-904
- **Story:** As a hiring manager reading the README, I want one page of results with confidence intervals and the method, so that I can trust the numbers.
- **Build:**
  - `eval/report/build_report.py RUN_IDS...` produces `docs/results.md` and `docs/img/*.png` (matplotlib). It contains:
    - the results table per config (section 8.6 template)
    - Wilson 95% intervals for resolve rates
    - a McNemar test for full vs naive (paired on the same tasks)
    - dollars per resolved
    - a tokens-per-task box plot
    - paired compaction savings (full vs `no_compaction` on the long-task subset)
    - the memory on/off comparison (custom set)
    - the chaos table
    - a list of every unresolved task with its outcome and a one-line reason, from the stuck report or verifier issues
  - Numbers are computed only from result files. There are no hand edits: the report has a footer with the run ids and git sha.
- **Acceptance criteria:**
  - [ ] Running on dry-run result fixtures produces the full page with every section.
  - [ ] The Wilson and McNemar functions match reference values (scipy or hand-computed).
- **Tests:** `tests/unit/test_stats.py::test_wilson`, `::test_mcnemar`, `tests/integration/test_report_build.py::test_sections_present`
- **Files:** `eval/report/*`, `docs/results.md`, tests

#### LH-906 · Security hardening pass
- **Epic:** E15 · **Points:** 3 · **Branch:** `feat/lh-906-hardening` · **Depends on:** LH-807
- **Story:** As a security reviewer, I want the sandbox and services checked against a written list, so that the claims in the README are true.
- **Build:**
  - `tests/integration/test_sandbox_hardening.py` inspects a live sandbox via `docker inspect` and asserts:
    - `CapDrop == ["ALL"]`, `ReadonlyRootfs`, `User == "agent"`, `SecurityOpt` contains `no-new-privileges`
    - pids, memory and CPU limits set
    - network mode `none` after setup
  - Write `docs/security.md`: threat model (assets, actors, entry points), controls mapped to tests, known gaps (for example a shared Docker host, gVisor optional), and the token rotation runbook.
  - Run `gitleaks` on the full history and `pip-audit`, and fix or document findings.
- **Acceptance criteria:**
  - [ ] The hardening tests pass.
  - [ ] `docs/security.md` maps each control to a test.
  - [ ] gitleaks and pip-audit are clean, or each finding is documented with a reason.
- **Tests:** `tests/integration/test_sandbox_hardening.py::test_inspect_flags`
- **Files:** `docs/security.md`, tests

#### LH-907 · Docs, README and demo
- **Epic:** E15 · **Points:** 3 · **Branch:** `feat/lh-907-docs` · **Depends on:** LH-905
- **Story:** As a visitor to the repo, I want to understand what this is, see the proof and run it in three commands, so that the project speaks for itself.
- **Build:**
  - `README.md` per section 11.1.
  - `docs/architecture.md` with the diagrams from section 3.
  - `docs/runbook.md`: start/stop, rotate tokens, drain a worker, cancel stuck tasks, replay test, upgrade with workflow versioning.
  - `docs/case-study.md` per section 11.2.
  - `docs/demo-script.md` per section 11.3.
  - Hooman records the video.
  - The repo is made public at the v1.0.0 gate.
- **Acceptance criteria:**
  - [ ] A fresh clone plus the README quickstart reaches a finished scripted task in under 10 minutes on a clean machine (tested by the reviewer in a fresh VM or container).
  - [ ] The docs style tests pass (no banned phrases, no long dashes).
- **Tests:** `tests/unit/test_docs_style.py` (extended to all docs), manual fresh-clone check in the gate report
- **Files:** `README.md`, `docs/*`

#### LH-908 · Terraform for AWS (Could)
- **Epic:** E15 · **Points:** 5 · **Branch:** `feat/lh-908-terraform` · **Depends on:** LH-907
- **Story:** As a platform engineer, I want an infrastructure-as-code deploy, so that Longhaul can run outside a laptop.
- **Build:**
  - `deploy/terraform/aws/`:
    - VPC with 2 private subnets and NAT
    - RDS Postgres (pgvector-capable version)
    - S3 bucket (versioned, encrypted)
    - ECS cluster, ECR repos, Secrets Manager entries for the tokens, CloudWatch log groups
  - Placement:
    - API and poller on ECS Fargate.
    - **Workers on ECS with EC2 capacity**, because each worker must start Docker sandboxes and Fargate tasks can't run a Docker daemon. This refines the earlier "Fargate workers" idea. Running each sandbox as its own Fargate task is noted as a post-v1 alternative.
  - Temporal: a variable `temporal_mode = cloud | self_hosted`. Cloud takes namespace, endpoint and mTLS cert ARNs. Self-hosted is documented, not built.
  - CI: `terraform fmt -check` and `terraform validate`. Applying is manual and is followed by teardown, with a cost note per hour in the README.
- **Acceptance criteria:**
  - [ ] `terraform validate` passes in CI.
  - [ ] `docs/deploy-aws.md` lists every resource and a teardown command.
- **Tests:** CI job `terraform`
- **Files:** `deploy/terraform/aws/**`, `docs/deploy-aws.md`, `.github/workflows/ci.yml`

#### LH-909 · Route tool calls through Keystone (Could)
- **Epic:** E15 · **Points:** 3 · **Branch:** `feat/lh-909-keystone` · **Depends on:** LH-907, Keystone v1 available
- **Story:** As a security reviewer, I want every agent action policy-checked and audited by Keystone, so that Longhaul inherits a central control plane.
- **Build:**
  - Add a config switch `tools.backend: direct | keystone`.
  - In keystone mode the worker exposes the sandbox tools as a small MCP server (`longhaul-sandbox`) registered as a Keystone upstream. `tool_exec` then calls the tool through Keystone's MCP endpoint as an MCP client, with the task id as the session.
  - A Keystone deny or approval decision maps to a `ToolResult(ok=False)` with the reason. A `require_approval` decision maps to a Longhaul pause.
- **Acceptance criteria:**
  - [ ] With Keystone running and a policy that denies `run_command` containing `curl`, the agent sees a denied result, and Keystone's audit log has every call.
  - [ ] `direct` mode is unchanged (all earlier tests pass).
- **Tests:** `tests/integration/test_keystone_backend.py::test_denied_command_surfaces` (marker `keystone`, skipped unless `KEYSTONE_URL` is set)
- **Files:** `src/longhaul/tools/keystone_backend.py`, `src/longhaul/tools/mcp_server.py`, tests
- **Note:** if Keystone isn't at v1 when Phase 9 starts, move this story to post-v1 without blocking the release.

### Integration checkpoint 9

- **Cumulative tests:** `make gate PHASE=9`. That covers every phase marker, `chaos_lite`, Playwright, and the hardening tests. The full chaos suite and the eval runs are manual and their result files are committed.
- **Manual smoke:**
  ```bash
  # on the eval VM
  uv run python eval/swebench/select_subset.py --seed 42 --check   # subset unchanged
  uv run python eval/run.py --set custom --config full --run-id custom-full-$(date +%F) --concurrency 3
  uv run python eval/run.py --set swebench --config full --run-id swe-full-$(date +%F) --concurrency 4
  ./eval/swebench/grade.sh swe-full-$(date +%F)
  uv run python eval/chaos/chaos.py --runs 20 --seed 7
  uv run python eval/report/build_report.py custom-full-* swe-* chaos-*
  ```

### Phase gate 9 (release)

- [ ] All Must and Should stories Done. Could stories Done or moved to post-v1 with a note.
- [ ] `docs/results.md` built from committed result files, with the footer run ids matching `eval/results/`.
- [ ] Chaos summary committed. Recovery and duplicate numbers are reported as they are.
- [ ] Hardening tests pass, and `docs/security.md` is complete.
- [ ] Fresh-clone quickstart under 10 minutes.
- [ ] README, case study and demo video done.
- [ ] Repo public. Tag `v1.0.0`. GitHub release notes list each phase's tag and highlights.

### Defend it

1. **Why only 40 SWE-bench tasks?** Cost and time. With 40 paired tasks we can detect large differences (about 15 points or more) between configs with McNemar's test. We report Wilson intervals so nobody reads more into small gaps than the data supports.
2. **Why exclude test-file edits from the SWE-bench patch?** The official harness applies its own test patch after ours. If our patch touches the same files, the test patch can fail to apply and the task fails for a reason unrelated to the fix. We document the filter.
3. **Is the naive baseline a straw man?** No. It has the same tools, model, limits and durable stack. It just drops planning, verification, memory and structured compaction in favour of a sliding window, which is what many simple agent loops do.
4. **What if the results are bad?** We publish them anyway, with the failure list. A clear negative result with a working harness is still a strong signal; a suspicious perfect one is not.
5. **Why EC2 workers in the AWS plan instead of Fargate?** Workers start Docker containers for sandboxes, and Fargate can't run a Docker daemon. The API and poller don't need Docker, so they stay on Fargate.

---

## 8. Evaluation and proof plan

### 8.1 Questions the eval answers

| # | Question | Evidence |
|---|---|---|
| Q1 | Does the harness solve more tasks than simpler setups with the same model? | Resolve rate: full vs naive vs single-shot, on SWE-bench Verified (40) and the custom set (15) |
| Q2 | Is it worth the money? | Dollars per resolved task for each config |
| Q3 | What does compaction buy? | Paired tokens and resolve rate, full vs `no_compaction`, on the long-task set |
| Q4 | Does memory help? | Paired turns-to-first-green, cost and resolve rate, memory on vs off, on the custom set |
| Q5 | Does it survive crashes? | Chaos suite: completion, diff equality, duplicate side effects, resume latency |
| Q6 | How often does it need a person, and does the stuck protocol fire for the right reasons? | Intervention-needed rate, stuck reasons, and an assisted run where Hooman answers stuck questions |

### 8.2 Task sets

| Set | Size | Source | Notes |
|---|---|---|---|
| SWE-bench Verified subset | 40 | `princeton-nlp/SWE-bench_Verified`, stratified by difficulty: 15 easy, 20 medium, 5 hard, seed 42 | `eval/swebench/subset.txt` is frozen after the first headline run. Graded by the official harness. |
| Custom set | 15 | 3 small repos Hooman owns, 5 tasks each | Categories: bugfix (5), feature (3), refactor (3), dependency (2), flaky (2). Hidden tests are written before any agent run and never shown to the agent. |
| Long-task set | 14 | The custom refactor, dependency and flaky tasks (7), plus the SWE-bench "1-4 hours" tasks (5), plus 2 synthetic long tasks (`longlog`-style, at real scale) | Defined before any run, so it isn't cherry-picked. |

### 8.3 Configurations

| Config | Planner | Verifier | Memory | Context | Stuck detector | Notes |
|---|---|---|---|---|---|---|
| `full` | on | on | on | two-tier | on | The product |
| `naive` | off | off | off | sliding window at hard limit | off | Same tools, model, limits and durable stack |
| `single_shot` | n/a | n/a | n/a | BM25 top-5 files + repo map | n/a | One call, unified diff |
| `full_no_compaction` | on | on | on | tier 1 only, sliding window at hard limit | on | Q3 ablation |
| `full_no_memory` | on | on | off | two-tier | on | Q4 ablation |
| `full_no_verifier` | on | off | on | two-tier | on | Extra ablation if budget allows |

### 8.4 Run matrix

| Run | Model class | Set | Configs | Task-runs |
|---|---|---|---|---|
| Headline | Sonnet-class | SWE-40 | full, naive, single_shot | 120 |
| Headline | Sonnet-class | Custom-15 | full, naive, single_shot | 45 |
| Variance | Sonnet-class | Custom-15 | full (second run) | 15 |
| Ablation Q3 | Haiku-class | Long-14 | full, full_no_compaction | 28 |
| Ablation Q4 | Haiku-class | Custom-15 | full, full_no_memory (memory warmed by a prior pass whose PRs were "merged" by the eval harness) | 30 |
| Assisted | Sonnet-class | Custom tasks that ended stuck in the headline run | full with Hooman answering hints | ≤ 15 |
| Chaos | Replay of 4 recorded custom tasks | n/a | full | 20 |

Fairness rules:
- Same model id and limits across configs in one comparison.
- Per-task caps: `task_usd: 4` on SWE-bench, `task_usd: 2` on custom, `task_turns: 150`, `task_wall_minutes: 120`.
- Plan gate off during evals.
- Every result file records the config hash, model id, git sha and seed.

### 8.5 Metrics and formulas

| Metric | Formula |
|---|---|
| Resolve rate | `resolved / attempted`. Attempted counts every started task, including errors and timeouts. |
| 95% CI | Wilson score interval on the resolve rate |
| Paired difference | Resolve(full) minus Resolve(naive) on the same tasks. Significance by McNemar's exact test on the discordant pairs. |
| Dollars per resolved | `sum(cost_usd over all attempted tasks in the config) / resolved` |
| Median wall time | Median over resolved tasks of `ended_at - started_at` |
| Tokens per task | `input_tokens_total + output_tokens_total + cache_read_total`, also reported as cost-weighted |
| Cache ratio | `cache_read / (input + cache_read)` per task, then the median |
| Compaction savings | Per task, `1 - input_tokens(full) / input_tokens(full_no_compaction)`, then the median and IQR over the long-task set |
| Intervention-needed rate | Tasks with at least one pause (stuck, ask_human, budget) / attempted |
| Hint effectiveness | In the assisted run: tasks resolved after at least one hint / tasks that received a hint |
| Silent failure rate | Tasks ending without a PR, stuck report or budget comment / attempted. Target 0. |
| Chaos completion | Runs that reach the same final outcome as the reference / runs |
| Diff equality | Runs whose final diff equals the no-kill reference / completed runs |
| Duplicate side effects | Sum over runs of (actual comments + PRs + label events in FakeGitHub minus expected) |
| Resume latency | Per kill: time from the kill to the next activity start in Temporal history. Report p50 and p95. |
| Retry cost overhead | Extra ledger cost from attempts above 1 / total cost, across chaos runs |

Sample size note: with n=40 paired tasks, a 15-point gap can reach significance if most discordant pairs point the same way. Smaller gaps will be reported with their intervals and called inconclusive.

### 8.6 Results table templates (filled only from result files)

**Table 1: SWE-bench Verified subset (n=40), Sonnet-class model**

| Config | Resolved | Rate | 95% CI | $ / resolved | Median wall time | Median tokens / task | Cache ratio |
|---|---|---|---|---|---|---|---|
| single_shot | __ / 40 | __% | __ to __ | $__ | __ | __ | n/a |
| naive | __ / 40 | __% | __ to __ | $__ | __ min | __ | __ |
| full | __ / 40 | __% | __ to __ | $__ | __ min | __ | __ |

McNemar full vs naive: discordant pairs b = __, c = __, p = __.

**Table 2: Custom set (n=15)**

| Config | Resolved | $ / resolved | Intervention-needed | Silent failures |
|---|---|---|---|---|
| single_shot | __ / 15 | $__ | n/a | n/a |
| naive | __ / 15 | $__ | __% | __ |
| full | __ / 15 | $__ | __% | __ |
| full (run 2) | __ / 15 | $__ | __% | __ |

**Table 3: Ablations (Haiku-class)**

| Comparison | Set | Metric | With | Without | Paired delta |
|---|---|---|---|---|---|
| Compaction | Long-14 | Resolved | __ | __ | __ |
| Compaction | Long-14 | Median input tokens / task | __ | __ | __% saved |
| Memory | Custom-15 | Median turns to first green test | __ | __ | __ |
| Memory | Custom-15 | Resolved | __ | __ | __ |

**Table 4: Chaos (20 runs, 1 to 3 kills each)**

| Kill type | Count | Completed | Diff equal | Duplicate side effects | Resume p50 / p95 |
|---|---|---|---|---|---|
| Worker SIGKILL | __ | __ | __ | __ | __ s / __ s |
| Sandbox kill | __ | __ | __ | __ | __ s / __ s |
| Temporal restart | __ | __ | __ | __ | __ s / __ s |
| Postgres restart | __ | __ | __ | __ | __ s / __ s |
| **All** | __ | __ / 20 | __ / __ | __ | __ s / __ s |

Retry cost overhead across chaos runs: __%.

**Table 5: Unresolved tasks (full config)**

| Task | Outcome | One-line reason (from stuck report or verifier) |
|---|---|---|
| __ | __ | __ |

### 8.7 Chaos methodology

1. **Record.** Run 4 custom tasks on the full config with the real model in `--record` mode. Save each final diff as the reference.
2. **Plan kills.** `chaos.py --seed 7` builds 20 runs: 5 per recorded task, each with 1 to 3 kills. Each kill gets a type (worker, sandbox, Temporal, Postgres) and a time drawn uniformly from 5% to 95% of the reference duration. The plan is saved before execution.
3. **Execute.** Each run starts the full stack fresh, starts the task with `ReplayProvider` and FakeGitHub, and applies the kills on schedule:
   - workers and the sandbox are restarted within 3 s
   - Temporal and Postgres are restarted via compose
4. **Assert and record.** For each run, check completion, diff equality, ledger rows and FakeGitHub counts, and read resume latency from Temporal history.
5. **Report.** Report everything in Table 4, including any failure, with the run's event log attached.

`ReplayProvider` makes model output identical across runs, so any difference in the final diff is a durability bug, not model randomness. Five live (non-replay) chaos runs are also done once for realism. They are only judged on completion and duplicate side effects.

### 8.8 Cost estimate

Assumptions, to be verified against the provider's pricing page before running:
- Sonnet-class: about $3 per million input tokens and $15 per million output tokens.
- Haiku-class: about one third of that.
- Cache reads: about 10% of the input price.
- A full-config SWE-bench task averages about 40 turns, with a 40k-token context of which ~70% is cache hits, and 1k output tokens per turn. That is about $0.06 per turn, or about $2.40 per task. Custom tasks are about 25 turns, roughly $1.25 per task.

| Item | Estimate |
|---|---|
| SWE-40 full + naive + single_shot (Sonnet-class) | ~$96 + ~$110 + ~$4 = ~$210 |
| Custom-15 three configs + variance run (Sonnet-class) | ~$60 |
| Ablations (Haiku-class) | ~$50 |
| Chaos recordings + 5 live chaos runs | ~$20 |
| Development and smoke runs across Phases 1 to 8 | ~$40 |
| **API total** | **~$380 (plan for $250 to $500)** |
| Eval VM (16 vCPU, 64 GB, 300 GB disk) for about 30 hours | ~$30 to $60 |
| AWS deploy test (Could), torn down the same day | ~$5 to $15 |
| **Grand total** | **~$420 to $575** |

Cost controls:
- the per-task caps above
- ablations on the cheaper model
- `--limit` dry runs before every full run
- prompt caching on
- stop rule: abort a config if spend passes 130% of its estimate and look at why first

---

## 9. Risks, assumptions and open decisions

| # | Type | Item | Impact | Mitigation / decision owner |
|---|---|---|---|---|
| R1 | Risk | Temporal Python SDK details (pydantic converter, CAN hints) differ from the plan | Rework in Phase 2 | Spike LH-201 with written fallbacks |
| R2 | Risk | Anthropic SDK error classes or token counting differ | Wrong retry behaviour | Spike LH-102, error mapping tests |
| R3 | Risk | Docker-in-CI flakiness on hosted runners | Red CI unrelated to code | Retry-once on infra errors only, cache images, keep integration tests small |
| R4 | Risk | SWE-bench environment images are large and slow to build | Eval delays | Build images for the 40 ids once on the VM and keep the disk. The gold-patch sanity check comes first. |
| R5 | Risk | Low absolute resolve rate | Weaker headline | Lead with durability and cost numbers, which are what this project claims. Publish the failure list. |
| R6 | Risk | Replay mismatch after code changes (request hashes differ) | Chaos tests break | Fall back to turn-index matching with a warning. Re-record at Phase 9 start. |
| R7 | Risk | API cost overrun | Budget | Caps, cheaper model for ablations, stop rule |
| R8 | Risk | Stories drift from the shared contracts | Integration bugs | Contracts module, reviewer sign-off flags, cumulative gate tests |
| R9 | Risk | Docker socket access on the worker is powerful | Host compromise if the worker is compromised | Worker runs on a dedicated host/VM, the sandbox is hardened, gVisor is optional. Documented in `docs/security.md`. |
| A1 | Assumption | Target repos are Python with a runnable test suite | Scope | Non-Python is post-v1 |
| A2 | Assumption | A 60 s pickup delay is fine | UX | Webhooks post-v1 |
| A3 | Assumption | One worker host is enough for v1 | Scale | Bundles allow cross-host resume. Multi-host is tested only in chaos. |
| D1 | Open decision | `aioboto3` vs `boto3` in threads for blobs | Minor | Developer decides in LH-202, ADR-0005 |
| D2 | Open decision | `fastembed` vs `sentence-transformers` | Image size | Developer decides in LH-601, ADR-0010 |
| D3 | Open decision | GitHub App in v1 or PAT only | Security story | Hooman, before Phase 3. Default: PAT for demos, App as Should. |
| D4 | Open decision | Draft PR on stuck (`open_draft_on_stuck`) default | Noise vs visibility | Hooman, Phase 5. Default: off. |
| D5 | Open decision | Make the repo public before v1.0.0? | Visibility | Hooman. Default: public at v1.0.0. |

---

## 10. Budget and timeline

### 10.1 Money

The total is about $420 to $575 (section 8.8). Spending is front-loaded only at Phase 9 eval runs. Phases 0 to 8 cost about $40 in API calls, because CI uses the scripted provider.

### 10.2 Calendar

Assuming about 12 to 15 story points per working day, with reviews in the evening. Full-time focus compresses this to about 4 weeks.

| Week | Phases | Points | Milestone |
|---|---|---|---|
| 1 | Phase 0, Phase 1 | 60 | `v0.0.1`, `v0.1.0`: agent fixes fixtures in a sandbox |
| 2 | Phase 2, first half of Phase 3 | ~55 | `v0.2.0`: crash-proof runs, chaos-lite green |
| 3 | Rest of Phase 3, Phase 4 | ~39 | `v0.3.0`, `v0.4.0`: label to PR on real GitHub, long tasks |
| 4 | Phase 5, Phase 6 | 51 | `v0.5.0`, `v0.6.0`: plan, verify, stuck, memory |
| 5 | Phase 7, Phase 8 | 48 | `v0.7.0`, `v0.8.0`: budgets, telemetry, console |
| 6 | Phase 9 | 40 + eval runs | `v1.0.0`: results, chaos table, docs, video, case study |

Cut line if time runs short: drop LH-908 and LH-909 (Could), then LH-806 (memory viewer, which the CLI covers), then LH-105 (OpenAI provider). The core claims don't depend on them.

---

## 11. Launch kit

### 11.1 README outline

1. **Title and one line:** "Longhaul: a background coding agent that survives crashes and tells you when it's stuck."
2. **Headline numbers (real, from `docs/results.md`):** chaos completion `[N]/20` with `[0]` duplicate side effects; full vs naive resolve rate on SWE-bench Verified subset `[x]%` vs `[y]%`; `$[z]` per resolved task.
3. **30-second GIF:** label issue, plan comment, approve, PR with run report.
4. **How it works:** the system context diagram and four bullets (durable turns, git checkpoints, two-tier context, plan/verify/stuck).
5. **Quickstart:** `make setup && make up && make demo` (scripted provider, no keys).
6. **Run on your repo:** PAT scopes, `longhaul.yaml`, `longhaul poller install`.
7. **Results:** Tables 1 to 4 and a link to the full method.
8. **Design decisions:** one line each, linked to the ADRs.
9. **Safety:** sandbox, no credentials inside, budgets, human merge.
10. **Limitations:** Python only, single host, polling delay, numbers from a 40-task subset.
11. **License.**

### 11.2 esteki.ca case study outline

This follows the format of the existing project cards: a short problem line, one hero metric, a one-line description, and "See the project".

- **Card:** "Agents crash and loop quietly." **Hero metric:** `[20/20]` runs resumed after crashes. **Tag:** Long-running agents, open source. **Title:** Longhaul. **Line:** "Works an issue for hours. Survives crashes. Asks when stuck." **Filters:** AI engineering, Software, Machine learning.
- **Page sections:**
  1. The problem, in three sentences, with the five failure modes.
  2. What I built: the diagram and the four ideas.
  3. Proof: the chaos table, the resolve rate vs baselines with intervals, and dollars per resolved.
  4. What didn't work: two or three honest findings from the failure list, for example "compaction hurt on X" if the data says so.
  5. Decisions I'd defend: Temporal over an SDK loop, git micro-checkpoints, memory write policy.
  6. Links: repo, results, demo video.

### 11.3 Two-minute demo script

| Time | Screen | Voice-over |
|---|---|---|
| 0:00 | Issue on `lh-demo-inventory` | "This bug needs a new exception class and tests. I label it `longhaul` and walk away." |
| 0:12 | Plan comment appears | "It reads the repo, reproduces the bug and posts a plan. I approve it with one comment." |
| 0:28 | Console timeline, turns streaming | "Every model turn and tool call is a durable step. Each change is a git checkpoint." |
| 0:45 | Terminal: `kill -9` worker | "Now I kill the worker mid-test-run." |
| 0:52 | Temporal UI shows retry, timeline resumes | "A new worker picks it up, resets to the last checkpoint and carries on. Nothing ran twice." |
| 1:10 | Verifier sends work back | "The Verifier runs the tests, finds a missed edge case and sends it back." |
| 1:25 | PR with run report | "The PR has one commit per plan step and a run report: 31 minutes, 46 turns, $[x]." |
| 1:40 | Results table | "On 40 SWE-bench Verified tasks it resolves [x]% vs [y]% for a naive loop on the same model. Every number comes from the committed result files." |
| 1:55 | Repo link | "Code, method and failures are all in the repo." |

### 11.4 Resume bullet templates

Fill these only with real numbers from `docs/results.md`.

- Built Longhaul, an open-source background coding agent on Temporal that runs GitHub issues for hours in hardened Docker sandboxes. It resumed `[N]/20` chaos runs after worker, sandbox and database kills, with `[0]` duplicate GitHub side effects.
- Designed two-tier context management (tool-output handles plus structured compaction), cutting median input tokens per task by `[x]%` on long tasks with a `[±y]`-point change in resolve rate.
- Implemented planner, coder and verifier roles with stuck detection and a human hint protocol, reaching `[x]%` vs `[y]%` (naive loop) resolve rate on a 40-task SWE-bench Verified subset at `$[z]` per resolved task.
- Added per-task and per-repo budgets, a cost ledger, OpenTelemetry GenAI traces, Prometheus/Grafana dashboards and a React/TypeScript console for approvals, hints and memory review.

---

## 12. Appendix

### 12.1 Glossary

| Term | Meaning |
|---|---|
| Activity | A Temporal unit of work that can do I/O. It is retried on failure according to its policy. |
| Workflow | Deterministic Temporal code that orchestrates activities. It is replayed from history after a crash. |
| Continue-As-New | Ends the current workflow run and starts a fresh one with the same id and carried state, to keep history small. |
| Checkpoint | A git commit on the scratch branch in the task's bare repo. |
| `pre_sha` / `post_sha` | Workspace commit before and after a tool call. Retries reset to `pre_sha`. |
| Handle | A reference (`out_<call_id>`) to a full tool output stored in blob storage. |
| Compaction | Replacing older turns with structured progress notes. |
| Elision | Replacing an old tool output in the model's view with a one-line stub and its handle. |
| Side effect | Any write outside our system (GitHub comment, label, PR, push). |
| Idempotency key | `workflow_id:purpose:seq`. It guarantees a side effect happens once. |
| Intervention | Any human input after the task starts, other than plan approval: a hint, budget top-up or stuck unblock. |
| Resolve | All repo tests plus the hidden or official tests pass after applying the agent's patch. |

### 12.2 References

- Temporal documentation: https://docs.temporal.io/
- Temporal Python SDK: https://github.com/temporalio/sdk-python
- Anthropic Messages API: https://docs.claude.com/en/api/messages
- Tool use with Claude: https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview
- Prompt caching: https://docs.claude.com/en/docs/build-with-claude/prompt-caching
- Anthropic, Building effective agents: https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, Effective context engineering for AI agents: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- SWE-bench: https://www.swebench.com/ and https://github.com/SWE-bench/SWE-bench
- Introducing SWE-bench Verified: https://openai.com/index/introducing-swe-bench-verified/
- OpenTelemetry GenAI semantic conventions: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- pgvector: https://github.com/pgvector/pgvector
- Aider repo map: https://aider.chat/docs/repomap.html
- OpenHands: https://github.com/All-Hands-AI/OpenHands
- GitHub fine-grained personal access tokens: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- GitHub App installation tokens: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
- gVisor: https://gvisor.dev/
- Wilson score interval: https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval
- McNemar's test: https://en.wikipedia.org/wiki/McNemar%27s_test
