"""Bible Writer MCP tool — binds src.utility.bible.agent.run_write_bible.

Merges one volume's populated context.xml into the cumulative English series
bible under bibles/<series_id>/.

The import is deferred to call time: a checkout without src/utility/bible/
must cost this one tool, not the whole server (a module-level import failure
here used to take all the other tools down with it at startup).
"""

from __future__ import annotations

from typing import Optional

from src.Deepseek.mcp.mcp_config import MCPConfig
from src.Deepseek.mcp.runtime import MCPRuntimeError, resolve_volume_dir


def register_bible_tools(mcp: object, cfg: MCPConfig) -> None:
    """Register the Bible Writer tool."""

    @mcp.tool()  # type: ignore[attr-defined]
    def write_bible(volume_id: str, series_id: Optional[str] = None) -> dict:
        resolve_volume_dir(volume_id, cfg)  # validates the volume exists under work/
        try:
            from src.utility.bible.agent import BibleError, run_write_bible
        except ImportError as exc:
            raise MCPRuntimeError(
                "write_bible is unavailable: the src/utility/bible package is missing from this checkout."
            ) from exc
        try:
            receipt = run_write_bible(volume_id, series_id=series_id)
        except BibleError as exc:
            raise MCPRuntimeError(str(exc)) from exc
        receipt["ok"] = True
        return receipt
