"""The inputs of the GitHub write activities, and the idempotency key format.

`post_comment`, `add_label`, `remove_label` and `create_pr` are activities (github/side_effects.py
registers them), and TaskWorkflow calls the first three by name with these models (LH-306). The
workflow may not import side_effects, because that module reaches the HTTP client and SQLAlchemy
(invariant I1). The models and the key format therefore sit here, in a module with no I/O, and
side_effects imports them from here and keeps exporting them.
"""

from pydantic import BaseModel

__all__ = ["CommentInput", "LabelInput", "PullRequestInput", "key_of"]


def key_of(workflow_id: str, purpose: str, seq: int) -> str:
    """The text `workflow_id:purpose:seq` of an idempotency key (plan section 3.7).

    It checks nothing. side_effects.effect_key checks the parts, and every write checks the
    whole key again before it uses it, so a key built here that is not valid fails there with a
    message that says why.
    """
    return f"{workflow_id}:{purpose}:{seq}"


class CommentInput(BaseModel):
    task_id: str
    repo: str
    number: int  # an issue or a pull request
    body: str
    idempotency_key: str


class LabelInput(BaseModel):
    task_id: str
    repo: str
    number: int
    label: str
    idempotency_key: str


class PullRequestInput(BaseModel):
    task_id: str
    repo: str
    head: str  # a branch of the repo, or owner:branch
    base: str
    title: str
    body: str
    draft: bool = True  # until a verifier has passed the work (Phase 5)
    idempotency_key: str
