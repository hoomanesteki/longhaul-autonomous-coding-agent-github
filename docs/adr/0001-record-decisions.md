# ADR-0001: Record decisions as ADRs

- Status: Accepted
- Date: 2026-10-25

## Context

Choices in this project have effects that last longer than the code around them. Six months from now, the code will say what we did but not why. A later reader, human or agent, needs the reasoning to change a decision safely.

## Decision

Every non-obvious choice gets a short ADR in `docs/adr/NNNN-title.md`, in the same PR as the code it describes. Start from `0000-template.md`. A superseded ADR stays in the repo, with its status changed to point at the new one.

## Consequences

Each decision costs a few minutes of writing. Reviewers can check the code against the stated decision. The ADR folder becomes the index of why the system looks the way it does.

## Alternatives

- Put reasoning only in PR descriptions. PR text is hard to find after merge and does not follow the code.
- Keep a wiki. A wiki drifts from the code and has no review step.
