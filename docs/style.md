# Voice and style guide

These rules cover the README, docs, comments, commit messages and PR descriptions.

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
  - robust, seamless, seamlessly, leverage, comprehensive, cutting-edge, state-of-the-art (unless citing a paper)
  - game-changer, unlock, empower, elevate, supercharge, best-in-class
  - em dashes, emoji
  - `tests/unit/test_docs_style.py` checks the docs for these and for a few filler phrases.
