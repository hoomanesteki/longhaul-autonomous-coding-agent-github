"""Agent tools and the registry that runs them."""


class ToolError(Exception):
    """Input a tool cannot act on and the model can fix by changing its call.

    The message is written for the model. The registry turns this error into an
    ok=False result. Errors that are not about the input, such as a container
    that no longer exists, are not ToolErrors and travel up to the caller.
    """


def to_utf8(text: str, what: str = "the text", *, error: type[ToolError] = ToolError) -> bytes:
    """`text` as UTF-8, or `error` when it has no UTF-8 form.

    A reply that was garbled or cut off in the middle of an emoji can leave half of a
    surrogate pair in a string. No sandbox can be handed that, and the model can fix it.
    """
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise error(f"{what} has a character that cannot be written as UTF-8") from None
