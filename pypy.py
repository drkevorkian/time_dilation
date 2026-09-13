#!/usr/bin/env python3
"""
Author : Andrew 
Git    : DrKevorkian
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from io import BytesIO

try:
    import tkinter as tk
    from tkinter import BOTH, LEFT, RIGHT, X, Y, StringVar, messagebox, ttk
except Exception:  # headless/server-only fallback
    tk = None
    ttk = None
    messagebox = None
    StringVar = None
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_version import APP_VERSION
from rental_core import CLIENT_ACTIONS, Database, RentalConflictError, ValidationError
from web_gateway import WebGatewayManager
from Vinfinder.ingestion import MobilePairingStore, VinPhotoStore, iso_utc


def detect_vin_photo(path: Path, expected_vin_suffixes: set[str] | None = None):
    """Try five increasingly aggressive, non-repeating OCR stages."""
    from Vinfinder.vin_ocr import find_best_vin_progressive

    return find_best_vin_progressive(
        path,
        max_attempts=5,
        expected_vin_suffixes=expected_vin_suffixes,
    )


def lookup_vin_details(vin: str) -> dict:
    """Decode one VIN through the existing VinLookup/NHTSA vPIC client."""
    from VinLookup.vin_core import lookup_vpic
    return lookup_vpic(vin)


def resolve_verified_vin_machine(httpd, vin: str) -> tuple[dict, bool]:
    """Resolve a verified VIN, promoting Fleet only on its first full match."""
    existing=httpd.db.find_machine_by_full_vin(vin)
    if existing:return existing,False
    decoded=lookup_vin_details(vin)
    return httpd.db.sync_machine_from_vin_lookup(vin,decoded),True


def import_pending_vin_photos(httpd) -> None:
    """Finish database ingestion for originals accepted before this integration existed."""
    if not httpd.vin_photos:
        return
    for row,path in httpd.vin_photos.pending_photos():
        try:
            content=path.read_bytes()
            machine,promoted=resolve_verified_vin_machine(httpd,row.vin)
            stored=httpd.db.store_mobile_before_photo(int(machine["id"]),row.vin,asdict(row),content)
            path.unlink(missing_ok=True)
            httpd.activity.add_event(
                f"Recovered VIN camera photo into image DB #{stored['image_database_id']} "
                f"for Fleet #{machine['id']} ({row.vin})"+("; Fleet VIN promoted" if promoted else "")
            )
        except Exception as exc:
            httpd.activity.add_event(
                f"VIN camera recovery retained {path.name} on disk: {type(exc).__name__}: {exc}"
            )

HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DISCOVERY_PORT = 8766
DISCOVER_MAGIC = b"RENTAL_CALENDAR_DISCOVER"
ADMIN_HEADER = "X-Admin-Key"
ACTIVE_DEVICE_SECONDS = 12
POLL_INTERVAL_SECONDS = 2.5
WIRE_OVERHEAD_ESTIMATE_PER_REQUEST = 512  # conservative TCP/IP/link framing allowance


def default_data_dir() -> Path:
    """Return a sensible per-platform server data directory."""
    override = os.environ.get("RENTAL_CALENDAR_DATA")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", Path.home())) / "RentalCalendarPython"
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / "RentalCalendarPython"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "RentalCalendarPython"
    return Path.home() / ".local" / "share" / "RentalCalendarPython"


def sys_platform() -> str:
    return sys.platform


def set_server_process_identity() -> None:
    """Give Windows an application identity instead of a generic Python label."""
    threading.current_thread().name = "Rental Calendar Server"
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "RentalCalendar.Server"
        )
        ctypes.windll.kernel32.SetConsoleTitleW(
            f"Rental Calendar Server v{APP_VERSION}"
        )
    except (AttributeError, OSError):
        pass


def default_db_path() -> Path:
    override = os.environ.get("RENTAL_CALENDAR_DB")
    if override:
        return Path(override).expanduser()
    return default_data_dir() / "rental_calendar.db"


def default_config_path(db_path: Path) -> Path:
    override = os.environ.get("RENTAL_CALENDAR_SERVER_CONFIG")
    if override:
        return Path(override).expanduser()
    return db_path.parent / "server.json"


def load_or_create_admin_key(config_path: Path, cli_key: str | None = None) -> tuple[str, bool]:
    """Load the remote-admin key, generating and persisting one on first run.

    Returns (key, created_new_key).
    """
    env_key = os.environ.get("RENTAL_CALENDAR_ADMIN_KEY", "").strip()
    requested = (cli_key or env_key).strip()
    config: dict = {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}

    existing = str(config.get("admin_key", "")).strip()
    key = requested or existing
    created = False
    if not key:
        key = secrets.token_urlsafe(24)
        created = True

    # Persist explicit/generated keys so a headless server can be restarted
    # without changing the credential used by remote Admin PCs.
    if key != existing or not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config["admin_key"] = key
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        if os.name != "nt":
            try:
                config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
    return key, created


# ---------------------------------------------------------------------------
# Network/address helpers used by the permanent server dashboard.
# ---------------------------------------------------------------------------

def _usable_ipv4(value: str) -> str | None:
    value = str(value or "").strip().split("%")[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address):
        return None
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
        return None
    return str(addr)


def _command_ipv4_candidates() -> list[str]:
    """Best-effort interface address discovery without third-party packages.

    Parse only interface-address fields.  In particular, do not collect every
    IPv4-looking value from tools such as ``ip addr`` because that output also
    contains broadcast addresses.
    """
    commands: list[list[str]] = []
    if os.name == "nt":
        commands.append(["ipconfig"])
    else:
        commands.extend([["hostname", "-I"], ["ip", "-4", "addr", "show"], ["ifconfig"]])

    found: list[str] = []
    generic_ipv4 = r"(?:\d{1,3}\.){3}\d{1,3}"
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue

        output = result.stdout or ""
        name = command[0].lower()
        if name == "hostname":
            matches = re.findall(rf"(?<![\d.])({generic_ipv4})(?![\d.])", output)
        elif name == "ip":
            matches = re.findall(rf"\binet\s+({generic_ipv4})(?:/\d+)?", output)
        elif name == "ifconfig":
            matches = re.findall(rf"\binet\s+(?:addr:)?({generic_ipv4})", output)
        else:  # Windows ipconfig
            matches = []
            for line in output.splitlines():
                if "IPv4" not in line:
                    continue
                match = re.search(rf"({generic_ipv4})", line)
                if match:
                    matches.append(match.group(1))

        for match in matches:
            candidate = _usable_ipv4(match)
            if candidate and candidate not in found:
                found.append(candidate)
    return found


def get_lan_ipv4_addresses() -> list[str]:
    """Return usable IPv4 addresses, with the default-route address first.

    The first address is normally the one another machine on the same LAN should
    use.  All detected addresses are still shown because VPNs, Wi-Fi/Ethernet,
    virtual adapters, or multi-homed servers can legitimately have more than one.
    """
    preferred: list[str] = []
    candidates: list[str] = []

    # UDP connect does not need a successful remote connection; it asks the OS
    # which local interface it would use for that route.
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80), ("192.0.2.1", 80)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(target)
            candidate = _usable_ipv4(sock.getsockname()[0])
            if candidate and candidate not in preferred:
                preferred.append(candidate)
        except OSError:
            pass
        finally:
            sock.close()

    for host in (socket.gethostname(), socket.getfqdn()):
        try:
            for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
                candidate = _usable_ipv4(info[4][0])
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
        except OSError:
            pass

    for candidate in _command_ipv4_candidates():
        if candidate not in candidates:
            candidates.append(candidate)

    ordered: list[str] = []
    for candidate in preferred + candidates:
        if candidate not in ordered:
            ordered.append(candidate)

    # Keep the likely real LAN addresses ahead of APIPA/link-local adapters.
    regular = []
    link_local = []
    for value in ordered:
        try:
            addr = ipaddress.ip_address(value)
            (link_local if addr.is_link_local else regular).append(value)
        except ValueError:
            regular.append(value)
    return regular + link_local


def connection_urls(bind_host: str, port: int) -> list[str]:
    """Return addresses another app can actually use to reach this server."""
    host = str(bind_host or "").strip()
    if host in {"", "0.0.0.0", "::"}:
        return [f"http://{ip}:{port}" for ip in get_lan_ipv4_addresses()]
    if host in {"127.0.0.1", "localhost", "::1"}:
        return [f"http://127.0.0.1:{port}"]
    return [f"http://{host}:{port}"]


class ServerActivity:
    """Thread-safe request/device history for the terminal dashboard."""

    def __init__(self) -> None:
        self.started_at = datetime.now()
        self._lock = threading.RLock()
        self.total_requests = 0
        self.total_http_rx = 0
        self.total_http_tx = 0
        self.total_wire_estimate = 0
        self.total_remote_wire_estimate = 0
        self.traffic_samples: deque[tuple[float, int]] = deque(maxlen=20000)
        self.remote_traffic_samples: deque[tuple[float, int]] = deque(maxlen=20000)
        self.path_counts: dict[str, int] = {}
        self.clients: dict[str, dict] = {}
        self.events: deque[tuple[datetime, str]] = deque(maxlen=12)

    def add_event(self, message: str) -> None:
        with self._lock:
            self.events.appendleft((datetime.now(), str(message)))

    def record_request(self, ip: str, role: str, method: str, path: str, http_bytes: int = 0) -> None:
        now = datetime.now()
        clean_path = str(path or "/")
        with self._lock:
            self.total_requests += 1
            request_bytes = max(0, int(http_bytes))
            self.total_http_rx += request_bytes
            # Count a deliberately conservative 512 bytes/request for TCP/IP/link
            # framing, handshakes and teardown beyond the HTTP bytes we can see.
            request_wire = request_bytes + WIRE_OVERHEAD_ESTIMATE_PER_REQUEST
            self.total_wire_estimate += request_wire
            self.traffic_samples.append((time.monotonic(), request_wire))
            if ip not in {"127.0.0.1", "::1"}:
                self.total_remote_wire_estimate += request_wire
                self.remote_traffic_samples.append((time.monotonic(), request_wire))
            request_key = f"{method} {clean_path}"
            self.path_counts[request_key] = int(self.path_counts.get(request_key, 0)) + 1
            row = self.clients.setdefault(
                ip,
                {
                    "ip": ip,
                    "role": role,
                    "first_seen": now,
                    "last_seen": now,
                    "requests": 0,
                    "last_request": "",
                    "last_status": "",
                },
            )
            # Prefer the strongest role observed for a device.
            rank = {"CLIENT": 1, "LOCAL": 2, "ADMIN": 3}
            if rank.get(role, 0) >= rank.get(str(row.get("role", "")), 0):
                row["role"] = role
            row["last_seen"] = now
            row["requests"] = int(row.get("requests", 0)) + 1
            row["last_request"] = f"{method} {clean_path}"

    def record_response(self, ip: str, status: int, http_bytes: int = 0) -> None:
        with self._lock:
            response_bytes = max(0, int(http_bytes))
            self.total_http_tx += response_bytes
            self.total_wire_estimate += response_bytes
            self.traffic_samples.append((time.monotonic(), response_bytes))
            if ip not in {"127.0.0.1", "::1"}:
                self.total_remote_wire_estimate += response_bytes
                self.remote_traffic_samples.append((time.monotonic(), response_bytes))
            row = self.clients.get(ip)
            if row:
                row["last_status"] = str(int(status))
                row["last_seen"] = datetime.now()

    def record_discovery(self, ip: str) -> None:
        self.record_request(ip, "CLIENT", "UDP", f"discovery:{DISCOVERY_PORT}")

    def snapshot(self) -> dict:
        with self._lock:
            now_mono = time.monotonic()
            cutoff = now_mono - 60.0
            while self.traffic_samples and self.traffic_samples[0][0] < cutoff:
                self.traffic_samples.popleft()
            while self.remote_traffic_samples and self.remote_traffic_samples[0][0] < cutoff:
                self.remote_traffic_samples.popleft()
            last_60_wire = sum(value for _when, value in self.traffic_samples)
            last_60_remote_wire = sum(value for _when, value in self.remote_traffic_samples)
            return {
                "started_at": self.started_at,
                "total_requests": self.total_requests,
                "total_http_rx": self.total_http_rx,
                "total_http_tx": self.total_http_tx,
                "total_wire_estimate": self.total_wire_estimate,
                "total_remote_wire_estimate": self.total_remote_wire_estimate,
                "last_60_wire": last_60_wire,
                "last_60_remote_wire": last_60_remote_wire,
                "path_counts": dict(self.path_counts),
                "clients": [dict(row) for row in self.clients.values()],
                "events": list(self.events),
            }



class AdminAccessStore:
    """Server-owned per-computer Admin approvals.

    The master Admin Key continues to work.  Approved Admin PCs receive their
    own random token, allowing one workstation to be revoked without rotating
    the master credential everywhere.
    """

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._approved: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        rows = cfg.get("approved_admins", {})
        if isinstance(rows, dict):
            self._approved = {
                str(token): dict(meta)
                for token, meta in rows.items()
                if str(token).strip() and isinstance(meta, dict)
            }

    def _persist(self) -> None:
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        cfg["approved_admins"] = self._approved
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        if os.name != "nt":
            try:
                self.config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    def is_authorized(self, token: str) -> bool:
        token = str(token or "").strip()
        if not token:
            return False
        with self._lock:
            return token in self._approved

    def request(self, machine_name: str, instance_id: str, ip: str) -> dict:
        machine_name = str(machine_name or "").strip() or "Unknown computer"
        instance_id = str(instance_id or "").strip()
        if not instance_id:
            raise ValidationError("Admin request is missing its computer identity.")
        now = datetime.now().replace(microsecond=0).isoformat()
        with self._lock:
            # Reuse an outstanding request from the same Admin instance so normal
            # reconnect attempts do not flood the approval list.
            for request_id, row in self._pending.items():
                if row.get("instance_id") == instance_id and row.get("status") == "pending":
                    row["ip"] = ip
                    row["machine_name"] = machine_name
                    row["last_seen"] = now
                    return {"request_id": request_id, "status": "pending"}

            request_id = secrets.token_urlsafe(24)
            self._pending[request_id] = {
                "request_id": request_id,
                "machine_name": machine_name,
                "instance_id": instance_id,
                "ip": ip,
                "requested_at": now,
                "last_seen": now,
                "status": "pending",
                "token": "",
            }
            # Keep the in-memory queue bounded on a LAN.
            if len(self._pending) > 100:
                oldest = sorted(
                    self._pending.values(),
                    key=lambda r: r.get("requested_at", ""),
                )
                for row in oldest[: len(self._pending) - 100]:
                    if row.get("status") != "pending":
                        self._pending.pop(row["request_id"], None)
            return {"request_id": request_id, "status": "pending"}

    def status(self, request_id: str, ip: str | None = None) -> dict:
        with self._lock:
            row = self._pending.get(str(request_id or ""))
            if not row:
                return {"status": "unknown"}
            if ip:
                row["last_seen"] = datetime.now().replace(microsecond=0).isoformat()
            payload = {"status": row.get("status", "unknown")}
            if row.get("status") == "approved":
                # The one-time token handoff goes only back to the IP that
                # originated this approval request. After handoff, the random
                # per-computer token itself is the credential and may survive DHCP changes.
                if ip and row.get("ip") and str(ip) != str(row.get("ip")):
                    return {"status": "unknown"}
                payload["admin_token"] = row.get("token", "")
                payload["machine_name"] = row.get("machine_name", "")
            elif row.get("status") == "denied":
                payload["machine_name"] = row.get("machine_name", "")
            return payload

    def approve(self, request_id: str) -> dict:
        with self._lock:
            row = self._pending.get(str(request_id or ""))
            if not row:
                raise ValidationError("Admin access request no longer exists.")
            if row.get("status") == "approved" and row.get("token"):
                return dict(row)
            token = secrets.token_urlsafe(32)
            now = datetime.now().replace(microsecond=0).isoformat()
            meta = {
                "machine_name": row.get("machine_name", "Unknown computer"),
                "instance_id": row.get("instance_id", ""),
                "approved_at": now,
                "last_ip": row.get("ip", ""),
            }
            self._approved[token] = meta
            row["status"] = "approved"
            row["token"] = token
            row["approved_at"] = now
            self._persist()
            return dict(row)

    def deny(self, request_id: str) -> dict:
        with self._lock:
            row = self._pending.get(str(request_id or ""))
            if not row:
                raise ValidationError("Admin access request no longer exists.")
            row["status"] = "denied"
            row["token"] = ""
            return dict(row)

    def revoke(self, token: str) -> bool:
        with self._lock:
            existed = self._approved.pop(str(token or ""), None) is not None
            if existed:
                self._persist()
            return existed

    def pending_rows(self) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self._pending.values()
                if row.get("status") == "pending"
            ]

    def approved_rows(self) -> list[dict]:
        with self._lock:
            result = []
            for token, meta in self._approved.items():
                row = dict(meta)
                row["token"] = token
                result.append(row)
            return result



class RentalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, db: Database, admin_key: str, access_store: AdminAccessStore | None = None):
        super().__init__(server_address, handler_class)
        self.db = db
        self.admin_key = admin_key
        self.access_store = access_store
        self.activity = ServerActivity()
        self.dashboard_active = False
        self.database_fault: list[Path] = []
        self.database_maintenance = False
        self.web_gateways = None
        self.config_path = None
        self.mobile_pairing = None
        self.vin_photos = None


class Handler(BaseHTTPRequestHandler):
    server: RentalHTTPServer

    def log_message(self, fmt: str, *args) -> None:
        # The live dashboard owns stdout while running.  Keep the old log format
        # for tests/embedded use where no dashboard has been started.
        if not getattr(self.server, "dashboard_active", False):
            print(f"[{self.log_date_time_string()}] {self.client_address[0]} {fmt % args}")

    def _request_role(self) -> str:
        supplied = str(self.headers.get(ADMIN_HEADER, "")).strip()
        if supplied and (
            hmac.compare_digest(supplied, self.server.admin_key)
            or (self.server.access_store is not None and self.server.access_store.is_authorized(supplied))
        ):
            return "ADMIN"
        if self.client_address[0] in {"127.0.0.1", "::1"}:
            return "LOCAL"
        return "CLIENT"

    def _begin_request(self) -> None:
        path = urlparse(self.path).path or "/"
        try:
            header_bytes = len(self.raw_requestline) + len(self.headers.as_bytes())
        except Exception:
            header_bytes = 256
        try:
            body_bytes = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            body_bytes = 0
        self.server.activity.record_request(
            self.client_address[0], self._request_role(), self.command, path, header_bytes + body_bytes
        )

    def _json(self, status: int, payload: dict | list | None = None) -> None:
        raw = json.dumps(
            payload if payload is not None else {}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        body = raw
        encoding = ""
        if len(raw) >= 512 and "gzip" in str(self.headers.get("Accept-Encoding", "")).lower():
            compressed = gzip.compress(raw, compresslevel=5)
            if len(compressed) < len(raw):
                body = compressed
                encoding = "gzip"

        # Approximate HTTP response bytes including the headers emitted below and
        # BaseHTTPRequestHandler's Server/Date headers.  The dashboard labels its
        # total as a conservative estimate rather than pretending to be a packet capture.
        response_http_bytes = len(body) + 256
        self.server.activity.record_response(self.client_address[0], int(status), response_http_bytes)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.server.activity.record_response(self.client_address[0], int(status), len(body) + 200)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _mobile_authorized(self) -> bool:
        supplied = str(self.headers.get("X-Mobile-Token", "")).strip()
        return bool(self.server.mobile_pairing and self.server.mobile_pairing.authorize(supplied))

    def _pairing_page(self) -> None:
        if not self._is_local():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Open the pairing page on the server computer."})
            return
        code, expires = self.server.mobile_pairing.create_code()
        urls = connection_urls(self.server.server_address[0], int(self.server.server_address[1]))
        server_url = urls[0] if urls else f"http://{socket.gethostname()}:{self.server.server_address[1]}"
        payload = json.dumps({"v": 1, "server": server_url, "code": code}, separators=(",", ":"))
        qr = ""
        try:
            import qrcode
            import qrcode.image.svg
            image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=3)
            stream = BytesIO()
            image.save(stream)
            qr = base64.b64encode(stream.getvalue()).decode("ascii")
        except Exception:
            pass
        qr_html = f'<img alt="Pairing QR code" src="data:image/svg+xml;base64,{qr}">' if qr else "<p><b>QR support is not installed.</b> Use the manual values below.</p>"
        html = f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Pair VIN Camera</title><style>body{{font:18px system-ui;max-width:680px;margin:40px auto;padding:20px;color:#17212b}}main{{border:1px solid #ccd5df;border-radius:18px;padding:28px;text-align:center}}img{{width:min(360px,90%);height:auto}}code{{display:block;overflow-wrap:anywhere;background:#f2f5f7;padding:12px;border-radius:8px}}small{{color:#586879}}</style>
<main><h1>Pair VIN Camera</h1><p>In the VIN Camera app, tap <b>Pair server</b> and scan this code.</p>{qr_html}<p>Server</p><code>{server_url}</code><p>One-time code</p><code>{code}</code><small>Expires {expires}. This page is available only on the server computer.</small></main>"""
        self._bytes(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValidationError("Invalid JSON request body.") from exc
        if not isinstance(data, dict):
            raise ValidationError("JSON request body must be an object.")
        return data

    def _is_local(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def _admin_authorized(self) -> bool:
        # Preserve easy single-PC operation: localhost remains trusted.
        if self._is_local():
            return True
        supplied = str(self.headers.get(ADMIN_HEADER, "")).strip()
        if not supplied:
            return False
        if hmac.compare_digest(supplied, self.server.admin_key):
            return True
        return bool(self.server.access_store and self.server.access_store.is_authorized(supplied))

    def _require_admin(self) -> bool:
        if not self._admin_authorized():
            self.server.activity.add_event(
                f"Blocked unauthorized Admin write/check from {self.client_address[0]}"
            )
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "Admin authorization required. Enter this server's Admin Key in Admin Settings.",
                    "code": "admin_auth_required",
                },
            )
            return False
        return True

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, RentalConflictError):
            self._json(HTTPStatus.CONFLICT, {"error": str(exc), "conflict": exc.conflict})
        elif isinstance(exc, (ValidationError, ValueError)):
            if urlparse(self.path).path.rstrip("/") == "/api/mobile/photos":
                self.server.activity.add_event(
                    f"VIN camera upload rejected from {self.client_address[0]}: {exc}"
                )
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        else:
            self.server.activity.add_event(
                f"ERROR from {self.client_address[0]}: {type(exc).__name__}: {exc}"
            )
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})

    def do_GET(self) -> None:
        self._begin_request()
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if (self.server.database_fault or self.server.database_maintenance) and path != "/api/health":
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,{"error":"Server database protection is active; a database file is missing."})
                return

            if path == "/mobile/setup":
                self._pairing_page()
                return

            if path == "/api/mobile/status":
                if not self._mobile_authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Mobile device is not paired."})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "server_time": iso_utc(), "hostname": socket.gethostname()})
                return

            if path == "/api/poll":
                # High-frequency endpoint: intentionally tiny.  Clients ask only
                # whether the database changed; full data moves only after a change.
                self._json(
                    HTTPStatus.OK,
                    {"v": self.server.db.version(), "t": datetime.now().replace(microsecond=0).isoformat(), "d": date.today().isoformat()},
                )
                return

            if path == "/api/admin-snapshot":
                if not self._require_admin():
                    return
                self._json(
                    HTTPStatus.OK,
                    {
                        "version": self.server.db.version(),
                        "server_time": datetime.now().replace(microsecond=0).isoformat(),
                        "today": date.today().isoformat(),
                        "settings": self.server.db.get_settings(),
                        "machines": self.server.db.list_machines(include_inactive=True),
                        "rentals": self.server.db.list_rentals(),
                    },
                )
                return

            if path == "/api/health":
                fault=[str(path) for path in self.server.database_fault]
                unavailable=bool(fault or self.server.database_maintenance)
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "application_version": APP_VERSION,
                        "version": None if unavailable else self.server.db.version(),
                        "database_available": not unavailable,
                        "database_maintenance":self.server.database_maintenance,
                        "missing_database_files": fault,
                        "server_time": datetime.now().replace(microsecond=0).isoformat(),
                        "remote_admin": True,
                        "hostname": socket.gethostname(),
                        "port": self.server.server_address[1],
                        "process_id": os.getpid(),
                    },
                )
                return

            if path == "/api/admin-access-status":
                request_id = qs.get("request_id", [""])[0]
                if self.server.access_store is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "unavailable"})
                else:
                    self._json(
                        HTTPStatus.OK,
                        self.server.access_store.status(request_id, self.client_address[0]),
                    )
                return

            if path == "/api/admin-check":
                if not self._require_admin():
                    return
                self._json(HTTPStatus.OK, {"ok": True, "admin": True})
                return

            if path == "/api/settings":
                self._json(HTTPStatus.OK, {"settings": self.server.db.get_settings()})
                return

            if path == "/api/operations-dashboard":
                if not self._require_admin():
                    return
                self._json(HTTPStatus.OK, {"dashboard": self.server.db.operations_dashboard()})
                return

            if path == "/api/server-statistics":
                if not self._require_admin():
                    return
                self._json(HTTPStatus.OK, {"statistics": self.server.db.database_statistics()})
                return

            if path == "/api/reports/profit-loss":
                if not self._require_admin():
                    return
                start = qs.get("start", [date.today().replace(month=1, day=1).isoformat()])[0]
                end = qs.get("end", [date.today().isoformat()])[0]
                self._json(HTTPStatus.OK, {"report": self.server.db.profit_loss_report(start, end)})
                return

            if path == "/api/reports/utilization":
                if not self._require_admin():
                    return
                start = qs.get("start", [date.today().replace(month=1, day=1).isoformat()])[0]
                end = qs.get("end", [date.today().isoformat()])[0]
                self._json(HTTPStatus.OK, {"machines": self.server.db.utilization_report(start, end)})
                return

            if path == "/api/finance/pricing":
                if not self._require_admin(): return
                self._json(HTTPStatus.OK, {"machines":self.server.db.list_machine_pricing(),"discounts":self.server.db.list_duration_discounts()})
                return

            if path.startswith("/api/records/"):
                if not self._require_admin():
                    return
                table = path.split("/")[3]
                self._json(HTTPStatus.OK, {"records": self.server.db.list_records(table)})
                return

            if path == "/api/export":
                if not self._require_admin():
                    return
                table = qs.get("table", ["reservations"])[0]
                self._json(HTTPStatus.OK, {"table": table, "csv": self.server.db.export_csv(table)})
                return

            if path == "/api/search":
                if not self._require_admin():
                    return
                self._json(HTTPStatus.OK, self.server.db.search_business(qs.get("q", [""])[0]))
                return

            if path == "/api/machines":
                include_inactive = qs.get("include_inactive", ["1"])[0] != "0"
                self._json(HTTPStatus.OK, {"machines": self.server.db.list_machines(include_inactive)})
                return

            if path.startswith("/api/machines/") and path.endswith("/photos"):
                if not self._require_admin(): return
                machine_id=int(path.split("/")[3])
                self._json(HTTPStatus.OK,{"photos":self.server.db.list_machine_photos(machine_id)})
                return

            if path.startswith("/api/rentals/") and path.endswith("/inspection-photos"):
                if not self._require_admin(): return
                rental_id=int(path.split("/")[3])
                self._json(HTTPStatus.OK,{"photos":self.server.db.list_inspection_photos(rental_id)})
                return

            if path == "/api/rentals":
                start = qs.get("start", [None])[0]
                end = qs.get("end", [None])[0]
                include_cancelled = qs.get("include_cancelled", ["1"])[0] != "0"
                self._json(
                    HTTPStatus.OK,
                    {"rentals": self.server.db.list_rentals(start, end, include_cancelled)},
                )
                return

            if path == "/api/reservations":
                start = qs.get("start", [None])[0]
                end = qs.get("end", [None])[0]
                include_cancelled = qs.get("include_cancelled", ["1"])[0] != "0"
                self._json(
                    HTTPStatus.OK,
                    {"reservations": self.server.db.list_reservations(start, end, include_cancelled)},
                )
                return

            if path.startswith("/api/reservations/"):
                parts = path.split("/")
                if len(parts) == 4:
                    reservation = self.server.db.get_reservation(int(parts[3]))
                    if reservation is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "Reservation not found."})
                    else:
                        self._json(HTTPStatus.OK, {"reservation": reservation})
                    return

            if path == "/api/snapshot":
                start = qs.get("start", [None])[0]
                end = qs.get("end", [None])[0]
                if not start or not end:
                    raise ValidationError("snapshot requires start and end query parameters")
                self._json(HTTPStatus.OK, self.server.db.snapshot(start, end))
                return

            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        self._begin_request()
        try:
            if self.server.database_fault or self.server.database_maintenance:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,{"error":"Server database protection is active; a database file is missing."})
                return
            path = urlparse(self.path).path.rstrip("/")

            if path == "/api/mobile/pair":
                data = self._read_json()
                if not self.server.mobile_pairing:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Mobile pairing is unavailable."})
                    return
                token = self.server.mobile_pairing.exchange(data.get("code", ""), data.get("device_name", ""))
                if not token:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Pairing code is invalid, expired, or already used."})
                    return
                self.server.activity.add_event(f"Paired VIN camera {data.get('device_name') or 'device'} ({self.client_address[0]})")
                self._json(HTTPStatus.CREATED, {"token": token})
                return

            if path == "/api/mobile/photos":
                if not self._mobile_authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Mobile device is not paired."})
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0 or length > 30 * 1024 * 1024:
                    raise ValidationError("Image is empty or exceeds the 30 MB limit.")
                content = self.rfile.read(length)
                row, duplicate = self.server.vin_photos.store(
                    upload_id=str(self.headers.get("X-Upload-Id", "")),
                    batch_id=str(self.headers.get("X-Batch-Id", "")),
                    sequence=int(self.headers.get("X-Photo-Sequence", "-1")),
                    captured_at=str(self.headers.get("X-Captured-At", "")),
                    filename=str(self.headers.get("X-Original-Filename", "photo.jpg")),
                    content=content,
                )
                machine,promoted=resolve_verified_vin_machine(self.server,row.vin)
                database_photo=self.server.db.store_mobile_before_photo(int(machine["id"]),row.vin,asdict(row),content)
                disk_copy=self.server.vin_photos.root/row.vin/row.filename
                try:disk_copy.unlink(missing_ok=True)
                except OSError:pass
                self.server.activity.add_event(
                    f"VIN camera stored before photo in image DB for Fleet #{machine['id']} "
                    f"({row.vin}; {machine.get('year') or ''} {machine.get('make') or ''} {machine.get('model') or ''})"
                    +("; Fleet VIN promoted" if promoted else "")
                )
                self._json(HTTPStatus.OK if duplicate else HTTPStatus.CREATED, {
                    "photo":asdict(row),"database_photo":database_photo,"machine":machine,"duplicate":duplicate,
                })
                return

            data = self._read_json()
            parts = path.split("/")

            if path == "/api/admin-access-request":
                if self.server.access_store is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Admin approval service is unavailable."})
                    return
                request = self.server.access_store.request(
                    data.get("machine_name", ""),
                    data.get("instance_id", ""),
                    self.client_address[0],
                )
                self.server.activity.add_event(
                    f"Admin approval requested by {data.get('machine_name') or 'Unknown'} ({self.client_address[0]})"
                )
                self._json(HTTPStatus.ACCEPTED, request)
                return

            # Client PCs retain only the narrow operational-state write surface.
            if len(parts) == 5 and parts[:3] == ["", "api", "reservations"] and parts[4] == "client-action":
                reservation_id = int(parts[3])
                action = str(data.get("action", "")).strip().lower()
                if action not in {"loaded", "returned_good", "late"}:
                    raise ValidationError("Reservation group action is not allowed.")
                result = self.server.db.apply_reservation_action(reservation_id, action)
                self.server.activity.add_event(
                    f"Client {self.client_address[0]}: reservation #{reservation_id} -> {action.replace('_', ' ').upper()} "
                    f"({len(result.get('updated_ids', []))} updated, {len(result.get('skipped_ids', []))} skipped)"
                )
                self._json(HTTPStatus.OK, result)
                return

            if len(parts) == 5 and parts[:3] == ["", "api", "rentals"] and parts[4] == "client-action":
                rental_id = int(parts[3])
                action = str(data.get("action", "")).strip().lower()
                if action not in CLIENT_ACTIONS:
                    raise ValidationError("Invalid client rental action.")
                rental = self.server.db.apply_client_action(
                    rental_id, action, data.get("missing_fuel_gallons")
                )
                detail = action.replace("_", " ").upper()
                if action == "returned_missing_fuel":
                    detail += f" ({data.get('missing_fuel_gallons')} gal)"
                self.server.activity.add_event(
                    f"Client {self.client_address[0]}: rental #{rental_id} -> {detail}"
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        "rental": rental,
                        "server_time": datetime.now().replace(microsecond=0).isoformat(),
                    },
                )
                return

            if not self._require_admin():
                return
            if path == "/api/customers":
                customer = self.server.db.create_customer(data)
                self._json(HTTPStatus.CREATED, {"customer": customer})
                return
            if path.startswith("/api/records/"):
                table = path.split("/")[3]
                record = self.server.db.create_simple_record(table, data)
                self._json(HTTPStatus.CREATED, {"record": record})
                return
            if len(parts) == 5 and parts[:3] == ["", "api", "reservations"] and parts[4] == "financials":
                reservation_id = int(parts[3])
                financials = self.server.db.set_reservation_financials(reservation_id, data)
                self._json(HTTPStatus.OK, {"financials": financials})
                return
            if path == "/api/backups":
                result = self.server.db.create_backup(data.get("destination"))
                self._json(HTTPStatus.CREATED, {"backup": result})
                return
            if len(parts)==5 and parts[:4]==["","api","finance","pricing"]:
                row=self.server.db.set_machine_pricing(int(parts[4]),data)
                self._json(HTTPStatus.OK,{"pricing":row}); return
            if path == "/api/finance/discounts":
                kind=str(data.get("discount_type") or "percent")
                value=data.get("discount_value",data.get("discount_percent",0))
                row=self.server.db.set_duration_discount(int(data.get("minimum_days")),float(value),kind)
                self._json(HTTPStatus.OK,{"discount":row}); return
            if path == "/api/finance/quote":
                quote=self.server.db.calculate_reservation_price([int(x) for x in data.get("machine_ids",[])],str(data.get("start_date")),str(data.get("end_date")))
                self._json(HTTPStatus.OK,{"quote":quote}); return
            if len(parts)==5 and parts[:3]==["","api","rentals"] and parts[4]=="inspection-photo":
                inspection=self.server.db.add_inspection_photo(int(parts[3]),data.get("inspection_type",""),data.get("filename",""),data.get("data_base64",""),data.get("notes",""))
                self._json(HTTPStatus.CREATED,{"inspection":inspection}); return
            if len(parts) == 5 and parts[:3] == ["", "api", "documents"] and parts[4] == "sign":
                document = self.server.db.sign_document(int(parts[3]), data.get("signer_name", ""))
                self._json(HTTPStatus.OK, {"document": document})
                return
            if len(parts) == 5 and parts[:3] == ["", "api", "machines"] and parts[4] == "telematics":
                record = self.server.db.update_machine_telemetry(int(parts[3]), data)
                self._json(HTTPStatus.CREATED, {"telematics": record})
                return
            if path == "/api/machines":
                machine = self.server.db.add_machine(data)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} added machine #{machine.get('id', '?')}"
                )
                self._json(HTTPStatus.CREATED, {"machine": machine})
                return
            if len(parts) == 5 and parts[:3] == ["", "api", "rentals"] and parts[4] == "reassign-machine":
                rental_id = int(parts[3])
                result = self.server.db.reassign_rental_machine(
                    rental_id, int(data.get("target_machine_id"))
                )
                rental = result.get("rental") or {}
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} reassigned rental #{rental_id} "
                    f"to machine #{rental.get('machine_id', '?')}"
                )
                self._json(HTTPStatus.OK, result)
                return

            if path == "/api/reservations":
                reservation = self.server.db.add_reservation(data)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} added reservation #{reservation.get('id', '?')} "
                    f"for {reservation.get('machine_count', 0)} machine(s)"
                )
                self._json(HTTPStatus.CREATED, {"reservation": reservation})
                return
            if path == "/api/rentals":
                rental = self.server.db.add_rental(data)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} added rental #{rental.get('id', '?')}"
                )
                self._json(HTTPStatus.CREATED, {"rental": rental})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except Exception as exc:
            self._handle_error(exc)

    def do_PUT(self) -> None:
        self._begin_request()
        if not self._require_admin():
            return
        try:
            if self.server.database_fault or self.server.database_maintenance:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,{"error":"Server database protection is active; a database file is missing."})
                return
            path = urlparse(self.path).path.rstrip("/")
            data = self._read_json()
            parts = path.split("/")
            if path == "/api/settings":
                settings = self.server.db.set_settings(data)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} updated server settings"
                )
                self._json(HTTPStatus.OK, {"settings": settings})
                return
            if len(parts) == 4 and parts[:3] == ["", "api", "machines"]:
                machine_id = int(parts[3])
                machine = self.server.db.update_machine(machine_id, data)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} edited machine #{machine_id}"
                )
                self._json(HTTPStatus.OK, {"machine": machine})
                return
            if len(parts) == 5 and parts[:3] == ["", "api", "records"]:
                record = self.server.db.update_record(parts[3], int(parts[4]), data)
                self._json(HTTPStatus.OK, {"record": record})
                return
            if len(parts) == 4 and parts[:3] == ["", "api", "reservations"]:
                reservation_id = int(parts[3])
                reservation = self.server.db.update_reservation(reservation_id, data)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} edited reservation #{reservation_id}"
                )
                self._json(HTTPStatus.OK, {"reservation": reservation})
                return
            if len(parts) == 4 and parts[:3] == ["", "api", "rentals"]:
                rental_id = int(parts[3])
                rental = self.server.db.update_rental(rental_id, data)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} edited rental #{rental_id}"
                )
                self._json(HTTPStatus.OK, {"rental": rental})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except Exception as exc:
            self._handle_error(exc)

    def do_DELETE(self) -> None:
        self._begin_request()
        if not self._require_admin():
            return
        try:
            if self.server.database_fault or self.server.database_maintenance:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,{"error":"Server database protection is active; a database file is missing."})
                return
            path = urlparse(self.path).path.rstrip("/")
            parts = path.split("/")
            if len(parts) == 5 and parts[:3] == ["", "api", "records"]:
                self.server.db.delete_record(parts[3], int(parts[4]))
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if len(parts) == 4 and parts[:3] == ["", "api", "machines"]:
                machine_id = int(parts[3])
                self.server.db.delete_machine(machine_id)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} removed machine #{machine_id} from Fleet"
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if len(parts) == 4 and parts[:3] == ["", "api", "reservations"]:
                reservation_id = int(parts[3])
                self.server.db.delete_reservation(reservation_id)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} deleted reservation #{reservation_id}"
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if len(parts) == 4 and parts[:3] == ["", "api", "rentals"]:
                rental_id = int(parts[3])
                self.server.db.delete_rental(rental_id)
                self.server.activity.add_event(
                    f"Admin {self.client_address[0]} deleted rental #{rental_id}"
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except Exception as exc:
            self._handle_error(exc)


def discovery_loop(
    port: int,
    http_port: int,
    stop_event: threading.Event,
    activity: ServerActivity | None = None,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", port))
        sock.settimeout(1.0)
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() == DISCOVER_MAGIC:
                if activity:
                    activity.record_discovery(addr[0])
                payload = f"RENTAL_CALENDAR_HERE|{socket.gethostname()}|{http_port}".encode("utf-8")
                try:
                    sock.sendto(payload, addr)
                except OSError:
                    pass
    finally:
        sock.close()


def database_watch_loop(httpd: RentalHTTPServer, stop_event: threading.Event) -> None:
    """Continuously block API traffic if either live database disappears."""
    previous: tuple[str,...]=()
    while not stop_event.wait(0.25):
        missing=httpd.db.missing_database_files()
        current=tuple(str(path) for path in missing)
        httpd.database_fault=missing
        if current and current != previous:
            httpd.activity.add_event("DATABASE PROTECTION: missing " + ", ".join(Path(path).name for path in current))
        previous=current


# ---------------------------------------------------------------------------
# Permanent console dashboard
# ---------------------------------------------------------------------------

def _enable_ansi_console() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        stdout_handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(stdout_handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if not kernel32.SetConsoleMode(stdout_handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            return False
        return True
    except Exception:
        return False


def _clip(text: object, width: int) -> str:
    value = str(text)
    if width <= 3:
        return value[: max(0, width)]
    return value if len(value) <= width else value[: width - 3] + "..."


def _uptime_text(started_at: datetime, now: datetime) -> str:
    seconds = max(0, int((now - started_at).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"
    return f"{hours:02}:{minutes:02}:{seconds:02}"



def _human_bytes(value: int | float) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024.0 or unit == "GB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024.0
    return f"{amount:.1f} GB"

class ServerDashboard:
    def __init__(
        self,
        server: RentalHTTPServer,
        bind_host: str,
        port: int,
        db_path: Path,
        config_path: Path,
        admin_key: str,
        created_key: bool,
        stop_event: threading.Event,
    ) -> None:
        self.server = server
        self.bind_host = bind_host
        self.port = port
        self.db_path = db_path
        self.config_path = config_path
        self.admin_key = admin_key
        self.created_key = created_key
        self.stop_event = stop_event
        self._thread: threading.Thread | None = None
        self._ansi = _enable_ansi_console()
        self._first_draw = True
        self._network_cache: tuple[float, list[str]] = (0.0, [])

    def _urls(self) -> list[str]:
        now = time.monotonic()
        cached_at, urls = self._network_cache
        if now - cached_at >= 5.0 or not urls:
            urls = connection_urls(self.bind_host, self.port)
            self._network_cache = (now, urls)
        return urls

    def render(self, width: int | None = None) -> str:
        snap = self.server.activity.snapshot()
        now = datetime.now()
        if width is None:
            width = shutil.get_terminal_size((120, 42)).columns
        width = max(88, min(int(width), 180))
        inner = width - 2
        rule = "=" * width
        subrule = "-" * width
        urls = self._urls()
        hostname = socket.gethostname()

        clients = sorted(
            snap["clients"],
            key=lambda row: row.get("last_seen", datetime.min),
            reverse=True,
        )
        active = [
            row
            for row in clients
            if (now - row.get("last_seen", now)).total_seconds() <= ACTIVE_DEVICE_SECONDS
        ]

        lines: list[str] = []
        lines.append(rule)
        title_left = f" RENTAL CALENDAR SERVER v{APP_VERSION}  |  ONLINE"
        title_right = now.strftime("%Y-%m-%d %I:%M:%S %p")
        gap = max(1, width - len(title_left) - len(title_right))
        lines.append(_clip(title_left + (" " * gap) + title_right, width))
        lines.append(rule)
        lines.append(" CONNECT TO THIS SERVER")
        if urls:
            lines.append(f"  PRIMARY LAN URL : {urls[0]}")
            for index, url in enumerate(urls[1:], start=2):
                lines.append(f"  LAN URL #{index:<7}: {url}")
        else:
            lines.append("  PRIMARY LAN URL : NOT DETECTED - check this server's network connection")
        lines.append(f"  HOSTNAME URL    : http://{hostname}:{self.port}  (works if hostname resolution is available)")
        lines.append(f"  LOCAL ONLY      : http://127.0.0.1:{self.port}")
        lines.append(f"  ADMIN KEY       : {self.admin_key}")
        if self.created_key:
            lines.append("                    ^ Newly generated on this server; enter it in Admin > Settings")
        lines.append(f"  API / DISCOVERY : TCP {self.port}  |  UDP {DISCOVERY_PORT}")
        lines.append(subrule)
        lines.append(" SERVER INFORMATION")
        lines.append(f"  Computer        : {hostname}")
        lines.append(f"  Bind address    : {self.bind_host}:{self.port}")
        lines.append(f"  Database        : {_clip(self.db_path, max(20, inner - 20))}")
        try:
            stats = self.server.db.database_statistics()
            lines.append(
                f"  Database files  : {stats['database_file_count']}  |  Combined size: "
                f"{_human_bytes(stats['total_size_bytes'])}"
            )
            most_range = (stats.get("most_rented_date_ranges") or [{}])[0]
            least_range = (stats.get("least_rented_date_ranges") or [{}])[0]
            most_machine = (stats.get("most_rented_machines") or [{}])[0]
            least_machine = (stats.get("least_rented_machines") or [{}])[0]
            if most_range:
                lines.append(f"  Most rented     : {most_range.get('start_date')} - {most_range.get('end_date')} ({most_range.get('rental_count', 0)})")
            if least_range:
                lines.append(f"  Least rented    : {least_range.get('start_date')} - {least_range.get('end_date')} ({least_range.get('rental_count', 0)})")
            if most_machine:
                lines.append(f"  Top machine     : {most_machine.get('unit_number') or most_machine.get('model')} ({most_machine.get('rental_count', 0)} rentals)")
            if least_machine:
                lines.append(f"  Least machine   : {least_machine.get('unit_number') or least_machine.get('model')} ({least_machine.get('rental_count', 0)} rentals)")
        except Exception:
            pass
        lines.append(f"  Server config   : {_clip(self.config_path, max(20, inner - 20))}")
        if self.server.db.migration_backup:
            lines.append(
                f"  Upgrade backup  : {_clip(self.server.db.migration_backup, max(20, inner - 20))}"
            )
        try:
            db_version = self.server.db.version()
        except Exception:
            db_version = "?"
        lines.append(f"  Database version: {db_version}  |  Uptime: {_uptime_text(snap['started_at'], now)}")
        lines.append(subrule)
        last_60 = int(snap.get("last_60_wire", 0))
        last_60_remote = int(snap.get("last_60_remote_wire", 0))
        kbps = (last_60 * 8.0 / 1000.0) / 60.0
        remote_kbps = (last_60_remote * 8.0 / 1000.0) / 60.0
        lines.append(" NETWORK TRAFFIC  |  optimized 2.5-second change polling")
        lines.append(
            f"  REMOTE LAN est.  : {_human_bytes(last_60_remote)}/60s  |  {remote_kbps:.3f} kbps avg"
            f"  |  Since start: {_human_bytes(int(snap.get('total_remote_wire_estimate', 0)))}"
        )
        lines.append(
            f"  All API est.     : {_human_bytes(last_60)}/60s  |  {kbps:.3f} kbps avg"
            f"  |  Since start: {_human_bytes(int(snap.get('total_wire_estimate', 0)))}"
        )
        lines.append(
            f"  HTTP received    : {_human_bytes(int(snap.get('total_http_rx', 0)))}"
            f"  |  HTTP sent: {_human_bytes(int(snap.get('total_http_tx', 0)))}"
        )
        lines.append("  NOTE: REMOTE LAN excludes localhost. Normal polling never uses Internet/WAN bandwidth.")
        lines.append(subrule)
        lines.append(
            f" CONNECTION ACTIVITY  |  Active PCs (<={ACTIVE_DEVICE_SECONDS}s): {len(active)}"
            f"  |  Seen since startup: {len(clients)}  |  Requests: {snap['total_requests']}"
        )

        role_w = 7
        ip_w = 17
        req_w = 8
        seen_w = 11
        status_w = 6
        fixed = 2 + role_w + 2 + ip_w + 2 + req_w + 2 + seen_w + 2 + status_w + 2
        last_w = max(18, width - fixed)
        lines.append(
            f"  {'ROLE':<{role_w}}  {'IP ADDRESS':<{ip_w}}  {'REQUESTS':>{req_w}}  "
            f"{'LAST SEEN':<{seen_w}}  {'HTTP':<{status_w}}  {'LAST REQUEST':<{last_w}}"
        )
        if clients:
            for row in clients[:10]:
                last_seen = row.get("last_seen", now)
                age = max(0, int((now - last_seen).total_seconds()))
                if age < 60:
                    seen = f"{age}s ago"
                elif age < 3600:
                    seen = f"{age // 60}m ago"
                else:
                    seen = last_seen.strftime("%H:%M:%S")
                lines.append(
                    f"  {_clip(row.get('role', '?'), role_w):<{role_w}}  "
                    f"{_clip(row.get('ip', '?'), ip_w):<{ip_w}}  "
                    f"{int(row.get('requests', 0)):>{req_w}}  "
                    f"{_clip(seen, seen_w):<{seen_w}}  "
                    f"{_clip(row.get('last_status', '-'), status_w):<{status_w}}  "
                    f"{_clip(row.get('last_request', '-'), last_w):<{last_w}}"
                )
        else:
            lines.append("  No Admin or Client PCs have contacted the server yet.")

        lines.append(subrule)
        lines.append(" RECENT SERVER EVENTS")
        events = snap["events"]
        if events:
            for when, message in events[:6]:
                prefix = f"  {when.strftime('%H:%M:%S')}  "
                lines.append(prefix + _clip(message, width - len(prefix)))
        else:
            lines.append("  Waiting for activity...")
        lines.append(subrule)
        lines.append(" Ctrl+C stops the server.  Keep this window open while Admin/Client PCs are in use.")
        lines.append(rule)
        return "\n".join(lines)

    def _draw_loop(self) -> None:
        if not self._ansi:
            # A redirected/non-interactive terminal cannot maintain a fixed
            # dashboard.  Print the important connection block once instead.
            print(self.render())
            return

        while not self.stop_event.is_set():
            text = self.render()
            if self._first_draw:
                sys.stdout.write("\x1b[2J\x1b[H")
                self._first_draw = False
            else:
                sys.stdout.write("\x1b[H")
            sys.stdout.write(text)
            sys.stdout.write("\x1b[J")
            sys.stdout.flush()
            self.stop_event.wait(1.0)

    def start(self) -> None:
        self.server.dashboard_active = True
        self.server.activity.add_event("Server started and is ready for Admin/Client connections")
        self._thread = threading.Thread(target=self._draw_loop, daemon=True, name="Server Dashboard")
        self._thread.start()

    def stop(self) -> None:
        self.server.dashboard_active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)




class WindowsTrayIcon:
    """Dependency-free native Windows notification-area icon.

    Runs its Win32 message pump on a daemon thread and marshals menu actions
    back to Tk's thread with root.after().
    """

    WM_TRAY = 0x8000 + 20
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    ID_OPEN = 1001
    ID_EXIT = 1002
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010
    NIM_MODIFY = 0x00000001
    NIIF_INFO = 0x00000001
    IMAGE_ICON = 1
    LR_SHARED = 0x00008000
    IDI_APPLICATION = 32512
    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002
    TPM_BOTTOMALIGN = 0x0020
    TPM_RETURNCMD = 0x0100

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    def __init__(self, root, on_open, on_exit, on_menu):
        self.root = root
        self.on_open = on_open
        self.on_exit = on_exit
        self.on_menu = on_menu
        self.hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._class_name = f"RentalCalendarTray_{os.getpid()}"
        self._wndproc_ref = None
        self._nid = None
        self.exit_requested = threading.Event()

    def start(self):
        if os.name != "nt" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="Rental Calendar Tray")
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def _run(self):
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        # Explicit native signatures are important on 64-bit Windows. Without
        # these, ctypes can truncate menu/window handles and yield a blank-looking tray menu.
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.c_void_p
        ]
        # UINT is required with TPM_RETURNCMD because the return value is the
        # selected command identifier, not merely a success flag.
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(self.NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        def schedule(callback):
            try:
                self.root.after(0, callback)
            except Exception:
                pass

        def show_native_menu(hwnd):
            menu=user32.CreatePopupMenu()
            if not menu:
                return
            try:
                user32.AppendMenuW(menu,self.MF_STRING,self.ID_OPEN,"Open Server")
                user32.AppendMenuW(menu,self.MF_SEPARATOR,0,None)
                user32.AppendMenuW(menu,self.MF_STRING,self.ID_EXIT,"Close Server")
                point=wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(point))
                user32.SetForegroundWindow(hwnd)
                command=int(user32.TrackPopupMenu(
                    menu,
                    self.TPM_RIGHTBUTTON | self.TPM_BOTTOMALIGN | self.TPM_RETURNCMD,
                    point.x,point.y,0,hwnd,None,
                ) or 0)
                if command == self.ID_OPEN:
                    schedule(self.on_open)
                elif command == self.ID_EXIT:
                    # Do not depend on calling Tk from this native message thread.
                    # The GUI polls this event and performs shutdown on its own thread.
                    self.exit_requested.set()
            finally:
                user32.DestroyMenu(menu)

        @WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY:
                event = int(lparam)
                if event == self.WM_LBUTTONDBLCLK:
                    schedule(self.on_open)
                    return 0
                if event == self.WM_RBUTTONUP:
                    # Draw the visible menu in Tk because some Windows 11 theme
                    # combinations render native popup text white-on-white. The
                    # Close button still uses the thread-safe exit event below.
                    schedule(self.on_menu)
                    return 0
            elif msg == self.WM_COMMAND:
                command = int(wparam) & 0xFFFF
                if command == self.ID_OPEN:
                    schedule(self.on_open)
                    return 0
                if command == self.ID_EXIT:
                    self.exit_requested.set()
                    return 0
            elif msg == self.WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == self.WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = wndproc
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        wc.hIcon = user32.LoadIconW(None, ctypes.c_void_p(self.IDI_APPLICATION))
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, self._class_name, "Rental Calendar Server Tray",
            0, 0, 0, 0, 0, None, None, hinst, None
        )
        self.hwnd = hwnd

        nid = self.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(self.NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = user32.LoadIconW(None, ctypes.c_void_p(self.IDI_APPLICATION))
        nid.szTip = "Rental Calendar Server - Online"
        self._nid = nid
        shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid))
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass

    def show_hidden_notice(self):
        if os.name != "nt" or not self.hwnd or self._nid is None:
            return
        try:
            nid = self._nid
            nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP | self.NIF_INFO
            nid.szInfoTitle = "Rental Calendar Server"
            nid.szInfo = "Server is still running. Right-click the tray icon for Open Server or Close Server."
            nid.dwInfoFlags = self.NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(nid))
            nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        except Exception:
            pass

    def stop(self):
        if os.name != "nt" or not self.hwnd:
            return
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(
                self.NIM_DELETE, ctypes.byref(self._nid)
            )
            ctypes.windll.user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)
            thread=self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        except Exception:
            pass



