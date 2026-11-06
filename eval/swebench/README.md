# SWE-bench Verified in the eval runner (LH-902)

Longhaul runs on a fixed subset of 40 SWE-bench Verified instances, and the official harness
grades the patches. This folder holds the subset, the adapter between the runner and the harness,
and the grading script. The runbook has the short version (`docs/runbook.md`, "Run the
evaluation"). This file has the notes of the spike and the details.

| File | What it is |
|---|---|
| `select_subset.py` | Draws the 40 ids (15 easy, 20 medium, 5 hard, seed 42) and writes `subset.txt`. |
| `subset.txt` | The frozen subset. It never changes after the first headline run. |
| `adapter.py` | Turns an instance into a task, cuts the test files out of the agent's diff, writes `predictions.jsonl`. |
| `images.py` | Builds the sandbox image of an instance and copies its repo to a folder. |
| `grade.sh` | Runs the official harness on a run and puts the verdicts into the result files. |
| `report.py` | Reads the harness report. `grade.sh` calls it. |

## Spike notes

Checked against `swebench` 5.0.2 (PyPI, installed with `uv sync --extra eval`) and the dataset
`SWE-bench/SWE-bench_Verified` on 2026-11-05. Paths are inside the installed package.

**This is not the 4.x package.** The plan was written for the API where the harness builds a base
image, an environment image and an instance image from tables in `harness/constants`. Version 5
has none of those tables, and each instance has one image.

| Question | Answer in 5.0.2 |
|---|---|
| How are the images built without an evaluation? | They are not built by default. `harness/run_evaluation.py` (`create_container`) pulls the image that the dataset row names, `swebench/sweb.eval.x86_64.<id>:latest` on Docker Hub, unless it is on the machine. `docker pull --platform linux/amd64 <image>` fetches one on its own. `swebench images build TASK_REPO` (`cli/images.py`, `image_builder/prepare_images.py`) builds from the Dockerfiles of a separate task repo, which the package does not contain. Its README example `swebench images build verified` does not match the code: the argument is a path. |
| What are the images called? | `image_builder/image_spec.py`, `ImageSpec.name`: `sweb.eval.<arch>.<instance_id>:<tag>`, lowercase. A remote name gets the namespace in front and every `__` becomes `_1776_`, because Docker Hub refuses double underscores: `swebench/sweb.eval.x86_64.django_1776_django-11099:latest`. The dataset has the full name in its `image` column, and `harness/utils.py` (`make_test_spec`) reads it from there. |
| Which layers are there? | One image per instance. `image_builder/__init__.py` still lists `build_base_images` and `build_env_images` in `__all__`, but neither exists. |
| Workdir and environment? | `image_builder/constants/__init__.py`: `CONTAINER_USER = "root"`, `CONTAINER_WORKDIR = "/testbed"`, `CONTAINER_ENV_NAME = "testbed"`. The eval script activates the environment with `source /opt/miniconda3/bin/activate` and `conda activate testbed`, then `cd /testbed`. The conda environment is `/opt/miniconda3/envs/testbed`. |
| Where is the test command of a repo? | Not in the package constants any more. It is in the `eval_script` column of the dataset, between the lines `: '>>>>> Start Test Output'` and `: '>>>>> End Test Output'` (`harness/constants`, `START_TEST_OUTPUT` and `END_TEST_OUTPUT`). `make_test_spec` splits the script, and `log_parser` names the parser that reads the output (`harness/log_parsers`). |
| How is a patch graded? | `run_instance` copies the patch in and tries `git apply --verbose`, then `--3way`, then `--reject`, then `patch --fuzz=5`. It runs the eval script, which puts the test patch on top and runs the command. `harness/grading.py` (`get_eval_report`) calls the instance resolved when every FAIL_TO_PASS and PASS_TO_PASS test passes. |
| What does the harness write? | `<report_dir>/<model_name_or_path>.<run_id>.json` (`harness/reporting.py`, schema 2) with lists of ids: `resolved_ids`, `unresolved_ids`, `empty_patch_ids`, `error_ids`, `incomplete_ids`. Per instance it writes `logs/run_evaluation/<run_id>/<model>/<instance>/report.json`, `run_instance.log` and `test_output.txt` in the working directory. An empty patch starts no container. A patch that does not apply raises inside `run_instance`, writes no report file, and lands in `error_ids`. |
| Caching | The harness skips an instance that already has a `report.json` under the same run id. Use a new run id to grade the same instance again. |

Test commands that the Verified set uses, from the dataset (the test files at the end are left
out of the hint that the agent sees):

| Repo | Command |
|---|---|
| django | `./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 <labels>` |
| sympy | `PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose <files>` |
| sphinx | `tox --current-env -epy39 -v -- <files>` |
| the rest | `pytest -rA <files>` (astropy adds `-vv -o console_output_style=classic --tb=no` for one instance, seaborn adds `--no-header`) |

**The dataset id.** The plan names `princeton-nlp/SWE-bench_Verified`. It has the same 500 ids,
the same difficulty labels and the same patches as `SWE-bench/SWE-bench_Verified` (compared row by
row on 2026-11-05), so the subset is the same from both. It lacks the `image`, `eval_script`,
`log_parser` and `eval_type` columns that the harness reads, and `run_evaluation` fails on it with a
`KeyError`. Everything here uses `SWE-bench/SWE-bench_Verified`.

**Other facts.** The images are published for x86_64 only. The package calls arm64 experimental,
and its README suggests `--namespace ''`, which `run_evaluation.main` of this version does not
accept. The package asks for about 120 GB of free disk for a full run, 16 GB of RAM and 8 cores.
The difficulty labels count 194 `<15 min fix`, 261 `15 min - 1 hour`, 42 `1-4 hours` and 3
`>4 hours`.

## The subset

`uv run python -m eval.swebench.select_subset` loads the dataset and takes 15, 20 and 5 ids from
the three classes (`<15 min fix`, `15 min - 1 hour`, `1-4 hours`) with `random.Random(42)`. Each
class is sorted by id first, so the order of the rows does not matter. The file has the ids in sorted
order, one per line. By repo the 40 ids are 23 django, 6 sympy, 4 sphinx, 2 each of astropy, matplotlib and pylint, and
1 scikit-learn. The command refuses to replace an existing file without `--force`.
`uv run python -m eval.swebench.select_subset --check` draws again and compares. Run it as a
module from the repo root: as a script it puts this folder first on `sys.path`, where the folder
called `swebench` hides the real package.

## How a task runs

1. `eval/run.py --set swebench` takes the first `--limit` ids of `subset.txt` (all 40 by default)
   and loads their rows.
2. `images.py` pulls the instance image if it is missing and builds a thin image on top of it.
   The sandbox manager expects four things that a harness image does not have:
   * the user `agent` with uid 10001, because every command runs as that user;
   * the repo at `/workspace`, where the task volume is mounted and the tools work. The editable
     install of the instance points at `/testbed`, so `/testbed` stays as a link to `/workspace`;
   * the conda environment first on `PATH`. The sandbox puts `/workspace/.venv/bin` first, so
     `/workspace/.venv` is a link to `/opt/miniconda3/envs/testbed` and `python`, `pytest` and `pip`
     are the instance's own;
   * `/usr/local/bin/python3` for the supervisor that wraps each command, and ripgrep for search.
   The image has no git history. A clone in a harness image holds the commits after the base
   commit, among them the fix, and the agent must not read it. The image is tagged
   `longhaul-swebench:<instance id>-<hash of its Dockerfile>` and is reused by the next run.
3. The working tree of the instance image (the files git tracks, and the ones its install left
   that git does not ignore) is copied to a temporary folder on the host. That folder is the
   `local:` repo of the task, with a file `.longhaulimage` that names the image above. The worker
   reads the file when it prepares the sandbox, and again when it has to make a new one after a
   crash. Files that git ignores, the compiled extensions among them, stay in the image.
4. The task starts with the problem statement as the issue (the hints of the dataset are not
   shown), the plan gate off, no setup command, the network cut, and the caps of plan section 8.4:
   4 dollars, 150 turns and 120 minutes. The test command of the benchmark, without the test files,
   is the task's test command, which the prompt shows as a hint. The `run_tests` tool keeps the
   command of the worker (`python -m pytest -q`), so the agent runs the hinted command with
   `run_command`.
5. When the task ends, the runner reads the agent's diff from the checkpoint repo and cuts out the
   files that the test patch of the instance touches. The harness puts that test patch on top of
   the patch; a patch that changes the same file would fail to apply, and the instance would count
   as unresolved for a reason that is not the fix. The result is `<task id>.patch`.
6. At the end of the run the runner writes `predictions.jsonl` with `instance_id`,
   `model_name_or_path` and `model_patch` for every task. A task that did not end as `pr_opened`
   delivered nothing, as in the custom set, so its patch is empty. The result file of a task that
   finished says `not graded yet`.
7. `grade.sh` runs the harness and fills in `resolved`.

## Grade a run

```bash
uv sync --extra eval
eval/swebench/grade.sh eval/results/<run id> [workers]
```

The harness needs Docker, the instance images (it pulls them) and a lot of disk. It runs in
`eval/results/<run id>/harness/` (or `SWEBENCH_WORK_DIR`), so its `logs/` folder does not land in
the repo. `report.py` then sets `resolved` and `grading` in each `<task id>.json` and rebuilds
`summary.json`:

| The report says | `resolved` | `grading` |
|---|---|---|
| resolved | true | every FAIL_TO_PASS and PASS_TO_PASS test passed |
| unresolved | false | the harness ran the tests and not all passed |
| empty patch | false | the agent delivered no change |
| error, and the log says the patch did not apply | false | the patch does not apply |
| error, any other | null | the harness did not grade it |

A null counts as `ungraded` in the summary, not as a failure. Look at the `run_instance.log` of
the instance under `harness/logs/run_evaluation/`, fix the cause (a missing image, a full disk) and
run `grade.sh` again: the harness skips the instances that have a `report.json` and runs the rest.

## Check the grader without an agent

The two sanity checks of the story run the harness on the reference patches and on empty patches
of two instances. They need no model and no key.

```bash
uv sync --extra eval
uv run pytest -m slow tests/integration/test_swebench_adapter.py
```

* `test_empty_patch_reports_zero_of_two` pulls no image and takes about 20 seconds. It passed on
  the development machine on 2026-11-05: 0 of 2 resolved, 2 empty patches.
* `test_gold_patch_sanity` pulls the images of `pallets__flask-5014` and `psf__requests-2317`,
  several GB each, and expects 2 of 2 resolved. It stops at the start when less than 30 GB of disk
  is free. The development machine is arm64 with 15 GB free, so it was **not run there**. Run it
  on the eval VM before the first headline run. The same check without pytest:

```bash
uv run python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Verified --predictions_path gold \
  --instance_ids pallets__flask-5014 psf__requests-2317 --max_workers 2 --run_id gold-sanity
```

The report file `gold.gold-sanity.json` must show `resolved_instances: 2`.

`tests/integration/test_swebench_runner.py` runs the rest of the path on a stand-in for an
instance image (a small image with the layout of a harness image): the image that is made from it,
the copy of the repo, the sandbox, a scripted task on the real stack, and the patch that comes out.
It needs `make up` and `make sandbox-image` and runs in `make itest`.

## The smoke run with the agent (needs a model key, run by hand)

Three instances, end to end, with the real model. It costs money (up to 4 dollars an instance).

```bash
make up && make sandbox-image && make migrate
uv sync --extra eval
export ANTHROPIC_API_KEY=...            # or put it in .env
export LONGHAUL_PROVIDER=anthropic
export LONGHAUL_CKPT_DIR=$HOME/.longhaul/ckpt

# shell 1: the worker, with the config that you mean to measure
uv run longhaul worker

# shell 2
uv run python eval/run.py --set swebench --config full --limit 3 --run-id swe-smoke-1 \
  --provider anthropic
eval/swebench/grade.sh eval/results/swe-smoke-1 3
cat eval/results/swe-smoke-1/summary.json
```

`--limit 3` takes the first three ids of `subset.txt`, which are `astropy__astropy-13453`,
`astropy__astropy-8707` and `django__django-10914`. The two astropy instances have compiled
extensions, which is the case in this design that a stand-in cannot show. If they fail to start,
look at the `.so` files first (see below). Put the output of the last command in the pull
request.

## Known limits

* **A restore deletes ignored files.** The checkpoint store runs `git clean -ffdx` when it has
  to put the workspace back on a commit, which happens when a tool call is retried after a crash,
  and that removes compiled extensions and other build output that git ignores. The tree starts
  clean, so a normal run never restores. After a crash in the middle of a call on an instance with
  built extensions (astropy, matplotlib, scikit-learn), the retry can find the environment broken.
  Re-run that task.
* **Links are not copied.** The sandbox's file API cannot make links, so a symlink that git tracks
  is missing from the copy of the repo, as it is for any `local:` task. The files that the
  built-in patterns of `agent/workspace.py` skip (`.env`, caches) are missing too.
* **The image is rebuilt per instance** and needs the network once, for `apt-get install ripgrep`.
  The tag follows the Dockerfile, so a change to it builds new images. Old ones stay until
  `docker rmi`.
* **Emulation.** On an arm64 machine the x86_64 images run under emulation and are slow. Set
  `LONGHAUL_SWEBENCH_PLATFORM` to pull and run another platform.
* **The sandbox limits** are `sandbox.limits` of the worker's config (2 CPUs, 4 GB). Some
  scikit-learn and astropy tests need more.
