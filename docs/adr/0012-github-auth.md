# ADR-0012: GitHub auth

- Status: Accepted
- Date: 2026-10-31

## Context

Longhaul reads issues, comments and pull requests from GitHub, writes comments, labels and pull requests back, and clones and pushes from the worker host (ADR-0009). Every one of those calls needs a credential. It is the most damaging secret the system holds, because write access to a repo is the power to push code.

Four forces shape the choice:

- A developer on a laptop wants the shortest path to a working setup. A shared deployment wants credentials that expire by themselves and do not belong to one person.
- The token must stay in the worker process environment (plan section 4.2). It must not reach the sandbox, a git config file, a remote URL, a log line or an exception message. Exception text ends up in Temporal's event history, so it counts as a log.
- GitHub limits requests per credential and signals the limit in several ways: a 429, a 403 with `x-ratelimit-remaining: 0`, or a 403 with a "secondary rate limit" message, with `retry-after` or `x-ratelimit-reset` as a hint in some of them. A plain 403 means "no permission", which no retry fixes. The client has to tell these apart (failure mode F10).
- Git needs the credential for clone and push, and it needs it by a different route than the API client.

## Decision

Two ways to authenticate, behind one small interface. Before every request the client asks `auth.token()` and sends the answer as `Authorization: Bearer <token>`. That is why an expiring token can renew itself without the client knowing.

1. **Personal access token, for local development (Must).** `PatAuth` holds the fine-grained token from `LONGHAUL_GITHUB_TOKEN`. Create it for the allowlisted repos only, with read and write on contents, issues and pull requests. Metadata read comes with every token.
2. **GitHub App, for any shared deployment (Should).** `AppAuth(app_id, key_path, installation_id)` signs a JWT with the app's private key (RS256, using PyJWT and cryptography). The JWT is dated one minute back for clock drift and lives nine minutes, under GitHub's limit of ten. It is exchanged at `POST /app/installations/{id}/access_tokens` for an installation token that lasts one hour. The token is cached until five minutes before it expires, and a lock lets one caller do the exchange while concurrent callers wait for its result. The key is read from `LONGHAUL_GITHUB_APP_KEY_PATH` on each exchange, so a rotated key needs no restart.
3. **Git gets the token one process at a time.** `git_auth_header(token)` returns `-c http.https://github.com/.extraheader=AUTHORIZATION: basic <base64 of x-access-token:token>`. The host puts these two arguments before `clone`, `fetch` or `push`. The setting lives for that one command and applies to github.com only, so it is never written to `.git/config`, never part of a remote URL, and never sent to another host after a redirect.
4. **The client reports and never retries.** It maps every response to `GitHubRetryable` or `GitHubFatal`, and the activity retry policy does the retrying. A write that reached GitHub but lost its reply is therefore reported as retryable, and the idempotency key of LH-304 finds it again.
5. **Tokens stay out of reprs, errors and logs.** The auth objects print no token. Text that comes back from a server or a transport has the token replaced before it is shortened, because a cut through the middle of a token hides it from the replacement. A token with spaces or non-ASCII characters is refused when the auth object is built: httpx2 reports an illegal header value by quoting the whole header.
6. **No redirects, and next-page links stay on the API host.** GitHub redirects the old name of a transferred repo to its new owner, and following that would leave the allowlist. A `Link` header that points elsewhere would carry the token with it.

How item 4 maps a response:

| Response | Error | Wait (`retry_after_s`) |
|---|---|---|
| Network error, timeout | `GitHubRetryable` | none, the policy's backoff |
| 5xx, 408 | `GitHubRetryable` | `retry-after` if sent |
| 429 | `GitHubRetryable` | see below |
| 403 with `retry-after`, `x-ratelimit-remaining: 0`, or "rate limit" or "abuse detection" in the message | `GitHubRetryable` | see below |
| 200 with a body that is not the JSON GitHub sends | `GitHubRetryable` | none |
| 3xx | `GitHubFatal` | |
| Every other 4xx, including a plain 403, 404 and 422 | `GitHubFatal` | |

For a rate limit the wait is the longer of `retry-after` and the time until `x-ratelimit-reset` (used only while the quota is spent), or 60 seconds when GitHub gives neither, which is the minimum it documents for secondary limits. No wait is longer than one hour.

## Consequences

- Local setup is one environment variable. A shared deployment needs an app registration, an installation and a key file on the worker host.
- FakeGitHub has no App endpoints, so the `AppAuth` tests answer the token exchange with a mocked endpoint and a generated key. The real exchange is checked once by hand in the Phase 3 smoke test.
- With a PAT, comments and pull requests appear under the token owner's name. With an App they appear as `app[bot]`. Idempotency does not depend on the author, only on the hidden key marker in the comment.
- A PAT does not renew itself. When it expires or is revoked, the first sign is a `GitHubFatal` 401 "Bad credentials". An installation token renews itself.
- `AppAuth` needs the worker clock to be close to GitHub's. More drift than the one minute of margin gives a 401, which is fatal and visible.
- The git header is base64, not encryption, and it shows in the process list while git runs. That is acceptable on a single-user machine or in a container that runs only the worker. On a host shared with other users, pass the same setting through the `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and `GIT_CONFIG_VALUE_0` environment variables instead.
- The log redaction filter (plan section 4.2) must also match `ghs_`, the prefix of installation tokens, next to `ghp_` and `github_pat_`.
- A renamed or transferred repo fails loudly until someone updates the allowlist. That is the intended outcome.

## Alternatives

- **Classic PAT with the `repo` scope.** Rejected: it reaches every repo the user can reach, so a leak costs far more than a fine-grained token's.
- **A PAT for everything.** Rejected for shared deployments: it belongs to a person, expires on their schedule and acts under their name. It stays as the local option because it needs no registration.
- **A GitHub App for everything.** Rejected: registering an app, downloading a key and installing it is too much setup for a first local run.
- **OAuth tokens.** Rejected: they need an interactive login and a callback URL.
- **The token in the remote URL, a credential helper or `.git-credentials`.** Rejected: the token lands in `.git/config` or on disk, and shows in `git remote -v` and in git's error messages.
- **SSH deploy keys.** Rejected: one key per repo, and they cannot call the REST API.
- **A client library such as PyGithub or githubkit.** Rejected: Longhaul needs about ten endpoints. A library adds a dependency and its own retry and error types on top of the activity retry policy, and a thin client is easy to test against FakeGitHub.
