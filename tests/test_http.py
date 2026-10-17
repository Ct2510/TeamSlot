import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from teamslot.server import TeamSlotServer
from teamslot.service import Scheduler


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        scheduler = Scheduler(Path(cls.temporary.name) / "test.db")
        cls.server = TeamSlotServer(("127.0.0.1", 0), scheduler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def request(self, method, path, payload=None, headers=None, raw=None):
        request_headers = dict(headers or {})
        if payload is not None:
            raw = json.dumps(payload).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(self.base_url + path, data=raw, method=method, headers=request_headers)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            body = json.loads(content) if "application/json" in response.headers.get("Content-Type", "") and content else content
            return response.status, body, response.headers

    def new_resource(self):
        status, resource, headers = self.request("POST", "/api/resources", {"name": self.id()})
        self.assertEqual(status, 201)
        return resource

    def test_health(self):
        status, payload, headers = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_create_list_book_conflict_cancel_flow(self):
        resource = self.new_resource()
        status, listed, headers = self.request("GET", "/api/resources")
        self.assertIn(resource, listed["resources"])
        booking_payload = {"resource_id": resource["id"], "title": "Planning", "organizer": "Demo", "start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z"}
        status, booking, headers = self.request("POST", "/api/bookings", booking_payload)
        self.assertEqual(status, 201)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, conflict, headers = self.request("POST", "/api/bookings", booking_payload)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "booking_conflict")
        query = urlencode({"resource_id": resource["id"], "start": "2030-01-01T09:00:00Z", "end": "2030-01-01T12:00:00Z"})
        status, listed, headers = self.request("GET", "/api/bookings?" + query)
        self.assertEqual(len(listed["bookings"]), 1)
        self.assertNotIn("cancel_token", listed["bookings"][0])
        status, availability, headers = self.request("GET", "/api/availability?" + query)
        self.assertEqual(status, 200)
        self.assertEqual(len(availability["intervals"]), 2)
        cancel_path = f"/api/bookings/{booking['id']}/cancel"
        self.assertEqual(self.request("POST", cancel_path, {})[0], 403)
        self.assertEqual(self.request("POST", cancel_path, {}, {"Authorization": "Bearer " + booking["cancel_token"]})[0], 200)
        self.assertEqual(self.request("POST", "/api/bookings", booking_payload)[0], 201)

    def test_reject_invalid_json(self):
        for body in [b"{", b"[]", b"null", b'"text"', b"\xff"]:
            with self.subTest(body=body):
                status, payload, headers = self.request("POST", "/api/resources", raw=body, headers={"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_reject_wrong_media_type(self):
        self.assertEqual(self.request("POST", "/api/resources", raw=b"hello")[0], 415)

    def test_reject_large_body(self):
        self.assertEqual(self.request("POST", "/api/resources", {"name": "a" * 17_000})[0], 413)

    def test_reject_extra_or_missing_fields(self):
        for payload in [{}, {"name": "Demo", "admin": True}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/api/resources", payload)[0], 400)

    def test_reject_invalid_booking_fields(self):
        payload = {"resource_id": None, "title": "", "organizer": "", "start": None, "end": None}
        status, body, headers = self.request("POST", "/api/bookings", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_field")

    def test_reject_missing_duplicate_or_unknown_query_parameters(self):
        for query in ["", "resource_id=a&resource_id=b&start=x&end=y", "resource_id=a&start=x&end=y&extra=z", "resource_id=&start=x&end=y"]:
            with self.subTest(query=query):
                status, body, headers = self.request("GET", "/api/bookings?" + query)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_query")

    def test_reject_non_integer_duration(self):
        query = urlencode({"resource_id": "a", "start": "x", "end": "y", "duration_minutes": "1.5"})
        self.assertEqual(self.request("GET", "/api/availability?" + query)[0], 400)

    def test_static_assets_and_security_headers(self):
        for path in ["/", "/app.js", "/styles.css"]:
            with self.subTest(path=path):
                status, body, headers = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_unknown_route_and_static_traversal(self):
        for path in ["/missing", "/../service.py", "/static/../service.py", "/.env"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_unsupported_method(self):
        self.assertEqual(self.request("DELETE", "/api/resources")[0], 405)

    def test_head_has_no_response_body(self):
        status, body, headers = self.request("HEAD", "/api/health")
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
