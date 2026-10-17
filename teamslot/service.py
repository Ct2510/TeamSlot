from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ServiceError(400, "invalid_time", "Time must be an ISO 8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ServiceError(400, "invalid_time", "Use an ISO 8601 time with a timezone offset.") from None


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_window(start: str, end: str, max_days: int = 31) -> tuple[str, str]:
    start_time = parse_time(start)
    end_time = parse_time(end)
    if start_time >= end_time:
        raise ServiceError(400, "invalid_window", "End must be later than start.")
    if end_time - start_time > timedelta(days=max_days):
        raise ServiceError(400, "invalid_window", f"Time window cannot exceed {max_days} days.")
    return format_time(start_time), format_time(end_time)


def validate_text(value: str, name: str, maximum: int = 100) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ServiceError(400, "invalid_field", f"{name} must contain 1 to {maximum} characters.")
    return value.strip()


def validate_identifier(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ServiceError(400, "invalid_field", f"{name} must be a UUID.")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ServiceError(400, "invalid_field", f"{name} must be a UUID.") from None


class Scheduler:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL REFERENCES resources(id),
                    title TEXT NOT NULL,
                    organizer TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('confirmed', 'cancelled')),
                    cancel_token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK(start < end)
                );
                CREATE INDEX IF NOT EXISTS idx_bookings_active_window
                ON bookings(resource_id, start, end) WHERE status = 'confirmed';
                PRAGMA user_version=1;
                """
            )

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    def create_resource(self, name: str) -> dict:
        name = validate_text(name, "name", 80)
        resource = {"id": str(uuid.uuid4()), "name": name, "created_at": format_time(datetime.now(timezone.utc))}
        try:
            with self.connection() as connection:
                connection.execute(
                    "INSERT INTO resources(id, name, created_at) VALUES (:id, :name, :created_at)", resource
                )
        except sqlite3.IntegrityError:
            raise ServiceError(409, "resource_exists", "A resource with that name already exists.") from None
        return resource

    def list_resources(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM resources ORDER BY name COLLATE NOCASE")]

    def require_resource(self, connection: sqlite3.Connection, resource_id: str):
        if not connection.execute("SELECT 1 FROM resources WHERE id = ?", (resource_id,)).fetchone():
            raise ServiceError(404, "resource_not_found", "Resource does not exist.")

    @staticmethod
    def public_booking(row: sqlite3.Row | dict) -> dict:
        return {key: value for key, value in dict(row).items() if key != "cancel_token_hash"}

    def create_booking(self, resource_id: str, title: str, organizer: str, start: str, end: str) -> dict:
        resource_id = validate_identifier(resource_id, "resource_id")
        title = validate_text(title, "title", 120)
        organizer = validate_text(organizer, "organizer", 80)
        start, end = validate_window(start, end, max_days=1)
        if parse_time(end) - parse_time(start) > timedelta(hours=8):
            raise ServiceError(400, "invalid_window", "A booking cannot exceed 8 hours.")
        cancel_token = secrets.token_urlsafe(32)
        booking = {
            "id": str(uuid.uuid4()), "resource_id": resource_id, "title": title, "organizer": organizer,
            "start": start, "end": end, "status": "confirmed",
            "cancel_token_hash": hashlib.sha256(cancel_token.encode()).hexdigest(),
            "created_at": format_time(datetime.now(timezone.utc)),
        }
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.require_resource(connection, resource_id)
                conflict = connection.execute(
                    "SELECT id FROM bookings WHERE resource_id = ? AND status = 'confirmed' "
                    "AND start < ? AND end > ? LIMIT 1", (resource_id, end, start)
                ).fetchone()
                if conflict:
                    raise ServiceError(409, "booking_conflict", "This resource is already booked for that time.")
                connection.execute(
                    "INSERT INTO bookings VALUES (:id, :resource_id, :title, :organizer, :start, :end, "
                    ":status, :cancel_token_hash, :created_at)", booking
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {**self.public_booking(booking), "cancel_token": cancel_token}

    def list_bookings(self, resource_id: str, start: str, end: str) -> list[dict]:
        resource_id = validate_identifier(resource_id, "resource_id")
        start, end = validate_window(start, end)
        with self.connection() as connection:
            self.require_resource(connection, resource_id)
            rows = connection.execute(
                "SELECT * FROM bookings WHERE resource_id = ? AND status = 'confirmed' "
                "AND start < ? AND end > ? ORDER BY start, id", (resource_id, end, start)
            )
            return [self.public_booking(row) for row in rows]

    def cancel_booking(self, booking_id: str, cancel_token: str) -> dict:
        booking_id = validate_identifier(booking_id, "booking_id")
        if not isinstance(cancel_token, str) or not cancel_token:
            raise ServiceError(403, "invalid_cancel_token", "A valid cancellation token is required.")
        supplied_hash = hashlib.sha256(cancel_token.encode()).hexdigest()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
                if row is None:
                    raise ServiceError(404, "booking_not_found", "Booking does not exist.")
                if not secrets.compare_digest(row["cancel_token_hash"], supplied_hash):
                    raise ServiceError(403, "invalid_cancel_token", "A valid cancellation token is required.")
                connection.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {**self.public_booking(row), "status": "cancelled"}

    def availability(self, resource_id: str, start: str, end: str, duration_minutes: int = 30) -> list[dict]:
        if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int) or not 1 <= duration_minutes <= 480:
            raise ServiceError(400, "invalid_duration", "duration_minutes must be an integer from 1 to 480.")
        start, end = validate_window(start, end)
        bookings = self.list_bookings(resource_id, start, end)
        cursor = parse_time(start)
        window_end = parse_time(end)
        minimum = timedelta(minutes=duration_minutes)
        intervals = []
        for booking in bookings:
            booking_start = max(parse_time(booking["start"]), cursor)
            booking_end = min(parse_time(booking["end"]), window_end)
            if booking_start - cursor >= minimum:
                intervals.append({"start": format_time(cursor), "end": format_time(booking_start)})
            cursor = max(cursor, booking_end)
        if window_end - cursor >= minimum:
            intervals.append({"start": format_time(cursor), "end": format_time(window_end)})
        return intervals
