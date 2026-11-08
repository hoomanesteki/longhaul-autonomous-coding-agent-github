# textkit

Text helpers for building identifiers and labels.

```python
from textkit import slugify, truncate

slugify("Hello, World!")  # "hello-world"
truncate("a long sentence", 10)  # "a long ..."
```

`textkit.config.load_defaults(path)` reads default settings from a TOML file.

Run the tests with `python -m pytest -q`.
