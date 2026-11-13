# Two-minute demo script

The script for the demo video and the 30-second GIF. The video and the GIF are not recorded yet. The repo is public and the docs site is live. The open steps are listed in the checklist at the end.

## What the video shows

One real task on a real repo, from a labeled issue to a pull request, with a worker killed in the middle. The model is a live one, so the recording needs an API key. The repo is `hoomanesteki/lh-demo-inventory`, which the example config already allowlists.

Every number said aloud is read from a committed file or from the run report on screen. A number that does not exist yet is not said.

## Before recording

- `make up`, `make sandbox-image` and `make migrate` are done, and the worker runs against the config with `lh-demo-inventory` allowlisted and `plan_approval_required: true`.
- `LONGHAUL_GITHUB_TOKEN`, `LONGHAUL_PROVIDER=anthropic` and the model key are set in the worker's environment, and the poller is installed.
- The console is built and served by `longhaul api` with `LONGHAUL_ADMIN_TOKEN` set.
- An issue on `lh-demo-inventory` asks for a new exception class and tests for it.
- Open: the issue, the Temporal UI at http://localhost:8233, the console, a terminal, and the pull request. `scripts/chaos_kill.sh` shows the kill on a scripted task if a live one is too slow to time.
- The tabs are cleared of anything private. No token is on screen.

## Timeline

| Time | Screen | Voice-over |
|---|---|---|
| 0:00 | Issue on `lh-demo-inventory` | "This bug needs a new exception class and tests. I label it `longhaul` and walk away." |
| 0:12 | Plan comment appears | "It reads the repo, reproduces the bug and posts a plan. I approve it with one comment." |
| 0:28 | Console timeline, turns streaming | "Every model turn and tool call is a durable step. Each change is a git checkpoint." |
| 0:45 | Terminal: `kill -9` of the worker | "Now I kill the worker mid-test-run." |
| 0:52 | Temporal UI shows the retry, the timeline resumes | "A new worker picks it up, resets to the last checkpoint and carries on. Nothing ran twice." |
| 1:10 | Verifier sends work back | "The Verifier runs the tests, finds a missed edge case and sends it back." |
| 1:25 | Pull request with the run report | "The PR has one commit per plan step and a run report. Here are the minutes, the turns and the dollars it shows." |
| 1:40 | Results page | "The chaos table: 18 runs survived one to three kills, and all 20 ended with the same diff and no duplicate write. The resolve rates against a naive loop come from the results page once those runs are made." |
| 1:55 | Repo link | "Code, method and failures are all in the repo." |

Notes on three rows:

- **0:52.** Temporal notices a dead worker when the heartbeat timeout passes, which is 15 to 60 seconds with the default timeouts. Cut the wait in the edit and keep the retry attempt visible in the Temporal UI.
- **1:25.** Read the three figures off the run report of the task as it is. Do not round them up.
- **1:40.** This row has two versions. Before the eval runs exist, show the chaos table and say the model answers in it were scripted. After the runs in `docs/results.md` are made, show its resolve-rate table and read the rates and intervals from the page. Say nothing about a number the page does not hold.

## The 30-second GIF

Cut from the same take, in this order: the label on the issue, the plan comment, the approval comment, and the pull request with its run report. It is saved as `docs/media/demo.gif`, and the README names that path in its Demo section.

## Recording checklist

These are the owner's steps. The repo is public, its secrets check is done, the docs site is live and `v1.0.0` is tagged. The rest is open.

- [ ] Run the evaluation commands in `docs/results.md` with a live model, and rebuild that page.
- [ ] Replace the pending lines in the README (Proof, and Status and limitations), `docs/case-study.md` and the 1:40 row above with the numbers on the rebuilt page.
- [ ] Record the video from the timeline above on `lh-demo-inventory`.
- [ ] Cut the 30-second GIF and save it as `docs/media/demo.gif`, then add the image to the README Demo section.
- [ ] Put the video link in the README and in `docs/case-study.md`.
- [x] Check the repo for secrets and private paths, then make the repo public. The repo is public. `gitleaks git --log-opts=--all` found no leaks in 583 commits on 2026-11-11.
- [x] Turn on GitHub Pages as the runbook says ("Docs site"). The site is live since 2026-11-12.
- [x] Merge `dev` into `main` at the Phase 9 gate and tag `v1.0.0`.
- [ ] Add the project card and page to the portfolio site from `docs/case-study.md`.
