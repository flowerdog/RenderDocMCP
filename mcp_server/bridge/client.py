"""
RenderDoc Bridge Client
Communicates with the RenderDoc extension via TCP socket.

Protocol: 4-byte big-endian length prefix + JSON payload (UTF-8)
"""

import json
import os
import socket
import struct
import uuid
from typing import Any


class RenderDocBridgeError(Exception):
    """Error communicating with RenderDoc bridge"""

    pass


class RenderDocBridge:
    """Client for communicating with RenderDoc extension via TCP socket"""

    HEADER_SIZE = 4

    def __init__(self, host: str = "127.0.0.1", port: int = 19876):
        self.host = host
        self.port = port
        # Allow tuning for heavier queries (e.g. get_pipeline_state on large captures).
        self.timeout = float(os.environ.get("RENDERDOC_MCP_TIMEOUT", "120"))
        self._socket: socket.socket | None = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call a method on the RenderDoc extension via TCP"""
        request = {
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }

        try:
            self._ensure_connected()

            payload = json.dumps(request).encode("utf-8")
            frame = struct.pack("!I", len(payload)) + payload
            self._socket.sendall(frame)

            header = self._recv_exact(self.HEADER_SIZE)
            msg_len = struct.unpack("!I", header)[0]
            resp_data = self._recv_exact(msg_len)

            response = json.loads(resp_data.decode("utf-8"))

            if "error" in response:
                error = response["error"]
                raise RenderDocBridgeError(f"[{error['code']}] {error['message']}")

            return response.get("result")

        except Exception as e:
            self._disconnect()
            # Retry once on transient socket/timeout issues.
            if isinstance(e, (socket.timeout, TimeoutError, ConnectionError, OSError)):
                return self._retry_once(request)
            if isinstance(e, RenderDocBridgeError):
                raise
            raise RenderDocBridgeError(f"Communication error: {e}")

    def _retry_once(self, request: dict[str, Any]) -> Any:
        """Reconnect and retry one request once."""
        try:
            self._ensure_connected()

            payload = json.dumps(request).encode("utf-8")
            frame = struct.pack("!I", len(payload)) + payload
            self._socket.sendall(frame)

            header = self._recv_exact(self.HEADER_SIZE)
            msg_len = struct.unpack("!I", header)[0]
            resp_data = self._recv_exact(msg_len)

            response = json.loads(resp_data.decode("utf-8"))
            if "error" in response:
                error = response["error"]
                raise RenderDocBridgeError(f"[{error['code']}] {error['message']}")
            return response.get("result")
        except Exception as retry_error:
            self._disconnect()
            if isinstance(retry_error, RenderDocBridgeError):
                raise
            raise RenderDocBridgeError(
                f"Communication error after retry: {retry_error}"
            )

    def _ensure_connected(self):
        """Establish TCP connection if not already connected"""
        if self._socket is not None:
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            self._socket = sock
        except Exception as e:
            raise RenderDocBridgeError(
                f"Cannot connect to RenderDoc MCP Bridge at {self.host}:{self.port}. "
                f"Make sure RenderDoc is running with the MCP Bridge extension loaded. "
                f"Error: {e}"
            )

    def _disconnect(self):
        """Close the TCP connection"""
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

    def _recv_exact(self, n: int) -> bytes:
        """Receive exactly n bytes from the socket"""
        buf = b""
        while len(buf) < n:
            chunk = self._socket.recv(n - len(buf))
            if not chunk:
                self._disconnect()
                raise RenderDocBridgeError(
                    "Connection closed by RenderDoc while reading response"
                )
            buf += chunk
        return buf
