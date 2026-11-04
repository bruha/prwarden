"""Read-only lookup of publicly announced GitHub incidents.

The reviewer retries transient GitHub failures for a bounded window. When that
window is exhausted, an operator still has to decide whether the fault is local
or a platform outage. This module answers that question from GitHub's public
status page so the terminal output and the Slack failure notification can say so
directly.

The lookup is best effort by design: it is only attempted after a GitHub command
has already failed, it never raises, and it returns None when the status page is
unreachable, malformed, or reports healthy service.
"""

import json
import urllib.error
import urllib.request
from typing import Any, Optional


STATUS_SUMMARY_URL = "https://www.githubstatus.com/api/v2/summary.json"
STATUS_TIMEOUT_SECONDS = 10
STATUS_RESPONSE_LIMIT_BYTES = 1_000_000
MAX_REPORTED_INCIDENTS = 3
MAX_REPORTED_COMPONENTS = 5
MAX_DESCRIPTION_LENGTH = 300
HEALTHY_INDICATORS = frozenset({"none", ""})
RESOLVED_INCIDENT_STATUSES = frozenset({"resolved", "postmortem"})
OPERATIONAL_COMPONENT_STATUS = "operational"


def _text(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _unresolved_incidents(payload: dict[str, Any]) -> list[str]:
    incidents = payload.get("incidents")
    if not isinstance(incidents, list):
        return []
    described: list[str] = []
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        status = _text(incident.get("status")).casefold()
        if status in RESOLVED_INCIDENT_STATUSES:
            continue
        name = _text(incident.get("name")) or "unnamed incident"
        started = _text(incident.get("created_at"), limit=40)
        detail = f"{name} ({status})" if status else name
        if started:
            detail += f" since {started}"
        described.append(detail)
        if len(described) == MAX_REPORTED_INCIDENTS:
            break
    return described


def _impaired_components(payload: dict[str, Any]) -> list[str]:
    components = payload.get("components")
    if not isinstance(components, list):
        return []
    described: list[str] = []
    for component in components:
        if not isinstance(component, dict) or component.get("group"):
            continue
        status = _text(component.get("status")).casefold()
        if not status or status == OPERATIONAL_COMPONENT_STATUS:
            continue
        name = _text(component.get("name")) or "unnamed component"
        described.append(f"{name} ({status})")
        if len(described) == MAX_REPORTED_COMPONENTS:
            break
    return described


def summarize_status_payload(payload: Any) -> Optional[str]:
    """Return a one-line incident summary, or None when GitHub reports health."""

    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    status = status if isinstance(status, dict) else {}
    indicator = _text(status.get("indicator"), limit=40).casefold()
    description = _text(status.get("description"), limit=MAX_DESCRIPTION_LENGTH)
    incidents = _unresolved_incidents(payload)
    components = _impaired_components(payload)

    if indicator in HEALTHY_INDICATORS and not incidents and not components:
        return None

    parts = [f"GitHub status: {description or indicator or 'not operational'}"]
    if incidents:
        parts.append("open incidents: " + "; ".join(incidents))
    if components:
        parts.append("impaired components: " + ", ".join(components))
    return " | ".join(parts)


def describe_github_incident(
    url: str = STATUS_SUMMARY_URL,
    timeout_seconds: int = STATUS_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Return a summary of any announced GitHub incident, or None.

    Every failure mode is treated as "no information available" so that a status
    lookup can never mask or replace the original command failure.
    """

    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(STATUS_RESPONSE_LIMIT_BYTES)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return None
    return summarize_status_payload(payload)
