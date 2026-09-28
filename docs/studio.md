# Experimental AnyBench Studio

Studio is an optional localhost view and controller for AnyBench. Install with `pip install 'any-bench[studio]'`, then run `anybench studio`. The command prints an authenticated local URL and opens the browser. Use `--no-open` to keep it in the terminal, `--port` to choose another loopback port, or `--attach PATH` to inspect an existing session directory or result JSONL.

The browser can launch guided runs using the existing `.anybench/config.json`. **Configure** lets you choose each candidate's built-in, Codex, Claude Code, OpenCode, or custom harness. With the built-in client, it exposes the HTTP retry count. External harnesses still require a Docker image, allowed hosts, and credential mapping. The form also accepts per-model input/output prices and a run budget in USD. Prices and budget are for display; they do not stop provider calls. The full JSON editor remains available for builder, judge, credentials, and advanced options. Guided JSON, model-array JSON, and TOML imports are supported.

**New benchmark** accepts repositories, commit and problem limits, or an existing AnyBench cases CSV. A CSV can be uploaded from the browser into the private `.anybench/studio_imports/` directory or selected by a path on the Studio host. Imported cases skip Git history drafting but still go through environment preparation and base/reference verification before candidate attempts. The CLI equivalent is `anybench start --dataset /path/to/cases.csv`. An advanced argument array for `build`, `validate`, `run`, or `evaluate` remains available. **Review workload** validates the launch settings and shows the workload before **Confirm and launch** authorizes possible provider charges.

To monitor a CLI invocation, add `--live`:

```sh
anybench start /path/to/repository --live
anybench run .anybench/cases.csv --models .anybench/candidates.json \
  --output .anybench/attempts.jsonl --live
```

Live mode starts the local server if needed and prints its authenticated URL. The browser and server can close without cancelling the benchmark worker. Existing runs attached after the fact are read-only and display whatever was saved; detailed events and controls require a run started with Studio or `--live`.

The first panel follows the latest model invocation from its saved instruction payload through response, tool activity, token usage, and estimated spend. Input and output totals include completed provider calls and saved external harness usage. Reasoning and content token counts appear separately when the provider reports a reasoning split; otherwise that split is marked unavailable. Remaining budget is shown only when a budget, prices, and usage are available for all observed calls. Estimates use the configured input and output prices without cache discounts. The board then follows repository selection, builder, environment recipe and image, base/reference verification, verified queue, candidate harnesses, authoritative evaluation, optional judge, and report. Select a problem or attempt to switch the board to its path and return through the breadcrumb. The inspector shows actual model prompts before dispatch and responses after receipt. Existing provider calls are nonstreaming. It also shows tool calls, context estimates and compaction, subprocess output, Docker lifecycle and resource samples, local test outcomes, and judge reasoning. External harness detail depends on its structured output; unavailable fields are shown as unavailable. The paged problem table, nested-span timeline and history slider, and model comparison are backed by saved run data. Logs and large payloads have explicit size limits and truncation markers.

**Concurrency** changes the number of candidate attempts admitted into the current model and sweep level. Existing attempts drain when the target is lowered. The built-in harness also gates later requests to its model endpoint; it cannot revoke a request already in flight. For external harnesses, the control limits harness processes, not requests inside a process. Configured provider limits remain an upper bound. Builder, image preparation, verification, and judge phases stay serial.

**Pause** drains active work and stops admitting more. **Resume** restarts the queue. **Graceful stop** drains active work, preserves completed results, and produces a partial report for a guided run. Studio can resume a stopped guided session when its saved operations are safe to replay. If a crash left a paid operation with unknown billing state, Studio blocks automatic replay. Completed JSONL results and existing manifests remain authoritative.

Changing concurrency during a run marks affected model/level groups as variable concurrency in the report. Throughput remains visible, while speedup and efficiency derived from a fixed worker count are unavailable for those groups. Test passes and optional judge scores remain separate.

Studio stores private events, artifacts, and controls under `.anybench/live/`. It binds to loopback, uses an authenticated browser cookie, validates requests from the local origin, and redacts configured credentials before event storage. The bundled browser assets make no CDN requests. Resource telemetry is best-effort: Docker metrics are for containers owned by the run, and NVIDIA metrics, when available, describe the host GPU.
