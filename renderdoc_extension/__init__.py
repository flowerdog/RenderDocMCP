"""
RenderDoc MCP Bridge Extension
Provides TCP socket server for external MCP server communication.
"""

import os
import tempfile

from . import socket_server
from . import request_handler
from . import renderdoc_facade
from . import file_server

# Global state
_context = None
_server = None
_file_server = None
_version = ""

# Server config via environment variables (set before launching RenderDoc)
_host = os.environ.get("RENDERDOC_MCP_HOST", "0.0.0.0")
_port = int(os.environ.get("RENDERDOC_MCP_PORT", "19876"))

# File server config
_file_server_port = int(os.environ.get("RENDERDOC_MCP_FILE_SERVER_PORT", "19877"))
_export_dir = os.environ.get(
    "RENDERDOC_MCP_EXPORT_DIR",
    os.path.join(tempfile.gettempdir(), "renderdoc_mcp_exports"),
)
_export_retention_days = int(os.environ.get("RENDERDOC_MCP_EXPORT_RETENTION_DAYS", "7"))

# Try to import qrenderdoc for UI integration (only available in RenderDoc)
try:
    import qrenderdoc as qrd

    _has_qrenderdoc = True
except ImportError:
    _has_qrenderdoc = False


def register(version, ctx):
    """
    Called when extension is loaded.

    Args:
        version: RenderDoc version string (e.g., "1.20")
        ctx: CaptureContext handle
    """
    global _context, _server, _file_server, _version
    _version = version
    _context = ctx

    # Start HTTP file server for exports
    _file_server = file_server.ExportFileServer(
        export_dir=_export_dir,
        port=_file_server_port,
        retention_days=_export_retention_days,
    )
    _file_server.start()

    file_server_base_url = "http://%s:%d" % (_host, _file_server_port)

    # Create facade and handler
    facade = renderdoc_facade.RenderDocFacade(
        ctx,
        export_dir=_export_dir,
        file_server_base_url=file_server_base_url,
    )
    handler = request_handler.RequestHandler(facade)

    _server = socket_server.MCPBridgeServer(
        host=_host, port=_port, handler=handler
    )
    _server.start()

    # Register menu item if UI is available
    if _has_qrenderdoc:
        try:
            ctx.Extensions().RegisterWindowMenu(
                qrd.WindowMenu.Tools, ["MCP Bridge", "Status"], _show_status
            )
        except Exception as e:
            print("[MCP Bridge] Could not register menu: %s" % str(e))

    print("[MCP Bridge] Extension loaded (RenderDoc %s)" % version)


def unregister():
    """Called when extension is unloaded"""
    global _server, _file_server
    if _server:
        _server.stop()
        _server = None
    if _file_server:
        _file_server.stop()
        _file_server = None
    print("[MCP Bridge] Extension unloaded")


def _show_status(ctx, data):
    """Show status dialog"""
    if _server and _server.is_running():
        ctx.Extensions().MessageDialog(
            "MCP Bridge TCP server is running on %s:%d" % (_server.host, _server.port),
            "MCP Bridge Status",
        )
    else:
        ctx.Extensions().ErrorDialog("MCP Bridge is not running", "MCP Bridge Status")
