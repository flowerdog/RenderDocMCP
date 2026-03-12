"""
HTTP File Server for RenderDoc MCP Export.
Serves exported files (textures, meshes) via HTTP for remote download.
Uses a daemon thread so it terminates when RenderDoc exits.

Compatible with Python 3.6 (no f-strings, no SimpleHTTPRequestHandler(directory=)).
"""

import os
import time
import threading

try:
    from http.server import HTTPServer as _HTTPServer, SimpleHTTPRequestHandler
    import socketserver

    class _SafeHTTPServer(_HTTPServer):
        """HTTPServer subclass that avoids socket.getfqdn() in server_bind.

        RenderDoc's embedded Python lacks the 'idna' encoding codec required
        by socket.getfqdn(), so we skip that call entirely.
        """

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name = host or "0.0.0.0"
            self.server_port = port

except ImportError:
    _SafeHTTPServer = None
    SimpleHTTPRequestHandler = object


def _make_handler_class(export_dir):
    """Create a handler class bound to the given export directory."""

    class ExportFileHandler(SimpleHTTPRequestHandler):
        """Serves files from the configured export directory."""

        _export_dir = export_dir

        def translate_path(self, path):
            # Python 3.6 SimpleHTTPRequestHandler.translate_path uses os.getcwd().
            # We override to map all requests to our export directory.
            try:
                from urllib.parse import unquote
            except ImportError:
                from urllib import unquote
            import posixpath

            path = unquote(path)
            path = posixpath.normpath(path)
            parts = path.split("/")
            result = self._export_dir
            for part in parts:
                if not part or part == "." or part == "..":
                    continue
                result = os.path.join(result, part)
            return result

        def log_message(self, format, *args):
            print("[MCP FileServer] %s" % (format % args))

    return ExportFileHandler


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
        print("[MCP FileServer] Cleaned up %d expired file(s)" % removed)
    return removed


class ExportFileServer(object):
    """HTTP server that serves exported files from a directory."""

    def __init__(self, export_dir, port=19877, retention_days=7):
        self.export_dir = export_dir
        self.port = port
        self.retention_days = retention_days
        self._httpd = None
        self._thread = None

    def start(self):
        """Start the HTTP file server in a daemon thread."""
        if _SafeHTTPServer is None:
            print("[MCP FileServer] http.server not available, skipping")
            return False

        if not os.path.isdir(self.export_dir):
            os.makedirs(self.export_dir)

        cleanup_expired_files(self.export_dir, self.retention_days)

        handler_class = _make_handler_class(self.export_dir)

        try:
            self._httpd = _SafeHTTPServer(("0.0.0.0", self.port), handler_class)
        except OSError as e:
            print("[MCP FileServer] Failed to bind port %d: %s" % (self.port, e))
            return False

        self._thread = threading.Thread(target=self._httpd.serve_forever)
        self._thread.daemon = True
        self._thread.start()

        print("[MCP FileServer] Serving exports from '%s' on port %d"
              % (self.export_dir, self.port))
        return True

    def stop(self):
        """Shutdown the HTTP server."""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
        self._thread = None
        print("[MCP FileServer] Stopped")

    def is_running(self):
        """Check if the server is running."""
        return self._thread is not None and self._thread.is_alive()
