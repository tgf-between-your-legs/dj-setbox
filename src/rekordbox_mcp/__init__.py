"""dj-setbox (package rekordbox_mcp): an MCP server and set-planning engine for rekordbox 7 libraries."""
__version__ = "0.2.0"


def main() -> None:
    from .server import main as _main
    _main()
