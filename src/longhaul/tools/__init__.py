"""Agent tools and the registry that runs them."""


class ToolError(Exception):
    """Input a tool cannot act on and the model can fix by changing its call.

    The message is written for the model. The registry turns this error into an
    ok=False result. Errors that are not about the input, such as a container
    that no longer exists, are not ToolErrors and travel up to the caller.
    """
