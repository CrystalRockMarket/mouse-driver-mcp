from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def _params(extra_env: dict, drop: tuple[str, ...] = ()) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra_env)
    env["MOUSE_DRIVER_DATA"] = os.environ["MOUSE_DRIVER_DATA"]
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mouse_driver.mcp_server"],
        cwd=str(ROOT),
        env=env,
    )


async def _handshake(params: StdioServerParameters) -> tuple[int, dict]:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("mouse_status", {})
            payload = json.loads(result.content[0].text)
            return len(tools.tools), payload


def test_mcp_handshake_without_display():
    """The server must complete initialize/tools-list even with no X server."""
    params = _params({"MOUSE_DRIVER_FORCE_NO_X": "1"}, drop=("DISPLAY", "MOUSE_DRIVER_DRY"))
    count, status = anyio.run(_handshake, params)
    assert count == 19
    assert status["ok"] is False
    assert status["driver"] == "unavailable"
    assert status["error"] == "XOpenDisplay failed"
    assert status["armed"] is False
    assert status["locked"] is False
    assert status["paused"] is False


def test_acting_tools_refuse_cleanly_without_x():
    params = _params({"MOUSE_DRIVER_FORCE_NO_X": "1"}, drop=("DISPLAY", "MOUSE_DRIVER_DRY"))

    async def run() -> dict:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("mouse_arm", {"ttl_s": 30})
                result = await session.call_tool("mouse_move", {"x": 100, "y": 100})
                return json.loads(result.content[0].text)

    payload = anyio.run(run)
    assert payload["ok"] is False
    assert payload["code"] == "driver_unavailable"
    assert "XOpenDisplay failed" in payload["error"]


def test_mcp_handshake_with_default_display_env():
    """With DISPLAY removed the server defaults it and still handshakes."""
    params = _params({}, drop=("DISPLAY", "MOUSE_DRIVER_DRY"))
    count, status = anyio.run(_handshake, params)
    assert count == 19
    assert status["ok"] is True
    assert status["backend"] in {"xtest", "xdotool"}
