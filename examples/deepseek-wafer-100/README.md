# DeepSeek V4.1 Flash on Wafer: 100-step case study

[Benchmark report](benchmark.html) · [Sanitized results](results.json)

AnyBench selected eight recent commits across inference engineering, backend,
frontend, database, and developer tooling. Four yielded focused Docker checks
that failed on the historical parent and passed after the reference patch.
DeepSeek V4.1 Flash completed and passed all four on the Wafer provider after
the step cap was raised to 100.

| Public repository and commit | Regression check | Parent | Reference | DeepSeek | Model calls | GLM judge |
| --- | --- | --- | --- | --- | ---: | ---: |
| [SGLang `752801e4`](https://github.com/sgl-project/sglang/commit/752801e4d184574f9c4f860b7c0dcab9f4ed6d93) | Reject invalid sampling seeds while accepting int64 bounds | Failed | Passed | Completed, passed | 24 | 0.90 |
| [FastAPI `aadfcce7`](https://github.com/fastapi/fastapi/commit/aadfcce76380ab169fe172d5cda21722e53c4924) | Propagate `exclude_defaults` through nested JSON encoding | Failed | Passed | Completed, passed | 17 | 1.00 |
| [shadcn/ui `04bb134c`](https://github.com/shadcn-ui/ui/commit/04bb134c52af23af7d77673618ef4e3862b7c310) | Preserve leading comments during the `cn` migration | Failed | Passed | Completed, passed | 71 | 0.95 |
| [SQLAlchemy `5503ac27`](https://github.com/sqlalchemy/sqlalchemy/commit/5503ac27c412cf99baa838db98bf10a8f1e26f16) | Report an unresolved Python 3.14 annotation as `MappedAnnotationError` | Failed | Passed | Completed, passed | 52 | 0.85 |

All four candidate checks exited successfully in the same Docker image IDs
used for parent/reference validation. SGLang, FastAPI, and SQLAlchemy used
focused assertion commands; shadcn/ui ran two Vitest tests. These checks do
not stand in for the repositories' full test suites. The mean GLM judge score
for the completed patches was 0.925, supplemental to the local checks.

## Run conditions

GLM-5.3 Flash built cases and judged patches on `inference-net/fp4`.
DeepSeek used `deepseek/deepseek-v4.1-flash` with reasoning enabled,
`provider.only=["wafer"]`, and provider fallbacks disabled. The final runs
allowed up to eight bounded retries for retryable HTTP errors, at most 100
model calls per case, and one hour per attempt. SGLang and FastAPI completed
with a one-million total-token allowance. Shadcn/ui and SQLAlchemy reached
that allowance and completed on reruns with a three-million allowance.

The 4/4 result selects one final completed attempt per case **after retries
and token-cap reruns**. It is not pass@1 from a single uniform sweep. Wafer
also returned HTTP 429 during an earlier 100-step pass. The report preserves
the earlier one-attempt, 20-step baseline: GLM solved 3/4, GPT-6 Luna Flex
2/4, and DeepSeek on InferenceNet 0/4. Qwen's provider returned HTTP 429 on
all four cases, so its coding performance was not measured. Different run
limits and times prevent a controlled ranking across those cohorts.

The other four selected commits, from vLLM, Spring Boot, Next.js, and Ruff,
did not obtain valid local regression checks and are excluded from the score.
This directory is a **report-only example**: the private campaign's local
snapshot paths and raw attempt artifacts are not published here.
