"""YouTube MCP server — transcripts, search, comments and stats for an LLM."""

from importlib.metadata import PackageNotFoundError, version

# The PyPI distribution name, which is not the import name (`youtube_mcp`) and not the
# console script (`youtube-mcp`): `youtube-mcp` on PyPI belongs to another account, so the
# distribution is namespaced by owner.
DISTRIBUTION_NAME = "dreadster3-youtube-mcp"

# Read from the installed distribution metadata, never hardcode: release-please bumps
# `project.version` in pyproject.toml on every release PR, and a hardcoded copy here would
# would ship the wrong version in /health.
try:
    __version__ = version(DISTRIBUTION_NAME)
except PackageNotFoundError:  # imported from a source tree that was never installed
    __version__ = "0.0.0+unknown"
