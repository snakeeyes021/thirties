"""The MCP stdio server that agy starts for Agent mode: Gemini's tools, relayed to the running app.

agy runs on the host (outside the Flatpak), so this file is self-contained (the standard library only) and is
copied into Gemini's workspace, where the host can run it: ``python3 -I tools_server.py <socket>``. Each tool
call is forwarded, one JSON line each way, over a Unix socket the app serves for the length of a turn (bridge.py);
the app does the work with its own data layer (tools.py). With no app listening, a call says so plainly.

The protocol is MCP over stdio: newline-delimited JSON-RPC 2.0 (initialize, tools/list, tools/call, ping).
"""

import json
import socket
import sys

SERVER_NAME = "thirties"
PROTOCOL_VERSION = "2025-03-26"

_NOTE = {"type": "string", "description": "The note's id, or its exact title."}
_LINE = {"type": "integer", "description": "The line number, as read_lists shows it (1 is the first line)."}
_EXPECT = {"type": "string", "description": "Optional: what the line says now. The change is refused if it "
                                            "doesn't say that any more."}
_DAY = {"type": "string", "description": "Optional: YYYY-MM-DD. Default: the conversation's day."}

TOOLS = [
    {"name": "read_lists",
     "description": "The person's lists (Joplin notes) exactly as written: headings, nesting, ticked and open "
                    "boxes, each line numbered. Optionally only one note.",
     "inputSchema": {"type": "object", "properties": {"note": _NOTE}}},
    {"name": "add_line",
     "description": "Add one line to a note: after a given line (0 = at the top), else at the end. Plain words "
                    "become an open to-do ('- [ ] ...'); text that starts with markup is written as given.",
     "inputSchema": {"type": "object", "required": ["note", "text"], "properties": {
         "note": _NOTE, "text": {"type": "string"},
         "after_line": {"type": "integer", "description": "Add after this line; 0 for the top. Default: the end."},
         "nested": {"type": "boolean", "description": "Indent it one level under the line it follows."},
         "checkbox": {"type": "boolean", "description": "Plain words as a to-do (default) or a plain bullet."}}}},
    {"name": "tick_line", "description": "Tick a to-do line's box.",
     "inputSchema": {"type": "object", "required": ["note", "line"],
                     "properties": {"note": _NOTE, "line": _LINE, "expect": _EXPECT}}},
    {"name": "untick_line", "description": "Clear a to-do line's tick.",
     "inputSchema": {"type": "object", "required": ["note", "line"],
                     "properties": {"note": _NOTE, "line": _LINE, "expect": _EXPECT}}},
    {"name": "edit_line",
     "description": "Change a line's words. Its indent, bullet and box stay, unless the new text starts with "
                    "markup of its own.",
     "inputSchema": {"type": "object", "required": ["note", "line", "text"],
                     "properties": {"note": _NOTE, "line": _LINE, "text": {"type": "string"}, "expect": _EXPECT}}},
    {"name": "remove_line", "description": "Remove one line (only that line; lines nested under it stay).",
     "inputSchema": {"type": "object", "required": ["note", "line"],
                     "properties": {"note": _NOTE, "line": _LINE, "expect": _EXPECT}}},
    {"name": "read_calendar", "description": "The day's calendar events, and invitations not answered yet.",
     "inputSchema": {"type": "object", "properties": {"day": _DAY}}},
    {"name": "read_plan",
     "description": "The day plan the app shows: work, sleep, routines and planned items, with status.",
     "inputSchema": {"type": "object", "properties": {"day": _DAY}}},
    {"name": "set_plan",
     "description": "Set the day's planned items. Replaces the planned (not done) tasks and activities on that "
                    "day; calendar events, work, sleep, routines and anything already done stay. The app shows "
                    "the plan to the person in half-hour blocks.",
     "inputSchema": {"type": "object", "required": ["items"], "properties": {
         "day": _DAY,
         "items": {"type": "array", "items": {"type": "object", "required": ["title", "start", "length"],
                                              "properties": {
             "title": {"type": "string"},
             "start": {"type": "string", "description": "Clock time, e.g. '14:30' or '2:30 PM'."},
             "length": {"type": ["integer", "string"], "description": "Minutes, or e.g. '1h 30m'."},
             "note": {"type": "string"},
             "task_ref": {"type": "string", "description": "Optional id of the list item it's for."}}}}}}},
    {"name": "now", "description": "The current time, the day, sunrise and sunset, and the current half-hour block.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def relay(socket_path, name, arguments, timeout=60.0):
    """One call to the app: the reply's text, or a plain error."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket_path)
            s.sendall((json.dumps({"tool": name, "args": arguments or {}}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data.decode() or "{}")
        return str(reply.get("result", "")), bool(reply.get("error"))
    except (OSError, ValueError) as e:
        return json.dumps({"error": f"Thirties isn't answering ({e}); try again in a moment."}), True


def handle(message, socket_path):
    """The JSON-RPC reply to one message, or None for a notification."""
    method = message.get("method")
    mid = message.get("id")
    if mid is None:
        return None
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        result = {"protocolVersion": asked, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": SERVER_NAME, "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        text, failed = relay(socket_path, params.get("name", ""), params.get("arguments") or {})
        if not failed:
            try:
                failed = "error" in json.loads(text)
            except (ValueError, TypeError):
                pass
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.stderr.write("usage: tools_server.py <socket path>\n")
        return 2
    socket_path = argv[0]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            out = handle(message, socket_path) if isinstance(message, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
