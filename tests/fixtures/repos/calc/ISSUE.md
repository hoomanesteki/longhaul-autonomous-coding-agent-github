# add() returns the difference

`calc.add` subtracts its arguments instead of adding them.

```python
from calc import add

add(2, 3)  # returns -1, expected 5
```

`multiply` looks fine. Only `add` is wrong.

Repro: run `python -m pytest -q`. The test `tests/test_calc.py::test_add_positive_numbers` fails. The other tests should keep passing.
