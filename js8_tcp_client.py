# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
js8_tcp_client.py - JS8Call TCP Client for CommStat

Provides persistent TCP connections to JS8Call instances using Qt networking.
Supports multiple simultaneous connections via TCPConnectionPool.
"""

import json
import time
from typing import Dict, List, Optional

from PyQt5.QtCore import QObject, pyqtSignal, QTimer
from PyQt5.QtNetwork import QTcpSocket, QAbstractSocket

from connector_manager import ConnectorManager
from constants import SPEED_OPTIONS


# Constants
DEFAULT_HOST = "127.0.0.1"
RECONNECT_INTERVAL_MS = 5000  # 5 seconds
MAX_RECONNECT_ATTEMPTS = 12   # 12 attempts = 1 minute


class JS8CallTCPClient(QObject):
    """
    Persistent TCP client for a single JS8Call instance.

    Uses Qt signals for asynchronous communication.
    Automatically handles reconnection on disconnect (up to 1 minute).
    """

    # Signals
    message_received = pyqtSignal(str, dict)      # rig_name, message_dict
    connection_changed = pyqtSignal(str, bool)    # rig_name, is_connected
    callsign_received = pyqtSignal(str, str)      # rig_name, callsign
    grid_received = pyqtSignal(str, str)          # rig_name, grid
    frequency_received = pyqtSignal(str, int)     # rig_name, frequency
    speed_received = pyqtSignal(str, int)         # rig_name, speed (submode)
    call_selected_received = pyqtSignal(str, str) # rig_name, selected_call (empty if none)
    status_message = pyqtSignal(str, str)         # rig_name, message (for live feed)
    gave_up = pyqtSignal(str)                     # rig_name - emitted when max reconnect attempts reached

    # Speed mode names
    SPEED_NAMES = {value: label.upper() for label, value in SPEED_OPTIONS}

    def __init__(self, rig_name: str, port: int, host: str = DEFAULT_HOST, parent: QObject = None):
        """
        Initialize TCP client.

        Args:
            rig_name: Name identifying this rig/connection.
            port: TCP port for JS8Call (typically 2442).
            host: IP address or hostname of the JS8Call computer (default 127.0.0.1).
            parent: Parent QObject.
        """
        super().__init__(parent)
        self.rig_name = rig_name
        self.port = port
        self.host = host
        self.buffer = b""
        self._enabled = True  # Connector enabled state; controls whether retries resume after a drop
        self._auto_reconnect = True
        self._reconnect_attempts = 0
        self._was_connected = False  # Tracks last-emitted connection state
        self._grid_requested = False  # STATION.GET_GRID is sent once per connection
        self.callsign = ""  # Cached callsign from JS8Call
        self.speed_name = ""  # Cached speed mode name (NORMAL, FAST, TURBO, etc.)
        self.frequency = 0.0  # Cached frequency in MHz

        # Create socket
        self.socket = QTcpSocket(self)
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.readyRead.connect(self._on_ready_read)
        self.socket.errorOccurred.connect(self._on_error)

        # Reconnect timer
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.timeout.connect(self._try_reconnect)
        self._reconnect_timer.setSingleShot(True)

    def connect_to_host(self) -> None:
        """Initiate connection to JS8Call."""
        state = self.socket.state()
        if state == QAbstractSocket.UnconnectedState:
            print(f"[{self.rig_name}] Connecting to {self.host}:{self.port}...")
            self.status_message.emit(self.rig_name, f"[{self.rig_name}] Attempting to connect on TCP port {self.port}")
            self.socket.connectToHost(self.host, self.port)
        elif state in (QAbstractSocket.ClosingState, QAbstractSocket.ConnectedState):
            # Wait for socket to fully close before reconnecting
            pass
        else:
            # Force abort and reconnect
            print(f"[{self.rig_name}] Aborting stale connection (state: {state})...")
            self.status_message.emit(self.rig_name, f"[{self.rig_name}] Attempting to connect on TCP port {self.port}")
            self.socket.abort()
            self.socket.connectToHost(self.host, self.port)

    def disconnect_from_host(self) -> None:
        """Disconnect from JS8Call and stop auto-reconnecting."""
        self._auto_reconnect = False
        self._reconnect_timer.stop()
        state = self.socket.state()
        if state == QAbstractSocket.ConnectedState:
            self.socket.disconnectFromHost()
        elif state != QAbstractSocket.UnconnectedState:
            self.socket.abort()   # still connecting or closing: drop it now

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable this connector.

        Enabled: reset the retry counter, allow auto-reconnect and connect now
        if down. Disabled: stop retries and drop the socket, so the connector
        is fully quiescent (even if a connection attempt was in flight)."""
        self._enabled = enabled
        self._auto_reconnect = enabled
        self._reconnect_timer.stop()
        if enabled:
            self._reconnect_attempts = 0
            if not self.is_connected():
                self.connect_to_host()
        else:
            self.disconnect_from_host()

    def shutdown(self) -> None:
        """Close immediately and stop retrying, before the client is deleted.
        connection_changed(False) is emitted right away if it was connected."""
        self._auto_reconnect = False
        self._reconnect_timer.stop()
        if self.socket.state() != QAbstractSocket.UnconnectedState:
            self.socket.abort()

    def is_connected(self) -> bool:
        """Return True if connected to JS8Call."""
        return self.socket.state() == QAbstractSocket.ConnectedState

    def send_message(
        self,
        msg_type: str,
        value: str = "",
        params: Optional[Dict] = None
    ) -> int:
        """
        Send a message to JS8Call.

        Args:
            msg_type: Message type (e.g., "TX.SEND_MESSAGE").
            value: Message value/content.
            params: Additional parameters.

        Returns:
            Request ID (timestamp in milliseconds).
        """
        if not self.is_connected():
            print(f"[{self.rig_name}] Cannot send: not connected")
            return -1

        if params is None:
            params = {}

        # Generate request ID
        request_id = int(time.time() * 1000)
        params["_ID"] = request_id

        message = {
            "type": msg_type,
            "value": value,
            "params": params
        }

        json_str = json.dumps(message) + "\n"
        self.socket.write(json_str.encode())
        self.socket.flush()

        return request_id

    def get_callsign(self) -> None:
        """Request callsign from JS8Call. Result emitted via callsign_received signal."""
        self.send_message("STATION.GET_CALLSIGN")

    def get_grid(self) -> None:
        """Request grid from JS8Call. Result emitted via grid_received signal."""
        self.send_message("STATION.GET_GRID")

    def get_frequency(self) -> None:
        """Request frequency from JS8Call. Result emitted via frequency_received signal."""
        self.send_message("RIG.GET_FREQ")

    def get_speed(self) -> None:
        """Request speed mode from JS8Call. Result emitted via speed_received signal."""
        self.send_message("MODE.GET_SPEED")

    def get_call_selected(self) -> None:
        """Request selected call from JS8Call. Result emitted via call_selected_received signal."""
        self.send_message("RX.GET_CALL_SELECTED")

    def send_tx_message(self, text: str) -> int:
        """
        Send a message to be transmitted by JS8Call.

        Args:
            text: Message text to transmit.

        Returns:
            Request ID.
        """
        return self.send_message("TX.SEND_MESSAGE", text)

    def _on_connected(self) -> None:
        """Handle successful connection."""
        print(f"[{self.rig_name}] Connected to JS8Call on port {self.port}")
        self._reconnect_timer.stop()
        self._reconnect_attempts = 0
        self._auto_reconnect = self._enabled   # a disabled connector must not come back
        self._was_connected = True
        self._grid_requested = False
        self.connection_changed.emit(self.rig_name, True)
        # Emit connected message immediately
        self.status_message.emit(
            self.rig_name,
            f"[{self.rig_name}] Connected on TCP port {self.port}"
        )
        self.get_callsign()  # Request callsign first, then speed mode

    def _on_disconnected(self) -> None:
        """Handle disconnection."""
        print(f"[{self.rig_name}] Disconnected from JS8Call")
        self.buffer = b""
        if self._was_connected:
            self._was_connected = False
            self.connection_changed.emit(self.rig_name, False)

        # Schedule reconnect if auto-reconnect is enabled. _try_reconnect is the
        # single authority for the attempt cap and give-up.
        if self._auto_reconnect and not self._reconnect_timer.isActive():
            print(f"[{self.rig_name}] Will retry in {RECONNECT_INTERVAL_MS // 1000}s...")
            self._reconnect_timer.start(RECONNECT_INTERVAL_MS)

    def _on_ready_read(self) -> None:
        """Handle incoming data from JS8Call."""
        self.buffer += self.socket.readAll().data()

        # Process complete messages (newline-delimited JSON)
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line.strip():
                continue

            try:
                message = json.loads(line.decode("utf-8"))
                self._process_message(message)
            except json.JSONDecodeError as e:
                print(f"[{self.rig_name}] JSON decode error: {e}")
            except Exception as e:
                print(f"[{self.rig_name}] Error processing message: {e}")

    def _process_message(self, message: dict) -> None:
        """
        Route incoming message to appropriate handler.

        Args:
            message: Parsed JSON message from JS8Call.
        """
        msg_type = message.get("type", "")
        value = message.get("value", "")
        params = message.get("params", {})

        # Handle specific response types
        if msg_type == "STATION.CALLSIGN":
            self.callsign = value  # Cache callsign
            self.callsign_received.emit(self.rig_name, value)
            # Now request speed mode (callsign will be included in that message)
            self.get_speed()

        elif msg_type == "STATION.GRID":
            self.grid_received.emit(self.rig_name, value)

        elif msg_type == "RIG.FREQ":
            dial_freq = params.get("DIAL", 0)
            self.frequency = dial_freq / 1000000  # Store dial frequency in MHz
            self.frequency_received.emit(self.rig_name, dial_freq)
            # Chain to the grid request (combined status printed after grid
            # received), once per connection: JS8Call repeats RIG.FREQ, and the
            # grid does not change with it.
            if not self._grid_requested:
                self._grid_requested = True
                self.get_grid()

        elif msg_type == "MODE.SPEED":
            speed = params.get("SPEED", 0)
            self.speed_name = self.SPEED_NAMES.get(speed, f"MODE {speed}")
            self.speed_received.emit(self.rig_name, speed)
            # Chain to frequency request (combined status printed after grid received)
            self.get_frequency()

        elif msg_type == "RX.DIRECTED":
            # Directed message received - emit for processing
            self.message_received.emit(self.rig_name, message)

        elif msg_type == "RX.ACTIVITY":
            # Band activity - emit for processing
            self.message_received.emit(self.rig_name, message)

        elif msg_type == "RX.SPOT":
            # Spot message - emit for processing
            self.message_received.emit(self.rig_name, message)

        elif msg_type == "RX.BAND_ACTIVITY":
            self.message_received.emit(self.rig_name, message)

        elif msg_type == "RX.CALL_ACTIVITY":
            self.message_received.emit(self.rig_name, message)

        elif msg_type == "RX.CALL_SELECTED":
            # Call selected response - emit both signals
            self.call_selected_received.emit(self.rig_name, value)
            self.message_received.emit(self.rig_name, message)

        # Ignore PING and other status messages silently

    def _on_error(self, error: QAbstractSocket.SocketError) -> None:
        """Handle socket errors."""
        if error == QAbstractSocket.ConnectionRefusedError:
            msg = f"[{self.rig_name}] Connection refused on TCP port {self.port}"
        elif error == QAbstractSocket.RemoteHostClosedError:
            msg = f"[{self.rig_name}] Connection closed by JS8Call"
        elif error == QAbstractSocket.HostNotFoundError:
            msg = f"[{self.rig_name}] Host not found: {self.host}"
        else:
            msg = f"[{self.rig_name}] Socket error: {self.socket.errorString()}"

        print(msg)
        self.status_message.emit(self.rig_name, msg)

        # Catch the case where the socket dropped without `disconnected` firing.
        # Why: errorOccurred is the only guaranteed signal on some failure paths;
        # without this fallback, downstream UI can stay stuck on "Connected".
        if self._was_connected and self.socket.state() != QAbstractSocket.ConnectedState:
            self._was_connected = False
            self.connection_changed.emit(self.rig_name, False)

        # Schedule a reconnect after any error (refused, host not found, timeout,
        # network down...), so a JS8Call on another computer is retried too.
        # _try_reconnect is the single authority for the attempt cap and give-up.
        if self._auto_reconnect and not self._reconnect_timer.isActive():
            print(f"[{self.rig_name}] Will retry in {RECONNECT_INTERVAL_MS // 1000}s...")
            self._reconnect_timer.start(RECONNECT_INTERVAL_MS)

    def _give_up_reconnect(self) -> None:
        """Stop auto-reconnect after exhausting attempts. Idempotent: gated by
        _auto_reconnect so gave_up is emitted at most once per session."""
        self._auto_reconnect = False
        self._reconnect_timer.stop()
        print(f"[{self.rig_name}] Giving up after {MAX_RECONNECT_ATTEMPTS} attempts. Use JS8 Connectors to reconnect.")
        self.status_message.emit(
            self.rig_name,
            f"[{self.rig_name}] Reconnect failed. Use Menu > JS8 CONNECTORS to reconnect."
        )
        self.gave_up.emit(self.rig_name)

    def _try_reconnect(self) -> None:
        """Attempt to reconnect to JS8Call."""
        if not self._auto_reconnect or self.is_connected():
            return

        if self._reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            self._give_up_reconnect()
            return

        self._reconnect_attempts += 1
        print(f"[{self.rig_name}] Reconnect attempt {self._reconnect_attempts}/{MAX_RECONNECT_ATTEMPTS}...")
        self.connect_to_host()


