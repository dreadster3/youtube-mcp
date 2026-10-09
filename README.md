# YouTube MCP Server

A single-process [MCP](https://modelcontextprotocol.io) server that exposes public YouTube data —
transcripts, search, video metadata and statistics, comments, channels, categories — as tools an LLM
can call. It speaks **stdio**: any local agent (Claude Desktop, an editor plugin, a script) launches
`youtube-mcp` as a subprocess and gets the tools over stdin/stdout. **No network exposure, no auth,
no account** — just a YouTube Data API key.

One Python process: FastMCP server, YouTube Data API client, transcript fetching and cache all live
in the same application. No sidecar, no second service, no Redis.

- **12 tools**, all namespaced `youtube_*`, read-only public data.
- **stdio by default.** HTTP is available for a shared/deployed instance — see
  [Optional: HTTP mode / container](#optional-http-mode--container).
- **API-key only.** No OAuth, no uploads, no channel management.
- **Dislike counts do not exist.** YouTube made them private in December 2021; no endpoint returns
  them, and this server will not estimate them.

---

## Quick start

```bash
git clone <this repo> && cd youtube-mcp
uv sync                       # creates .venv from uv.lock
export YOUTUBE_API_KEY=...    # required — the server fails fast without it
uv run youtube-mcp            # speaks MCP on stdin/stdout
```

Point an MCP client at it:

```json
{
  "mcpServers": {
    "youtube": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/youtube-mcp", "youtube-mcp"],
      "env": { "YOUTUBE_API_KEY": "your-key-here" }
    }
  }
}
```

The console script dispatches on `MCP_TRANSPORT`, which defaults to `stdio`:

```bash
uv run youtube-mcp                          # stdio (default)
MCP_TRANSPORT=http uv run youtube-mcp       # streamable HTTP on 0.0.0.0:8088
```

The cache is a local SQLite file (`DATABASE_PATH`, default `cache.db` in the working directory).
It is **ephemeral by design**: deleting it costs quota, never correctness.

---

## Read this first: YouTube blocks cloud IPs

Transcripts are not available through the official API — `captions.download` requires OAuth consent
from the video's owner. Anything fetching arbitrary transcripts is scraping YouTube's internal
`timedtext` endpoint, and **YouTube blocks most known cloud-provider IP ranges (AWS, GCP, Azure)**.
Symptoms are `RequestBlocked` / `IpBlocked`, surfaced by this server as `TRANSCRIPT_IP_BLOCKED`
(retryable, transient).

Transcript fetching therefore needs a **residential/clean IP**. From a cloud VM it will fail until a
proxy is configured — budget for a rotating residential proxy before moving this to a VPS, not after.
The server ships with no proxy configured.

Proxy configuration is exposed through environment variables, unset by default:

| Variables | Behaviour |
| --- | --- |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | Webshare rotating proxy, biased to `filter_ip_locations=["pt","es"]` to limit added latency |
| `HTTP_PROXY` / `HTTPS_PROXY` | Generic proxy alternative |

Neither guarantees success — that caveat is the upstream library's, not a hedge. Data API calls
(`youtube_search_videos` and friends) use the official API and are not affected by this; only
transcripts are.

---

## Tools

| Tool | What it does | Quota bucket |
| --- | --- | --- |
| `youtube_get_transcript` | Caption text for one video; timestamps off by default | none (scrape, not Data API) |
| `youtube_get_timestamped_transcript` | Same, as `{text, start, duration}` segments for chaptering/deep links | none |
| `youtube_list_transcript_languages` | Caption tracks available for a video, and whether each is auto-generated | none |
| `youtube_search_in_transcript` | Case-insensitive substring search inside a transcript, server-side | none |
| `youtube_search_videos` | Search by keyword | **scarce: 100 calls/day, own bucket** |
| `youtube_get_video` | Video metadata by ID — snippet, statistics, duration, status | shared (10,000/day) |
| `youtube_get_video_stats` | View/like/comment counts for up to 50 IDs | **own 10,000/day bucket** via `videos:batchGetStats` |
| `youtube_get_comments` | Top-level comments with each thread's `total_reply_count`, plus an optional truncated reply sample | shared (10,000/day) |
| `youtube_get_comment_replies` | Every reply to one top-level comment, pageable (`comments.list?parentId`) | shared (10,000/day) |
| `youtube_get_channel` | Channel by ID or `@handle`, plus its uploads playlist ID | shared (10,000/day) |
| `youtube_list_channel_videos` | Channel uploads, newest first, via the uploads playlist | shared (10,000/day) |
| `youtube_list_categories` | Video categories, optionally per region | shared (10,000/day) |
| `youtube_get_quota_status` | Used/remaining per bucket for today, plus the reset time | none (reads the local counter) |

Notes that the tool descriptions also carry, because the model is the main consumer:

- **No dislikes anywhere.** Not on videos, not on comments, not on `batchGetStats`. Tool
  descriptions say so explicitly so the model stops asking.
- **Reply counts are always there; the embedded reply text is a sample.** `youtube_get_comments`
  reports the thread's `total_reply_count` on every item — the count to quote, and the number
  YouTube gives, not `len(replies)`. With `include_replies=true` the items also carry `replies`:
  the API truncates that list (docs: "a limited number of replies … only a subset", ~5 in
  practice), so it is a sample, never the whole conversation. For complete replies to one comment
  use `youtube_get_comment_replies` with that item's `comment_id`.
- **Transcripts are unavailable for some videos, and that is normal.** Captions disabled by the
  uploader (`TRANSCRIPT_DISABLED`), no track in the requested language (`TRANSCRIPT_NOT_FOUND`),
  age-restricted due to broken upstream cookie auth in `youtube-transcript-api` 1.2.4
  (`TRANSCRIPT_AGE_RESTRICTED`), or YouTube demanding a PO token (`TRANSCRIPT_UPSTREAM_ERROR`). None
  of these are outages; only `TRANSCRIPT_IP_BLOCKED` and `TRANSCRIPT_UPSTREAM_ERROR` are worth
  retrying.
- `youtube_list_channel_videos` deliberately resolves the channel's uploads playlist and pages
  `playlistItems` instead of searching — 2 units from the large shared pool rather than 100/day.
  `youtube_search_videos` is the scarce, deliberate operation.
- `youtube_get_quota_status` reports the server's own per-bucket counter — the same numbers
  `/health` exposes — and spends nothing: it reads a local counter, never the API, and cannot raise
  `quotaExceeded`. Like that counter it is **approximate and process-local**: it counts only this
  process, resets to zero on restart, and knows nothing about another process using the same key, so
  Google can still refuse a call this tool reports as affordable.
- Transcripts are truncated at `RESPONSE_LIMIT` by cumulative characters (both variants) and return
  `next_cursor`; an uncapped 3-hour transcript would blow the model's context window.

---

## Quota

Three buckets, because Google counts them separately:

| Bucket | Budget | Consumed by |
| --- | --- | --- |
| `search` | 100 calls/day | `search.list`, one unit per call — **including each additional page** |
| `stats` | 10,000 calls/day | `videos:batchGetStats` only |
| `shared` | 10,000 units/day | `videos.list`, `channels.list`, `playlistItems.list`, `commentThreads.list`, `comments.list`, `videoCategories.list` |

- Quotas reset at **midnight Pacific Time** (`America/Los_Angeles`, DST-aware). Not midnight UTC, not
  midnight local. A `403 quotaExceeded` means done for the day — the error message says "resets
  midnight Pacific", and the model is told not to retry until then.
- `youtube_get_video_stats` uses `videos:batchGetStats` specifically because that method has **its own
  10,000/day bucket** and returns up to 50 videos per call. Pulling counts through
  `youtube_get_video` would burn the shared pool instead.
- The server keeps its own per-bucket counter and warns in the log at 10% remaining. It is
  **approximate** — the authoritative accounting is Google's, and counters reset to zero on restart,
  deliberately, so a restart can never over-count. The API does not tell you which bucket tripped,
  which is why we count locally; the `/health` route (HTTP mode only) exposes this accounting as
  `quota_remaining_approximate`.
- A local refusal (`QuotaExceeded`) happens *before* the HTTP call, so a rejected call costs nothing.

**Do not rotate API keys to get more quota.** Creating multiple Google Cloud projects for the same
API service or use case to acquire more quota than your project was assigned is prohibited by
YouTube's Developer Policies; Google has issued warnings to developers doing it. Keys within one
project share a quota, so rotation *requires* separate projects — which is exactly the prohibited
pattern. This server does not implement rotation and will not accept a key list.

The legitimate path for more quota is the
[YouTube API audit / quota extension form](https://support.google.com/youtube/contact/yt_api_form).
Until then: cache aggressively, treat `youtube_search_videos` as scarce, and prefer
`youtube_list_channel_videos` / `youtube_search_in_transcript`.

---

## Configuration

Read once at startup with `pydantic-settings` (`.env` is honoured if present). Environment variable
names map case-insensitively onto field names. **The server fails fast**: a missing `YOUTUBE_API_KEY`
raises at startup, so a misconfigured launch dies immediately rather than on the first tool call.

| Variable | Default | Notes |
| --- | --- | --- |
| `YOUTUBE_API_KEY` | *(required)* | Single key. Parsed as a `SecretStr`, so it never appears in logs, reprs or tracebacks. |
| `YOUTUBE_TRANSCRIPT_LANG` | `en` | Default transcript language when a tool call does not name one. |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http`. Selects the transport in the `youtube-mcp` console script. |
| `MCP_HOST` | `0.0.0.0` | HTTP listen address (HTTP mode only). |
| `MCP_PORT` | `8088` | HTTP listen port (HTTP mode only). |
| `FASTMCP_STATELESS_HTTP` | `true` | See [HTTP mode](#optional-http-mode--container) — leave it true for replicas. |
| `RESPONSE_LIMIT` | `50000` | Transcript truncation threshold, in characters. Cumulative-character policy, same for both variants. |
| `CACHE_TTL_SECONDS` | `3600` | TTL for cached video statistics, in seconds. `0` means never expires, not "zero seconds" — see [Caching](#caching). |
| `DATABASE_PATH` | `cache.db` | SQLite cache file. **Relative by default** — set an absolute path in a container. |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | unset | Webshare proxy for transcript fetching; password is a `SecretStr`. |
| `HTTP_PROXY` / `HTTPS_PROXY` | unset | Generic proxy alternative. |
| `LOG_LEVEL` | `INFO` | Level for the server and application loggers. |

**Never commit a real API key or proxy credential to this repository.**

---

## Caching

SQLite via `aiosqlite` — one file, no extra service. Redis is not worth a tenant for this.

| Cached | Key | TTL |
| --- | --- | --- |
| Transcripts | `transcript:<video_id>:<lang>` (and `…:styled`) | forever — a published transcript does not change |
| Transcript track lists | `transcript_tracks:<video_id>` | forever |
| Channel → uploads playlist | `uploads_playlist:<channel_id>` | forever — it never changes |
| Video stats | `video_stats:<video_id>` | `CACHE_TTL_SECONDS` (default 3600s; `0` = never expires) — view counts move |

Why the split: transcripts and playlist mappings are **immutable** — a published transcript is
published, and a channel's uploads playlist ID is assigned once — so re-fetching them can only ever
return the same bytes. Expiring them would buy nothing and cost quota. Video statistics are the one
genuinely mutable thing here, so they are the one entry with a real TTL, and that TTL is the
operator's `CACHE_TTL_SECONDS` (the tool description quotes the value in force).

Transcript and stats cache hits cost **no quota at all**, which is the point: caching is the primary
quota strategy, not an optimisation. Cache values are JSON, expiry is per entry, and expired rows are
deleted on read. Nothing depends on the cache surviving a restart — a cold cache costs quota, not
correctness.

---

## Development

```bash
uv sync                              # dev dependencies included
uv run pytest                        # full suite, offline
uv run python -c "from youtube_mcp.server import create_app; print('importable')"
```

The suite is fully offline: Data API responses come from recorded JSON fixtures in
`tests/fixtures/`, and transcript fetching is stubbed. No test hits the live API, so a test run costs
no quota and works with the network unplugged. Coverage is on by default through `pyproject.toml`
(`--cov=youtube_mcp`); run `uv run pytest --cov-report=html` for the browsable report.

### CI / development tasks

The repo ships a root `Taskfile.yaml` ([go-task](https://taskfile.dev)) so the gates are one command
each instead of remembered incantations. Run it as `task <name>` if go-task is installed, or through
uv — which works anywhere uv exists and needs no separate install:

```bash
uvx --from go-task-bin task <name>      # go-task has no PyPI package called `go-task`
uvx --from go-task-bin task --list      # what is available
```

| Task | What it runs |
| --- | --- |
| `install` | `uv sync` — dev dependencies included |
| `run` | `uv run youtube-mcp` — stdio server for a local agent; `task run MCP_TRANSPORT=http` for the HTTP transport |
| `lint` | `ruff check .` — the rule set is in `[tool.ruff.lint]` |
| `fmt` / `fmt-check` | `ruff format .` / `ruff format --check .` (the latter never mutates) |
| `typecheck` | `mypy`, strict on `src/youtube_mcp` |
| `test` | `uv run pytest` — the offline suite |
| `coverage` | same suite with `--cov-report=term-missing` named explicitly |
| `lock-check` | `uv lock --check` — fails if `uv.lock` drifted from `pyproject.toml` |
| `check` | `lint` + `fmt-check` + `lock-check` + `test` — the local gate |
| `docker-build` | builds `youtube-mcp:<tag>`, where `<tag>` is `git describe --tags --always` (a bare SHA on an untagged checkout, `dev` if that fails too) |
| `default` | `task -l` — what a bare `task` runs |

`check` is the only gate; it is docker-free, so it stays fast and works offline. `docker-build` is
separate because docker is the one slow, network-touching action here.

Layout:

```
src/youtube_mcp/
├── server.py          # FastMCP instance, tool registration, create_app factory, /health
├── config.py          # pydantic-settings, one model, read once
├── cache.py           # aiosqlite TTL cache
├── youtube/           # client.py (httpx), quota.py (per-bucket counters), models.py
├── transcript/        # fetch.py (library wrapper + thread offload), errors.py (taxonomy)
└── tools/             # data.py, transcripts.py, errors.py — one module per tool group
tests/                 # pytest + recorded fixtures
deploy/                # Dockerfile
Taskfile.yaml          # the development tasks documented above
```

### Manual smoke test

Needs a real API key and, for the transcript cases, a residential IP.

```bash
export YOUTUBE_API_KEY=...
uv run youtube-mcp            # stdio — point an MCP client at it
# or, in HTTP mode:
MCP_TRANSPORT=http uv run youtube-mcp &
curl -s localhost:8088/health | jq
```

Then, with an MCP client pointed at `http://localhost:8088/mcp` (the MCP Inspector works the same
way), or straight from the CLI:

```bash
uv run fastmcp list http://localhost:8088/mcp --transport http   # should list all 12 tools
```

For stdio, the same call is `uv run fastmcp list --command "env YOUTUBE_API_KEY=$YOUTUBE_API_KEY uv run youtube-mcp"`,
or drive the server from any client that already speaks it. The `env` prefix is required: the MCP
SDK spawns stdio children with a **sanitized** environment, so an exported `YOUTUBE_API_KEY` never
reaches the server and it would fail fast on the key check. Clients configured with JSON pass `env`
themselves and need no wrapper.

1. **Known captions video** — `youtube_get_transcript` with `dQw4w9WgXcQ`. Expect text, a
   `language_code`, and `truncated: false`. Repeat the call: it should be served from cache and cost
   nothing.
2. **Captions-disabled video** — call `youtube_get_transcript` on a video with captions turned off.
   Expect the `TRANSCRIPT_DISABLED` error path: one line, no traceback, and no retry hint (it is not
   retryable). This is normal, not an outage.
3. **Stats** — `youtube_get_video_stats` with two IDs including one bogus ID. Expect the real video's
   counts plus the bogus ID in `failed_video_ids` — partial success, not an error.
4. **Quota path** — exhaust or simulate the search bucket, then call `youtube_search_videos`. The
   error must surface `quotaExceeded` and mention **"resets midnight Pacific"**. If it does not, the
   model will keep retrying and burn the day's budget.
5. **IP-block check** (only relevant from a cloud host) — a transcript call from a blocked IP should
   return `TRANSCRIPT_IP_BLOCKED`, retryable, mentioning the proxy.

---

## Optional: HTTP mode / container

Only needed when the server is shared by several clients or deployed as a long-lived process. The
tool surface is identical in both transports; nothing is HTTP-only.

```bash
MCP_TRANSPORT=http uv run youtube-mcp
# or, equivalently, uvicorn against the factory:
uv run uvicorn youtube_mcp.server:create_app --factory --host 0.0.0.0 --port 8088
```

- **MCP endpoint:** `http://<host>:8088/mcp` (streamable HTTP). The app is a factory with no
  module-level `app` object, and there is deliberately no import-time settings read — so
  `import youtube_mcp.server` works without an API key, which is what makes the factory testable.
- **Health probe:** `GET /health` returns `{"status": "ok", "server", "version",
  "quota_remaining_approximate"}`. No auth, no external calls, so it is safe as both a liveness and a
  readiness probe.
- **`stateless_http=True` (the default) is what makes replicas work.** The stateful transport keeps
  sessions in server memory, which breaks across replicas, and sticky sessions do not reliably fix it:
  most MCP clients use `fetch()` internally and never forward `Set-Cookie`. Stateless HTTP makes each
  request independent, so any replica can serve any request. Set `FASTMCP_STATELESS_HTTP=false` only
  for a single-replica debugging session.
- **Reverse proxies:** SSE streaming still needs `proxy_buffering off; proxy_cache off;`
  `proxy_http_version 1.1`, `Connection ''`, and generous read/send timeouts (the docs use 300s).
  Stateless mode removes the session-affinity problem but does **not** remove streaming buffering
  concerns — a buffering proxy will still stall a long tool call.
- **No auth.** Run it on localhost, or behind whatever network boundary you already trust. If it is
  ever exposed more broadly, add a FastMCP `StaticTokenVerifier` on the `/mcp` route.

### Container

Build from the repository root, where the `.dockerignore` and sources are:

```bash
docker build -f deploy/Dockerfile -t youtube-mcp:dev .
```

Two stages: a builder that runs `uv sync --frozen --no-dev --compile-bytecode` into `/app/.venv`,
and a `python:3.13-slim` runtime that copies **only** the venv (`/app/.venv`) and the package source
(`/app/src`). No `uv`, no build tooling, no test dependencies in the final image. The image runs as a
non-root user (`app`, uid/gid 10001, overridable at build time via `APP_UID`/`APP_GID`).

The entrypoint is the `youtube-mcp` console script, which dispatches on `MCP_TRANSPORT`. The image
sets no transport of its own, so it **defaults to `stdio`** — the same default as a local run. Run it
with `-i` and the agent on the other end drives it over stdin/stdout; with `YOUTUBE_API_KEY` set and
no piped stdin the server reads EOF and exits 0 immediately (correct MCP stdio behaviour, not a
crash), while a keyless run fails fast with exit 1 before EOF matters. Set
`MCP_TRANSPORT=http` to serve it as a long-running HTTP service instead (`/health` answers):

```bash
# stdio (default in the image) — `-i` keeps stdin open; EOF ends the server
# (an MCP client launches it this way; see the JSON config above)
docker run -i --rm -e YOUTUBE_API_KEY=... youtube-mcp:dev

# HTTP — explicit `MCP_TRANSPORT=http`, long-running, /health on the published port
docker run --rm -p 8088:8088 -e YOUTUBE_API_KEY=... -e MCP_TRANSPORT=http youtube-mcp:dev
docker run --rm -p 9000:9000 -e YOUTUBE_API_KEY=... -e MCP_TRANSPORT=http -e MCP_PORT=9000 youtube-mcp:dev
```

`DATABASE_PATH=/data/cache.db` is the image default, and `/data` exists and is writable by the
non-root user. **No volume is needed**: the cache is ephemeral by design and a cold cache costs
quota, not correctness. To keep it across restarts, point `DATABASE_PATH` at a mounted volume (or
mount one at `/data`) — that is the persistent variant, and it is the only reason to add a volume.

A local build of this Dockerfile measured ~212 MB on disk / ~78 MB compressed (`docker save |
gzip -1`), against a ~42.6 MB `python:3.13-slim` base. Treat that as indicative, not a budget —
CI measures the real number.

---

## Non-goals for v1

Do not build these without being asked — explicitly out of scope:

- Video upload, editing or deletion.
- Caption upload or management (needs OAuth and ownership).
- Analytics API, Reporting API, or any channel-owner data.
- OAuth of any kind.
- Multi-key quota rotation (a policy violation — see [Quota](#quota)).
- Dislike counts.
- Downloading video or audio media.
- Live chat retrieval.
- Any UI. This is a tool provider.

---

## Decisions

Answered, for the record (previously open questions):

- **Transport.** stdio is the default for a local agent; HTTP is the opt-in for shared/deployed use.
  The container image keeps that same stdio default and takes HTTP as an explicit `MCP_TRANSPORT=http`.
- **Auth.** None. This is a local stdio server; HTTP mode is expected to sit behind a trusted
  boundary.
- **Cache persistence.** Ephemeral. No volume in the container; set `DATABASE_PATH` to a mounted
  path if you want the warm-cache variant.
- **Proxy.** Unconfigured — correct for a residential/clean IP. Configure Webshare or a generic proxy
  if the server ever runs from a cloud host, or transcripts will be blocked.

---

## References

- FastMCP docs — <https://gofastmcp.com>
- YouTube Data API errors — <https://developers.google.com/youtube/v3/docs/errors>
- Quota costs — <https://developers.google.com/youtube/v3/determine_quota_cost>
- `videos:batchGetStats` — <https://developers.google.com/youtube/v3/docs/videos/batchGetStats>
- Developer Policies (quota) — <https://developers.google.com/youtube/terms/developer-policies-guide>
- `youtube-transcript-api` — <https://github.com/jdepoix/youtube-transcript-api>
