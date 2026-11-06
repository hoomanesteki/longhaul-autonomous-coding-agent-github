import pytest
from textkit import camel_to_snake


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("userName", "user_name"),
        ("HTTPServer", "http_server"),
        ("getHTTPResponseCode", "get_http_response_code"),
        ("already_snake", "already_snake"),
        ("word", "word"),
        ("", ""),
    ],
)
def test_camel_to_snake(text: str, expected: str) -> None:
    assert camel_to_snake(text) == expected
