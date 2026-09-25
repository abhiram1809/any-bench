# Boltons historical cases

Source: [mahmoud/boltons](https://github.com/mahmoud/boltons). The builder reviewed five commits at the repository head on 2026-09-25 and accepted two. Both were verified in Docker: their checks failed on the parent snapshot and passed after the reference change. The [reviewed dataset](cases.csv) contains the reference diffs and evaluator commands; [results](results.json) records full commit IDs, changed files, and measured usage.

| Historical commit | What changed | Qwen3.8-27B |
| --- | --- | --- |
| [`4e5faa3`](https://github.com/mahmoud/boltons/commit/4e5faa3d7e4008d89e0d8bf1ea87b6d9a061a16d) | Spooled stream write compatibility and text seeks | Token limit. Its partial patch passed the selected check, but the unfinished attempt counted as a failure. |
| [`1216b46`](https://github.com/mahmoud/boltons/commit/1216b467c10b852d08aa6895c80b6b9ec8f990d8) | Multi-file September fix sweep | Token limit; selected check failed. |

## How we prepared it

GLM-5.3 Flash saw each commit message, changed-file list, and patch. We replaced builder commands that depended on tests introduced by the target with small checks against functions already present in the parent. The stream case checks `bytearray` writes; the multi-file sweep uses one representative `bytes2human` behavior. That second check verifies a real regression but does **not** cover every change in the large sweep.

The shared [example image](../Dockerfile.python-libs) adds pytest and freezegun so the candidate can inspect repository tests. We used that image for both validation and candidate execution. Both attempts reached the configured 250,000 total-token cap. The result shows a harness completion limit as well as model difficulty; the passing partial stream patch was not scored by the judge because the attempt did not finish.

See the [shared replay instructions](../README.md) to revalidate the pinned cases or run another model.
