#!/usr/bin/env python3
"""
cterm_server - Persistent, self-terminating MCP tool server.
This server runs in the background, bound to a Unix Domain Socket (UDS),
and shuts down after a period of inactivity.
"""
import logging
import os
import sys
import time
import threading
import asyncio
import socket
import json
from pathlib import Path
import pwd

logger = logging.getLogger(__name__)

# Import the MCP server and tools
try:
    from tools_mcp import mcp
except ImportError:
    try:
        from .tools_mcp import mcp
    except ImportError:
        logger.error("Could not import tools_mcp. Ensure tools_mcp.py is in the same directory.")
        sys.exit(1)

# 20 minutes of inactivity
INACTIVITY_TIMEOUT_SECONDS = 1200

def get_socket_path() -> Path:
    """Returns the UDS path based on the current user (using UID for robustness)."""
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        username = os.environ.get('USER', 'default')
    return Path(f"/tmp/cterm_mcp_{username}.sock")

class MCPServer:
    """Wrapper to handle MCP protocol over UDS"""
    def __init__(self, mcp_instance):
        self.mcp = mcp_instance
        self.last_activity = time.time()
        
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = time.time()
    
    async def handle_request(self, request_data: str, writer: asyncio.StreamWriter):
        """
        Handle an MCP request and write response(s) to writer.

        For streaming tool results the server sends multiple newline-delimited
        JSON frames before the final result frame:
          {"jsonrpc":"2.0","id":N,"stream":{"fd":"stdout"|"stderr","line":"..."}}
          {"jsonrpc":"2.0","id":N,"result": <final result dict>}

        Non-streaming tools send a single result frame as before.
        """
        self.update_activity()

        def send(obj: dict):
            """Serialise obj and write it as a newline-terminated frame."""
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))

        try:
            request = json.loads(request_data)
            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id", 1)

            if method == "tools/list":
                tools = []
                for tool_name in dir(self.mcp):
                    if not tool_name.startswith('_'):
                        tool_func = getattr(self.mcp, tool_name, None)
                        if callable(tool_func) and hasattr(tool_func, '__mcp_tool__'):
                            tools.append({
                                "name": tool_name,
                                "description": tool_func.__doc__ or f"Tool: {tool_name}",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {},
                                    "required": []
                                }
                            })
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": tools},
                })

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})

                if not hasattr(self.mcp, tool_name):
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                    })
                    return

                tool_func = getattr(self.mcp, tool_name)
                try:
                    result = tool_func(**arguments)
                except Exception as e:
                    send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": f"Tool execution error: {str(e)}"},
                    })
                    return

                # -------------------------------------------------------- #
                #  Detect a streaming generator vs a plain dict result      #
                # -------------------------------------------------------- #
                import types
                if isinstance(result, types.GeneratorType):
                    # Drain the generator, forwarding stream frames live and
                    # holding back the final "result" frame until the end so
                    # we can wrap it in the jsonrpc envelope.
                    final_result = None
                    for chunk in result:
                        if chunk.get("type") == "stream":
                            # Live output line — send immediately so the
                            # client can display it as it arrives.
                            send({
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "stream": {
                                    "fd":   chunk["fd"],
                                    "line": chunk["line"],
                                    "end":  chunk.get("end", "\n"),
                                },
                            })
                            # Flush so bytes reach the client without waiting
                            # for the write buffer to fill.
                            await writer.drain()
                        elif chunk.get("type") == "result":
                            final_result = chunk
                        # Unknown chunk types are silently ignored.

                    # Send the final summary frame.
                    if final_result is not None:
                        payload = {k: v for k, v in final_result.items() if k != "type"}
                        send({"jsonrpc": "2.0", "id": request_id, "result": payload})
                    else:
                        # Generator ended without a result frame — shouldn't
                        # happen, but handle gracefully.
                        send({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {"ok": True, "command": arguments.get("command", "")},
                        })
                else:
                    # Plain dict — original single-frame behaviour.
                    send({"jsonrpc": "2.0", "id": request_id, "result": result})

            else:
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })

        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "Parse error"}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32603, "message": f"Internal error: {str(e)}"}})


def run_server():
    """Starts the MCP server on UDS with inactivity timeout."""
    print("Starting cterm MCP tool server...")
    
    mcp_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(mcp_loop)
    
    socket_path = get_socket_path()
    mcp_server = MCPServer(mcp)
    
    # 1. Cleanup old socket file if present
    if socket_path.exists():
        try:
            os.unlink(socket_path)
        except OSError as e:
            if e.errno != 13:  # errno 13 is Permission denied
                logger.error("Error unlinking old socket %s: %s", socket_path, e)
    
    print(f"Server starting on UDS: {socket_path}")
    
    # 2. Inactivity Check Thread
    def check_for_timeout():
        while True:
            time.sleep(30)  # Check every 30 seconds
            if time.time() - mcp_server.last_activity > INACTIVITY_TIMEOUT_SECONDS:
                logger.info("Inactivity timeout reached. Shutting down server.")
                mcp_loop.call_soon_threadsafe(mcp_loop.stop)
                break
    
    threading.Thread(target=check_for_timeout, daemon=True).start()
    
    # 3. Setup UDS Server
    server = None
    
    try:
        # Create UDS socket
        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind(str(socket_path))
        server_socket.listen(5)
        
        # Make socket world-readable (but not world-writable)
        os.chmod(socket_path, 0o666)
        
        logger.debug("Socket created at %s", socket_path)

        # Handle client connections
        async def handle_client(reader, writer):
            try:
                # Read the request (up to 64KB)
                data = await reader.read(65536)
                if not data:
                    writer.close()
                    await writer.wait_closed()
                    return
                
                request_text = data.decode('utf-8').strip()
                logger.debug("Received request: %s...", request_text[:100])

                # Process the request, streaming frames directly to writer
                await mcp_server.handle_request(request_text, writer)

                # Final drain to flush any buffered bytes
                await writer.drain()
                logger.debug("Response(s) sent.")

            except Exception as e:
                logger.exception("Error handling client: %s", e)
            finally:
                writer.close()
                await writer.wait_closed()
        
        # Start the asyncio server
        logger.debug("Starting asyncio server...")
        server = mcp_loop.run_until_complete(
            asyncio.start_server(handle_client, sock=server_socket)
        )
        
        print(f"✓ MCP server listening on {socket_path}")
        
        # List registered tools
        tool_count = 0
        for name in dir(mcp):
            if not name.startswith('_') and callable(getattr(mcp, name, None)):
                tool_count += 1
        print(f"✓ Registered {tool_count} tools")
        
        # Run event loop
        mcp_loop.run_forever()
        
        logger.debug("Event loop stopped gracefully.")
        
    except SystemExit:
        logger.debug("Caught SystemExit.")
    except Exception as e:
        logger.exception("Server loop failed: %s", e)
    finally:
        # Cleanup
        if server:
            server.close()
            mcp_loop.run_until_complete(server.wait_closed())
        
        if socket_path.exists():
            logger.debug("Unlinking socket file: %s", socket_path)
            os.unlink(socket_path)
        
        mcp_loop.close()
        logger.info("Server process exiting.")

if __name__ == "__main__":
    run_server()
