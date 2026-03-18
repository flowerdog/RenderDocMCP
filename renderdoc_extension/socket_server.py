"""
TCP Socket Server for RenderDoc MCP Bridge
Uses non-blocking sockets with QTimer polling to integrate with Qt event loop.

Protocol: 4-byte big-endian length prefix + JSON payload (UTF-8)
"""

import json
import socket
import struct
import time
import traceback

from PySide2.QtCore import QObject, QTimer


class MCPBridgeServer(QObject):
    """Non-blocking TCP server for MCP bridge communication.

    Design: single-active-client model. When a new client connects, any
    existing client is replaced. Idle connections are closed after
    IDLE_TIMEOUT_SEC seconds to prevent zombie connections from blocking
    new clients.
    """

    HEADER_SIZE = 4
    RECV_BUFSIZE = 262144  # 256 KB
    LISTEN_BACKLOG = 16
    IDLE_TIMEOUT_SEC = 300  # 5 minutes
    SEND_TIMEOUT_SEC = 10
    SLOW_REQUEST_MS = 1000

    def __init__(self, host, port, handler, parent=None):
        super(MCPBridgeServer, self).__init__(parent)
        self.host = host
        self.port = port
        self.handler = handler
        self._server_socket = None
        self._client_socket = None
        self._recv_buffer = b""
        self._last_activity = 0
        self._timer = None
        self._running = False

        self._stats = {
            "accept_count": 0,
            "replace_count": 0,
            "error_count": 0,
        }

    def start(self):
        """Start the TCP server"""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.setblocking(False)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(self.LISTEN_BACKLOG)
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

    def get_stats(self):
        """Return connection statistics for diagnostics"""
        return dict(self._stats)

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
        """Non-blocking poll: accept connections, check idle, read/process data"""
        if not self._running:
            return

        self._try_accept()

        if self._client_socket is None:
            return

        if self._check_idle_timeout():
            return

        self._try_recv()
        self._process_messages()

    def _try_accept(self):
        """Try to accept new client connections (non-blocking).

        Drains the entire backlog so pending connections never pile up.
        New clients replace any existing active client.
        """
        try:
            while True:
                client, addr = self._server_socket.accept()

                self._configure_client_socket(client)

                if self._client_socket is not None:
                    print(
                        "[MCP Bridge] New client %s:%d replacing previous client"
                        % (addr[0], addr[1])
                    )
                    self._close_client()
                    self._stats["replace_count"] += 1
                else:
                    print("[MCP Bridge] Client connected from %s:%d" % (addr[0], addr[1]))

                self._client_socket = client
                self._recv_buffer = b""
                self._last_activity = time.time()
                self._stats["accept_count"] += 1
        except BlockingIOError:
            pass
        except Exception as e:
            print("[MCP Bridge] Accept error: %s" % str(e))
            self._stats["error_count"] += 1

    @staticmethod
    def _configure_client_socket(sock):
        """Apply non-blocking mode and TCP keepalive to an accepted socket."""
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Windows: idle 30s, interval 5s, max failures determined by OS
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30000, 5000))
        except (AttributeError, OSError):
            pass

    def _check_idle_timeout(self):
        """Close client if idle for too long. Returns True if closed."""
        if self._last_activity and self.IDLE_TIMEOUT_SEC > 0:
            idle_sec = time.time() - self._last_activity
            if idle_sec > self.IDLE_TIMEOUT_SEC:
                print(
                    "[MCP Bridge] Client idle for %d seconds, closing"
                    % int(idle_sec)
                )
                self._close_client()
                return True
        return False

    def _try_recv(self):
        """Try to read available data from client (non-blocking)"""
        try:
            data = self._client_socket.recv(self.RECV_BUFSIZE)
            if not data:
                print("[MCP Bridge] Client disconnected")
                self._close_client()
                return
            self._recv_buffer += data
            self._last_activity = time.time()
        except BlockingIOError:
            pass
        except (ConnectionError, OSError):
            print("[MCP Bridge] Client connection lost")
            self._close_client()
            self._stats["error_count"] += 1

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

        method = request.get("method")
        start_time = time.time()
        try:
            response = self.handler.handle(request)
        except Exception as e:
            traceback.print_exc()
            response = {
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(e)}
            }
            self._stats["error_count"] += 1
        finally:
            elapsed_ms = int((time.time() - start_time) * 1000)
            if elapsed_ms >= self.SLOW_REQUEST_MS:
                print(
                    "[MCP Bridge] Slow request: method=%s, elapsed_ms=%d"
                    % (method, elapsed_ms)
                )

        self._send_response(response)

    def _send_response(self, response):
        """Encode and send a length-prefixed JSON response.

        Temporarily switches the socket to blocking mode with a timeout
        so sendall works reliably even for large payloads.
        """
        if self._client_socket is None:
            return

        try:
            payload = json.dumps(response).encode("utf-8")
            frame = struct.pack("!I", len(payload)) + payload
            self._client_socket.settimeout(self.SEND_TIMEOUT_SEC)
            self._client_socket.sendall(frame)
            self._client_socket.setblocking(False)
            self._last_activity = time.time()
        except Exception as e:
            print("[MCP Bridge] Send error: %s" % str(e))
            self._close_client()
            self._stats["error_count"] += 1
