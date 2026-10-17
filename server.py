"""Jarri phone service: Zorin Connect over its session D-Bus service, for Jarri Workspace.

Backend only. Jarri's phone panel is a client of this service.
Zorin Connect keeps doing the KDE Connect protocol, pairing and clipboard sync.
This service only calls the Zorin service that is already running.

  GET  /status            -> {available, devices: [{id, name, kind, connected, paired, battery}]}
  POST /command {deviceId, command: {kind, ...}} -> 204 | {error}

Commands are fixed: ping, ring, clipboardPush, clipboardPull, shareText, shareUri, shareFile, sms.
Binds to 127.0.0.1 only and accepts requests from the same allowed origins as the other services.
"""

import json
import os
import re
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARRI_PHONE_PORT", "8782"))
SERVICE = "org.gnome.Shell.Extensions.ZorinConnect"
ROOT_PATH = "/org/gnome/Shell/Extensions/ZorinConnect"
DEVICE_IFACE = "org.gnome.Shell.Extensions.ZorinConnect.Device"
CALL_TIMEOUT_MS = 3000
ALLOWED_ORIGINS = {
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:8792",
    "http://127.0.0.1:8792",
}
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
DEVICE_ID = re.compile(r"^[A-Za-z0-9]+$")


class ZorinUnavailable(Exception):
    pass


def session_call(path, interface, method, params, reply_type):
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bus.call_sync(
            SERVICE, path, interface, method, params,
            GLib.VariantType.new(reply_type), Gio.DBusCallFlags.NONE,
            CALL_TIMEOUT_MS, None,
        )
    except GLib.Error as error:
        raise ZorinUnavailable(str(error))


def battery_for(path):
    # org.gtk.Actions.Describe("battery") -> (enabled, signature, [v]); the first v holds
    # (charging, icon, level, time).
    try:
        reply = session_call(path, "org.gtk.Actions", "Describe",
                             GLib.Variant("(s)", ("battery",)), "((bgav))")
    except ZorinUnavailable:
        return None
    _enabled, _signature, params = reply.unpack()[0]
    if not params:
        return None
    charging, _icon, level, _time = params[0]
    if level < 0:
        return None
    return {"charging": bool(charging), "level": int(level)}


def status():
    try:
        managed = session_call(ROOT_PATH, "org.freedesktop.DBus.ObjectManager",
                               "GetManagedObjects", None, "(a{oa{sa{sv}}})").unpack()[0]
    except ZorinUnavailable:
        return {"available": False, "devices": []}
    devices = []
    for path, interfaces in managed.items():
        props = interfaces.get(DEVICE_IFACE)
        if props is None:
            continue
        connected = bool(props.get("Connected", False))
        devices.append({
            "id": path.rsplit("/", 1)[-1],
            "name": str(props.get("Name", "")),
            "kind": str(props.get("Type", "")),
            "connected": connected,
            "paired": bool(props.get("Paired", False)),
            "battery": battery_for(path) if connected else None,
        })
    devices.sort(key=lambda d: d["name"])
    return {"available": True, "devices": devices}


def gvariant_string(text):
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def action_for(command):
    kind = command.get("kind")
    if kind == "ping":
        message = str(command.get("message") or "").strip()
        return "ping", f"[<{gvariant_string(message)}>]"
    if kind == "ring":
        return "ring", "[]"
    if kind == "clipboardPush":
        return "clipboardPush", "[]"
    if kind == "clipboardPull":
        return "clipboardPull", "[]"
    if kind == "shareText":
        return "shareText", f"[<{gvariant_string(str(command.get('text', '')))}>]"
    if kind == "shareUri":
        return "shareUri", f"[<{gvariant_string(str(command.get('uri', '')))}>]"
    if kind == "shareFile":
        return "share", "[]"
    if kind == "sms":
        return "sms", "[]"
    raise ValueError("unknown command")


def send(device_id, command):
    if not DEVICE_ID.match(device_id):
        raise ValueError("invalid device id")
    action, parameters = action_for(command)
    params = GLib.Variant.parse(
        GLib.VariantType.new("(sava{sv})"),
        f"('{action}', {parameters}, {{}})",
    )
    session_call(f"{ROOT_PATH}/Device/{device_id}", "org.gtk.Actions", "Activate",
                 params, "()")


class Handler(BaseHTTPRequestHandler):
    server_version = "jarri-phone"

    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return False
        return self.headers.get("Host") in ALLOWED_HOSTS

    def _send(self, status_code, body=None):
        self.send_response(status_code)
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        self._send(204)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        if urlparse(self.path).path != "/status":
            return self._send(404, {"error": "not found"})
        self._send(200, status())

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        if urlparse(self.path).path != "/command":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        try:
            send(str(req.get("deviceId", "")), req.get("command") or {})
        except (ValueError, ZorinUnavailable) as error:
            return self._send(400, {"error": str(error)})
        self._send(204)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"jarri phone service on http://{HOST}:{PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
