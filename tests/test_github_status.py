import json
import unittest
import urllib.error
from unittest.mock import patch

from src.github_status import (
    STATUS_SUMMARY_URL,
    describe_github_incident,
    summarize_status_payload,
)


def _payload(**overrides):
    payload = {
        "status": {"indicator": "none", "description": "All Systems Operational"},
        "components": [
            {"name": "Git Operations", "status": "operational"},
            {"name": "API Requests", "status": "operational"},
        ],
        "incidents": [],
    }
    payload.update(overrides)
    return payload


class StatusSummaryTests(unittest.TestCase):
    def test_healthy_status_produces_no_note(self):
        self.assertIsNone(summarize_status_payload(_payload()))

    def test_open_incident_and_impaired_components_are_summarized(self):
        summary = summarize_status_payload(
            _payload(
                status={
                    "indicator": "major",
                    "description": "Partial System Outage",
                },
                components=[
                    {"name": "Git Operations", "status": "degraded_performance"},
                    {"name": "API Requests", "status": "operational"},
                    {"name": "Copilot", "status": "major_outage"},
                ],
                incidents=[
                    {
                        "name": "Incident with GitHub.com",
                        "status": "investigating",
                        "created_at": "2026-08-17T13:40:03.629Z",
                    }
                ],
            )
        )

        self.assertEqual(
            summary,
            "GitHub status: Partial System Outage"
            " | open incidents: Incident with GitHub.com (investigating) "
            "since 2026-08-17T13:40:03.629Z"
            " | impaired components: Git Operations (degraded_performance), "
            "Copilot (major_outage)",
        )

    def test_component_groups_and_operational_components_are_excluded(self):
        summary = summarize_status_payload(
            _payload(
                status={"indicator": "minor", "description": "Minor Service Outage"},
                components=[
                    {"name": "Group", "status": "degraded_performance", "group": True},
                    {"name": "Webhooks", "status": "operational"},
                    {"name": "Actions", "status": "partial_outage"},
                ],
            )
        )

        self.assertEqual(
            summary,
            "GitHub status: Minor Service Outage"
            " | impaired components: Actions (partial_outage)",
        )

    def test_resolved_incidents_are_ignored(self):
        self.assertIsNone(
            summarize_status_payload(
                _payload(
                    incidents=[
                        {"name": "Earlier incident", "status": "resolved"},
                        {"name": "Postmortem", "status": "postmortem"},
                    ]
                )
            )
        )

    def test_degraded_component_alone_still_reports(self):
        summary = summarize_status_payload(
            _payload(
                components=[{"name": "Issues", "status": "degraded_performance"}],
            )
        )

        self.assertEqual(
            summary,
            "GitHub status: All Systems Operational"
            " | impaired components: Issues (degraded_performance)",
        )

    def test_reported_incidents_and_components_are_bounded(self):
        summary = summarize_status_payload(
            _payload(
                status={"indicator": "major", "description": "Major Service Outage"},
                incidents=[
                    {"name": f"Incident {index}", "status": "investigating"}
                    for index in range(6)
                ],
                components=[
                    {"name": f"Component {index}", "status": "partial_outage"}
                    for index in range(9)
                ],
            )
        )

        self.assertEqual(summary.count("Incident "), 3)
        self.assertEqual(summary.count("Component "), 5)

    def test_unusable_payloads_produce_no_note(self):
        for name, payload in (
            ("not a mapping", ["status"]),
            ("empty", {}),
            ("wrong types", {"status": [], "incidents": "none", "components": 7}),
        ):
            with self.subTest(name=name):
                self.assertIsNone(summarize_status_payload(payload))


class StatusLookupTests(unittest.TestCase):
    def test_successful_lookup_requests_status_page_and_summarizes(self):
        payload = _payload(
            status={"indicator": "major", "description": "Partial System Outage"}
        )

        class _Response:
            def read(self, *_args):
                return json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return False

        with patch(
            "src.github_status.urllib.request.urlopen", return_value=_Response()
        ) as urlopen:
            note = describe_github_incident()

        self.assertEqual(note, "GitHub status: Partial System Outage")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, STATUS_SUMMARY_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

    def test_unreachable_status_page_is_silent(self):
        failures = (
            urllib.error.URLError("unreachable"),
            urllib.error.HTTPError(STATUS_SUMMARY_URL, 503, "busy", {}, None),
            TimeoutError("timed out"),
            OSError("socket closed"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "src.github_status.urllib.request.urlopen", side_effect=failure
                ):
                    self.assertIsNone(describe_github_incident())

    def test_malformed_response_body_is_silent(self):
        class _Response:
            def read(self, *_args):
                return b"<html>status page</html>"

            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return False

        with patch("src.github_status.urllib.request.urlopen", return_value=_Response()):
            self.assertIsNone(describe_github_incident())


if __name__ == "__main__":
    unittest.main()
