#!/usr/bin/env python3
"""
cterm_server - Persistent, self-terminating MCP tool server.
This server runs in the background, bound to a Unix Domain Socket (UDS),
and shuts down after a period of inactivity.
"""
import os
import sys
import time
import threading
import asyncio
import socket
import json
from pathlib import Path
import pwd

# Import the MCP server and tools
try:
    from tools_mcp import mcp
except ImportError:
    try:
        from .tools_mcp import mcp
    except ImportError:
        print("ERROR: Could not import tools_mcp. Ensure tools_mcp.py is in the same directory.", file=sys.stderr)
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
    
    async def handle_request(self, request_data):
        """Handle an MCP request and return response"""
        self.update_activity()
        
        try:
            request = json.loads(request_data)
            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id", 1)
            
            if method == "tools/list":
                # List available tools
                tools = []
                for tool_name in dir(self.mcp):
                    if not tool_name.startswith('_'):
                        tool_func = getattr(self.mcp, tool_name, None)
                        if callable(tool_func) and hasattr(tool_func, '__mcp_tool__'):
                            # Get tool metadata
                            tools.append({
                                "name": tool_name,
                                "description": tool_func.__doc__ or f"Tool: {tool_name}",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {},
                                    "required": []
                                }
                            })
                
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": tools}
                }
                
            elif method == "tools/call":
                # Call a specific tool
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                
                if hasattr(self.mcp, tool_name):
                    tool_func = getattr(self.mcp, tool_name)
                    try:
                        result = tool_func(**arguments)
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": result
                        }
                    except Exception as e:
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32000,
                                "message": f"Tool execution error: {str(e)}"
                            }
                        }
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": f"Tool not found: {tool_name}"
                        }
                    }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}"
                    }
                }
            
            return json.dumps(response)
            
        except json.JSONDecodeError:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"}
            })
        except Exception as e:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"}
            })

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
                print(f"Error unlinking old socket {socket_path}: {e}", file=sys.stderr)
    
    print(f"Server starting on UDS: {socket_path}")
    
    # 2. Inactivity Check Thread
    def check_for_timeout():
        while True:
            time.sleep(30)  # Check every 30 seconds
            if time.time() - mcp_server.last_activity > INACTIVITY_TIMEOUT_SECONDS:
                print("Inactivity timeout reached. Shutting down server.")
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
        
        print(f"DEBUG: Socket created at {socket_path}")
        
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
                print(f"DEBUG: Received request: {request_text[:100]}...")
                
                # Process the request
                response = await mcp_server.handle_request(request_text)
                print(f"DEBUG: Sending response: {response[:100]}...")
                
                # Send response
                writer.write((response + "\n").encode('utf-8'))
                await writer.drain()
                
            except Exception as e:
                print(f"ERROR handling client: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc(file=sys.stderr)
            finally:
                writer.close()
                await writer.wait_closed()
        
        # Start the asyncio server
        print("DEBUG: Starting asyncio server...")
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
        
        print("DEBUG: Event loop stopped gracefully.")
        
    except SystemExit:
        print("DEBUG: Caught SystemExit.", file=sys.stderr)
    except Exception as e:
        print(f"CRITICAL ERROR: Server loop failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
    finally:
        # Cleanup
        if server:
            server.close()
            mcp_loop.run_until_complete(server.wait_closed())
        
        if socket_path.exists():
            print(f"DEBUG: Unlinking socket file: {socket_path}")
            os.unlink(socket_path)
        
        mcp_loop.close()
        print("Server process exiting.")

if __name__ == "__main__":
    run_server()