"""
File-based IPC Server for RenderDoc MCP Bridge
Uses file polling since RenderDoc's Python doesn't have socket/QtNetwork modules.
"""

import json
import os
import threading
import time
import traceback
import tempfile

# PySide2 ships with official RenderDoc builds but not with every custom build
# (e.g. ones embedding a newer Python). Without it, poll from a worker thread and
# marshal handling onto the UI thread through MiniQtHelper.InvokeOntoUIThread.
try:
    from PySide2.QtCore import QObject, QTimer

    _HAS_PYSIDE = True
except ImportError:
    QObject = object
    QTimer = None
    _HAS_PYSIDE = False


# IPC directory
# RENDERDOC_MCP_IPC_DIR lets a second RenderDoc instance (e.g. an elevated one) use its own channel
IPC_DIR = os.environ.get("RENDERDOC_MCP_IPC_DIR") or os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
# Each request is request_<id>.json, answered by response_<id>.json; both are written to a .tmp file and
# renamed, so several MCP server processes can share the directory. request.json / lock / response.json
# is the older single-slot protocol, still served for clients that predate it.
REQUEST_PREFIX, REQUEST_SUFFIX = "request_", ".json"
REQUEST_FILE = os.path.join(IPC_DIR, "request.json")
RESPONSE_FILE = os.path.join(IPC_DIR, "response.json")
LOCK_FILE = os.path.join(IPC_DIR, "lock")
# faulthandler output of this RenderDoc instance (one per IPC directory, so two instances do not truncate each other's)
FAULT_LOG = os.path.join(IPC_DIR, "fault.log")
# A request carries "deadline" (epoch seconds: when its client stops waiting). One that is picked up later than
# that is dropped unanswered: its client is gone (timed out, or killed before it could withdraw the file) and
# running it would only delay the requests of clients that are still waiting. Seconds of clock slack:
DEADLINE_GRACE = 2.0


def _retry_io(fn, *args):
    """Run a file operation, retrying for a few seconds while another process (an on-access virus
    scanner reading a new file) briefly holds the file and Windows reports PermissionError."""
    for attempt in range(50):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.1)


class MCPBridgeServer(QObject):
    """File-based IPC server for MCP bridge communication"""

    def __init__(self, host, port, handler, parent=None, ui_invoke=None):
        if _HAS_PYSIDE:
            super(MCPBridgeServer, self).__init__(parent)
        self.handler = handler
        self._timer = None
        self._thread = None
        self._busy = False
        self._running = False
        # Callable that runs a function on the UI thread (needed without PySide2)
        self._ui_invoke = ui_invoke
        # The function handed to it, created once: qrenderdoc's C++ callback glue changes the
        # callable's reference count without holding the GIL (std::function copy), so a bound
        # method created per request and dropped by this thread right away could lose an update.
        self._ui_entry = self._handle_on_ui_thread

        # Create IPC directory
        if not os.path.exists(IPC_DIR):
            os.makedirs(IPC_DIR)

    def start(self):
        """Start the server with polling"""
        self._running = True

        # Clean up old files
        self._cleanup_files()

        if _HAS_PYSIDE:
            # Start polling timer (check every 100ms)
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._poll_request)
            self._timer.start(100)
        elif self._ui_invoke is not None:
            self._thread = threading.Thread(target=self._poll_loop, name="MCPBridgePoll")
            self._thread.daemon = True
            self._thread.start()
        else:
            self._running = False
            print("[MCP Bridge] Neither PySide2 nor a UI invoke function is available")
            return False

        print("[MCP Bridge] File-based IPC server started (%s)" % ("QTimer" if _HAS_PYSIDE else "thread"))
        print("[MCP Bridge] IPC directory: %s" % IPC_DIR)
        return True

    def stop(self):
        """Stop the server"""
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None
        self._thread = None
        self._cleanup_files()
        print("[MCP Bridge] Server stopped")

    def _poll_loop(self):
        """Worker-thread poller used when PySide2 is unavailable"""
        while self._running:
            if not self._busy and self._pending_requests():
                self._busy = True
                try:
                    self._ui_invoke(self._ui_entry)
                except Exception:
                    self._busy = False
                    traceback.print_exc()
            time.sleep(0.1)

    def _handle_on_ui_thread(self):
        try:
            self._poll_request()
        finally:
            self._busy = False

    def is_running(self):
        """Check if server is running"""
        return self._running

    def _cleanup_files(self):
        """Remove IPC files"""
        names = []
        try:
            names = [n for n in os.listdir(IPC_DIR) if n.startswith(("request_", "response_"))]
        except Exception:
            pass
        for f in [REQUEST_FILE, RESPONSE_FILE, LOCK_FILE] + [os.path.join(IPC_DIR, n) for n in names]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass

    def _pending_requests(self):
        """[(request path, response path)] waiting to be served, oldest first"""
        pending = []
        try:
            names = os.listdir(IPC_DIR)
        except Exception:
            return pending
        for name in names:
            if name.startswith(REQUEST_PREFIX) and name.endswith(REQUEST_SUFFIX):
                path = os.path.join(IPC_DIR, name)
                rid = name[len(REQUEST_PREFIX):-len(REQUEST_SUFFIX)]
                try:
                    pending.append((os.path.getmtime(path), path, os.path.join(IPC_DIR, "response_%s.json" % rid)))
                except OSError:
                    pass
        pending.sort()
        # older clients: one request.json slot, guarded by a lock file while it is written
        if os.path.exists(REQUEST_FILE) and not os.path.exists(LOCK_FILE):
            pending.append((0, REQUEST_FILE, RESPONSE_FILE))
        return [(req, resp) for _, req, resp in pending]

    def _poll_request(self):
        """Serve every pending request"""
        if not self._running:
            return
        for request_file, response_file in self._pending_requests():
            self._serve(request_file, response_file)

    def _serve(self, request_file, response_file):
        try:
            # Read request (a client that timed out may have withdrawn it since it was listed)
            try:
                with open(request_file, "r", encoding="utf-8") as f:
                    request = json.load(f)
            except (IOError, OSError):
                return

            # Remove request file
            _retry_io(os.remove, request_file)

            deadline = request.get("deadline")
            if isinstance(deadline, (int, float)) and time.time() > deadline + DEADLINE_GRACE:
                print("[MCP Bridge] Dropped %s: its client stopped waiting %.0f s ago" % (
                    request.get("method"), time.time() - deadline))
                return

            # Process request
            try:
                response = self.handler.handle(request)
            except Exception as e:
                traceback.print_exc()
                response = {
                    "id": request.get("id"),
                    "error": {"code": -32603, "message": str(e)}
                }

            # Write response: to a temp file first, then rename, so the client never sees a
            # half-written document (large results take longer than its polling delay)
            tmp_file = response_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(response, f)
            _retry_io(os.replace, tmp_file, response_file)

        except Exception as e:
            print("[MCP Bridge] Error processing request: %s" % str(e))
            traceback.print_exc()
