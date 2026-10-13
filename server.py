"""Entry point kept at the repo root so existing MCP configs (`python server.py`) keep working.

The code lives in src/rekordbox_mcp; `pip install -e .` also gives `dj-setbox` (MCP server), `rbmcp` (CLI) and `setbox-web`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rekordbox_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
