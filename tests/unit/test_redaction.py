"""telemetry/logging.py: redact(), the function that logs, run reports and the console share.

CI runs gitleaks over the whole history, so no secret-shaped string is written out in this
file. Each fake is put together from parts when the test module loads.
"""

import json

import pytest

from longhaul.telemetry.logging import REDACTED, redact

pytestmark = [pytest.mark.unit, pytest.mark.phase7]


def openai_key(length: int = 32) -> str:
    return "sk-" + ("Ab3_-" * 20)[:length]


def github_token(length: int = 36) -> str:
    return "ghp_" + "a" * length


def github_app_token(prefix: str, length: int = 36) -> str:
    """Other GitHub tokens: installation (ghs), user (ghu), OAuth (gho) and refresh (ghr)."""
    return prefix + "_" + "a" * length


def fine_grained_token(length: int = 40) -> str:
    return "github_pat_" + ("11AB_" * 20)[:length]


def aws_key_id(length: int = 16) -> str:
    return "AKIA" + ("Q7ZX" * 8)[:length]


def key_block(kind: str = "RSA", *, end: bool = True) -> str:
    """A private key with a body of three lines. Without `end` the text stops mid-key."""
    head = "-----BEGIN " + kind + " PRIVATE KEY-----"
    foot = "-----END " + kind + " PRIVATE KEY-----"
    body = "\n".join(["MIIEowIBAAKCAQEA" + "x" * 20, "y" * 64, "z" * 31 + "=="])
    return f"{head}\n{body}\n{foot}" if end else f"{head}\n{body}"


