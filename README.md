# YouTube MCP Server

A single-process [MCP](https://modelcontextprotocol.io) server that exposes public YouTube data —
transcripts, search, video metadata and statistics, comments, channels, categories — as tools an LLM
can call. It runs in the homelab Kubernetes cluster and is consumed by OpenWebUI (native MCP, or
through `mcpo` as an OpenAPI proxy), and possibly n8n.

One Python process: FastMCP server, YouTube Data API client, transcript fetching and cache all live
in the same application. No sidecar, no second service, no Redis.

- **11 tools**, all namespaced `youtube_*`, read-only public data.
- **API-key only.** No OAuth, no uploads, no channel management.
- **The container image is the artifact.** No PyInstaller/Nuitka single-binary builds — CPython does
  not statically link, and self-extracting archives are coupled to the target's glibc.
- **Dislike counts do not exist.** YouTube made them private in December 2021; no endpoint returns
  them, and this server will not estimate them.

---

## Read this first: YouTube blocks cloud IPs

Transcripts are not available through the official API — `captions.download` requires OAuth consent
from the video's owner. Anything fetching arbitrary transcripts is scraping YouTube's internal
`timedtext` endpoint, and **YouTube blocks most known cloud-provider IP ranges (AWS, GCP, Azure)**.
Symptoms are `RequestBlocked` / `IpBlocked`, surfaced by this server as
`TRANSCRIPT_IP_BLOCKED` (retryable, transient).

**Residential hosting is the favourable case**, which is why this runs on the homelab's residential
Lisbon connection and ships with no proxy configured. **If this ever moves behind a VPS, transcript
fetching will fail without a rotating residential proxy** — budget for one before the move, not after.

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
| `youtube_get_comments` | Top-level comments, `time` or `relevance` order, pageable | shared (10,000/day) |
| `youtube_get_channel` | Channel by ID or `@handle`, plus its uploads playlist ID | shared (10,000/day) |
| `youtube_list_channel_videos` | Channel uploads, newest first, via the uploads playlist | shared (10,000/day) |
| `youtube_list_categories` | Video categories, optionally per region | shared (10,000/day) |

Notes that the tool descriptions also carry, because the model is the main consumer:

- **No dislikes anywhere.** Not on videos, not on comments, not on `batchGetStats`. Tool
  descriptions say so explicitly so the model stops asking.
- **Comments are top-level only in this version.** Each comment carries `total_reply_count`, but reply
  texts are not fetched. Do not read the count as content that was retrieved.
- **Transcripts are unavailable for some videos, and that is normal.** Captions disabled by the
  uploader (`TRANSCRIPT_DISABLED`), no track in the requested language (`TRANSCRIPT_NOT_FOUND`),
  age-restricted due to broken upstream cookie auth in `youtube-transcript-api` 1.2.4
  (`TRANSCRIPT_AGE_RESTRICTED`), or YouTube demanding a PO token (`TRANSCRIPT_UPSTREAM_ERROR`). None
  of these are outages; only `TRANSCRIPT_IP_BLOCKED` and `TRANSCRIPT_UPSTREAM_ERROR` are worth
  retrying.
- `youtube_list_channel_videos` deliberately resolves the channel's uploads playlist and pages
  `playlistItems` instead of searching — 2 units from the large shared pool rather than 100/day.
  `youtube_search_videos` is the scarce, deliberate operation.
- Transcripts are truncated at `RESPONSE_LIMIT` and return `next_cursor`; an uncapped 3-hour
  transcript would blow the model's context window.

---

## Quota

Three buckets, because Google counts them separately:

| Bucket | Budget | Consumed by |
| --- | --- | --- |
| `search` | 100 calls/day | `search.list`, one unit per call — **including each additional page** |
| `stats` | 10,000 calls/day | `videos:batchGetStats` only |
| `shared` | 10,000 units/day | `videos.list`, `channels.list`, `playlistItems.list`, `commentThreads.list`, `videoCategories.list` |

- Quotas reset at **midnight Pacific Time** (`America/Los_Angeles`, DST-aware). Not midnight UTC, not
  midnight local. A `403 quotaExceeded` means done for the day — the error message says "resets
  midnight Pacific", and the model is told not to retry until then.
- `youtube_get_video_stats` uses `videos:batchGetStats` specifically because that method has **its own
  10,000/day bucket** and returns up to 50 videos per call. Pulling counts through
  `youtube_get_video` would burn the shared pool instead.
- The server keeps its own per-bucket counter and warns in the log at 10% remaining. It is
  **approximate** — the authoritative accounting is Google's, and counters reset to zero on restart,
  deliberately, so a restart can never over-count. The API does not tell you which bucket tripped,
  which is why we count locally; `/health` exposes this accounting as
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
raises at startup (`create_app`), so a misconfigured pod crashes immediately rather than on the first
tool call.

| Variable | Default | Notes |
| --- | --- | --- |
| `YOUTUBE_API_KEY` | *(required)* | Single key. Parsed as a `SecretStr`, so it never appears in logs, reprs or tracebacks. |
| `YOUTUBE_TRANSCRIPT_LANG` | `en` | Default transcript language when a tool call does not name one. |
| `MCP_TRANSPORT` | `http` | `http` or `stdio`. Selects the transport in the `youtube-mcp` console script. |
| `MCP_HOST` | `0.0.0.0` | HTTP listen address. |
| `MCP_PORT` | `8088` | HTTP listen port. |
| `FASTMCP_STATELESS_HTTP` | `true` | See [Kubernetes](#kubernetes) — leave it true in a cluster. |
| `RESPONSE_LIMIT` | `50000` | Transcript truncation threshold, in characters (segment count for the timestamped variant). |
| `CACHE_TTL_SECONDS` | `3600` | Declared in `config.py`; the caching paths currently use per-target TTLs instead (see [Caching](#caching)) — so setting this has **no effect today**. |
| `DATABASE_PATH` | `cache.db` | SQLite cache file. **Relative by default** — set an absolute path in a container. |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | unset | Webshare proxy for transcript fetching; password is a `SecretStr`. |
| `HTTP_PROXY` / `HTTPS_PROXY` | unset | Generic proxy alternative. |
| `LOG_LEVEL` | `INFO` | Level for uvicorn and the application loggers. |

Secrets belong in Sealed Secrets or External Secrets in `home-ops`. **Never commit a real API key or
proxy credential to this repository.**

---

## Running

### Locally, with uv

```bash
uv sync                       # creates .venv from uv.lock
export YOUTUBE_API_KEY=...    # required
uv run youtube-mcp            # HTTP on 0.0.0.0:8088
```

The console script dispatches on `MCP_TRANSPORT`:

```bash
# streamable HTTP (default) — uvicorn with the factory target
uv run youtube-mcp

# stdio, for desktop clients and local testing
MCP_TRANSPORT=stdio uv run youtube-mcp
```

Under HTTP the app itself is a factory with no module-level `app` object, so uvicorn is invoked as:

```bash
uv run uvicorn youtube_mcp.server:create_app --factory --host 0.0.0.0 --port 8088
```

That factory form is what the container runs. There is deliberately no import-time settings read, so
`import youtube_mcp.server` works without an API key — which is what makes the factory testable.

### Docker

Build from the repository root, where the `.dockerignore` and sources are:

```bash
docker build -f deploy/Dockerfile -t youtube-mcp:dev .

docker run --rm -p 8088:8088 -e YOUTUBE_API_KEY=... youtube-mcp:dev
```

Two stages: a builder that runs `uv sync --frozen --no-dev --compile-bytecode` into `/app/.venv`,
and a `python:3.13-slim` runtime that copies **only** the venv (`/app/.venv`) and the package source
(`/app/src`). No `uv`, no build tooling, no test dependencies in the final image. The image runs as a
non-root user (`app`, uid/gid 10001, overridable at build time via `APP_UID`/`APP_GID`).

The entrypoint honours the same host/port configuration:

```bash
docker run --rm -p 9000:9000 -e YOUTUBE_API_KEY=... -e MCP_PORT=9000 youtube-mcp:dev
```

A local build of this Dockerfile measured ~212 MB on disk / ~78 MB compressed (`docker save |
gzip -1`), against a ~42.6 MB `python:3.13-slim` base. Treat that as indicative, not a budget —
CI measures the real number.

### Kubernetes

Manifests live in [`dreadster3/home-ops`](https://github.com/dreadster3/home-ops) and are reconciled
by Flux. **This repository is the source; `home-ops` is the deployment truth** — do not commit
manifests here.

Points that matter when writing those manifests:

- **Probes.** `/health` is a `GET` returning `{"status": "ok", "server", "version",
  "quota_remaining_approximate"}`. No auth, no external calls, so it is safe as both a liveness and a
  readiness probe. (Custom routes bypass MCP auth middleware by design.)
- **`stateless_http=True` (the default) is required for replicas to work.** The stateful transport
  keeps sessions in server memory, which breaks across replicas, and sticky sessions do not reliably
  fix it: most MCP clients use `fetch()` internally and never forward `Set-Cookie`. Stateless HTTP
  makes each request independent, so any replica can serve any request. Set
  `FASTMCP_STATELESS_HTTP=false` only for a single-replica debugging session.
- **Reverse proxy / ingress.** SSE streaming still needs `proxy_buffering off; proxy_cache off;`
  `proxy_http_version 1.1`, `Connection ''`, and generous read/send timeouts (the docs use 300s).
  Stateless mode removes the session-affinity problem but does **not** remove streaming buffering
  concerns — a buffering proxy will still stall a long tool call.
- **`DATABASE_PATH=/data/cache.db`** is the image default; `/data` is created and owned by the
  non-root user. Mount a PVC there for a warm cache across restarts, or leave it ephemeral: **a cold
  cache costs quota, not correctness.** Decide with the operator (see [Open decisions](#open-decisions)).
- Resource requests/limits, and consider a **NetworkPolicy** restricting egress to the YouTube API
  and, if configured, the proxy.
- Single container, single process. No sidecar.

---

## Caching

SQLite via `aiosqlite` — one file, no extra service. Redis is not worth a tenant for this.

| Cached | Key | TTL |
| --- | --- | --- |
| Transcripts | `transcript:<video_id>:<lang>` (and `…:styled`) | forever — a published transcript does not change |
| Transcript track lists | `transcript_tracks:<video_id>` | forever |
| Video stats | `video_stats:<video_id>` | 300s — view counts move |
| Channel → uploads playlist | `uploads_playlist:<channel_id>` | forever — it never changes |

Transcript and stats cache hits cost **no quota at all**, which is the point: caching is the primary
quota strategy, not an optimisation. Cache values are JSON, expiry is per entry, and expired rows are
deleted on read.

---

## Development

```bash
uv sync                              # dev dependencies included
uv run pytest                        # 362 tests, offline
uv run python -c "from youtube_mcp.server import create_app; print('importable')"
```

The suite is fully offline: Data API responses come from recorded JSON fixtures in
`tests/fixtures/`, and transcript fetching is stubbed. No test hits the live API, so a test run costs
no quota and works with the network unplugged. Coverage is on by default through `pyproject.toml`
(`--cov=youtube_mcp`); run `uv run pytest --cov-report=html` for the browsable report.

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
```

### Manual smoke test

Needs a real API key and, for the transcript cases, a residential IP.

```bash
export YOUTUBE_API_KEY=...
uv run youtube-mcp            # or: docker run --rm -p 8088:8088 -e YOUTUBE_API_KEY=$YOUTUBE_API_KEY youtube-mcp:dev
curl -s localhost:8088/health | jq
```

Then, with an MCP client pointed at `http://localhost:8088/mcp` (the MCP Inspector works the same
way), or straight from the CLI:

```bash
uv run fastmcp list http://localhost:8088/mcp --transport http   # should list all 11 tools
```

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

## Open decisions

Unresolved by design — flagged for the operator rather than assumed:

1. **Cache persistence** — PVC-backed SQLite versus ephemeral in-pod storage. Ephemeral is simpler;
   a cold cache costs quota but not correctness.
2. **OpenWebUI MCP client behaviour** — which protocol era it negotiates, and whether it wants
   streamable HTTP or SSE. **Unverified.** This is the most likely integration surprise; check
   OpenWebUI's own docs before trusting this server's transport assumptions.
3. **Auth on the MCP endpoint** — in-cluster-only for v1, presumably. If the endpoint is exposed more
   broadly, add a `StaticTokenVerifier` at minimum.
4. **Proxy** — unconfigured, given residential hosting. Confirm whether a VPS deployment is plausible;
   if so, budget for a rotating residential proxy before it is needed.
5. **Exposure** — OpenWebUI only, or also n8n and desktop clients? Affects transport priorities.

---

## References

- FastMCP docs — <https://gofastmcp.com>
- YouTube Data API errors — <https://developers.google.com/youtube/v3/docs/errors>
- Quota costs — <https://developers.google.com/youtube/v3/determine_quota_cost>
- `videos:batchGetStats` — <https://developers.google.com/youtube/v3/docs/videos/batchGetStats>
- Developer Policies (quota) — <https://developers.google.com/youtube/terms/developer-policies-guide>
- `youtube-transcript-api` — <https://github.com/jdepoix/youtube-transcript-api>
