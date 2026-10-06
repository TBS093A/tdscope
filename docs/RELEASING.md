# Releasing tdscope

## How the pipelines fit together

| When | Workflow | What happens |
|------|----------|--------------|
| every commit (locally) | `pre-commit` | gitleaks, ruff, no real thread dumps, no private words (from a git-ignored list) |
| pull request (**pre-merge gate**) | `CI` | ruff, mypy, tests on Python 3.10-3.14 (Linux) plus macOS and Windows |
| | `Security` | gitleaks over the full history, CodeQL (Python + workflows) and bandit (SAST), pip-audit and dependency review (SCA), fuzzing + hostile input + exported HTML in a real browser (dynamic testing) |
| merge to `main` (**post-merge**) | `Post-merge` | build sdist + wheel, verify metadata and contents, install and smoke-test on Linux/macOS/Windows, publish a dev build (`X.Y.Z.devN`) to **TestPyPI** |
| tag `vX.Y.Z` (**release**) | `Release` | tag and changelog checks, the full `CI` and `Security` workflows again, build and verification, **manual approval**, publish to **PyPI**, GitHub release with build provenance, install from PyPI and smoke-test on 3 OSes |
| weekly | `Security`, Dependabot | new advisories surface without code changes; action and dependency updates |

Merges to `main` require the `ci-passed` and `security-passed` checks.

Releases come from tags and not from merges. PyPI versions are immutable, so a
release has to be a deliberate decision. Each merge still exercises the whole publishing
path through TestPyPI.

Versions come from git tags (hatch-vcs). `v0.2.0` builds `0.2.0`, and the commits after
it build `0.2.1.devN`. There is no version number to edit in the code.

## One-time setup

1. **PyPI trusted publisher.** On <https://pypi.org/manage/account/publishing/>, add a
   *pending publisher*. No API token is ever stored.

   | Field | Value |
   |-------|-------|
   | PyPI project name | `tdscope` |
   | Owner | `TBS093A` |
   | Repository | `tdscope` |
   | Workflow | `release.yml` |
   | Environment | `pypi` |

2. **TestPyPI trusted publisher.** On <https://test.pypi.org/manage/account/publishing/>,
   add the same entry with workflow `post-merge.yml` and environment `testpypi`. Then
   enable the post-merge upload:

   ```bash
   gh variable set TESTPYPI_PUBLISH --body true --repo TBS093A/tdscope
   ```

3. **GitHub environments** `pypi` and `testpypi`. These already exist. `pypi` requires a
   reviewer's approval and only accepts `v*` tags.

## Making a release

1. Move the entries of `CHANGELOG.md` under a dated heading, for example
   `## 0.2.0 - 2026-11-03`, and merge that through a pull request.
2. Tag the merge commit on `main` and push the tag:

   ```bash
   git switch main && git pull
   git tag -a v0.2.0 -m "tdscope 0.2.0"
   git push origin v0.2.0
   ```

3. Approve the `pypi` deployment in the run of the **Release** workflow.
4. The workflow publishes to PyPI, creates the GitHub release and verifies that
   `pip install tdscope==0.2.0` works on Linux, macOS and Windows.

A failed check before the approval publishes nothing. Fix the problem, delete the tag
(`git push --delete origin v0.2.0 && git tag -d v0.2.0`) and tag again. After a
successful upload, the version number is used up on PyPI, so release the fix as `0.2.1`.
