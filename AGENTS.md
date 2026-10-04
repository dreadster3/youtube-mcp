# AGENTS.md

Agent-facing conventions for this repo. Project context lives in [README.md](README.md); the task
runner is [go-task](https://taskfile.dev) with `Taskfile.yaml` at the root — run tasks as `task <name>`.

## Commits: Conventional Commits, mandatory

Format: `type(scope): summary`, or `type(scope)!: summary` for a breaking change.

- **A scope is preferred on every commit**, even where the type allows omitting it:
  `feat(tools):`, `fix(cache):`, `docs(readme):`, `ci(workflows):`, `test(client):`,
  `refactor(client):`, `chore(release):`.
- Subject: imperative mood, 50 characters or fewer, no trailing period, lowercase after the colon.
- Body only when the why is not obvious from the subject. Bullet points over prose.

Good:

```
feat(tools): add transcript language listing
fix(cache): clamp TTL read to stored expiry
ci(workflows): pin setup-uv to v10.2.0
```

Bad:

```
Added a transcript language tool.     # not conventional, past tense, trailing period
fix(server): Fix the bug              # capitalized, vague, no scope for the area touched
```

## Repo conventions

- **Never commit to `main` directly** — the environment guard blocks it. Work on a feature branch
  and open a PR.
- **Feature PRs squash-merge**, so the PR title becomes the commit subject; it follows the same
  conventional format and the same rules.
- **The test suite is offline.** No test may hit the network — no YouTube or Google calls. Data API
  responses come from recorded fixtures in `tests/fixtures/`, and transcript fetching is stubbed
  (`MockTransport`, fixtures, stubs only).
- **`task check` and `task typecheck` must be green for any meaningful change.** `check` is
  lint + fmt-check + lock-check + test and stays network-free; mypy is strict on `src/youtube_mcp`
  and is its own CI job because `check` deliberately excludes it.
- **The release path is load-bearing, not cosmetic.** release-please derives changelog entries and
  version bumps from conventional commits only, and under squash-merge the PR title is that commit
  subject — `feat:`/`fix:`/`perf:` bump a release, a plain title bumps nothing.
