# Configuration and roles

Use separate JSON arrays for builder, candidate, and judge models. Set `role` explicitly for new configs; older configs without it default to `candidate` for compatibility. `run` refuses entries marked builder or judge. A candidate example:

```json
[{"name":"candidate","role":"candidate","base_url":"https://provider.example/v1","model":"model-id","api_key_env":"CANDIDATE_API_KEY","context_profile":"enhanced","context_window_tokens":200000}]
```

The built-in enhanced harness is default. Its context window is 200,000 tokens unless overridden per endpoint. It includes full reads, discoverable files, read-only exploration agents, bounded compaction, and container test execution. The candidate has 30 shared model calls by default. For an external coding harness, configure `harness`, its prebuilt `image`, and `allowed_hosts`; external harnesses keep their own context orchestration. Consult the project's README for external configuration and Docker proxy requirements.

`anybench doctor --json --models CANDIDATES.json --image IMAGE --output-dir SESSION` checks runtime prerequisites and presence of named environment variables without printing values or making paid requests. Build an image with the target's dependencies before validation. Use `--image-map` for multiple repositories.

Scoring: `test_passed` is available only for a local test case; `judge_score` is an optional separate signal. Count `error` and `exhausted` as failures, even if a final test happened to pass. External-only and untested cases must remain visibly unverified. Do not merge builder or judge entries into candidates or silently retry terminal paid attempts.
