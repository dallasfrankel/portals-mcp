"""
Portals Game Designer Agent — powered by OpenRouter.

Connects to the portals-mcp server via MCP and uses OpenRouter for the LLM brain.
Run: python agent.py [--model MODEL] [--vision-model MODEL] [--api-key KEY]
"""

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "qwen/qwen3-coder-next"
DEFAULT_VISION_MODEL = "meta-llama/llama-3.2-11b-vision-instruct"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
SYSTEM_PROMPT_PATH = Path(__file__).parent / "agent_prompt.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_system_prompt() -> str:
    """Load the system prompt from agent_prompt.md."""
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return "You are a helpful assistant for building 3D games in Portals."


def mcp_schema_to_openai_tool(tool) -> dict:
    """Convert an MCP tool definition to an OpenAI tool-calling dict."""
    input_schema = tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": input_schema,
        },
    }


def print_assistant(text: str) -> None:
    print(f"\n\033[36massistant:\033[0m {text}")


def print_tool_call(name: str, args: dict) -> None:
    print(f"\n\033[33m  → calling tool: {name}({json.dumps(args, indent=2)})\033[0m")


def print_tool_result(result: str) -> None:
    preview = result[:500] + ("..." if len(result) > 500 else "")
    print(f"\033[90m  ← {preview}\033[0m")


def try_parse_tool_call_from_text(text: str, valid_tool_names: list[str]) -> dict | None:
    """Fallback: detect a tool call the model wrote as plain JSON text.

    Handles formats like:
      {"name": "tool_name", "arguments": {...}}
      {"tool": "tool_name", "args": {...}}
      tool_name({"key": "value"})
    Returns {"name": str, "arguments": dict} or None.
    Always returns a dict if the text looks like a tool call attempt — even if the
    tool name is unknown — so that the error gets fed back to the model.
    """
    text = text.strip()

    # --- JSON object formats ---
    # Try to find a JSON object anywhere in the text
    for match in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text):
        raw = match.group()
        obj = None
        # First attempt: parse as-is
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            pass
        # Second attempt: fix bare backslashes (Windows paths written by the model).
        # Since json.loads already failed we know backslashes are not escaped —
        # safe to double all of them.
        if obj is None:
            try:
                obj = json.loads(raw.replace('\\', '\\\\'))
            except json.JSONDecodeError:
                pass
        if obj is None:
            continue

        name = obj.get("name") or obj.get("tool") or obj.get("function")
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if name:
            # Return even if unknown — the routing code will send the error back to the model
            return {"name": name, "arguments": args if isinstance(args, dict) else {}}

    # --- function-call style: tool_name({...}) ---
    for tool_name in valid_tool_names:
        pattern = re.escape(tool_name) + r'\s*\(\s*(\{.*?\})\s*\)'
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                args = json.loads(m.group(1))
                return {"name": tool_name, "arguments": args}
            except json.JSONDecodeError:
                pass

    return None


# ---------------------------------------------------------------------------
# Built-in local tools (file I/O, shell)
# ---------------------------------------------------------------------------

BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a local file and return it as text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Absolute path to the file to read",
                    }
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a local file. Creates the file if it doesn't exist, overwrites if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Absolute path to the file to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "The text content to write to the file",
                    },
                },
                "required": ["filePath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return its output. Use for running Python scripts, git, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_image",
            "description": (
                "Describe the contents of an image file using a vision model. "
                "Use this to review GLB thumbnails (PNG files) to identify what a 3D model looks like "
                "before adding it to catalog.json."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Absolute path to the image file (.png, .jpg, .jpeg)",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Optional question or focus for the description. "
                            "Default: describe what 3D object or model is shown."
                        ),
                    },
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the directory to list",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_room_items",
            "description": (
                "Search a room JSON file for items matching filters. "
                "Returns matching items with their IDs and fields. "
                "Use this instead of read_file when you need to find specific items in a room — "
                "much more efficient than loading the entire file into context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Absolute path to the room JSON file (returned by get_room_data)",
                    },
                    "type": {
                        "type": "string",
                        "description": "Filter by prefabName (item type), e.g. 'ResizableCube', 'GLB', 'Trigger', 'SpawnPoint'. Case-insensitive partial match.",
                    },
                    "color": {
                        "type": "string",
                        "description": "Filter by color string, e.g. 'red', '#ff0000'. Case-insensitive partial match.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Filter by name field. Case-insensitive partial match.",
                    },
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_room_item",
            "description": (
                "Delete an item from a room JSON file by its ID. "
                "Reads the file, removes the item (and its logic entry if present), and saves the file. "
                "Use find_room_items first to get the item ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Absolute path to the room JSON file",
                    },
                    "itemId": {
                        "type": "string",
                        "description": "The ID of the item to delete",
                    },
                },
                "required": ["filePath", "itemId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_room_item",
            "description": (
                "Update fields of a specific item in a room JSON file by its ID. "
                "Reads the file, merges the provided updates into the item, and saves the file. "
                "Use find_room_items first to get the item ID. "
                "For position changes, pass {\"position\": {\"x\": 1, \"y\": 2, \"z\": 3}}. "
                "For color changes, pass {\"color\": \"#ff0000\"}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Absolute path to the room JSON file",
                    },
                    "itemId": {
                        "type": "string",
                        "description": "The ID of the item to update",
                    },
                    "updates": {
                        "type": "object",
                        "description": "Dict of fields to update on the item (shallow merge)",
                    },
                },
                "required": ["filePath", "itemId", "updates"],
            },
        },
    },
]

BUILTIN_TOOL_NAMES = {t["function"]["name"] for t in BUILTIN_TOOLS}

PROJECT_DIR = Path(__file__).parent.resolve()

# Set at startup from CLI args — used by describe_image
_vision_model: str = DEFAULT_VISION_MODEL
_openai_client: OpenAI | None = None
# Last room data file path returned by get_room_data — used to fix placeholder paths
_last_room_file_path: str = ""


def _resolve_file_path(args: dict) -> str:
    """Return the filePath from args. If it looks like a placeholder or doesn't exist,
    fall back to the last known room data file path."""
    path = args.get("filePath", "")
    if path and not path.startswith("<") and Path(path).exists():
        return path
    # Path is missing, a placeholder, or doesn't exist — try the cached path
    if _last_room_file_path and Path(_last_room_file_path).exists():
        return _last_room_file_path
    return path  # Return as-is so the error message is meaningful