class ServerControlGUI(tk.Tk if tk is not None else object):
    """Desktop control panel for the Rental Calendar server."""

    def __init__(
        self,
        server: RentalHTTPServer,
        bind_host: str,
        port: int,
        db_path: Path,
        config_path: Path,
        admin_key: str,
        stop_event: threading.Event,
    ):
        if tk is None:
            raise RuntimeError("Tkinter is unavailable.")
        super().__init__()
        self.httpd = server
        self.bind_host = bind_host
        self.port = int(port)
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.admin_key = admin_key
        self.stop_event = stop_event
        self._closing = False
        self._refresh_after = None
        self._tray_poll_after = None
        self._tray_menu_window = None
        self._database_fault_prompted = False

        self.title(f"Rental Calendar Server - v{APP_VERSION}")
        self.geometry("1180x760")
        self.minsize(980, 650)
        # The X button hides the server to the Windows notification area.
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.tray = WindowsTrayIcon(
            self, self.restore_from_tray, self.exit_server_from_tray, self.show_tray_menu
        ) if os.name == "nt" else None
        if self.tray is not None:
            self.tray.start()

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill=X)
        ttk.Label(top, text=f"Rental Calendar Server  v{APP_VERSION}", font=("Segoe UI", 20, "bold")).pack(side=LEFT)
        self.online_var = StringVar(value="ONLINE")
        ttk.Label(top, textvariable=self.online_var, font=("Segoe UI", 11, "bold")).pack(side=RIGHT)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill=BOTH, expand=True, padx=12, pady=(0, 12))
        self.overview_tab = ttk.Frame(self.tabs, padding=10)
        self.admin_tab = ttk.Frame(self.tabs, padding=10)
        self.backup_tab = ttk.Frame(self.tabs, padding=10)
        self.web_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.overview_tab, text="Server Status")
        self.tabs.add(self.admin_tab, text="Admin Access")
        self.tabs.add(self.backup_tab, text="Backups & Recovery")
        self.tabs.add(self.web_tab, text="Web Broadcasts")

        self._build_overview()
        self._build_admin_access()
        self._build_backup_recovery()
        self._build_web_broadcasts()
        self.refresh_gui()
        self._tray_poll_after = self.after(200,self._poll_tray_commands)

    def _poll_tray_commands(self):
        if self._closing:
            return
        if self.tray is not None and self.tray.exit_requested.is_set():
            self.tray.exit_requested.clear()
            self._shutdown_server()
            return
        self._tray_poll_after=self.after(200,self._poll_tray_commands)

    def destroy(self):
        """Cancel scheduled Tcl callbacks before destroying the root window."""
        self._closing=True
        for attribute in ("_refresh_after","_tray_poll_after"):
            callback=getattr(self,attribute,None)
            if callback:
                try:self.after_cancel(callback)
                except Exception:pass
                setattr(self,attribute,None)
        return super().destroy()

    def _build_backup_recovery(self):
        notice=ttk.LabelFrame(self.backup_tab,text=" System Backups ",padding=10)
        notice.pack(fill=X,pady=(0,10))
        ttk.Label(notice,text="Each system backup contains a consistent copy of both the rental database and image database.\nA safety backup is created automatically before every restore.",justify="left").pack(side=LEFT)
        ttk.Button(notice,text="Create Backup Now",command=self.create_system_backup).pack(side=RIGHT)
        columns=("created","label","size","id")
        self.backup_tree=ttk.Treeview(self.backup_tab,columns=columns,show="headings",height=14,selectmode="browse")
        for name,title,width in (("created","Created",180),("label","Type",130),("size","Combined Size",130),("id","Backup ID",420)):
            self.backup_tree.heading(name,text=title); self.backup_tree.column(name,width=width,anchor="w")
        self.backup_tree.pack(fill=BOTH,expand=True)
        actions=ttk.Frame(self.backup_tab,padding=(0,10)); actions.pack(fill=X)
        ttk.Button(actions,text="Refresh List",command=self.refresh_backup_list).pack(side=LEFT)
        ttk.Button(actions,text="Restore Selected Backup",command=self.restore_selected_backup).pack(side=LEFT,padx=8)
        danger=ttk.LabelFrame(self.backup_tab,text=" Intentional Live Database Reset ",padding=10)
        danger.pack(fill=X)
        ttk.Label(danger,text="This permanently deletes the selected live database and immediately creates a new empty replacement.").pack(side=LEFT)
        self.reset_database_var=StringVar(value="Image database")
        ttk.Combobox(danger,textvariable=self.reset_database_var,values=("Image database","Rental database"),state="readonly",width=20).pack(side=LEFT,padx=10)
        ttk.Button(danger,text="Delete and Create Empty Database",command=self.reset_selected_database).pack(side=RIGHT)
        self.refresh_backup_list()

    def _build_web_broadcasts(self):
        intro=ttk.LabelFrame(self.web_tab,text=" Server-hosted Web Gateways ",padding=12); intro.pack(fill=X,pady=(0,12))
        ttk.Label(intro,text="The Server owns these listeners, authenticates rental creation, and records all web traffic.\nTurning a broadcast off removes that page from the LAN even while Admin or Client desktop applications remain open.",justify="left").pack(anchor="w")
        self.web_status_vars={}; self.web_port_vars={}
        config=self._load_server_config()
        for row,(role,label,default_port) in enumerate((("admin","Admin Web",8775),("client","Client Web",8776))):
            frame=ttk.LabelFrame(self.web_tab,text=f" {label} ",padding=12); frame.pack(fill=X,pady=6)
            port=StringVar(value=str(config.get(f"{role}_web_port",default_port))); status=StringVar(value="Disabled")
            self.web_port_vars[role]=port; self.web_status_vars[role]=status
            ttk.Label(frame,text="TCP port:").pack(side=LEFT); ttk.Entry(frame,textvariable=port,width=8).pack(side=LEFT,padx=8)
            ttk.Label(frame,textvariable=status).pack(side=LEFT,padx=15)
            ttk.Button(frame,text="Enable",command=lambda r=role:self.enable_web_broadcast(r)).pack(side=RIGHT)
            ttk.Button(frame,text="Disable",command=lambda r=role:self.disable_web_broadcast(r)).pack(side=RIGHT,padx=6)
        self.refresh_web_broadcast_status()

    def _load_server_config(self):
        try:return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:return {}

    def _save_web_config(self,role: str,enabled: bool,port: int):
        config=self._load_server_config(); config[f"{role}_web_enabled"]=bool(enabled); config[f"{role}_web_port"]=int(port)
        self.config_path.write_text(json.dumps(config,indent=2),encoding="utf-8")

    def enable_web_broadcast(self,role: str):
        try:
            port=int(self.web_port_vars[role].get())
            if not 1024 <= port <= 65535:raise ValueError("Port must be from 1024 through 65535.")
            self.httpd.web_gateways.start(role,port); self._save_web_config(role,True,port); self.refresh_web_broadcast_status()
        except Exception as exc:messagebox.showerror("Web broadcast failed",str(exc),parent=self)

    def disable_web_broadcast(self,role: str):
        port=int(self.web_port_vars[role].get() or (8775 if role=="admin" else 8776))
        self.httpd.web_gateways.stop(role); self._save_web_config(role,False,port); self.refresh_web_broadcast_status()

    def refresh_web_broadcast_status(self):
        if not hasattr(self,"web_status_vars"):return
        addresses=get_lan_ipv4_addresses()
        for role,var in self.web_status_vars.items():
            item=self.httpd.web_gateways.status(role) if self.httpd.web_gateways else None
            if item:
                port=item[2]; host=addresses[0] if addresses else "127.0.0.1"; var.set(f"Enabled: http://{host}:{port}")
            else:var.set("Disabled")

    def refresh_backup_list(self):
        if not hasattr(self,"backup_tree"):return
        self.backup_tree.delete(*self.backup_tree.get_children())
        for row in self.httpd.db.list_system_backups():
            self.backup_tree.insert("","end",iid=row["id"],values=(row.get("created_at",""),row.get("label",""),_human_bytes(row.get("total_size_bytes",0)),row["id"]))

    def create_system_backup(self):
        self.httpd.database_maintenance=True
        self.update_idletasks(); time.sleep(0.3)
        try:
            backup=self.httpd.db.create_system_backup("manual")
            self.refresh_backup_list()
            self.httpd.activity.add_event(f"System backup created: {backup['id']}")
            messagebox.showinfo("Backup complete",f"Saved both databases as:\n{backup['id']}",parent=self)
        except Exception as exc:messagebox.showerror("Backup failed",str(exc),parent=self)
        finally:self.httpd.database_maintenance=False

    def restore_selected_backup(self):
        selection=self.backup_tree.selection()
        if not selection:
            messagebox.showinfo("Select backup","Select a backup to restore first.",parent=self); return
        backup_id=selection[0]
        if not messagebox.askyesno("Restore system backup",f"Restore both live databases from:\n{backup_id}?\n\nThe current system will be backed up first.",parent=self):return
        self.httpd.database_maintenance=True
        self.update_idletasks(); time.sleep(0.3)
        try:
            result=self.httpd.db.restore_system_backup(backup_id)
            self.httpd.database_fault=[]; self.httpd.database_maintenance=False; self._database_fault_prompted=False
            self.refresh_backup_list()
            self.httpd.activity.add_event(f"System restored from backup: {backup_id}")
            messagebox.showinfo("Restore complete",f"System restored.\n\nPre-restore safety backup:\n{result['safety_backup']['id']}",parent=self)
        except Exception as exc:
            self.httpd.database_fault=self.httpd.db.missing_database_files(); self.httpd.database_maintenance=False
            messagebox.showerror("Restore failed",str(exc),parent=self)

    def reset_selected_database(self):
        target=self.httpd.db.image_db_path if self.reset_database_var.get()=="Image database" else self.httpd.db.path
        if not messagebox.askyesno("Permanently reset database",f"Delete ALL live data in {target.name} and create an empty replacement?\n\nCreate a backup first unless this loss is intentional.",icon="warning",parent=self):return
        if not messagebox.askyesno("Final confirmation",f"This cannot be undone without a backup. Reset {target.name} now?",icon="warning",parent=self):return
        self.httpd.database_maintenance=True; self.update_idletasks(); time.sleep(0.3)
        try:
            self.httpd.db.recreate_database_file(target)
            self.httpd.database_fault=[]; self.httpd.database_maintenance=False; self._database_fault_prompted=False
            self.httpd.activity.add_event(f"Intentional live database reset: {target.name}")
            messagebox.showinfo("Database reset",f"{target.name} was deleted and recreated empty.",parent=self)
        except Exception as exc:
            self.httpd.database_fault=self.httpd.db.missing_database_files(); self.httpd.database_maintenance=False
            messagebox.showerror("Reset failed",str(exc),parent=self)

    def _build_overview(self):
        connection = ttk.LabelFrame(self.overview_tab, text=" Connect to this Server ", padding=10)
        connection.pack(fill=X, pady=(0, 10))
        self.primary_url_var = StringVar(value="Detecting...")
        self.hostname_url_var = StringVar(value="")
        self.key_var = StringVar(value=self.admin_key)

        ttk.Label(connection, text="Primary LAN URL:").grid(row=0, column=0, sticky="w")
        ttk.Entry(connection, textvariable=self.primary_url_var, state="readonly", width=58).grid(
            row=0, column=1, sticky="ew", padx=(8, 6)
        )
        ttk.Button(connection, text="Copy URL", command=lambda: self._copy(self.primary_url_var.get())).grid(
            row=0, column=2, sticky="ew"
        )

        ttk.Label(connection, text="Hostname URL:").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(connection, textvariable=self.hostname_url_var, state="readonly", width=58).grid(
            row=1, column=1, sticky="ew", padx=(8, 6), pady=(7, 0)
        )
        ttk.Button(connection, text="Copy URL", command=lambda: self._copy(self.hostname_url_var.get())).grid(
            row=1, column=2, sticky="ew", pady=(7, 0)
        )

        ttk.Label(connection, text="Master Admin Key:").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(connection, textvariable=self.key_var, state="readonly", width=58).grid(
            row=2, column=1, sticky="ew", padx=(8, 6), pady=(7, 0)
        )
        ttk.Button(connection, text="Copy Admin Key", command=lambda: self._copy(self.admin_key)).grid(
            row=2, column=2, sticky="ew", pady=(7, 0)
        )
        connection.columnconfigure(1, weight=1)

        info = ttk.LabelFrame(self.overview_tab, text=" Server / Network Information ", padding=10)
        info.pack(fill=X, pady=(0, 10))
        self.info_var = StringVar(value="")
        ttk.Label(info, textvariable=self.info_var, justify="left").pack(anchor="w")

        traffic = ttk.LabelFrame(self.overview_tab, text=" Network Traffic ", padding=10)
        traffic.pack(fill=X, pady=(0, 10))
        self.traffic_var = StringVar(value="")
        ttk.Label(traffic, textvariable=self.traffic_var, justify="left").pack(anchor="w")

        activity = ttk.LabelFrame(self.overview_tab, text=" Connected Computers ", padding=8)
        activity.pack(fill=BOTH, expand=True, pady=(0, 10))
        cols = ("role", "ip", "requests", "last_seen", "http", "request")
        self.connection_tree = ttk.Treeview(activity, columns=cols, show="headings", height=8)
        labels = {
            "role": "Role", "ip": "IP Address", "requests": "Requests",
            "last_seen": "Last Seen", "http": "HTTP", "request": "Last Request",
        }
        widths = {"role": 90, "ip": 150, "requests": 85, "last_seen": 110, "http": 65, "request": 430}
        for col in cols:
            self.connection_tree.heading(col, text=labels[col])
            self.connection_tree.column(col, width=widths[col], anchor="w")
        self.connection_tree.pack(fill=BOTH, expand=True)

        events = ttk.LabelFrame(self.overview_tab, text=" Recent Server Events ", padding=8)
        events.pack(fill=X)
        self.events_text = tk.Text(events, height=6, wrap="word", state="disabled")
        self.events_text.pack(fill=X)

        bottom = ttk.Frame(self.overview_tab)
        bottom.pack(fill=X, pady=(10, 0))
        ttk.Button(bottom, text="Hide to System Tray", command=self.hide_to_tray).pack(side=LEFT)
        ttk.Button(bottom, text="Exit Server", command=self.exit_server).pack(side=RIGHT)

    def _build_admin_access(self):
        note = ttk.LabelFrame(self.admin_tab, text=" Admin Approval ", padding=10)
        note.pack(fill=X, pady=(0, 10))
        ttk.Label(
            note,
            text=(
                "An Admin PC can request access without knowing the master key. "
                "Approve it here to issue that computer its own unique Admin token. "
                "Clients do not request Admin access and remain limited to Client operations."
            ),
            wraplength=1050,
            justify="left",
        ).pack(anchor="w")

        pending = ttk.LabelFrame(self.admin_tab, text=" Pending Admin Requests ", padding=8)
        pending.pack(fill=BOTH, expand=True, pady=(0, 10))
        pcols = ("computer", "ip", "requested")
        self.pending_tree = ttk.Treeview(pending, columns=pcols, show="headings", height=8, selectmode="browse")
        for col, label, width in [
            ("computer", "Computer", 300),
            ("ip", "IP Address", 180),
            ("requested", "Requested", 220),
        ]:
            self.pending_tree.heading(col, text=label)
            self.pending_tree.column(col, width=width, anchor="w")
        self.pending_tree.pack(fill=BOTH, expand=True)
        pbuttons = ttk.Frame(pending)
        pbuttons.pack(fill=X, pady=(8, 0))
        ttk.Button(pbuttons, text="Approve Selected", command=self.approve_selected).pack(side=LEFT)
        ttk.Button(pbuttons, text="Keep as Client / Deny Admin", command=self.deny_selected).pack(side=LEFT, padx=6)

        approved = ttk.LabelFrame(self.admin_tab, text=" Approved Admin Computers ", padding=8)
        approved.pack(fill=BOTH, expand=True)
        acols = ("computer", "last_ip", "approved", "token")
        self.approved_tree = ttk.Treeview(approved, columns=acols, show="headings", height=8, selectmode="browse")
        for col, label, width in [
            ("computer", "Computer", 300),
            ("last_ip", "Last IP", 180),
            ("approved", "Approved", 220),
            ("token", "Token", 180),
        ]:
            self.approved_tree.heading(col, text=label)
            self.approved_tree.column(col, width=width, anchor="w")
        self.approved_tree.pack(fill=BOTH, expand=True)
        abuttons = ttk.Frame(approved)
        abuttons.pack(fill=X, pady=(8, 0))
        ttk.Button(abuttons, text="Revoke Selected Admin", command=self.revoke_selected).pack(side=LEFT)

    def _copy(self, value: str):
        value = str(value or "")
        if not value:
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self.online_var.set("COPIED")
        self.after(1200, lambda: self.online_var.set("ONLINE") if not self._closing else None)

    def show_tray_menu(self):
        if self._closing:
            return
        if self._tray_menu_window is not None:
            try:self._tray_menu_window.destroy()
            except Exception:pass
        menu=tk.Toplevel(self)
        self._tray_menu_window=menu
        menu.overrideredirect(True)
        menu.configure(bg="#FFFFFF",highlightbackground="#666666",highlightthickness=1)
        try:menu.attributes("-topmost",True)
        except Exception:pass
        def dismiss():
            if self._tray_menu_window is menu:self._tray_menu_window=None
            try:menu.destroy()
            except Exception:pass
        def open_server():
            dismiss(); self.restore_from_tray()
        def close_server():
            dismiss()
            if self.tray is not None:self.tray.exit_requested.set()
            else:self.after_idle(self._shutdown_server)
        button_options={"bg":"#FFFFFF","fg":"#111111","activebackground":"#D9E8FF","activeforeground":"#000000",
            "font":("Segoe UI",10),"relief":"flat","bd":0,"anchor":"w","padx":14,"pady":7,"width":18}
        tk.Button(menu,text="Open Server",command=open_server,**button_options).pack(fill=X)
        tk.Frame(menu,bg="#CCCCCC",height=1).pack(fill=X,padx=4)
        tk.Button(menu,text="Close Server",command=close_server,**button_options).pack(fill=X)
        menu.update_idletasks()
        width=menu.winfo_reqwidth(); height=menu.winfo_reqheight()
        x=max(0,self.winfo_pointerx()-width); y=max(0,self.winfo_pointery()-height)
        menu.geometry(f"{width}x{height}+{x}+{y}")
        menu.bind("<Escape>",lambda _event:dismiss())
        menu.bind("<FocusOut>",lambda _event:self.after(100,dismiss))
        try:
            menu.focus_force()
        except Exception:pass

    def hide_to_tray(self):
        if self._closing:
            return
        if os.name == "nt" and self.tray is not None:
            # withdraw removes the taskbar button; the native tray icon remains.
            self.withdraw()
            self.tray.show_hidden_notice()
        else:
            # Cross-platform fallback when a native notification-area API is unavailable.
            self.iconify()

    def restore_from_tray(self):
        if self._closing:
            return
        self.deiconify()
        self.state("normal")
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

    def approve_selected(self):
        sel = self.pending_tree.selection()
        if not sel:
            messagebox.showinfo("Admin Approval", "Select a pending Admin request first.", parent=self)
            return
        request_id = sel[0]
        try:
            row = self.httpd.access_store.approve(request_id)
            self.httpd.activity.add_event(
                f"Approved Admin computer {row.get('machine_name', 'Unknown')} ({row.get('ip', '')})"
            )
            self.refresh_gui()
        except Exception as exc:
            messagebox.showerror("Admin Approval", str(exc), parent=self)

    def deny_selected(self):
        sel = self.pending_tree.selection()
        if not sel:
            messagebox.showinfo("Admin Approval", "Select a pending Admin request first.", parent=self)
            return
        request_id = sel[0]
        try:
            row = self.httpd.access_store.deny(request_id)
            self.httpd.activity.add_event(
                f"Denied Admin computer {row.get('machine_name', 'Unknown')} ({row.get('ip', '')})"
            )
            self.refresh_gui()
        except Exception as exc:
            messagebox.showerror("Admin Approval", str(exc), parent=self)

    def revoke_selected(self):
        sel = self.approved_tree.selection()
        if not sel:
            messagebox.showinfo("Revoke Admin", "Select an approved Admin computer first.", parent=self)
            return
        token = sel[0]
        values = self.approved_tree.item(token, "values")
        computer = values[0] if values else "this computer"
        if not messagebox.askyesno(
            "Revoke Admin",
            f"Remove Admin access for {computer}?\n\nThat PC can request approval again later.",
            parent=self,
        ):
            return
        if self.httpd.access_store.revoke(token):
            self.httpd.activity.add_event(f"Revoked Admin access for {computer}")
        self.refresh_gui()

    def refresh_gui(self):
        if self._closing:
            return
        missing=self.httpd.db.missing_database_files()
        self.httpd.database_fault=missing
        if missing and not self._database_fault_prompted:
            self._database_fault_prompted=True
            names=", ".join(path.name for path in missing)
            intentional=messagebox.askyesno(
                "Database deletion detected",
                f"The server can no longer find: {names}\n\nWas this deletion intentional?\n\n"
                "Yes: permanently reset the missing file and create a new empty database now.\n"
                "No: stop the server without creating or overwriting anything.",
                parent=self,
            )
            if intentional:
                try:
                    for path in missing:self.httpd.db.recreate_database_file(path)
                    self.httpd.database_fault=[]
                    self._database_fault_prompted=False
                    self.httpd.activity.add_event(f"Intentional database reset confirmed: {names}")
                    messagebox.showinfo("Database recreated",f"Created a new empty database for: {names}",parent=self)
                except Exception as exc:
                    messagebox.showerror("Database recovery failed",str(exc),parent=self)
                    self._shutdown_server(); return
            else:
                self.httpd.activity.add_event(f"Unintentional database loss reported: {names}; server stopped")
                self._shutdown_server(); return
        now = datetime.now()
        urls = connection_urls(self.bind_host, self.port)
        self.primary_url_var.set(urls[0] if urls else "No LAN address detected")
        self.hostname_url_var.set(f"http://{socket.gethostname()}:{self.port}")
        snap = self.httpd.activity.snapshot()

        try:
            db_version = self.httpd.db.version()
        except Exception:
            db_version = "?"
        backup = f"\nUpgrade backup: {self.httpd.db.migration_backup}" if self.httpd.db.migration_backup else ""
        try:
            stats = self.httpd.db.database_statistics()
            database_summary = (
                f"\nDatabase files: {stats['database_file_count']}    •    "
                f"Combined size: {_human_bytes(stats['total_size_bytes'])}"
            )
        except Exception:
            database_summary = ""
        self.info_var.set(
            f"Computer: {socket.gethostname()}    •    Bind: {self.bind_host}:{self.port}    •    "
            f"TCP {self.port} / UDP {DISCOVERY_PORT}\n"
            f"Database: {self.db_path}\nConfig: {self.config_path}\n"
            f"Database version: {db_version}    •    Uptime: {_uptime_text(snap['started_at'], now)}{database_summary}{backup}"
        )

        last_60 = int(snap.get("last_60_wire", 0))
        last_60_remote = int(snap.get("last_60_remote_wire", 0))
        kbps = (last_60 * 8.0 / 1000.0) / 60.0
        remote_kbps = (last_60_remote * 8.0 / 1000.0) / 60.0
        self.traffic_var.set(
            f"Remote LAN: {_human_bytes(last_60_remote)}/60s ({remote_kbps:.3f} kbps avg)  •  "
            f"Since start: {_human_bytes(int(snap.get('total_remote_wire_estimate', 0)))}\n"
            f"All API: {_human_bytes(last_60)}/60s ({kbps:.3f} kbps avg)  •  "
            f"HTTP RX {_human_bytes(int(snap.get('total_http_rx', 0)))} / "
            f"TX {_human_bytes(int(snap.get('total_http_tx', 0)))}"
        )

        for item in self.connection_tree.get_children():
            self.connection_tree.delete(item)
        clients = sorted(snap["clients"], key=lambda r: r.get("last_seen", datetime.min), reverse=True)
        for idx, row in enumerate(clients[:25]):
            last_seen = row.get("last_seen", now)
            age = max(0, int((now - last_seen).total_seconds()))
            seen = f"{age}s ago" if age < 60 else (f"{age//60}m ago" if age < 3600 else last_seen.strftime("%H:%M:%S"))
            self.connection_tree.insert(
                "", "end", iid=f"conn-{idx}",
                values=(
                    row.get("role", "?"), row.get("ip", "?"), row.get("requests", 0),
                    seen, row.get("last_status", "-"), row.get("last_request", "-"),
                ),
            )

        self.events_text.configure(state="normal")
        self.events_text.delete("1.0", "end")
        for when, message in snap["events"][:12]:
            self.events_text.insert("end", f"{when.strftime('%H:%M:%S')}  {message}\n")
        if not snap["events"]:
            self.events_text.insert("end", "Waiting for activity...\n")
        self.events_text.configure(state="disabled")

        current = set(self.pending_tree.get_children())
        pending_rows = self.httpd.access_store.pending_rows() if self.httpd.access_store else []
        desired = set()
        for row in pending_rows:
            rid = row["request_id"]
            desired.add(rid)
            values = (row.get("machine_name", ""), row.get("ip", ""), row.get("requested_at", ""))
            if rid in current:
                self.pending_tree.item(rid, values=values)
            else:
                self.pending_tree.insert("", "end", iid=rid, values=values)
        for rid in current - desired:
            self.pending_tree.delete(rid)

        # Update approved Admin rows in-place. Rebuilding this Treeview every
        # second used to destroy the user's selection before Revoke could be clicked.
        approved_current = set(self.approved_tree.get_children())
        approved_desired = set()
        if self.httpd.access_store:
            for row in self.httpd.access_store.approved_rows():
                token = row["token"]
                approved_desired.add(token)
                values = (
                    row.get("machine_name", ""),
                    row.get("last_ip", ""),
                    row.get("approved_at", ""),
                    token[:10] + "...",
                )
                if token in approved_current:
                    self.approved_tree.item(token, values=values)
                else:
                    self.approved_tree.insert("", "end", iid=token, values=values)
        for token in approved_current - approved_desired:
            self.approved_tree.delete(token)

        self.online_var.set("ONLINE")
        self._refresh_after = self.after(1000, self.refresh_gui)

    def _shutdown_server(self):
        if self._closing:
            return
        self._closing = True
        self.online_var.set("STOPPING")
        # Windows/Tk can occasionally leave a withdrawn root or native tray
        # message loop alive even after normal teardown. Close remains graceful
        # first; this daemon watchdog only fires if the process is still present.
        if os.name == "nt":
            def force_process_exit():
                time.sleep(6.0)
                os._exit(0)
            threading.Thread(target=force_process_exit,daemon=True,name="Server shutdown watchdog").start()
        if self._refresh_after:
            try:
                self.after_cancel(self._refresh_after)
            except Exception:
                pass
        if self._tray_poll_after:
            try:
                self.after_cancel(self._tray_poll_after)
            except Exception:
                pass
        self.stop_event.set()
        if self.httpd.web_gateways is not None:self.httpd.web_gateways.stop_all()
        if self.tray is not None:
            self.tray.stop()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        self.httpd.db.release_file_guards()
        try:
            self.quit()
        finally:
            self.destroy()

    def exit_server_from_tray(self):
        # The GUI is withdrawn when this is called. Do not display a confirmation
        # dialog with a hidden parent; on Windows 11 that can look like nothing
        # happened. Choosing Close Server from the tray is the confirmation.
        # Let tk_popup return and release its grab before destroying the root.
        # Destroying Tk from inside the popup callback can strand pythonw.exe.
        try:
            self.after_idle(self._shutdown_server)
        except Exception:
            self._shutdown_server()

    def exit_server(self):
        if self._closing:
            return
        if not messagebox.askyesno(
            "Exit Server",
            "Stop the Rental Calendar server?\n\nAdmin and Client PCs will disconnect until it is restarted.",
            parent=self,
        ):
            return
        self._shutdown_server()