PATTERN_CASES = [
    # One case per pattern of the story, the secret inside a sentence.
    pytest.param(f"key is {openai_key()} ok", f"key is {REDACTED} ok", id="openai-key"),
    pytest.param(
        "OPENAI_API_KEY=" + "sk-ant-api03-" + "Zq9_" * 6,
        "OPENAI_API_KEY=" + REDACTED,
        id="anthropic-key-with-hyphens",
    ),
    pytest.param(f"token {github_token()}.", f"token {REDACTED}.", id="github-token"),
    # ADR-0012: the installation token of a GitHub App starts with ghs_, and the
    # app and OAuth flows issue ghu_, gho_ and ghr_ tokens that can be just as powerful.
    pytest.param(
        f"token {github_app_token('ghs')}.", f"token {REDACTED}.", id="installation-token"
    ),
    pytest.param(f"token {github_app_token('ghu')}.", f"token {REDACTED}.", id="user-to-server"),
    pytest.param(f"token {github_app_token('gho')}.", f"token {REDACTED}.", id="oauth-token"),
    pytest.param(f"token {github_app_token('ghr')}.", f"token {REDACTED}.", id="refresh-token"),
    pytest.param(github_app_token("ghs", 30), REDACTED, id="installation-token-at-its-shortest"),
    pytest.param(
        github_app_token("ghs", 29),
        github_app_token("ghs", 29),
        id="installation-token-one-short",
    ),
    pytest.param(
        f'{{"pat": "{fine_grained_token()}"}}', f'{{"pat": "{REDACTED}"}}', id="github-pat"
    ),
    pytest.param(f"id={aws_key_id()};", f"id={REDACTED};", id="aws-access-key-id"),
    pytest.param(
        f"before\n{key_block()}\nafter",
        f"before\n{REDACTED}\nafter",
        id="private-key-block-through-its-end-line",
    ),
    pytest.param(key_block("OPENSSH"), REDACTED, id="openssh-private-key-block"),
    pytest.param(
        "-----BEGIN " + "PRIVATE KEY-----\nabc\n-----END " + "PRIVATE KEY-----",
        REDACTED,
        id="private-key-block-without-a-kind",
    ),
    pytest.param(
        f"{key_block('RSA')}\nmiddle\n{key_block('EC')}",
        f"{REDACTED}\nmiddle\n{REDACTED}",
        id="two-private-key-blocks",
    ),
    pytest.param(
        f"output cut short:\n{key_block(end=False)}",
        f"output cut short:\n{REDACTED}",
        id="private-key-block-cut-before-its-end-line",
    ),
    pytest.param(
        f"{openai_key()} and {github_token()} and {aws_key_id()}",
        f"{REDACTED} and {REDACTED} and {REDACTED}",
        id="several-secrets-in-one-line",
    ),
    # The length limits of the patterns: the shortest secret and one character too short.
    pytest.param(openai_key(20), REDACTED, id="openai-key-at-its-shortest"),
    pytest.param(github_token(30), REDACTED, id="github-token-at-its-shortest"),
    pytest.param(fine_grained_token(30), REDACTED, id="github-pat-at-its-shortest"),
    # Text that must stay as it is.
    pytest.param("an ordinary line of text", "an ordinary line of text", id="plain-text"),
    pytest.param(openai_key(19), openai_key(19), id="openai-key-one-short"),
    pytest.param(github_token(29), github_token(29), id="github-token-one-short"),
    pytest.param(fine_grained_token(29), fine_grained_token(29), id="github-pat-one-short"),
    pytest.param(aws_key_id(15), aws_key_id(15), id="aws-key-one-short"),
    pytest.param("akia" + "q7zx" * 4, "akia" + "q7zx" * 4, id="aws-prefix-in-lower-case"),
    pytest.param(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI public-key",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI public-key",
        id="public-key-line",
    ),
    pytest.param(
        "-----BEGIN " + "PUBLIC KEY-----\nabc\n-----END " + "PUBLIC KEY-----",
        "-----BEGIN " + "PUBLIC KEY-----\nabc\n-----END " + "PUBLIC KEY-----",
        id="public-key-block",
    ),
    pytest.param(
        "the private key is read from a file",
        "the private key is read from a file",
        id="the-words-private-key",
    ),
    # A workflow id such as this one holds "sk-" inside "task-" and runs on for 20 characters.
    # Only a key that starts a word is a key.
    pytest.param(
        "lh-acme-my-task-tracker-pro-edition-v2-5",
        "lh-acme-my-task-tracker-pro-edition-v2-5",
        id="sk-inside-a-longer-word",
    ),
    pytest.param(
        "OK\\ntask-tracker-pro-edition-v2-5",
        "OK\\ntask-tracker-pro-edition-v2-5",
        id="sk-inside-a-longer-word-after-an-escaped-newline",
    ),
    # Text that shows a newline, tab or carriage return as two characters (a repr, a %r log
    # argument, str() of a list, JSON) puts a letter just before the key. It still starts a word.
    pytest.param(
        "OK\\n" + openai_key(), "OK\\n" + REDACTED, id="openai-key-after-an-escaped-newline"
    ),
    pytest.param("OK\\t" + openai_key(), "OK\\t" + REDACTED, id="openai-key-after-an-escaped-tab"),
    pytest.param(
        "OK\\r" + openai_key(),
        "OK\\r" + REDACTED,
        id="openai-key-after-an-escaped-carriage-return",
    ),
    pytest.param(
        repr("OK\n" + openai_key()),
        repr("OK\n" + REDACTED),
        id="openai-key-in-the-repr-of-a-multi-line-string",
    ),
    pytest.param(
        json.dumps({"stdout": "OK\n" + openai_key()}),
        json.dumps({"stdout": "OK\n" + REDACTED}),
        id="openai-key-in-json-encoded-output",
    ),
    pytest.param("", "", id="empty-text"),
]


@pytest.mark.parametrize(("text", "expected"), PATTERN_CASES)
def test_patterns(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_a_second_pass_changes_nothing() -> None:
    once = redact(f"{openai_key()}\n{key_block()}\n{github_token()}")
    assert redact(once) == once


def test_a_long_output_with_one_secret_keeps_the_rest() -> None:
    filler = "line of tool output\n" * 5_000
    text = filler + f"export TOKEN={github_token()}\n" + filler
    assert redact(text) == filler + f"export TOKEN={REDACTED}\n" + filler


def test_the_marker_is_the_one_the_story_names() -> None:
    assert REDACTED == "[redacted]"
