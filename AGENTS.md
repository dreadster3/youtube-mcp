# AGENTS.md

Agent-facing conventions for this repo. Project context lives in [README.md](README.md); the task
runner is [go-task](https://taskfile.dev) with `Taskfile.yaml` at the root — run tasks as `task <name>`.

## Project overview (for future agents)

- **What it is.** A single-process, **stdio-first** MCP server exposing **11 `youtube_*` tools**
  over public YouTube data — transcripts (incl. search inside a transcript), video search/metadata/
  stats, top-level comments, channel + uploads-playlist listing, categories — for any local agent.
  HTTP is optional and adds `/health`. Import package `youtube_mcp`; dist `dreadster3-youtube-mcp`
  (PyPI `youtube-mcp` is squatted); console script `youtube-mcp`.
- **Stack, pinned exactly.** `fastmcp==4.0.10` — pin EXACTLY, 4.x breaks in minors; traps:
  `ToolError` for tool errors, pydantic return → `structuredContent`, `stateless_http` for
  replicas, custom routes before `http_app()`, `ctx.elicit` broken on modern protocols.
  `httpx.AsyncClient` (direct REST, NOT `google-api-python-client`).
  `youtube-transcript-api==1.2.4` — sync, NOT thread-safe, no built-in timeout: every offloaded
  call needs a fresh instance + `anyio.fail_after` + `abandon_on_cancel=True` or the timeout never
  fires; cookie auth is dead upstream, so age-restricted is unsupported. `aiosqlite` TTL cache
  (`ttl=0` = never expires), `pydantic>=2.12`, uv + `.python-version` 3.13.
- **Architecture** (`src` layout). `transcript/`: `errors` (CLOSED taxonomy — 5 `TRANSCRIPT_*`
  codes + `INVALID_REQUEST`; do not add codes), `fetch`. `youtube/`: `quota` (3 buckets — search
  100/day, stats 10,000/day via `batchGetStats`, shared 10,000/day; midnight-PT `zoneinfo` reset),
  `client` (`trust_env=False`, reason-first error translation, `quotaExceeded` never retried,
  backoff+jitter on 429/5xx only), `models` (tolerant `from_api` parsers). `tools/`:
  `register(mcp, deps)` closures behind one uniform `ToolError` seam. `server.py`: import-safe
  `create_app` factory, fail-fast `require_api_key`, `/health` before `http_app()`.
- **Hard constraints (do not violate).** No multi-key quota rotation (Google policy violation);
  NO dislike counts anywhere; no OAuth. `search.list` is the scarce bucket (100/day) — list
  channels via the uploads playlist, not search. `quotaExceeded` = do not retry until midnight PT.
  The transcript source is a scrape: treat failures as expected, never crash the server.
- **Working here.** `uv sync`; run `task check` + `task typecheck` before calling work done. New
  tools need load-bearing descriptions (the LLM's only docs — state quota bucket + limitations)
  and offline tests. Structured output = return pydantic models; error surface = `ToolError` only.
- **Deeper context.** [README.md](README.md) (full config table), `tests/fixtures/` (recorded API
  envelopes), this repo's git history (design rationale).
- **Release flow.** release-please on push to `main` opens ONE accumulating release PR; merging it
  tags + releases AND publishes (PyPI trusted publishing + GHCR) in the same run. PR titles =
  commit subjects (see Commits, below) — they ARE the changelog.

## Commits: Conventional Commits, mandatory

Format: `type(scope): summary`, or `type(scope)!: summary` for a breaking change.

- **A scope is preferred on every commit**, even where the type allows omitting it:
  `feat(tools):`, `fix(cache):`, `docs(readme):`, `ci(workflows):`, `test(client):`,
  `refactor(client):`, `chore(release):`.
- Subject: imperative mood, no trailing period, lowercase after the colon.
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
