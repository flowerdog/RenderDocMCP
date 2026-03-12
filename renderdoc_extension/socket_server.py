"""
TCP Socket Server for RenderDoc MCP Bridge
Uses non-blocking sockets with QTimer polling to integrate with Qt event loop.

Protocol: 4-byte big-endian length prefix + JSON payload (UTF-8)
"""

import json
import socket
import struct
import traceback

from PySide2.QtCore import QObject, QTimer


class MCPBridgeServer(QObject):
    """Non-blocking TCP server for MCP bridge communication"""

    HEADER_SIZE = 4
    RECV_BUFSIZE = 262144  # 256 KB

    def __init__(self, host, port, handler, parent=None):
        super(MCPBridgeServer, self).__init__(parent)
        self.host = host
        self.port = port
        self.handler = handler
        self._server_socket = None
        self._client_socket = None
        self._recv_buffer = b""
        self._timer = None
        self._running = False

    def start(self):
        """Start the TCP server"""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.setblocking(False)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(1)
        self._running = True

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(10)

        print("[MCP Bridge] TCP server listening on %s:%d" % (self.host, self.port))
        return True

    def stop(self):
        """Stop the server and clean up"""
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None
        self._close_client()
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None
        print("[MCP Bridge] TCP server stopped")

    def is_running(self):
        """Check if server is running"""
        return self._running

    def _close_client(self):
        """Close current client connection"""
        if self._client_socket:
            try:
                self._client_socket.close()
            except Exception:
                pass
            self._client_socket = None
            self._recv_buffer = b""

    def _poll(self):
        """Non-blocking poll: accept connections and read/process data"""
        if not self._running:
            return

        if self._client_socket is None:
            self._try_accept()
            return

        self._try_recv()
        self._process_messages()

    def _try_accept(self):
        """Try to accept a new client connection (non-blocking)"""
        try:
            client, addr = self._server_socket.accept()
            client.setblocking(False)
            self._client_socket = client
            self._recv_buffer = b""
            print("[MCP Bridge] Client connected from %s:%d" % (addr[0], addr[1]))
        except BlockingIOError:
            pass
        except Exception as e:
            print("[MCP Bridge] Accept error: %s" % str(e))

    def _try_recv(self):
        """Try to read available data from client (non-blocking)"""
        try:
            data = self._client_socket.recv(self.RECV_BUFSIZE)
            if not data:
                print("[MCP Bridge] Client disconnected")
                self._close_client()
                return
            self._recv_buffer += data
        except BlockingIOError:
            pass
        except (ConnectionError, OSError):
            print("[MCP Bridge] Client connection lost")
            self._close_client()

    def _process_messages(self):
        """Extract and handle complete length-prefixed messages from buffer"""
        if self._client_socket is None:
            return

        while len(self._recv_buffer) >= self.HEADER_SIZE:
            msg_len = struct.unpack("!I", self._recv_buffer[:self.HEADER_SIZE])[0]
            total_len = self.HEADER_SIZE + msg_len
            if len(self._recv_buffer) < total_len:
                break

            msg_data = self._recv_buffer[self.HEADER_SIZE:total_len]
            self._recv_buffer = self._recv_buffer[total_len:]
            self._handle_message(msg_data)

    def _handle_message(self, data):
        """Decode JSON request, dispatch to handler, send response"""
        try:
            request = json.loads(data.decode("utf-8"))
        except Exception as e:
            print("[MCP Bridge] Invalid JSON: %s" % str(e))
            response = {
                "id": None,
                "error": {"code": -32700, "message": "Parse error: %s" % str(e)}
            }
            self._send_response(response)
            return

        try:
            response = self.handler.handle(request)
        except Exception as e:
            traceback.print_exc()
            response = {
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(e)}
            }

        self._send_response(response)

    def _send_response(self, response):
        """Encode and send a length-prefixed JSON response"""
        if self._client_socket is None:
            return

        try:
            payload = json.dumps(response).encode("utf-8")
            frame = struct.pack("!I", len(payload)) + payload
            self._client_socket.sendall(frame)
        except Exception as e:
            print("[MCP Bridge] Send error: %s" % str(e))
            self._close_client()
