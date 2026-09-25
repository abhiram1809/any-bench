# Humanize historical cases

Source: [python-humanize/humanize](https://github.com/python-humanize/humanize). The builder reviewed five commits at the repository head on 2026-09-25 and accepted four. Three became verified Docker cases; one translation workflow was skipped because the gettext catalog tooling was unavailable. The [reviewed dataset](cases.csv) keeps the reference diffs and evaluator commands. [Results](results.json) records full commit IDs, changed files, and measured usage.

| Historical commit | What changed | Validation | Qwen3.8-27B |
| --- | --- | --- | --- |
| [`392aef7`](https://github.com/python-humanize/humanize/commit/392aef707c0e74341ab4a51420984e9ea6b566c5) | Round years in `naturaldelta` | Parent fail, reference pass | Completed, test pass; judge 0.90 |
| [`ca892b3`](https://github.com/python-humanize/humanize/commit/ca892b368a535a79cc5e2181d052245b94fd6652) | Carry rounding in `intword` | Parent fail, reference pass | Completed, test pass; judge 0.95 |
| [`ffdf407`](https://github.com/python-humanize/humanize/commit/ffdf407fdfe1a5738159b989eb2df190ed780448) | Accept general iterables in `natural_list` | Parent fail, reference pass | Completed, test pass; judge 0.85 |
| [`bada0f8`](https://github.com/python-humanize/humanize/commit/bada0f88b4d115b96d94db1fe73b20579e84fc14) | Preserve translations during catalog updates | Skipped: gettext workflow unavailable | Not run |

## How we prepared it

GLM-5.3 Flash drafted problems from commit messages, patches, and changed files. We checked each proposed evaluator command against the *parent* checkout, because tests added by the target commit are absent from the candidate workspace. The reviewed commands assert behavior through the existing package code; each verified command failed before its commit and passed after the reference change.

These historical commits use a source layout and expect a generated `humanize._version` module. The shared [example image](../Dockerfile.python-libs) places `/repo/src` on `PYTHONPATH` and supplies a minimal version-module shim. It also adds the test dependencies used during candidate exploration. Validation and candidate attempts used the same image.

The three candidate completions passed their private checks, but these checks cover only the selected behaviors. See the [shared replay instructions](../README.md) to revalidate the pinned cases or run another model.