def execute_builtin_tool(name: str, args: dict) -> str:
    """Execute a built-in local tool and return the result as text."""
    # Auto-fix placeholder file paths for room-data tools
    if name in ("read_file", "find_room_items", "delete_room_item", "update_room_item"):
        original = args.get("filePath", "")
        resolved = _resolve_file_path(args)
        if resolved != original:
            args = {**args, "filePath": resolved}
    try:
        if name == "read_file":
            fp = Path(args["filePath"])
            if not fp.exists():
                return f"Error: file not found: {fp}"
            content = fp.read_text(encoding="utf-8", errors="replace")
            # For large room JSON files, return a compact summary to avoid context overload.
            # Steer the model toward find_room_items / delete_room_item / update_room_item.
            if len(content) > 20_000 and content.lstrip().startswith("{"):
                try:
                    data = json.loads(content)
                    if "roomItems" in data:
                        items = data["roomItems"]
                        lines = [
                            f"Room JSON file: {fp}",
                            f"Total items: {len(items)}  |  Has logic: {'yes' if data.get('logic') else 'no'}  |  Has quests: {'yes' if data.get('quests') else 'no'}",
                            "",
                            "Use find_room_items to search for specific items, or delete_room_item / update_room_item to modify them.",
                            "Only call read_file on this path if you need to add NEW items (not modify existing ones).",
                            "",
                            "Item index (id | prefabName | color | pos):",
                        ]
                        for item_id, item in list(items.items())[:150]:
                            itype = item.get("prefabName") or item.get("type", "?")
                            color = item.get("color") or item.get("name") or ""
                            pos = item.get("pos") or item.get("position") or {}
                            x, y, z = pos.get("x", 0), pos.get("y", 0), pos.get("z", 0)
                            lines.append(f"  {item_id} | {itype} | {color} | ({x:.1f},{y:.1f},{z:.1f})")
                        if len(items) > 150:
                            lines.append(f"  ... and {len(items) - 150} more items (use find_room_items to search)")
                        return "\n".join(lines)
                except (json.JSONDecodeError, Exception):
                    pass
            # Truncate very large non-room files to avoid blowing context
            if len(content) > 50_000:
                return content[:50_000] + f"\n\n... (truncated, {len(content)} chars total)"
            return content

        elif name == "write_file":
            fp = Path(args["filePath"])
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(args["content"], encoding="utf-8")
            return f"Written {len(args['content'])} chars to {fp}"

        elif name == "run_command":
            result = subprocess.run(
                args["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(PROJECT_DIR),
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr
            if result.returncode != 0:
                output += f"\n(exit code {result.returncode})"
            return output or "(no output)"

        elif name == "describe_image":
            fp = Path(args["filePath"])
            if not fp.exists():
                return f"Error: file not found: {fp}"
            suffix = fp.suffix.lower()
            if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                return f"Error: unsupported image format: {suffix}"
            if _openai_client is None:
                return "Error: OpenAI client not initialised"
            prompt = args.get("prompt") or (
                "This is a 4-view thumbnail of a 3D model. "
                "Describe what the object is, its shape, colour, and any notable features. "
                "Be specific — this description will be used to name and categorise the asset."
            )
            img_b64 = base64.b64encode(fp.read_bytes()).decode()
            try:
                resp = _openai_client.chat.completions.create(
                    model=_vision_model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                        ],
                    }],
                )
                return resp.choices[0].message.content or "(no description returned)"
            except Exception as e:
                return f"Error calling vision model '{_vision_model}': {e}"

        elif name == "list_directory":
            dp = Path(args["path"])
            if not dp.exists():
                return f"Error: directory not found: {dp}"
            entries = sorted(dp.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            lines = []
            for e in entries[:200]:  # cap listing size
                prefix = "[dir] " if e.is_dir() else "      "
                lines.append(f"{prefix}{e.name}")
            return "\n".join(lines) or "(empty directory)"

        elif name == "find_room_items":
            fp = Path(args["filePath"])
            if not fp.exists():
                return f"Error: file not found: {fp}"
            try:
                data = json.loads(fp.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Error: could not parse JSON: {e}"
            items = data.get("roomItems", {})
            filter_type = args.get("type", "").lower()
            filter_color = args.get("color", "").lower()
            filter_name = args.get("name", "").lower()
            results = []
            for item_id, item in items.items():
                # Support both prefabName (actual field) and type (alias)
                prefab = item.get("prefabName") or item.get("type", "")
                if filter_type and filter_type not in prefab.lower():
                    continue
                if filter_color and filter_color not in str(item.get("color", "")).lower():
                    continue
                if filter_name and filter_name not in str(item.get("name", "")).lower():
                    continue
                results.append({"id": item_id, **item})
            if not results:
                active_filters = {k: v for k, v in {"type": filter_type, "color": filter_color, "name": filter_name}.items() if v}
                return f"No items found matching {active_filters}. Total items in room: {len(items)}."
            output = json.dumps(results, indent=2)
            if len(output) > 20_000:
                output = output[:20_000] + f"\n... (truncated — {len(results)} matches, showing partial)"
            return output

        elif name == "delete_room_item":
            fp = Path(args["filePath"])
            if not fp.exists():
                return f"Error: file not found: {fp}"
            try:
                data = json.loads(fp.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Error: could not parse JSON: {e}"
            item_id = args["itemId"]
            items = data.get("roomItems", {})
            if item_id not in items:
                sample = list(items.keys())[:5]
                return f"Error: item '{item_id}' not found. Sample IDs: {sample}"
            item_type = items[item_id].get("type", "?")
            del data["roomItems"][item_id]
            # Also remove from logic if present
            if item_id in data.get("logic", {}):
                del data["logic"][item_id]
            fp.write_text(json.dumps(data), encoding="utf-8")
            return f"Deleted {item_type} item '{item_id}'. Room now has {len(data['roomItems'])} items. File saved to {fp}."

        elif name == "update_room_item":
            fp = Path(args["filePath"])
            if not fp.exists():
                return f"Error: file not found: {fp}"
            try:
                data = json.loads(fp.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Error: could not parse JSON: {e}"
            item_id = args["itemId"]
            items = data.get("roomItems", {})
            if item_id not in items:
                sample = list(items.keys())[:5]
                return f"Error: item '{item_id}' not found. Sample IDs: {sample}"
            updates = args.get("updates", {})
            if not updates:
                return "Error: 'updates' dict is empty — nothing to change."
            # Deep merge for nested dicts (e.g. position), shallow merge otherwise
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(items[item_id].get(key), dict):
                    items[item_id][key].update(value)
                else:
                    items[item_id][key] = value
            fp.write_text(json.dumps(data), encoding="utf-8")
            return f"Updated item '{item_id}': set {list(updates.keys())}. File saved to {fp}."

        else:
            return f"Error: unknown built-in tool: {name}"
    except Exception as e:
        return f"Error in {name}: {e}"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def agent_loop(model: str, vision_model: str, api_key: str) -> None:
    global _vision_model, _openai_client, _last_room_file_path

    system_prompt = load_system_prompt()
    print(f"Loaded system prompt ({len(system_prompt)} chars)")

    _openai_client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    _vision_model = vision_model

    # --- Connect to MCP server via stdio ---
    server_params = StdioServerParameters(
        command="npx",
        args=["portals-mcp"],
        env={**os.environ},
    )

    print("Connecting to portals-mcp server...")

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Discover MCP tools and merge with built-in tools
            tools_result = await session.list_tools()
            mcp_tools = tools_result.tools
            openai_tools = [mcp_schema_to_openai_tool(t) for t in mcp_tools] + BUILTIN_TOOLS
            mcp_tool_names = {t.name for t in mcp_tools}
            tool_names = [t.name for t in mcp_tools] + list(BUILTIN_TOOL_NAMES)

            print(f"Connected! {len(openai_tools)} tools available: {', '.join(tool_names)}")
            print(f"Model: {model}  |  Vision: {vision_model}")
            print("Type your message (or 'quit' to exit).\n")

            # Conversation history
            messages: list[dict] = [
                {"role": "system", "content": system_prompt},
            ]

            client = _openai_client

            while True:
                # --- User input ---
                try:
                    user_input = input("\033[32myou:\033[0m ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nBye!")
                    break

                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit"):
                    print("Bye!")
                    break

                messages.append({"role": "user", "content": user_input})

                # --- LLM turn (may loop for tool calls) ---
                while True:
                    print("\033[90m  (thinking...)\033[0m", end="\r", flush=True)
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tools=openai_tools,
                    )
                    print("                 ", end="\r", flush=True)  # clear thinking indicator

                    msg = response.choices[0].message

                    # Collect tool calls — native or parsed from text
                    calls_to_run: list[dict] = []

                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            calls_to_run.append({
                                "name": tc.function.name,
                                "arguments": json.loads(tc.function.arguments or "{}"),
                                "id": tc.id,
                            })
                    elif msg.content:
                        # Fallback: model may have written the call as text
                        parsed = try_parse_tool_call_from_text(msg.content, tool_names)
                        if parsed:
                            calls_to_run.append(parsed)

                    if calls_to_run:
                        # Append the assistant message (preserve tool_calls for OpenAI history)
                        if msg.tool_calls:
                            messages.append({
                                "role": "assistant",
                                "content": msg.content,
                                "tool_calls": [
                                    {
                                        "id": tc.id,
                                        "type": "function",
                                        "function": {
                                            "name": tc.function.name,
                                            "arguments": tc.function.arguments,
                                        },
                                    }
                                    for tc in msg.tool_calls
                                ],
                            })
                        else:
                            messages.append({"role": "assistant", "content": msg.content or ""})

                        for call in calls_to_run:
                            fn_name = call["name"]
                            fn_args = call["arguments"]
                            fn_id = call.get("id", "")

                            print_tool_call(fn_name, fn_args)

                            # Route to built-in or MCP
                            if fn_name in BUILTIN_TOOL_NAMES:
                                result_text = execute_builtin_tool(fn_name, fn_args)
                            elif fn_name in mcp_tool_names:
                                try:
                                    result = await session.call_tool(fn_name, fn_args)
                                    result_text = ""
                                    for block in result.content:
                                        if hasattr(block, "text"):
                                            result_text += block.text
                                        else:
                                            result_text += str(block)
                                except Exception as e:
                                    result_text = f"Error calling {fn_name}: {e}"
                            else:
                                result_text = (
                                    f"Unknown tool: '{fn_name}'. "
                                    f"That is not a callable tool — it may be a Python script. "
                                    f"To run Python scripts use the 'run_command' tool, e.g.: "
                                    f"run_command({{\"command\": \"python tools/{fn_name} --help\"}}). "
                                    f"Available tools: {', '.join(tool_names)}"
                                )

                            # Special post-processing for get_room_data:
                            # extract and cache the file path, then rewrite the result
                            # so the model cannot miss it.
                            if fn_name == "get_room_data":
                                path_match = re.search(
                                    r'(?:[A-Za-z]:\\[^\n]+\.json|/[^\n]+\.json)',
                                    result_text,
                                )
                                if path_match:
                                    _last_room_file_path = path_match.group(0).strip()
                                    result_text = (
                                        f"Room data downloaded successfully.\n"
                                        f"FILE PATH: {_last_room_file_path}\n\n"
                                        f"Use this exact filePath value in the next tool call:\n"
                                        f"  find_room_items(filePath=\"{_last_room_file_path}\", ...)\n"
                                        f"  delete_room_item(filePath=\"{_last_room_file_path}\", itemId=\"...\")\n"
                                        f"  read_file(filePath=\"{_last_room_file_path}\")"
                                    )

                            print_tool_result(result_text)

                            # Feed tool result back to model — OpenAI requires tool_call_id
                            tool_result: dict = {"role": "tool", "content": result_text}
                            if fn_id:
                                tool_result["tool_call_id"] = fn_id
                            messages.append(tool_result)

                        # Loop back so the model can process tool results
                        continue

                    # No tool calls — just text output
                    assistant_text = msg.content or ""
                    messages.append({"role": "assistant", "content": assistant_text})
                    print_assistant(assistant_text)
                    break  # Done with this turn


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Portals Game Designer Agent (OpenRouter)")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"OpenRouter model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--vision-model", default=DEFAULT_VISION_MODEL,
        help=f"OpenRouter vision model for image description (default: {DEFAULT_VISION_MODEL})",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("OPENROUTER_API_KEY", ""),
        help="OpenRouter API key (or set OPENROUTER_API_KEY env var)",
    )
    args = parser.parse_args()

    if not args.api_key:
        print("Error: OpenRouter API key required. Set OPENROUTER_API_KEY env var or pass --api-key.")
        sys.exit(1)

    try:
        asyncio.run(agent_loop(args.model, args.vision_model, args.api_key))
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
