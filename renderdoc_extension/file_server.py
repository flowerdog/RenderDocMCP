"""
HTTP File Server launcher for RenderDoc MCP Export.

Instead of running http.server inside RenderDoc's limited embedded Python,
we launch a separate process using the system Python. This gives us:
- A full-featured Python environment (not RenderDoc's stripped 3.6)
- A dedicated console window for visibility
- Process-level isolation from RenderDoc's Qt event loop
- Stability independent of RenderDoc's internal state

Compatible with Python 3.6 (this file runs inside RenderDoc).
"""

import os
import subprocess
import sys


def _find_system_python():
    """Find a usable system Python executable (not RenderDoc's embedded one).

    Search order:
    1. RENDERDOC_MCP_PYTHON env var (explicit override)
    2. 'py -3' (Windows Python Launcher - most reliable on Windows)
    3. 'python' from PATH (check it's not RenderDoc's embedded one)
    """
    explicit = os.environ.get("RENDERDOC_MCP_PYTHON")
    if explicit and os.path.isfile(explicit):
        return explicit

    # Try Windows Python Launcher
    try:
        proc = subprocess.Popen(
            ["py", "-3", "-c", "import sys; print(sys.executable)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _ = proc.communicate(timeout=5)
        if proc.returncode == 0:
            exe = stdout.decode("utf-8", errors="replace").strip()
            if exe and os.path.isfile(exe):
                return exe
    except Exception:
        pass

    # Try 'python' from PATH, verify it has http.server (not RenderDoc's)
    try:
        proc = subprocess.Popen(
            ["python", "-c", "import http.server; import sys; print(sys.executable)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _ = proc.communicate(timeout=5)
        if proc.returncode == 0:
            exe = stdout.decode("utf-8", errors="replace").strip()
            if exe and os.path.isfile(exe):
                return exe
    except Exception:
        pass

    return None


class ExportFileServer(object):
    """Launches the HTTP file server as a separate process with its own console."""

    def __init__(self, export_dir, port=19877, retention_days=7):
        self.export_dir = export_dir
        self.port = port
        self.retention_days = retention_days
        self._process = None

    def start(self):
        """Start the file server in a new process with a visible console window."""
        python_exe = _find_system_python()
        if python_exe is None:
            print("[MCP FileServer] ERROR: Cannot find system Python.")
            print("[MCP FileServer] Set RENDERDOC_MCP_PYTHON to the Python executable path.")
            return False

        if not os.path.isdir(self.export_dir):
            os.makedirs(self.export_dir)

        script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "file_server_process.py",
        )

        if not os.path.isfile(script_path):
            print("[MCP FileServer] ERROR: Server script not found: %s" % script_path)
            return False

        cmd = [
            python_exe,
            script_path,
            self.export_dir,
            str(self.port),
            str(self.retention_days),
        ]

        try:
            # CREATE_NEW_CONSOLE = 0x10 gives the process its own visible terminal
            self._process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as e:
            print("[MCP FileServer] Failed to start server process: %s" % str(e))
            return False

        print("[MCP FileServer] Started (pid=%d, python=%s, port=%d)"
              % (self._process.pid, python_exe, self.port))
        return True

    def stop(self):
        """Terminate the file server process."""
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
            print("[MCP FileServer] Stopped")

    def is_running(self):
        """Check if the server process is still alive."""
        if self._process is None:
            return False
        return self._process.poll() is None
