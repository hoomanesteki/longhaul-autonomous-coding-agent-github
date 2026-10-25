# slugify() breaks accented letters

`textkit.slugify` treats every non-ASCII letter as a separator, so accented
words get split apart.

```python
from textkit import slugify

slugify("Crème Brûlée")  # returns "cr-me-br-l-e", expected "creme-brulee"
```

Accented Latin letters should fold to their base letter (`è` becomes `e`,
`û` becomes `u`). Plain ASCII input must behave as before.

Repro: run `python -m pytest -q`. The test `tests/test_slug.py::test_slugify_accented_latin` fails. The other tests in that file should keep passing.
