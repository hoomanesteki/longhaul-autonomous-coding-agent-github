"""The marks in text that GitHub acts on, and how to end their effect (LH-306).

Text that the model wrote reaches GitHub in two places: the body of a pull request and the title
of its commit, which is also the title of the pull request. In both, GitHub reads a few marks as
commands:

* `#7`, `owner/repo#7` and `GH-7` refer to an issue, and after a word such as "fixes" they close
  it when the pull request merges. A squash merge writes the title on the default branch, where
  the same words close the issue again.
* `@name` notifies a person or a team.
* `https://github.com/owner/repo/issues/7` is a link to an issue, and closes it the same way.

`defuse_marks` puts an invisible zero width space after the `@`, after the `#` of a number, after
`GH-` and after `://`. Each mark stops working, and a reader sees the text that was written.
"""

import re

__all__ = ["ZERO_WIDTH_SPACE", "defuse_marks"]

ZERO_WIDTH_SPACE = "\N{ZERO WIDTH SPACE}"
# An @ that starts a word is a mention. One inside a word, as in an address, is not.
_MENTION = re.compile(r"(?<![\w@])@(?=[A-Za-z0-9])")
_ISSUE_NUMBER = re.compile(r"#(?=\d)")
# GitHub reads GH-7 as issue 7 of the same repo. A word such as "GH-style" is left alone.
_GH_NUMBER = re.compile(r"(GH-)(?=\d)", re.IGNORECASE)
_SCHEME = re.compile(r"://")


def defuse_marks(text: str) -> str:
    """The text with each mark that GitHub acts on ended. See the module docstring."""
    text = _MENTION.sub(f"@{ZERO_WIDTH_SPACE}", text)
    text = _ISSUE_NUMBER.sub(f"#{ZERO_WIDTH_SPACE}", text)
    text = _GH_NUMBER.sub(rf"\1{ZERO_WIDTH_SPACE}", text)
    return _SCHEME.sub(f"://{ZERO_WIDTH_SPACE}", text)
