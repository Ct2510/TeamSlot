import concurrent.futures
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from teamslot.service import Scheduler, ServiceError, parse_time


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "teamslot.db"
        self.scheduler = Scheduler(self.database)
        self.resource = self.scheduler.create_resource("Meeting Room A")

    def book(self, start="2030-01-01T10:00:00Z", end="2030-01-01T11:00:00Z", **changes):
        payload = {
            "resource_id": self.resource["id"], "title": "Planning", "organizer": "Demo user",
            "start": start, "end": end,
        }
        payload.update(changes)
        return self.scheduler.create_booking(**payload)

    def assert_error(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_resource_names_are_trimmed_and_unique(self):
        self.assert_error("resource_exists", lambda: self.scheduler.create_resource(" meeting room a "))
        self.assertEqual(self.scheduler.list_resources(), [self.resource])

    def test_invalid_resource_names(self):
        for name in [None, 12, "", "  ", "a" * 81]:
            with self.subTest(name=name):
                self.assert_error("invalid_field", lambda: self.scheduler.create_resource(name))

    def test_booking_normalizes_offsets_to_utc(self):
        booking = self.book("2030-01-01T18:00:00+08:00", "2030-01-01T19:00:00+08:00")
        self.assertEqual(booking["start"], "2030-01-01T10:00:00.000000Z")
        self.assertEqual(booking["end"], "2030-01-01T11:00:00.000000Z")

    def test_overlap_variants_are_rejected(self):
        self.book()
        for start, end in [("09:30", "10:30"), ("10:30", "11:30"), ("10:15", "10:45"), ("09:00", "12:00"), ("10:00", "11:00")]:
            with self.subTest(start=start, end=end):
                self.assert_error("booking_conflict", lambda: self.book(f"2030-01-01T{start}:00Z", f"2030-01-01T{end}:00Z"))

    def test_adjacent_bookings_are_allowed(self):
        self.book()
        self.book("2030-01-01T09:00:00Z", "2030-01-01T10:00:00Z")
        self.book("2030-01-01T11:00:00Z", "2030-01-01T12:00:00Z")
        self.assertEqual(len(self.scheduler.list_bookings(self.resource["id"], "2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z")), 3)

    def test_other_resources_can_share_the_same_time(self):
        self.book()
        other = self.scheduler.create_resource("Room B")
        self.assertEqual(self.book(resource_id=other["id"])["resource_id"], other["id"])

    def test_offset_equivalent_overlap_is_rejected(self):
        self.book()
        self.assert_error("booking_conflict", lambda: self.book("2030-01-01T18:30:00+08:00", "2030-01-01T19:30:00+08:00"))

    def test_invalid_time_values(self):
        for value in [None, 12, "bad", "2030-01-01", "2030-01-01T10:00:00", "9999-12-31T23:59:59-08:00"]:
            with self.subTest(value=value):
                self.assert_error("invalid_time", lambda: self.book(start=value))

    def test_invalid_durations(self):
        for start, end in [("10:00", "10:00"), ("11:00", "10:00"), ("01:00", "10:00")]:
            with self.subTest(start=start, end=end):
                self.assert_error("invalid_window", lambda: self.book(f"2030-01-01T{start}:00Z", f"2030-01-01T{end}:00Z"))

    def test_unknown_resource_is_rejected(self):
        self.assert_error("resource_not_found", lambda: self.book(resource_id=str(uuid.uuid4())))

    def test_invalid_identifiers_are_rejected(self):
        for identifier in [None, "bad", 17]:
            with self.subTest(identifier=identifier):
                self.assert_error("invalid_field", lambda: self.book(resource_id=identifier))

    def test_cancel_requires_matching_capability(self):
        booking = self.book()
        self.assert_error("invalid_cancel_token", lambda: self.scheduler.cancel_booking(booking["id"], "wrong"))
        self.assert_error("invalid_cancel_token", lambda: self.scheduler.cancel_booking(booking["id"], ""))
        self.assertEqual(self.scheduler.cancel_booking(booking["id"], booking["cancel_token"])["status"], "cancelled")

    def test_cancel_is_idempotent_and_releases_time(self):
        booking = self.book()
        for attempt in range(2):
            self.assertEqual(self.scheduler.cancel_booking(booking["id"], booking["cancel_token"])["status"], "cancelled")
        self.assertEqual(self.book()["status"], "confirmed")

    def test_cancel_missing_booking(self):
        self.assert_error("booking_not_found", lambda: self.scheduler.cancel_booking(str(uuid.uuid4()), "token"))

    def test_public_list_contains_no_tokens_or_hashes(self):
        self.book()
        bookings = self.scheduler.list_bookings(self.resource["id"], "2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z")
        self.assertEqual(len(bookings), 1)
        self.assertNotIn("cancel_token", bookings[0])
        self.assertNotIn("cancel_token_hash", bookings[0])

    def test_database_stores_only_token_digest(self):
        booking = self.book()
        with self.scheduler.connection() as connection:
            digest = connection.execute("SELECT cancel_token_hash FROM bookings").fetchone()[0]
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, booking["cancel_token"])

    def test_availability_clips_bookings_and_filters_short_gaps(self):
        self.book("2030-01-01T08:00:00Z", "2030-01-01T09:30:00Z")
        self.book("2030-01-01T10:00:00Z", "2030-01-01T10:45:00Z")
        self.book("2030-01-01T11:00:00Z", "2030-01-01T13:00:00Z")
        intervals = self.scheduler.availability(self.resource["id"], "2030-01-01T09:00:00Z", "2030-01-01T12:00:00Z", 30)
        self.assertEqual(intervals, [{"start": "2030-01-01T09:30:00.000000Z", "end": "2030-01-01T10:00:00.000000Z"}])

    def test_empty_calendar_is_fully_available(self):
        intervals = self.scheduler.availability(self.resource["id"], "2030-01-01T09:00:00Z", "2030-01-01T12:00:00Z")
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["end"], "2030-01-01T12:00:00.000000Z")

    def test_fully_booked_calendar_has_no_availability(self):
        self.book()
        self.assertEqual(self.scheduler.availability(self.resource["id"], "2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z"), [])

    def test_invalid_availability_duration(self):
        for duration in [0, -1, 481, True, "30", 1.5]:
            with self.subTest(duration=duration):
                self.assert_error("invalid_duration", lambda: self.scheduler.availability(self.resource["id"], "2030-01-01T09:00:00Z", "2030-01-01T12:00:00Z", duration))

    def test_query_window_is_limited(self):
        self.assert_error("invalid_window", lambda: self.scheduler.list_bookings(self.resource["id"], "2030-01-01T00:00:00Z", "2030-03-01T00:00:00Z"))

    def test_restart_preserves_data(self):
        self.book()
        restarted = Scheduler(self.database)
        self.assertEqual(restarted.list_resources(), [self.resource])
        self.assertEqual(len(restarted.list_bookings(self.resource["id"], "2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z")), 1)

    def test_concurrent_requests_allow_exactly_one_winner(self):
        workers = 8
        barrier = threading.Barrier(workers)

        def reserve_once(attempt):
            independent_scheduler = Scheduler(self.database)
            barrier.wait(timeout=10)
            try:
                independent_scheduler.create_booking(self.resource["id"], f"Attempt {attempt}", "Concurrent user", "2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z")
                return "confirmed"
            except ServiceError as error:
                return error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(reserve_once, range(workers)))
        self.assertEqual(results.count("confirmed"), 1)
        self.assertEqual(results.count("booking_conflict"), workers - 1)

    def test_failed_transaction_does_not_poison_next_write(self):
        self.book()
        self.assert_error("booking_conflict", self.book)
        self.assertEqual(self.book("2030-01-01T12:00:00Z", "2030-01-01T13:00:00Z")["status"], "confirmed")

    def test_fractional_seconds_are_preserved(self):
        booking = self.book("2030-01-01T10:00:00.100001Z", "2030-01-01T10:00:00.100002Z")
        self.assertLess(parse_time(booking["start"]), parse_time(booking["end"]))


if __name__ == "__main__":
    unittest.main()
