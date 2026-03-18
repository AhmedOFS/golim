"""LLM interaction module for cterm using Ollama's native tool calling"""
import subprocess
import sys
import threading
import time
import json
import asyncio
import os
import socket
from pathlib import Path
import re
class FastMCPClient:
    """Real UDS client for FastMCP communication"""
    def __init__(self, socket_path):
        self.socket_path = socket_path

    def _send_request(self, method, params=None):
        """Send JSON-RPC style request over UDS - creates new connection each time"""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        
        try:
            sock.connect(str(self.socket_path))
            
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params or {}
            }
            message = json.dumps(request) + "\n"
            sock.sendall(message.encode())
            
            # Read response
            response_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                if b"\n" in response_data:
                    break
            
            if not response_data:
                raise ConnectionError("No response from server")
            
            return json.loads(response_data.decode().strip())
            
        finally:
            sock.close()

    async def list_tools(self):
        """Get list of available tools from MCP server"""
        try:
            response = self._send_request("tools/list")
            
            # Try different response formats
            tools_data = None
            if "result" in response:
                if isinstance(response["result"], dict) and "tools" in response["result"]:
                    tools_data = response["result"]["tools"]
                elif isinstance(response["result"], list):
                    tools_data = response["result"]
            elif "tools" in response:
                tools_data = response["tools"]
            
            if not tools_data:
                raise ValueError("No tools in response")
            
            # Convert to simple objects with name and description
            tools = []
            for tool_data in tools_data:
                tool = type('Tool', (object,), {
                    'name': tool_data.get('name'),
                    'description': tool_data.get('description', ''),
                    'inputSchema': tool_data.get('inputSchema', {}),
                    'parameters': self._convert_schema_to_parameters(tool_data)
                })
                tools.append(tool)
            
            return tools
        except Exception as e:
            # Fallback to hardcoded list
            return self._get_fallback_tools()
    
    def _convert_schema_to_parameters(self, tool_data):
        """Convert tool schema to Ollama-compatible parameters format"""
        # Simple conversion - expand this based on actual schema
        name = tool_data.get('name')
        
        # Define parameters for known tools
        params_map = {
            'list_files': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Directory path to list'}
                },
                'required': ['path']
            },
            'read_file': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'File path to read'}
                },
                'required': ['path']
            },
            'run_shell': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string', 'description': 'Shell command to execute'}
                },
                'required': ['command']
            },
            'calculate': {
                'type': 'object',
                'properties': {
                    'a': {'type': 'number', 'description': 'First number'},
                    'b': {'type': 'number', 'description': 'Second number'},
                    'op': {'type': 'string', 'description': 'Operation: add, sub, mul, div', 'enum': ['add', 'sub', 'mul', 'div']}
                },
                'required': ['a', 'b', 'op']
            },
            'fetch_json': {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'description': 'URL to fetch JSON from'}
                },
                'required': ['url']
            }
        }
        
        return params_map.get(name, tool_data.get('inputSchema', {}))
    
    def _get_fallback_tools(self):
        """Fallback tool definitions"""
        tools = []
        for name, params in {
            'list_files': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
            'read_file': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
            'run_shell': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']},
            'calculate': {'type': 'object', 'properties': {'a': {'type': 'number'}, 'b': {'type': 'number'}, 'op': {'type': 'string'}}, 'required': ['a', 'b', 'op']},
            'fetch_json': {'type': 'object', 'properties': {'url': {'type': 'string'}}, 'required': ['url']},
        }.items():
            tool = type('Tool', (object,), {
                'name': name,
                'description': f'Tool: {name}',
                'parameters': params
            })
            tools.append(tool)
        return tools

    async def call_tool(self, tool_name, args):
        """Execute a tool via UDS"""
        try:
            if not Path(self.socket_path).exists():
                raise ConnectionRefusedError(f"UDS socket not found at {self.socket_path}")
            
            response = self._send_request("tools/call", {
                "name": tool_name,
                "arguments": args
            })
            
            # Extract result from response
            if "result" in response:
                return response["result"]
            elif "error" in response:
                return {"ok": False, "error": response["error"].get("message", "Unknown error")}
            else:
                return {"ok": True, "result": response}
                
        except Exception as e:
            return {"ok": False, "error": f"Tool execution error: {str(e)}"}
    
    def close(self):
        """Close is now a no-op since we don't maintain persistent connections"""
        pass

def get_socket_path() -> Path:
    """Returns the UDS path based on the current user."""
    return Path(f"/tmp/cterm_mcp_{os.getlogin()}.sock")

class Spinner:
    def __init__(self, message: str = "Thinking"):
        self.message = message
        self.running = False
        self.thread = None
        self.frames = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
        self.frame_idx = 0

    def _spin(self):
        while self.running:
            frame = self.frames[self.frame_idx % len(self.frames)]
            sys.stderr.write(f"\r{frame} {self.message}...")
            sys.stderr.flush()
            self.frame_idx += 1
            time.sleep(0.1)
        sys.stderr.write("\r" + " "*(len(self.message)+10) + "\r")
        sys.stderr.flush()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)

def chat_with_model(model: str, message: str, binary: str = "ollama") -> str:
    """Call the LLM via Ollama CLI"""
    spinner = Spinner("Thinking")
    try:
        spinner.start()
        result = subprocess.run([binary, "run", model, message],
                                capture_output=True, text=True, check=True)
        spinner.stop()
        response = result.stdout.strip() or result.stderr.strip()
        if not response:
            raise RuntimeError("Empty response from model")
        return response
    except Exception:
        spinner.stop()
        raise

