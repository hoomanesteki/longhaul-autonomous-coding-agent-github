# Twelve helpers in longlog return wrong results

Each module in `src/longlog` has one bug, and each has one test in `tests/` that shows it. All
twelve tests fail.

Repro: run `python -m pytest -q`. The suite prints thousands of trace lines before its summary,
so start from the summary line. The failing tests are:

- tests/test_chunks.py::test_chunk_keeps_the_last_partial_chunk
- tests/test_clamp.py::test_clamp_allows_the_upper_bound
- tests/test_dedupe.py::test_dedupe_keeps_the_first_copy
- tests/test_initials.py::test_initials_uses_every_word
- tests/test_leap.py::test_a_century_divisible_by_400_is_a_leap_year
- tests/test_ordinal.py::test_the_teens_end_in_th
- tests/test_pages.py::test_page_count_rounds_up
- tests/test_percent.py::test_percent_of_a_whole
- tests/test_ranges.py::test_inclusive_range_includes_the_stop
- tests/test_slug.py::test_slugify_lowercases
- tests/test_stats.py::test_mean_keeps_the_fraction
- tests/test_words.py::test_word_count_ignores_extra_spaces

Fix one module at a time. The fix for each is small. When you are done, all twelve tests pass.