class TCPConnectionPool(QObject):
    """
    Manages multiple JS8CallTCPClient instances.

    Creates and manages TCP connections for all configured connectors.
    Aggregates signals from all clients.
    """

    # Aggregate signals (from any client)
    any_message_received = pyqtSignal(str, dict)    # rig_name, message
    any_connection_changed = pyqtSignal(str, bool)  # rig_name, is_connected
    any_status_message = pyqtSignal(str, str)       # rig_name, message (for live feed)
    any_callsign_received = pyqtSignal(str, str)    # rig_name, callsign
    any_grid_received = pyqtSignal(str, str)        # rig_name, grid

    def __init__(self, connector_manager: ConnectorManager, parent: QObject = None):
        """
        Initialize connection pool.

        Args:
            connector_manager: ConnectorManager for database access.
            parent: Parent QObject.
        """
        super().__init__(parent)
        self.connector_manager = connector_manager
        self.clients: Dict[str, JS8CallTCPClient] = {}

    def connect_all(self) -> None:
        """Open TCP connections for every auto-connect connector at startup.

        Mirrors per-row Reconnect for every auto_connect=1 row: force-enable in
        the DB, then let refresh_connections create the client and connect.
        auto_connect=0 rows are forced enabled=0 so they stay quiescent — no
        socket attempt, and they're hidden from send dropdowns. The user can
        still click Reconnect on them in JS8 Connectors to bring them up
        manually for the current session.

        Auto-disable on max retries (_on_client_gave_up) still applies during
        the session.
        """
        for conn in self.connector_manager.get_all_connectors(enabled_only=False):
            target_enabled = conn.get("auto_connect", 1) == 1
            self.connector_manager.set_enabled(conn["id"], target_enabled)
        self.refresh_connections()

    def disconnect_all(self) -> None:
        """Disconnect and remove all TCP clients."""
        for client in self.clients.values():
            client.shutdown()
            client.deleteLater()
        self.clients.clear()

    def refresh_connections(self) -> None:
        """
        Refresh pool to match current database configuration.

        Every configured connector stays in the pool — enabled rows connect with
        auto-reconnect; disabled rows are registered but make no socket attempt.
        Port/host changes recreate the client; rows removed from the DB are
        evicted; enable-state flips connect or disconnect as needed.
        """
        all_connectors = self.connector_manager.get_all_connectors(enabled_only=False)
        db_names = {c["rig_name"] for c in all_connectors}
        current_names = set(self.clients.keys())

        # Remove only truly-deleted connectors
        for name in current_names - db_names:
            self._remove_client(name)

        for conn in all_connectors:
            rig_name = conn["rig_name"]
            tcp_port = conn["tcp_port"]
            server = conn.get("server", DEFAULT_HOST)
            is_enabled = conn.get("enabled", 1) == 1

            if rig_name not in self.clients:
                self._create_client(rig_name, tcp_port, server, enabled=is_enabled)
                continue

            client = self.clients[rig_name]

            # Port/host change → recreate
            if client.port != tcp_port or client.host != server:
                self._remove_client(rig_name)
                self._create_client(rig_name, tcp_port, server, enabled=is_enabled)
                continue

            # Propagate enable-state change to existing client
            if client.enabled != is_enabled:
                client.set_enabled(is_enabled)

    def _create_client(self, rig_name: str, port: int, host: str = DEFAULT_HOST, enabled: bool = True) -> None:
        """Create a single TCP client.

        If `enabled` is True, attempt to connect immediately with auto-reconnect.
        Otherwise the client is registered in the pool but stays disconnected
        until enabled via JS8 Connectors (e.g. a manual Reconnect on an
        auto_connect=0 row).
        """
        client = JS8CallTCPClient(rig_name, port, host, self)

        # Connect signals to aggregate signals
        client.message_received.connect(self.any_message_received)
        client.connection_changed.connect(self.any_connection_changed)
        client.status_message.connect(self.any_status_message)
        client.callsign_received.connect(self.any_callsign_received)
        client.grid_received.connect(self.any_grid_received)
        client.gave_up.connect(self._on_client_gave_up)

        self.clients[rig_name] = client
        client.set_enabled(enabled)   # connects now if enabled

    def _on_client_gave_up(self, rig_name: str) -> None:
        """Handle client giving up after max reconnect attempts - disable the connector."""
        conn = self.connector_manager.get_connector_by_name(rig_name)
        if conn:
            self.connector_manager.set_enabled(conn["id"], False)
            client = self.clients.get(rig_name)
            if client is not None:
                client.set_enabled(False)
            print(f"[{rig_name}] Connector disabled. Use Menu > JS8 CONNECTORS to reconnect.")

    def _remove_client(self, rig_name: str) -> None:
        """Disconnect and remove a client."""
        if rig_name in self.clients:
            client = self.clients.pop(rig_name)
            client.shutdown()
            client.deleteLater()

    def get_client(self, rig_name: str) -> Optional[JS8CallTCPClient]:
        """
        Get client by rig name.

        Args:
            rig_name: Name of the rig.

        Returns:
            JS8CallTCPClient or None if not found.
        """
        return self.clients.get(rig_name)

    def get_default_client(self) -> Optional[JS8CallTCPClient]:
        """
        Get the default connector's client.

        Returns:
            JS8CallTCPClient for default rig or None.
        """
        default = self.connector_manager.get_default_connector()
        if default:
            return self.clients.get(default["rig_name"])
        return None

    def get_connected_rig_names(self) -> List[str]:
        """
        Get list of connected rig names.

        Returns:
            List of rig names that are currently connected.
        """
        return [
            name for name, client in self.clients.items()
            if client.is_connected()
        ]

    def get_all_rig_names(self) -> List[str]:
        """
        Get list of all configured rig names.

        Returns:
            List of all rig names (connected or not).
        """
        return list(self.clients.keys())

    def is_any_connected(self) -> bool:
        """
        Check if any client is connected.

        Returns:
            True if at least one client is connected.
        """
        return any(client.is_connected() for client in self.clients.values())

    def get_connection_status(self) -> Dict[str, bool]:
        """
        Get connection status for all clients.

        Returns:
            Dictionary mapping rig_name to connected status.
        """
        return {
            name: client.is_connected()
            for name, client in self.clients.items()
        }