def chat_with_model_api(model: str, messages: list, tools: list = None, binary: str = "ollama") -> dict:
    """Call Ollama API with native tool support"""
    import requests
    
    payload = {
        "model": model,
        "messages": messages,
        "stream": False
    }
    
    if tools:
        payload["tools"] = tools
    
    response = requests.post(
        "http://localhost:11434/api/chat",
        json=payload,
        timeout=60
    )
    response.raise_for_status()
    return response.json()

class ToolAgent:
    """Agent that uses Ollama's native tool calling API"""
    def __init__(self, model: str, binary: str = "ollama"):
        self.model = model
        self.binary = binary
        self.mcp_client = None
        self.tools = []
        self.use_native_tools = self._check_native_tool_support()

    def _check_native_tool_support(self):
        """Check if Ollama and model support native tools"""
        try:
            import requests
            response = requests.get("http://localhost:11434/api/tags", timeout=5)
            if response.status_code == 200:
                return True
        except:
            pass
        return False

    def __enter__(self):
        socket_path = get_socket_path()
        
        try:
            self.mcp_client = FastMCPClient(socket_path)
            self.tools = asyncio.run(self.mcp_client.list_tools())
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                subprocess.run(
                    ["systemctl", "--user", "start", "cterm-mcp.service"], 
                    check=True, 
                    capture_output=True,
                    text=True
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError("Failed to activate tool server.")

            for _ in range(10):
                time.sleep(0.2)
                try:
                    self.mcp_client = FastMCPClient(socket_path)
                    self.tools = asyncio.run(self.mcp_client.list_tools())
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    continue
            else:
                raise TimeoutError("Tool server started but socket never became available.")
            
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.mcp_client:
            self.mcp_client.close()
        
    def run(self, user_message: str) -> str:
        """Run agent with native Ollama tool support if available"""
        if self.use_native_tools:
            return self._run_with_native_tools(user_message)
        else:
            return self._run_with_fallback(user_message)
    
    def _run_with_native_tools(self, user_message: str) -> str:
        """Use Ollama's native tool calling API"""
        # Convert tools to Ollama format
        ollama_tools = []
        # for t in self.tools:
        #     print(vars(t))        # dictionary of fields
        for t in self.tools:
            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": getattr(t, 'description', ''),
                    "parameters": getattr(t, 'parameters', {})
                }
            })
       # print(f"ollama tools are {ollama_tools}")
        messages = [
             {"role": "system", "content": "use the tools available to you to perform tasks asked of you. use the run shell tool to execute commands, and use snap for desktop app installations when possible"},
             {"role": "user", "content": user_message}
                ]
        try:
            response = chat_with_model_api(self.model, messages, ollama_tools, self.binary)
            #print(json.dumps(response, indent=2)) 
            message = response.get("message", {})
         
            # Check if model wants to call a tool
            if "tool_calls" in message and message["tool_calls"]:
                tool_call = message["tool_calls"][0]
                function = tool_call.get("function", {})
                tool_name = function.get("name")
                args = function.get("arguments", {})
                
                # Show tool usage
                print(f"🔧 Using tool: {tool_name} with tool args {args}", file=sys.stderr)
                
                # Execute the tool
                tool_result = asyncio.run(self.mcp_client.call_tool(tool_name, args))
                print(tool_result)
                # Send result back to model
                messages.append(message)
                messages.append({
                    "role": "tool",
                    "content": json.dumps(tool_result)
                })
                
                # Get final answer
                final_response = chat_with_model_api(self.model, messages, ollama_tools, self.binary)
              
                return final_response.get("message", {}).get("content", "No response")
    
            # No tool call, return direct answer
            print("no tool calls")
            
            return message.get("content", "No response")
            
        except Exception as e:
            print(f"exception is {e}")
            return self._run_with_fallback(user_message)
        

    def _run_with_fallback(self, user_message: str) -> str:
            """Fallback to manual XML tag method"""
            tools_list = []
            for t in self.tools:
                tools_list.append(f"- {t.name}: {getattr(t, 'description', '')}")
            tools_text = "\n".join(tools_list)
            print("system running on fallback")
            system_prompt = f"""You have access to these tools:
    {tools_text}

    User asks: {user_message}

    CRITICAL: If you need a tool, output ONLY:
    <tool_call>{{"tool":"tool_name","args":{{"param":"value"}}}}</tool_call>

    Example: <tool_call>{{"tool":"list_files","args":{{"path":"."}}}}</tool_call>

    Respond now:"""

            response_text = chat_with_model(self.model, system_prompt, self.binary)
            
            # Parse and execute
            if "<tool_call>" not in response_text:
                return response_text
            
            try:
                if "</tool_call>" in response_text:
                    tool_json = response_text.split('<tool_call>')[1].split('</tool_call>')[0].strip()
                else:
                    tool_json = response_text.split('<tool_call>')[1].strip()
                    # Extract complete JSON
                    brace_count = 0
                    for i, char in enumerate(tool_json):
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                tool_json = tool_json[:i+1]
                                break
                
                call = json.loads(tool_json)
                tool_name = call.get("tool")
                args = call.get("args", {})
                
                # Show tool usage
                print(f"🔧 Using tool: {tool_name}", file=sys.stderr)
                
                tool_result = asyncio.run(self.mcp_client.call_tool(tool_name, args))
                
                # Get final answer
                followup_prompt = f"""Tool result: {json.dumps(tool_result)}

    Based on this, answer the user's question: {user_message}"""
                
                return chat_with_model(self.model, followup_prompt, self.binary)
                
            except Exception as e:
                return f"Error: {e}\n\nRaw response: {response_text}"

def chat_with_tools(model: str, message: str, binary: str = "ollama") -> str:
        """Convenience function to chat with tool support"""
        agent = ToolAgent(model, binary)
        with agent:
            return agent.run(message)