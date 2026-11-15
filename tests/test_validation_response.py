import os
import unittest

os.environ.setdefault("CPL_APP_NAME", "Test API")
os.environ.setdefault("CPL_APP_ENV", "dev")
os.environ.setdefault("CPL_APP_HOST", "127.0.0.1")
os.environ.setdefault("CPL_APP_PORT", "8000")
os.environ.setdefault("CPL_APP_ALLOWED_HOSTS", "http://localhost")
os.environ.setdefault("CPL_AUTH_JWT_SECRET", "test-secret-that-is-long-enough-for-unit-tests")
os.environ.setdefault("CPL_AUTH_TOKEN_EXPIRE_MINUTES", "60")
os.environ.setdefault("CPL_DB_HOST", "localhost")
os.environ.setdefault("CPL_DB_PORT", "1433")
os.environ.setdefault("CPL_DB_USER", "sa")
os.environ.setdefault("CPL_DB_PWD", "unused")
os.environ.setdefault("CPL_DB_NAME", "unused")
os.environ.setdefault("CPL_DB_POOL_SIZE", "1")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.exceptions import add_exception_handlers
from schemas.auth_schema import AdminCreateRequest


class ValidationResponseTests(unittest.TestCase):
    def test_a_short_contact_number_is_a_422_not_a_server_error(self):
        app = FastAPI()
        add_exception_handlers(app)

        @app.post("/admins")
        def create(body: AdminCreateRequest):
            return {"ok": True}

        response = TestClient(app).post(
            "/admins",
            json={"username": "ada-admin", "email": "ada@example.com", "phone_code": "+91", "phone_number": "900000001"},
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertIn("10 digits", body["message"])
        self.assertIsInstance(body["data"], list)
