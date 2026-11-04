# Recording of a real run of the inventory fix

This folder holds the model calls of one real run on `tests/fixtures/repos/inventory`, one JSON
file per call. The full chaos suite (`pytest -m "chaos and not chaos_lite"`) plays them with the
replay provider, so every run sees the same model answers and a difference in the final diff is a
durability bug and not model noise.

It is empty until the owner records a run, because that needs a key and costs money. Until then
the suite records a scripted run of `tests/fixtures/scripts/inventory_fix.yaml` into a temporary
folder and plays that. To record, see "Record and replay model calls" in `docs/runbook.md`.
