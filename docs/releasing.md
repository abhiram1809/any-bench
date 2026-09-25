# Releasing AnyBench

The distribution name is `any-bench`; the CLI command is `anybench`. Release from a clean, reviewed commit after the CI and real Docker test job pass.

1. Confirm `pyproject.toml` and `src/anybench/__init__.py` have the same version, and that the version is not already on PyPI.
2. In the PyPI account that will own `any-bench`, register a [pending trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) for GitHub owner `abhiram1809`, repository `any-bench`, workflow `release.yml`, and environment `pypi`. If the name is no longer available, rename the distribution in `pyproject.toml` and documentation before tagging.
3. Push a `vX.Y.Z` tag for the reviewed commit. `.github/workflows/release.yml` runs tests, builds a wheel and source distribution, checks the tag version, then uploads through PyPI Trusted Publishing. It does not need a stored PyPI API token.
4. Confirm `python -m pip install any-bench` works in a fresh environment and `anybench start --help` is available.

Do not publish from an uncommitted working tree: the tagged GitHub commit is the source PyPI receives.
