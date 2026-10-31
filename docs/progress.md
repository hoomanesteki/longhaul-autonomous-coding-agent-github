# Progress

Status values: Todo, In progress, Blocked, Done. One table per phase.

## Phase 0: Foundation and backlog import (tag v0.0.1)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-001 | Repo scaffold and tooling | 3 | Done | merge | 2026-10-25 |
| LH-002 | Local stack with docker compose | 3 | Done | merge | 2026-10-25 |
| LH-003 | CI pipeline and branch protection | 3 | In progress | merge | |
| LH-004 | CLAUDE.md, ADRs and contribution docs | 3 | Done | merge | 2026-10-25 |
| LH-005 | Settings and config loading | 2 | Done | merge | 2026-10-25 |
| LH-006 | Backlog import into GitHub issues | 3 | In progress | merge | |
| LH-007 | Toy repo fixtures | 2 | Done | merge | 2026-10-25 |
| LH-008 | Database baseline and migrations | 2 | Done | merge | 2026-10-25 |

## Phase 1: Agent core without durability (tag v0.1.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-101 | Shared contracts module | 3 | Done | merge | 2026-10-30 |
| LH-102 | Spike: Messages API tool use, caching and token counting | 2 | Done | merge | 2026-10-30 |
| LH-103 | Provider protocol and ScriptedProvider | 3 | Done | merge | 2026-10-30 |
| LH-104 | AnthropicProvider | 5 | Done | merge | 2026-10-30 |
| LH-105 | OpenAI-compatible provider (Should) | 3 | Done | merge | 2026-10-30 |
| LH-106 | Sandbox image | 2 | Done | merge | 2026-10-30 |
| LH-107 | Docker sandbox manager | 5 | Done | merge | 2026-10-30 |
| LH-108 | Tool registry and file tools | 5 | Done | merge | 2026-10-30 |
| LH-109 | Exec and git tools | 3 | Done | merge | 2026-10-30 |
| LH-110 | Turn functions and in-process loop | 5 | Done | merge | 2026-10-30 |
| LH-111 | run-local CLI and first e2e | 3 | In progress | | |
| LH-191 | Sandbox write and exec safety | 5 | Done | merge | 2026-10-30 |
| LH-192 | Tool input and output hardening | 5 | Done | merge | 2026-10-30 |
| LH-193 | OpenAI-compatible provider mapping fixes | 3 | Done | merge | 2026-10-30 |
| LH-194 | Settings, env and database safety | 3 | Done | merge | 2026-10-30 |
| LH-195 | CI, gate and test collection integrity | 3 | Done | merge | 2026-10-30 |
| LH-196 | Backlog import safety | 2 | Done | merge | 2026-10-30 |
| LH-197 | Scripted provider determinism | 3 | Done | merge | 2026-10-30 |
| LH-198 | Contracts v2 from the API research | 5 | Done | merge | 2026-10-30 |

## Phase 2: Temporal durability and checkpoints (tag v0.2.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-201 | Spike: Temporal Python SDK patterns | 2 | Todo | | |
| LH-202 | Blob store | 3 | Todo | | |
| LH-203 | Task workflow skeleton | 5 | Todo | | |
| LH-204 | LLM and tool activities | 5 | Todo | | |
| LH-205 | Git checkpoint store | 5 | Todo | | |
| LH-206 | Sandbox recovery and retry-safe tool execution | 5 | Todo | | |
| LH-207 | Continue-As-New | 3 | Todo | | |
| LH-208 | Worker process and run CLI | 3 | Todo | | |
| LH-209 | Chaos test v1 | 5 | Todo | | |

## Phase 3: GitHub integration (tag v0.3.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-301 | FakeGitHub test server | 3 | Todo | | |
| LH-302 | GitHub client and auth | 5 | Todo | | |
| LH-303 | Repo allowlist and issue intake | 3 | Todo | | |
| LH-304 | Idempotent side-effect activities | 5 | Todo | | |
| LH-305 | Clone from GitHub and push from the host | 5 | Todo | | |
| LH-306 | PR creation and label lifecycle | 3 | Todo | | |
| LH-307 | Poller workflow on a Temporal Schedule | 5 | Todo | | |
| LH-308 | Comment commands and issue-to-PR e2e | 5 | Todo | | |

## Phase 4: Context engineering (tag v0.4.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-401 | Token meter and context budget | 3 | Todo | | |
| LH-402 | Output handles and read_output | 3 | Todo | | |
| LH-403 | Structured compaction | 5 | Todo | | |
| LH-404 | Eviction and compaction in the workflow | 3 | Todo | | |
| LH-405 | Prompt layout and cache breakpoints | 3 | Todo | | |
| LH-406 | Context metrics and ablation flags | 2 | Todo | | |
| LH-407 | Long-task test | 3 | Todo | | |

## Phase 5: Planner, verifier and stuck handling (tag v0.5.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-501 | Planner role and plan schema | 5 | Todo | | |
| LH-502 | Human plan gate | 3 | Todo | | |
| LH-503 | Coder role with step tracking | 5 | Todo | | |
| LH-504 | Verifier role and send-back loop | 5 | Todo | | |
| LH-505 | Stuck detector | 5 | Todo | | |
| LH-506 | Pause, ask and hint flow | 3 | Todo | | |
| LH-507 | Final outcomes and stuck report | 3 | Todo | | |

## Phase 6: Memory (tag v0.6.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-601 | Embedder and memory schema | 3 | Todo | | |
| LH-602 | Repo map builder | 5 | Todo | | |
| LH-603 | Memory store API and verified commands | 3 | Todo | | |
| LH-604 | Retrieval into the prompt | 3 | Todo | | |
| LH-605 | Lesson extraction | 3 | Todo | | |
| LH-606 | Promotion on merged PRs and human approval | 3 | Todo | | |
| LH-607 | Memory poisoning test and ablation flag | 2 | Todo | | |

## Phase 7: Budgets, cost and observability (tag v0.7.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-701 | Pricing table and cost ledger | 3 | Todo | | |
| LH-702 | Budget enforcement and top-ups | 5 | Todo | | |
| LH-703 | OpenTelemetry traces | 5 | Todo | | |
| LH-704 | Prometheus metrics and Grafana dashboard | 3 | Todo | | |
| LH-705 | Run report | 3 | Todo | | |
| LH-706 | Structured logs and redaction | 2 | Todo | | |

## Phase 8: Console (tag v0.8.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-801 | Console API | 5 | Todo | | |
| LH-802 | Console scaffold and typed client | 3 | Todo | | |
| LH-803 | Tasks list page | 3 | Todo | | |
| LH-804 | Task timeline page | 5 | Todo | | |
| LH-805 | Task actions | 3 | Todo | | |
| LH-806 | Memory viewer | 3 | Todo | | |
| LH-807 | Console auth | 2 | Todo | | |
| LH-808 | Console e2e with Playwright | 3 | Todo | | |

## Phase 9: Evaluation, chaos, hardening and release (tag v1.0.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-901 | Eval task sets and runner | 5 | Todo | | |
| LH-902 | SWE-bench Verified adapter and grading | 8 | Todo | | |
| LH-903 | Baselines: single-shot and naive loop | 5 | Todo | | |
| LH-904 | Full chaos suite | 5 | Todo | | |
| LH-905 | Results report | 3 | Todo | | |
| LH-906 | Security hardening pass | 3 | Todo | | |
| LH-907 | Docs, README and demo | 3 | Todo | | |
| LH-908 | Terraform for AWS (Could) | 5 | Todo | | |
| LH-909 | Route tool calls through Keystone (Could) | 3 | Todo | | |
