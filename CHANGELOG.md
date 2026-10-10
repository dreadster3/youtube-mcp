# Changelog

## [0.3.0](https://github.com/dreadster3/youtube-mcp/compare/v0.2.0...v0.3.0) (2026-10-10)


### Features

* **tools:** add local quota status tool ([#27](https://github.com/dreadster3/youtube-mcp/issues/27)) ([388a469](https://github.com/dreadster3/youtube-mcp/commit/388a469d1abf77dfa1f95d0c658b3387a9dcfff4))
* **tools:** add playlist metadata and playlist item tools ([#26](https://github.com/dreadster3/youtube-mcp/issues/26)) ([dc18c3b](https://github.com/dreadster3/youtube-mcp/commit/dc18c3b5031bce28d82120ed3fc4180b644dee6b))
* **tools:** surface comment replies and reply counts ([#24](https://github.com/dreadster3/youtube-mcp/issues/24)) ([d986720](https://github.com/dreadster3/youtube-mcp/commit/d986720a44cb4f2757b19cc47ff7402bae8da384))
* **transcript:** add translate_to to the transcript tools ([#25](https://github.com/dreadster3/youtube-mcp/issues/25)) ([11a41b7](https://github.com/dreadster3/youtube-mcp/commit/11a41b7b8367495d5d8d5b17d996e4cac69dad56))

## [0.2.0](https://github.com/dreadster3/youtube-mcp/compare/v0.1.0...v0.2.0) (2026-10-05)


### Features

* **renovate:** enable renovate ([#17](https://github.com/dreadster3/youtube-mcp/issues/17)) ([2431838](https://github.com/dreadster3/youtube-mcp/commit/2431838a7739bb3539dda8aeb620d7ad3da6ad2d))


### Bug Fixes

* **deps:** update dependency fastmcp to v4.0.11 ([#9](https://github.com/dreadster3/youtube-mcp/issues/9)) ([3cc063f](https://github.com/dreadster3/youtube-mcp/commit/3cc063f4f5ce85378a98d8689ace674b2800c0b1))

## 0.1.0 (2026-10-04)


### Features

* batch 1 - project scaffold, config, cache, transcript error taxonomy ([69d0f61](https://github.com/dreadster3/youtube-mcp/commit/69d0f617772270968dae7c5f0824cd58cdb8afea))
* batch 2 - quota counter, YouTube Data API client, models (worktree batch 2, pre-review) ([d3b20c0](https://github.com/dreadster3/youtube-mcp/commit/d3b20c0955bdccf6a9b607db736b247ccd1532be))
* batch 3 - transcript fetch layer (worktree batch 3, pre-review) ([873d7aa](https://github.com/dreadster3/youtube-mcp/commit/873d7aa5ebcd0d0341437678ce6e3431b81b3c81))
* batch 4 - MCP tool surface (11 tools) + FastMCP server ([68a3820](https://github.com/dreadster3/youtube-mcp/commit/68a38205a1af76c2f48bbad2728e9c6e5cff7386))
* batch 5 - container packaging + README ([9c56c02](https://github.com/dreadster3/youtube-mcp/commit/9c56c0266f025a90a0239241b9afbfa45338e9a2))
* CI + release workflows, dist rename, dynamic __version__ ([#1](https://github.com/dreadster3/youtube-mcp/issues/1)) ([6fce378](https://github.com/dreadster3/youtube-mcp/commit/6fce378f10edeee614706143d11a15840a4eb581))
* operator-finalized stdio-first mode, honored CACHE_TTL_SECONDS, image transport dispatch ([e2a24e9](https://github.com/dreadster3/youtube-mcp/commit/e2a24e98e1877696730c4846a5f05a27fce0dc53))
* task run + user's Taskfile edit (ci-&gt;check, help default, docker tasks removed) ([fb2647e](https://github.com/dreadster3/youtube-mcp/commit/fb2647e1f258bf0af0259d23b55992b54ea864ed))
* Taskfile CI gate + ruff/mypy enforcement (53 findings -&gt; 0) ([a9a10af](https://github.com/dreadster3/youtube-mcp/commit/a9a10af4d55d87c8a1d9150a807094a3efbb3729))


### Bug Fixes

* batch 4 review fixes - cursor clamping, channel-miss message, comments description ([ffce381](https://github.com/dreadster3/youtube-mcp/commit/ffce381ddccbbf0f69e532df5391357887d0e6a1))
* **docker:** default image transport to stdio ([#5](https://github.com/dreadster3/youtube-mcp/issues/5)) ([152dccf](https://github.com/dreadster3/youtube-mcp/commit/152dccfd2b7c9d05324d9112846213e78fd06fac))
* **release:** rename publish env to 'release' ([#4](https://github.com/dreadster3/youtube-mcp/issues/4)) ([20f71f2](https://github.com/dreadster3/youtube-mcp/commit/20f71f24e8199887ca088502b58dffd96ffe5533))
* stdio-mode review fixes - working smoke-test cmd, SIGINT handler, TTL=0 docs ([8c233a3](https://github.com/dreadster3/youtube-mcp/commit/8c233a31c1decced4feb864141c3a2447dea19a6))


### Documentation

* **agents:** add agent conventions — semantic commits w/ preferred scope ([#3](https://github.com/dreadster3/youtube-mcp/issues/3)) ([ad33262](https://github.com/dreadster3/youtube-mcp/commit/ad332627820e62289887e9c134b1fde67480cdce))
* **agents:** drop commit subject length rule ([#7](https://github.com/dreadster3/youtube-mcp/issues/7)) ([442ced5](https://github.com/dreadster3/youtube-mcp/commit/442ced5a84dec5c0b20c605ca4a06657451ca3a0))
* replace section sign symbol with 'section' in comments and docstrings ([fd160aa](https://github.com/dreadster3/youtube-mcp/commit/fd160aaa31dfb312462b98d2e25c246ac23cc273))
