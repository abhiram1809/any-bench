# Workflow details

AnyBench cases are built from first-parent Git history. A usable case needs a single-parent commit, a behavior-focused problem statement, and preferably a safe local test that fails on the parent and passes on the target. The imported `gold_diff` is reconstructed by `git diff` and must not be authored in annotations. Inspect the repository's README, dependency files, CI workflow, and instructions before proposing commands. Docker tests run with network disabled; dependencies must be in the image.

Start with the latest 50 commits and aim for five **verified** cases. `prepare` outputs commit message, changed paths, a bounded patch excerpt, and Git identity. An excerpt marked truncated requires direct Git inspection. Include an annotation for each reviewed commit so accepted and rejected decisions can be audited. To revise, edit annotations and import to a fresh output path or use explicit `--overwrite`; revalidate.

`validate --check-tests` emits per-case `verified`, `invalid`, or `skipped` statuses. `--verified-output` exports all verified rows by default. The guided workflow must pass `--max-verified 5` explicitly. A case with no local command, or one requiring external services, is skipped and must be reported separately. When structured outputs contain at least one verified case, invalid cases are printed and recorded while the command succeeds so the session can continue with the selected subset. Review each test command for safety before running it. Repository-specific images can be supplied with `--image` or an exact repository-to-image JSON map with `--image-map`.

If a build uses an API builder instead, use `anybench build ... --models BUILDER.json --builder NAME --commits 50 --max-cases 5 --output CASES.csv`. Its decision journal and frozen commit list permit `--resume`; the builder incurs separate API use. Host authoring needs no builder endpoint.

Outputs have matching `.manifest.json` input fingerprints. `--resume` skips every recorded attempt, including errors and exhausted attempts, and every recorded judge result, including judge errors. It does not retry paid failures. Use a new session or explicit `--overwrite` for a deliberate rerun. Keep all session artifacts outside the candidate checkout. Summaries and HTML reports are derived from saved records and may be regenerated.

AnyBench uses a private historical Git dataset and local test checks; it does not claim the official SWE-bench JSON schema or test patch format. Explain this distinction when a user specifically requests official SWE-bench compatibility.
