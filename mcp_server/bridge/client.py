"""
RenderDoc Bridge Client
Communicates with the RenderDoc extension via file-based IPC.
"""

import json
import os
import tempfile
import time
import uuid
from typing import Any


# IPC directory (must match renderdoc_extension/socket_server.py)
# RENDERDOC_MCP_IPC_DIR lets a second RenderDoc instance (e.g. an elevated one) use its own channel
IPC_DIR = os.environ.get("RENDERDOC_MCP_IPC_DIR") or os.path.join(tempfile.gettempdir(), "renderdoc_mcp")


class RenderDocBridgeError(Exception):
    """Error communicating with RenderDoc bridge"""

    pass


class RenderDocBridge:
    """Client for communicating with RenderDoc extension via file-based IPC.

    Each call writes request_<id>.json and waits for response_<id>.json (both renamed into place
    once complete), so several MCP server processes can share one RenderDoc instance.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 19876):
        # host/port are kept for API compatibility but not used
        self.host = host
        self.port = port
        self.timeout = 120.0  # seconds (remote phone replay can be slow)

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        """Call a method on the RenderDoc extension (timeout in seconds, default self.timeout)"""
        timeout = self.timeout if timeout is None else timeout
        if not os.path.isdir(IPC_DIR):
            raise RenderDocBridgeError(
                f"No RenderDoc MCP Bridge found (IPC directory {IPC_DIR} does not exist). "
                "Make sure RenderDoc is running with the MCP Bridge extension loaded."
            )

        request_id = uuid.uuid4().hex
        request = {"id": request_id, "method": method, "params": params or {}}
        request_file = os.path.join(IPC_DIR, f"request_{request_id}.json")
        response_file = os.path.join(IPC_DIR, f"response_{request_id}.json")

        try:
            # Write under a name the extension ignores, then rename: it never reads a partial request
            with open(request_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump(request, f)
            os.replace(request_file + ".tmp", request_file)

            start_time = time.time()
            while not os.path.exists(response_file):
                if time.time() - start_time > timeout:
                    # Withdraw the request if it was not picked up yet, so it does not run later
                    try:
                        os.remove(request_file)
                    except OSError:
                        pass
                    raise RenderDocBridgeError("Request timed out")
                time.sleep(0.05)

            response = _read_and_remove(response_file)
        except RenderDocBridgeError:
            raise
        except Exception as e:
            raise RenderDocBridgeError(f"Communication error: {e}")

        if "error" in response:
            error = response["error"]
            raise RenderDocBridgeError(f"[{error['code']}] {error['message']}")
        return response.get("result")


def _read_and_remove(path: str, attempts: int = 50) -> dict:
    """Read a response file, then delete it.

    A file that was just renamed into place can be held briefly by another process (on-access virus scanners
    open large new files), which shows up as PermissionError; retry for a few seconds before giving up.
    """
    for attempt in range(attempts):
        try:
            with open(path, "r", encoding="utf-8") as f:
                response = json.load(f)
            break
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
    for attempt in range(attempts):
        try:
            os.remove(path)
            break
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
    return response
