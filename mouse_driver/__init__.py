"""Mouse Driver — human-shaped control of the real X11 pointer on the desktop host.

The public entry points are the stdio MCP server (``mouse_driver.mcp_server``),
the always-on-top GTK HUD (``hud/hud.py``) and the telemetry HTTP API
(``mouse_driver.telemetry``). Nothing here reads files outside the project's
``data/`` directory or types secrets.
"""

__version__ = "1.0.0"
