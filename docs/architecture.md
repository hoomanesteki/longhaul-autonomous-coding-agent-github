# Architecture

Longhaul runs a coding agent as a Temporal workflow. The workflow is pure and
only schedules activities. Activities do the I/O: calling the model, running
tools in a sandbox, writing checkpoints and talking to GitHub.

All pieces share one module of types, `src/longhaul/contracts.py`. It has no
I/O and imports only the standard library and pydantic. Vendor shapes, such as
the Anthropic Messages API format, live in `src/longhaul/agent/render.py`.

## Contracts changelog

Every change to `contracts.py` gets an entry here, with the new
`contracts_version` and the reason. Changes also need a note in the PR.

| Version | Change |
|---|---|
| 1 | First version (LH-101). Records, messages and blocks, provider errors, task and plan types, carry state, and the protocols for providers, sandboxes, blobs, checkpoints, memory and GitHub. `Budgets` and `RepoSettings` moved here from `settings.py`. |
