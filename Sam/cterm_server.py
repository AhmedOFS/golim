#!/usr/bin/env python3
"""
cterm_server - MCP stdio tool server using official SDK
"""

import sys
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

try:
    from tools_mcp import mcp as mcp_tools
except ImportError:
    try:
        from .tools_mcp import mcp as mcp_tools
    except ImportError:
        print("ERROR: Could not import tools_mcp.", file=sys.stderr)
        sys.exit(1)

import inspect

app = Server("cterm")


def discover_tools():
    tools = []
    for name in dir(mcp_tools):
        if name.startswith('_'):
            continue
        fn = getattr(mcp_tools, name, None)
        if not callable(fn) or not getattr(fn, '__mcp_tool__', False):
            continue

        try:
            sig = inspect.signature(fn)
        except (ValueError, TypeError):
            continue

        properties = {}
        required = []
        for param_name, param in sig.parameters.items():
            annotation = param.annotation
            if annotation == bool:
                json_type = "boolean"
            elif annotation in (float, int):
                json_type = "number"
            else:
                json_type = "string"

            properties[param_name] = {"type": json_type}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        tools.append(types.Tool(
            name=name,
            description=fn.__doc__ or f"Tool: {name}",
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": required
            }
        ))
    return tools


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return discover_tools()


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    fn = getattr(mcp_tools, name, None)
    if fn is None or not getattr(fn, '__mcp_tool__', False):
        raise ValueError(f"Tool not found: {name}")

    sig = inspect.signature(fn)
    coerced = {}
    for param_name, param in sig.parameters.items():
        val = arguments.get(param_name)
        if val is not None:
            if param.annotation == bool and isinstance(val, str):
                val = val.lower() == "true"
            elif param.annotation in (float, int):
                val = param.annotation(val)
        coerced[param_name] = val

    result = fn(**coerced)
    return [types.TextContent(type="text", text=str(result))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())