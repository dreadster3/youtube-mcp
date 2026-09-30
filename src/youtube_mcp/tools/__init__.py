"""MCP tool surface (§8). One module per tool group, each exposing `register(mcp, deps)`.

`deps` carries the client, cache, settings and transcript config — tools are closures over
it, so there is no module-level mutable state and the module imports without reading
configuration (the app factory owns that).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastmcp import FastMCP

from youtube_mcp.cache import Cache
from youtube_mcp.config import Settings
from youtube_mcp.youtube.client import YouTubeClient


@dataclass(frozen=True)
class Deps:
    """Everything a tool needs, resolved once by `server.create_app`."""

    client: YouTubeClient
    cache: Cache | None
    settings: Settings
    #: Extra languages tried after the requested ones (the configured default). Empty by
    #: default; a hook for operators who want a fixed fallback chain.
    fallback_languages: tuple[str, ...] = field(default=())


def register_all(mcp: FastMCP, deps: Deps) -> None:
    """Register every §8 tool group on `mcp`."""
    from youtube_mcp.tools import data, transcripts

    transcripts.register(mcp, deps)
    data.register(mcp, deps)
