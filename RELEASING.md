# Releasing

**Commits decide the version; milestones decide when.** Closing a milestone runs `.github/workflows/release.yml`:

1. semantic-release reads the squash-merged PR titles since the last tag. `feat:` bumps the minor version, and `fix:` or `perf:` bumps the patch.
2. The computed version must equal the closed milestone's title (`v0.2` → `0.2.0`). If it doesn't, the job fails before tagging.
3. The version is written into `pyproject.toml` and the plugin manifest. The job builds sdist and wheel, commits `chore(release)`, tags `vX.Y.Z`, and creates the GitHub release with `dist/*` attached.
4. The `pypi` job uploads `dist/*` with trusted publishing, so no token is stored.

A milestone that closes with nothing to release fails the job. The manual `workflow_dispatch` with `version: v0.2` runs the same path.

**Before 1.0:** never put `!` or `BREAKING CHANGE` in a PR title or body, because semantic-release would jump to 1.0.0. The `PR title` check enforces conventional titles and the no-`!` rule.

## One-time setup

- [ ] Tag the pre-package baseline: `git tag v0.1.0 c764d62 && git push origin v0.1.0`. Without a tag, semantic-release starts at 1.0.0.
- [ ] Install the release GitHub App on this repo. Add its client ID as the repository variable `RELEASE_BOT_CLIENT_ID` and its private key as the secret `RELEASE_BOT_PRIVATE_KEY`. The workflow mints a token scoped to this repository only. If `main` is protected, let the app bypass it for the `chore(release)` commit.
- [ ] Settings → General → Pull Requests: allow squash merging only, with the default commit message set to **Pull request title**. Otherwise the `feat:`/`fix:` title never reaches `main`.
- [ ] PyPI → Publishing → add a pending publisher: project `backtest2live`, owner `martianmobile`, repo `backtest2live`, workflow `release.yml`, environment `pypi`.
- [ ] TestPyPI: the same, with workflow `testpypi.yml` and environment `testpypi`.
- [ ] Create GitHub environments `pypi` and `testpypi`. On `pypi`, set **Deployment branches and tags → Selected branches → `main`**. This is required: a trusted publisher matches on the workflow file name and the environment, not on the ref, so without it any branch carrying an edited `release.yml` could upload. Optionally add a required reviewer on `pypi`.

## Rehearsal

Run **Actions → TestPyPI rehearsal**. It publishes `0.2.0.devN` to TestPyPI, installs it in a clean runner, and runs `bt2live`. A green run proves the trusted-publishing path before a real release.
