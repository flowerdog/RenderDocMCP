"""
Standalone HTTP file server for RenderDoc MCP exports.

Launched as a separate process by the RenderDoc extension to avoid
instability from running http.server inside RenderDoc's embedded Python.

Usage: python file_server_process.py <export_dir> <port> [retention_days]
"""

import os
import sys
import time
import signal
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ExportHandler(SimpleHTTPRequestHandler):
    """Serves files from the configured export directory."""

    export_dir = "."

    def translate_path(self, path):
        from urllib.parse import unquote
        import posixpath

        path = unquote(path)
        path = posixpath.normpath(path)
        parts = path.split("/")
        result = self.export_dir
        for part in parts:
            if not part or part == "." or part == "..":
                continue
            result = os.path.join(result, part)
        return result

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.log_message("Client disconnected during transfer")

    def log_message(self, fmt, *args):
        print("[FileServer] %s" % (fmt % args), flush=True)


def cleanup_expired_files(export_dir, retention_days):
    """Remove files older than retention_days from export_dir."""
    if retention_days <= 0:
        return 0

    cutoff = time.time() - (retention_days * 86400)
    removed = 0

    try:
        for name in os.listdir(export_dir):
            filepath = os.path.join(export_dir, name)
            if not os.path.isfile(filepath):
                continue
            try:
                if os.stat(filepath).st_mtime < cutoff:
                    os.remove(filepath)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass

    if removed > 0:
        print("[FileServer] Cleaned up %d expired file(s)" % removed, flush=True)
    return removed


def main():
    if len(sys.argv) < 3:
        print("Usage: %s <export_dir> <port> [retention_days]" % sys.argv[0])
        sys.exit(1)

    export_dir = sys.argv[1]
    port = int(sys.argv[2])
    retention_days = int(sys.argv[3]) if len(sys.argv) > 3 else 7

    if not os.path.isdir(export_dir):
        os.makedirs(export_dir)

    cleanup_expired_files(export_dir, retention_days)

    ExportHandler.export_dir = export_dir

    server = ThreadingHTTPServer(("0.0.0.0", port), ExportHandler)

    def shutdown_handler(signum, frame):
        print("\n[FileServer] Shutting down...", flush=True)
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    print("=" * 60, flush=True)
    print("[FileServer] RenderDoc MCP Export File Server", flush=True)
    print("[FileServer] Serving: %s" % export_dir, flush=True)
    print("[FileServer] Port:    %d" % port, flush=True)
    print("[FileServer] URL:     http://0.0.0.0:%d/" % port, flush=True)
    print("=" * 60, flush=True)
    print("[FileServer] Ready. Close this window to stop.", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[FileServer] Stopped.", flush=True)


if __name__ == "__main__":
    main()
