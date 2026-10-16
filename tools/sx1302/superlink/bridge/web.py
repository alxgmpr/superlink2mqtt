"""Bundled management UI and authenticated, bounded JSON API."""
from concurrent.futures import TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import signal
import threading
from urllib.parse import urlsplit

from .management import Management

log = logging.getLogger("superlink.web")
ASSETS = Path(__file__).with_name("frontend")
MAX_BODY = 128 * 1024


def is_ingress(config, peer):
    # Trust the TCP peer, never a client-controlled forwarding header.
    return config.ingress and peer == "172.30.32.2"


class ManagementServer:
    def __init__(self, runtime, config):
        self.management = Management(runtime)
        self.runtime = runtime
        self.config = config
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, format, *args):
                # Do not log request bodies, headers or URL query strings.
                pass

            def reply(self, status, body, content_type="application/json"):
                if content_type == "application/json":
                    body = json.dumps(body, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'")
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                if is_ingress(owner.config, self.client_address[0]):
                    return True
                supplied = self.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied.encode(), f"Bearer {owner.config.token}".encode()):
                    self.reply(401, {"error": "Enter a valid access token"})
                    return False
                origin = self.headers.get("Origin")
                if origin:
                    try:
                        parsed = urlsplit(origin)
                        same_origin = (parsed.scheme in ("http", "https")
                                       and parsed.netloc == self.headers.get("Host"))
                    except ValueError:
                        same_origin = False
                    if not same_origin:
                        self.reply(403, {"error": "Cross-origin requests are not allowed"})
                        return False
                return True

            def execute(self, operation):
                try:
                    future = owner.runtime.submit_management(operation)
                except queue.Full:
                    self.reply(503, {"error": "Gateway busy; try again shortly"})
                    return
                try:
                    result = future.result(timeout=5)
                except TimeoutError:
                    cancelled = future.cancel()
                    self.reply(503, {"error": "Gateway unavailable; request cancelled" if cancelled else
                                    "Request still running; check device state before retrying"})
                except ValueError as exc:
                    self.reply(400, {"error": str(exc)})
                except Exception:
                    log.error("Management operation failed")
                    self.reply(500, {"error": "Operation failed; inspect gateway storage and service health"})
                else:
                    self.reply(200, result)

            def do_GET(self):
                if owner.config.ingress and not is_ingress(owner.config, self.client_address[0]):
                    self.reply(403, {"error": "Use Home Assistant ingress"})
                    return
                path = urlsplit(self.path).path
                if path == "/api/session":
                    self.reply(200, {"ingress": is_ingress(owner.config, self.client_address[0])})
                    return
                if path == "/favicon.ico":
                    self.reply(204, b"", "image/x-icon")
                    return
                if path == "/api/state":
                    if self.authorized():
                        self.execute(owner.management.snapshot)
                    return
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/request.js": ("request.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path not in assets:
                    self.reply(404, {"error": "Not found"})
                    return
                name, mime = assets[path]
                self.reply(200, (ASSETS / name).read_bytes(), mime)

            def do_POST(self):
                if owner.config.ingress and not is_ingress(owner.config, self.client_address[0]):
                    self.reply(403, {"error": "Use Home Assistant ingress"})
                    return
                if not self.authorized():
                    return
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self.reply(415, {"error": "Expected application/json"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                        self.reply(413, {"error": "Request must be between 1 and 131072 bytes"})
                        return
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, UnicodeError, RecursionError):
                    self.reply(400, {"error": "Expected a JSON object"})
                    return
                path = urlsplit(self.path).path
                if path.startswith("/api/devices/") and path.endswith("/command"):
                    parts = path.split("/")
                    if len(parts) != 5:
                        self.reply(404, {"error": "Not found"})
                        return
                    self.execute(lambda: owner.management.command(parts[3], body.get("command")))
                else:
                    self.reply(404, {"error": "Not found"})

        self.server = ThreadingHTTPServer((config.host, config.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name="superlink-web")

    @property
    def address(self):
        return self.server.server_address

    def start(self):
        self.thread.start()
        log.info("Management UI listening on %s:%s", *self.address)

    def stop(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()


def run_demo():
    """A clearly labelled, read-only demo that never loads the native HAL."""
    from .config import RuntimeConfig, WebConfig
    from .events import DeviceStateEvent, PropertyEvent, LinkSignal
    from .runtime import BridgeRuntime
    from .store import DeviceRecord, InMemoryDeviceStore

    class DemoRadio:
        def start(self): pass
        def stop(self): pass
        def receive(self): return []
        def version(self): return "demo — no radio"
        def send(self, *args, **kwargs): raise RuntimeError("Demo cannot transmit")

    token = os.environ.get("SUPERLINK_WEB_TOKEN") or secrets.token_urlsafe(32)
    runtime = BridgeRuntime(RuntimeConfig(gw_mac=bytes.fromhex("020000000001")),
                            DemoRadio(), InMemoryDeviceStore())
    runtime.demo = True
    host = os.environ.get("SUPERLINK_DEMO_HOST", "127.0.0.1")
    web = ManagementServer(runtime, WebConfig(token=token, host=host))
    for suffix, name, property_name, value, unit in (
            (1, "Workshop door", "DOOR", "closed", None),
            (2, "Hallway motion", "MOTION", False, None),
            (3, "Utility room", "TEMPERATURE", 21.4, "°C")):
        mac = bytes.fromhex(f"0200000000{suffix:02x}")
        runtime.store.save(DeviceRecord(mac=mac, name=name, adopted=True))
        web.management.on_event(DeviceStateEvent(mac, "active"))
        web.management.on_event(PropertyEvent(mac, 1, property_name, 0, b"", value, unit, True))
        web.management.on_event(PropertyEvent(mac, 2, "BATTERY", 0, b"", 96 - suffix * 7, "%", True))
        web.management.on_event(LinkSignal(mac, -54 - suffix * 5, 8))
    print(f"Demo: http://127.0.0.1:8099\nAccess token: {token}", flush=True)
    web.start()
    previous_sigterm = signal.signal(signal.SIGTERM, lambda *_: runtime.stop())
    try:
        runtime.run()
    finally:
        web.stop()
        signal.signal(signal.SIGTERM, previous_sigterm)
