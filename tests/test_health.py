from unittest.mock import patch

from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse


class HealthTests(TestCase):
    def test_health_uses_the_configured_database(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class UnavailableDatabaseTests(SimpleTestCase):
    @patch("config.urls.connection.cursor", side_effect=OperationalError("private details"))
    def test_database_failure_returns_a_generic_response(self, cursor):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        cursor.assert_called_once()
