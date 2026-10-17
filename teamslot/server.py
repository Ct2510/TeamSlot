from __future__ import annotations

import json
import logging
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .service import Scheduler, ServiceError


LOGGER = logging.getLogger("teamslot")
MAX_BODY_BYTES = 16_384
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def required_fields(payload: dict, fields: set[str]) -> dict:
    if set(payload) != fields:
        raise ServiceError(400, "invalid_fields", "Expected exactly these fields: " + ", ".join(sorted(fields)))
    return payload


class TeamSlotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], scheduler: Scheduler):
        self.scheduler = scheduler
        super().__init__(address, TeamSlotHandler)


class TeamSlotHandler(BaseHTTPRequestHandler):
    server_version = "TeamSlot/" + __version__
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, message, *arguments):
        LOGGER.info("%s %s", self.command, urlsplit(self.path).path)

    def respond(self, status: int, payload, content_type: str = "application/json; charset=utf-8"):
        if isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise ServiceError(400, "invalid_transfer_encoding", "Chunked request bodies are not supported.")
        if self.headers.get_content_type() != "application/json":
            raise ServiceError(415, "unsupported_media_type", "Send Content-Type: application/json.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise ServiceError(400, "invalid_length", "Exactly one Content-Length header is required.")
        try:
            length = int(lengths[0])
        except ValueError:
            raise ServiceError(400, "invalid_length", "Content-Length must be an integer.") from None
        if length < 0:
            raise ServiceError(400, "invalid_length", "Content-Length cannot be negative.")
        if length > MAX_BODY_BYTES:
            raise ServiceError(413, "body_too_large", "Request body exceeds 16 KiB.")
        try:
            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                raise ValueError("Incomplete request body")
            payload = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ServiceError(400, "invalid_json", "Body must contain valid UTF-8 JSON.") from None
        if not isinstance(payload, dict):
            raise ServiceError(400, "invalid_json", "Body must be a JSON object.")
        return payload

    def query(self, encoded: str, allowed: set[str], required: set[str]) -> dict:
        parameters = parse_qs(encoded, keep_blank_values=True)
        if not required <= set(parameters) or not set(parameters) <= allowed:
            raise ServiceError(400, "invalid_query", "Required query parameters: " + ", ".join(sorted(required)))
        if any(len(values) != 1 or not values[0] for values in parameters.values()):
            raise ServiceError(400, "invalid_query", "Query parameters must be non-empty and appear only once.")
        return {key: values[0] for key, values in parameters.items()}

    def dispatch(self):
        route = urlsplit(self.path)
        scheduler = self.server.scheduler
        if self.command == "GET" and route.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[route.path]
            self.respond(200, (Path(__file__).parent / "static" / filename).read_bytes(), content_type)
            return
        if route.path == "/api/health" and self.command == "GET":
            with scheduler.connection() as connection:
                connection.execute("SELECT 1").fetchone()
            self.respond(200, {"status": "ok", "version": __version__})
            return
        if route.path == "/api/resources":
            if self.command == "GET":
                self.respond(200, {"resources": scheduler.list_resources()})
                return
            if self.command == "POST":
                payload = required_fields(self.read_json(), {"name"})
                self.respond(201, scheduler.create_resource(**payload))
                return
            raise ServiceError(405, "method_not_allowed", "Use GET or POST for resources.")
        if route.path in {"/api/bookings", "/api/availability"} and self.command == "GET":
            required = {"resource_id", "start", "end"}
            allowed = required | ({"duration_minutes"} if route.path == "/api/availability" else set())
            parameters = self.query(route.query, allowed, required)
            if route.path == "/api/availability":
                try:
                    parameters["duration_minutes"] = int(parameters.get("duration_minutes", "30"))
                except ValueError:
                    raise ServiceError(400, "invalid_duration", "duration_minutes must be an integer.") from None
                self.respond(200, {"intervals": scheduler.availability(**parameters)})
            else:
                self.respond(200, {"bookings": scheduler.list_bookings(**parameters)})
            return
        if route.path == "/api/bookings" and self.command == "POST":
            payload = required_fields(self.read_json(), {"resource_id", "title", "organizer", "start", "end"})
            self.respond(201, scheduler.create_booking(**payload))
            return
        cancellation = re.fullmatch(r"/api/bookings/([^/]+)/cancel", route.path)
        if cancellation and self.command == "POST":
            required_fields(self.read_json(), set())
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            self.respond(200, scheduler.cancel_booking(cancellation.group(1), token))
            return
        if route.path in {"/api/bookings", "/api/availability", "/api/health"} or cancellation:
            raise ServiceError(405, "method_not_allowed", "HTTP method is not supported for this endpoint.")
        raise ServiceError(404, "not_found", "Endpoint does not exist.")

    def handle_request(self):
        try:
            self.dispatch()
        except ServiceError as error:
            self.respond(error.status, {"error": {"code": error.code, "message": str(error)}})
        except sqlite3.OperationalError:
            LOGGER.exception("Database unavailable")
            self.respond(503, {"error": {"code": "database_unavailable", "message": "Database is temporarily unavailable."}})
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except Exception:
            LOGGER.exception("Unhandled request error")
            self.respond(500, {"error": {"code": "internal_error", "message": "An unexpected error occurred."}})

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request
    do_PATCH = handle_request
    do_HEAD = handle_request
    do_OPTIONS = handle_request