def main() -> None:
    set_server_process_identity()
    parser = argparse.ArgumentParser(description="Rental Calendar cross-platform LAN server")
    parser.add_argument("--host", default=HOST, help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--admin-key", default=None, help="Set/replace the remote Admin Key")
    parser.add_argument("--show-admin-key", action="store_true", help="Print the saved Admin Key and exit")
    parser.add_argument("--no-gui", action="store_true", help="Run the legacy terminal dashboard instead of the GUI")
    args = parser.parse_args()

    args.db = args.db.expanduser()
    config_path = default_config_path(args.db)
    admin_key, created = load_or_create_admin_key(config_path, args.admin_key)
    if args.show_admin_key:
        print(admin_key)
        return

    db = Database(args.db)
    db.acquire_file_guards()
    access_store = AdminAccessStore(config_path)
    httpd = RentalHTTPServer((args.host, args.port), Handler, db, admin_key, access_store)
    httpd.config_path=config_path
    httpd.mobile_pairing = MobilePairingStore(config_path)
    httpd.vin_photos = VinPhotoStore(
        default_data_dir() / "vin_photos",
        lambda path: detect_vin_photo(
            path,
            {str(machine.get("vin_last4") or "").upper() for machine in db.list_machines()},
        ),
    )
    threading.Thread(
        target=import_pending_vin_photos,args=(httpd,),daemon=True,name="VIN Photo Database Recovery"
    ).start()
    httpd.web_gateways=WebGatewayManager(db,admin_key,httpd.activity,httpd)
    try:web_config=json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:web_config={}
    for role,default_port in (("admin",8775),("client",8776)):
        if web_config.get(f"{role}_web_enabled"):
            try:httpd.web_gateways.start(role,int(web_config.get(f"{role}_web_port",default_port)))
            except Exception as exc:httpd.activity.add_event(f"Could not start {role} Web broadcast: {exc}")
    actual_port = int(httpd.server_address[1])
    stop_event = threading.Event()
    discover = threading.Thread(
        target=discovery_loop,
        args=(DISCOVERY_PORT, actual_port, stop_event, httpd.activity),
        daemon=True,
        name="LAN Discovery",
    )
    discover.start()
    database_watch=threading.Thread(
        target=database_watch_loop,args=(httpd,stop_event),daemon=True,name="Database file protection"
    )
    database_watch.start()

    if db.migration_backup:
        httpd.activity.add_event(f"Pre-migration database backup: {db.migration_backup.name}")
    httpd.activity.add_event("Server started and is ready for Admin/Client connections")

    graphical_environment = (
        os.name == "nt"
        or sys.platform == "darwin"
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    use_gui = not args.no_gui and tk is not None and graphical_environment
    if use_gui:
        httpd.dashboard_active = True
        serve_thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
            name="Rental HTTP Server",
        )
        serve_thread.start()
        gui = ServerControlGUI(
            httpd, args.host, actual_port, args.db, config_path, admin_key, stop_event
        )
        try:
            gui.mainloop()
        finally:
            stop_event.set()
            try:
                httpd.shutdown()
            except Exception:
                pass
            httpd.server_close()
            serve_thread.join(timeout=2.0)
            httpd.web_gateways.stop_all()
            db.release_file_guards()
        return

    # Headless/terminal fallback remains available for Linux servers without Tk.
    dashboard = ServerDashboard(
        httpd,
        args.host,
        actual_port,
        args.db,
        config_path,
        admin_key,
        created,
        stop_event,
    )
    dashboard.start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        httpd.activity.add_event("Server shutdown requested from keyboard")
    finally:
        stop_event.set()
        httpd.server_close()
        dashboard.stop()
        httpd.web_gateways.stop_all()
        db.release_file_guards()
        if sys.stdout.isatty():
            print("\nRental Calendar server stopped.")


if __name__ == "__main__":
    main()
