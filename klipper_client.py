# Klipper API client for communicating via Unix domain socket
#
# Copyright (C) 2026  Jonas Kennedy
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import socket
import json
import threading
import logging
import time

log = logging.getLogger(__name__)

class KlipperClient:
    def __init__(self, sock_path="/tmp/klippy_uds"):
        self.sock_path = sock_path
        self.sock = None
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}  # id -> threading.Event, result
        self._subscriptions = {}  # method -> callback
        self._connected = False
        self._reader_thread = None

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)
        self._connected = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        log.info("Connected to Klipper at %s", self.sock_path)

    def disconnect(self):
        self._connected = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def is_connected(self):
        return self._connected

    def _next_id(self):
        with self._lock:
            self._id += 1
            return self._id

    def _reader_loop(self):
        partial = b""
        while self._connected:
            try:
                data = self.sock.recv(4096)
            except OSError:
                if self._connected:
                    log.error("Socket read error, disconnecting")
                    self._connected = False
                break
            if not data:
                log.info("Klipper socket closed")
                self._connected = False
                break
            partial += data
            messages = partial.split(b'\x03')
            partial = messages.pop()
            for raw in messages:
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from Klipper: %s", raw[:200])
                    continue
                self._handle_message(msg)

    def _handle_message(self, msg):
        # Response to a request
        if "id" in msg:
            req_id = msg["id"]
            with self._lock:
                if req_id in self._pending:
                    event, _ = self._pending[req_id]
                    self._pending[req_id] = (event, msg)
                    event.set()
            return
        # Async notification (subscription)
        method = msg.get("method")
        if method and method in self._subscriptions:
            params = msg.get("params", {})
            try:
                self._subscriptions[method](params)
            except Exception:
                log.exception("Error in subscription callback for %s", method)

    def send_request(self, method, params=None, timeout=5.0):
        req_id = self._next_id()
        msg = {"id": req_id, "method": method}
        if params:
            msg["params"] = params
        event = threading.Event()
        with self._lock:
            self._pending[req_id] = (event, None)
        raw = json.dumps(msg).encode() + b'\x03'
        try:
            self.sock.sendall(raw)
        except OSError as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise ConnectionError("Failed to send to Klipper: %s" % e)
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError("Klipper request timed out: %s" % method)
        with self._lock:
            _, result = self._pending.pop(req_id)
        if "error" in result:
            raise RuntimeError("Klipper error: %s" % result["error"])
        return result.get("result", {})

    def register_subscription(self, method, callback):
        self._subscriptions[method] = callback

    # High-level helpers

    def get_info(self):
        return self.send_request("info")

    def run_gcode(self, script, timeout=5.0):
        return self.send_request("gcode/script", {"script": script},
                                 timeout=timeout)

    def subscribe_objects(self, objects):
        return self.send_request("objects/subscribe", {"objects": objects})

    def query_objects(self, objects):
        return self.send_request("objects/query", {"objects": objects})

    def serial_bridge_send(self, bridge, data):
        return self.send_request("serial_bridge/send", {
            "bridge": bridge, "data": data
        })

    def serial_bridge_subscribe(self, bridge, response_method):
        return self.send_request("serial_bridge/subscribe", {
            "bridge": bridge,
            "response_template": {"method": response_method}
        })

    def emergency_stop(self):
        return self.send_request("emergency_stop")
