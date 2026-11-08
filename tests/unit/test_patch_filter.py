"""The patch that goes to the official harness: the agent's diff without the test files (LH-902).

The harness applies the instance's own test patch on top of the model patch. A model patch that
also changes one of those files makes the test patch fail to apply, so the files are left out.
"""

from __future__ import annotations

import pytest

from eval.swebench import adapter

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

SOURCE = """\
diff --git a/pkg/core.py b/pkg/core.py
index 1111111..2222222 100644
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

TEST_EDIT = """\
diff --git a/tests/test_core.py b/tests/test_core.py
index 3333333..4444444 100644
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1,3 +1,4 @@
 def test_add():
     assert add(1, 2) == 3
+    assert add(2, 2) == 4
"""

NEW_TEST = """\
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..5555555
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test_new():
+    assert True
"""

OTHER_TEST = """\
diff --git a/tests/test_other.py b/tests/test_other.py
index 6666666..7777777 100644
--- a/tests/test_other.py
+++ b/tests/test_other.py
@@ -1 +1,2 @@
 import os
+import sys
"""

DELETION = """\
diff --git a/tests/test_old.py b/tests/test_old.py
deleted file mode 100644
index 8888888..0000000
--- a/tests/test_old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_old():
-    pass
"""

RENAME = """\
diff --git a/tests/test_a.py b/tests/test_b.py
similarity index 90%
rename from tests/test_a.py
rename to tests/test_b.py
index 9999999..aaaaaaa 100644
--- a/tests/test_a.py
+++ b/tests/test_b.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

MODE_ONLY = """\
diff --git a/scripts/run.sh b/scripts/run.sh
old mode 100644
new mode 100755
"""

BINARY = """\
diff --git a/tests/data.bin b/tests/data.bin
index bbbbbbb..ccccccc 100644
Binary files a/tests/data.bin and b/tests/data.bin differ
"""

TEST_PATCH = TEST_EDIT + NEW_TEST + DELETION


def test_excludes_test_patch_files() -> None:
    agent = SOURCE + TEST_EDIT + OTHER_TEST + NEW_TEST

    filtered = adapter.model_patch(agent, TEST_PATCH)

    # The test patch touches test_core.py, test_new.py and test_old.py. The agent's source change
    # and its edit of an unrelated test file stay.
    assert filtered == SOURCE + OTHER_TEST


def test_the_files_of_a_test_patch_are_all_the_paths_it_names() -> None:
    assert adapter.test_patch_files(TEST_PATCH) == [
        "tests/test_core.py",
        "tests/test_new.py",
        "tests/test_old.py",
    ]


def test_a_rename_names_both_ends() -> None:
    assert adapter.test_patch_files(RENAME) == ["tests/test_a.py", "tests/test_b.py"]


def test_a_patch_that_touches_no_test_file_comes_back_unchanged() -> None:
    agent = SOURCE + OTHER_TEST

    assert adapter.model_patch(agent, TEST_PATCH) == agent


def test_a_patch_of_test_files_only_becomes_empty() -> None:
    assert adapter.model_patch(TEST_EDIT + NEW_TEST, TEST_PATCH) == ""


def test_an_empty_patch_stays_empty() -> None:
    assert adapter.model_patch("", TEST_PATCH) == ""


def test_a_rename_into_a_test_file_is_dropped() -> None:
    assert (
        adapter.model_patch(
            SOURCE + RENAME,
            "diff --git a/tests/test_b.py b/tests/test_b.py\n"
            "--- a/tests/test_b.py\n+++ b/tests/test_b.py\n",
        )
        == SOURCE
    )


def test_sections_without_hunks_are_kept_or_dropped_by_their_path() -> None:
    agent = SOURCE + MODE_ONLY + BINARY
    test_patch = "diff --git a/tests/data.bin b/tests/data.bin\nBinary files differ\n"

    assert adapter.model_patch(agent, test_patch) == SOURCE + MODE_ONLY


def test_text_inside_a_hunk_that_looks_like_a_header_does_not_split_the_patch() -> None:
    tricky = (
        "diff --git a/docs/notes.md b/docs/notes.md\n"
        "--- a/docs/notes.md\n"
        "+++ b/docs/notes.md\n"
        "@@ -1 +1,2 @@\n"
        " context\n"
        "+diff --git a/tests/test_core.py b/tests/test_core.py\n"
    )

    assert adapter.model_patch(tricky, TEST_PATCH) == tricky


def test_the_end_of_the_patch_keeps_its_newline() -> None:
    filtered = adapter.model_patch(SOURCE + TEST_EDIT, TEST_PATCH)

    assert filtered.endswith("\n")
    assert filtered == SOURCE


def test_a_patch_that_does_not_start_with_a_header_is_refused() -> None:
    with pytest.raises(ValueError, match="diff --git"):
        adapter.model_patch("not a patch\n", TEST_PATCH)
