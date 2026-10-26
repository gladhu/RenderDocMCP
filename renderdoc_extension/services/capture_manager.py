"""
Capture management service for RenderDoc.
"""

import renderdoc as rd


class CaptureManager:
    """Capture management service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_capture_status(self):
        """Check if a capture is loaded and get API info"""
        if not self.ctx.IsCaptureLoaded():
            return {"loaded": False}

        result = {"loaded": True, "api": None, "filename": None, "vendor": None, "known_gpu_quirks": []}

        try:
            result["filename"] = self.ctx.GetCaptureFilename()
        except Exception:
            pass

        # Get API type via replay
        def callback(controller):
            try:
                from ..utils import Helpers
                props = controller.GetAPIProperties()
                result["api"] = str(props.pipelineType)
                result["vendor"] = str(getattr(props, "vendor", ""))
                result["known_gpu_quirks"] = Helpers.gpu_quirks(
                    result["api"], result["vendor"]
                )
            except Exception:
                pass

        self._invoke(callback)
        return result

    def get_replay_context(self):
        """
        Get the current replay context information, including remote device status.

        Returns:
            dict with replay context details:
            - remote_connected: bool - whether a remote device is connected
            - remote_hostname: str - hostname of the remote device (empty if not connected)
            - remote_friendly_name: str - display name of the remote device
            - is_localhost: bool - whether the remote host is localhost
            - capture_loaded: bool - whether a capture is loaded
            - capture_local: bool - whether the loaded capture is local (only if capture loaded)
        """
        result = {
            "remote_connected": False,
            "remote_hostname": "",
            "remote_friendly_name": "",
            "is_localhost": True,
            "capture_loaded": self.ctx.IsCaptureLoaded(),
        }

        try:
            remote = self.ctx.Replay().CurrentRemote()
            result["remote_connected"] = remote.IsConnected()
            result["remote_hostname"] = remote.Hostname()
            result["remote_friendly_name"] = remote.Name()
            result["is_localhost"] = remote.IsLocalhost()
        except Exception:
            pass

        if result["capture_loaded"]:
            try:
                result["capture_local"] = self.ctx.IsCaptureLocal()
            except Exception:
                pass

        return result

    def list_captures(self, directory):
        """
        List all .rdc files in the specified directory.

        Args:
            directory: Directory path to search

        Returns:
            dict with 'captures' list containing file info
        """
        import os
        import datetime

        # Validate directory exists
        if not os.path.isdir(directory):
            raise ValueError("Directory not found: %s" % directory)

        captures = []

        try:
            for filename in os.listdir(directory):
                if filename.lower().endswith(".rdc"):
                    filepath = os.path.join(directory, filename)
                    if os.path.isfile(filepath):
                        stat = os.stat(filepath)
                        # Format timestamp as ISO 8601
                        mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
                        captures.append({
                            "filename": filename,
                            "path": filepath,
                            "size_bytes": stat.st_size,
                            "modified_time": mtime.isoformat(),
                        })
        except Exception as e:
            raise ValueError("Failed to list directory: %s" % str(e))

        # Sort by modified time (newest first)
        captures.sort(key=lambda x: x["modified_time"], reverse=True)

        return {
            "directory": directory,
            "count": len(captures),
            "captures": captures,
        }

    def open_capture(self, capture_path, use_remote=None):
        """
        Open a capture file in RenderDoc, with remote replay support.

        If a remote device is already connected in RenderDoc GUI, the capture
        will automatically be replayed on the remote device instead of locally.
        This is essential for captures from mobile devices (e.g. Android/Vulkan)
        that require specific GPU extensions not available on the local PC.

        Args:
            capture_path: Full path to the .rdc file (local path)
            use_remote: If True, force remote replay; if False, force local replay;
                        if None (default), auto-detect (use remote if connected, else local)

        Returns:
            dict with success status and capture info, including which device
            (local or remote) is used for replay.
        """
        import os
        import time

        # Validate file exists (locally)
        if not os.path.isfile(capture_path):
            raise ValueError("Capture file not found: %s" % capture_path)

        # Validate extension
        if not capture_path.lower().endswith(".rdc"):
            raise ValueError("Invalid file type. Expected .rdc file: %s" % capture_path)

        # Determine replay mode: remote or local
        remote = self.ctx.Replay().CurrentRemote()
        remote_connected = remote.IsConnected()

        if use_remote is True and not remote_connected:
            raise ValueError(
                "Remote replay requested but no remote device is connected. "
                "Please connect to a remote device in RenderDoc first."
            )

        replay_remote = False
        if use_remote is True:
            replay_remote = True
        elif use_remote is False:
            replay_remote = False
        else:
            # Auto-detect: use remote if connected
            replay_remote = remote_connected

        # Create ReplayOptions with defaults
        opts = rd.ReplayOptions()

        file_to_load = capture_path
        orig_filename = capture_path
        is_local = True

        if replay_remote:
            # Remote replay: copy capture to remote device first
            try:
                # CopyCaptureToRemote(localpath, window)
                # Pass None for window - SWIG typemap handles None -> NULL for QWidget*
                remote_path = self.ctx.Replay().CopyCaptureToRemote(capture_path, None)
                if not remote_path:
                    raise ValueError(
                        "Failed to copy capture to remote device. "
                        "The remote server may not be responding."
                    )
                file_to_load = remote_path   # Use the remote path for loading
                orig_filename = capture_path  # Keep original local path as reference
                is_local = False              # The file is now on the remote device
            except ValueError:
                raise
            except Exception as e:
                raise ValueError("Failed to copy capture to remote: %s" % str(e))

        # Open the capture
        # LoadCapture will automatically close any existing capture
        try:
            self.ctx.LoadCapture(
                file_to_load,    # captureFile (remote path if remote, local path if local)
                opts,            # ReplayOptions
                orig_filename,   # origFilename (always the local/original path)
                False,           # temporary (False = permanent load)
                is_local,        # local (False if replaying on remote device)
            )
        except Exception as e:
            raise ValueError("Failed to open capture: %s" % str(e))

        # Wait for capture to finish loading (LoadCapture is async)
        # Poll IsCaptureLoaded() with a timeout
        max_wait_seconds = 120
        poll_interval = 0.5
        elapsed = 0.0
        while not self.ctx.IsCaptureLoaded() and elapsed < max_wait_seconds:
            time.sleep(poll_interval)
            elapsed += poll_interval
            # Check if loading failed (not loading and not loaded = error)
            if not self.ctx.IsCaptureLoading() and not self.ctx.IsCaptureLoaded():
                raise ValueError("Capture loading failed (unknown error)")

        if not self.ctx.IsCaptureLoaded():
            raise ValueError(
                "Capture loading timed out after %d seconds. "
                "The file may be very large or the replay device may be slow." % max_wait_seconds
            )

        # Get capture info
        result = {
            "success": True,
            "capture_path": capture_path,
            "filename": os.path.basename(capture_path),
            "replay_remote": replay_remote,
        }

        if replay_remote:
            result["remote_hostname"] = remote.Hostname()
            result["remote_name"] = remote.Name()
            result["remote_file_path"] = file_to_load

        # Get API type if possible (may require replay thread)
        try:
            api_result = {"api": None}

            def callback(controller):
                try:
                    props = controller.GetAPIProperties()
                    api_result["api"] = str(props.pipelineType)
                except Exception:
                    pass

            self._invoke(callback)
            if api_result["api"]:
                result["api"] = api_result["api"]
        except Exception:
            pass

        return result
