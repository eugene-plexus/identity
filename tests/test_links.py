"""Pending identity links: file / list / approve / reject + audience guards.

The audience matrix on links is the most carefully scoped on this
component — exercising each route with the wrong audience is the
main thing this file is for.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient


def _sample_link_body() -> dict[str, object]:
    return {
        "linkId": "00000000-0000-0000-0000-000000000001",
        "platform": "discord",
        "accountId": "discord-user-123",
        "displayName": "Sarah",
        "handle": "sarah#1234",
        "firstSeen": datetime.now(UTC).isoformat(),
        "triggeringMessage": "@eugene hey!",
        "status": "pending",
    }


# --------------------------------------------------------------------------- #
# Filing pending links (service-only)
# --------------------------------------------------------------------------- #


def test_file_pending_link_via_service_token(
    authed_client: TestClient, connector_service_token: str
) -> None:
    response = authed_client.post(
        "/v1/identity/links/pending",
        json=_sample_link_body(),
        headers={"Authorization": f"Bearer {connector_service_token}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["platform"] == "discord"


def test_file_pending_link_rejected_for_operator(
    authed_operator_client: TestClient,
) -> None:
    """Even the operator cannot file a pending link — that's by design.
    Only connector adapters introduce unknown identities, and operator-
    forged entries would bypass the spoof-resistance design."""
    response = authed_operator_client.post(
        "/v1/identity/links/pending", json=_sample_link_body()
    )
    assert response.status_code == 401


def test_file_duplicate_pending_link_returns_409(
    authed_client: TestClient, connector_service_token: str
) -> None:
    headers = {"Authorization": f"Bearer {connector_service_token}"}
    body = _sample_link_body()
    first = authed_client.post(
        "/v1/identity/links/pending", json=body, headers=headers
    )
    assert first.status_code == 201
    body["linkId"] = "00000000-0000-0000-0000-000000000002"  # different id, same (platform, account)
    second = authed_client.post(
        "/v1/identity/links/pending", json=body, headers=headers
    )
    assert second.status_code == 409


# --------------------------------------------------------------------------- #
# Listing (either audience)
# --------------------------------------------------------------------------- #


def test_list_pending_links_empty_to_start(client: TestClient) -> None:
    response = client.get("/v1/identity/links/pending")
    assert response.status_code == 200
    assert response.json() == {"links": []}


def test_list_pending_links_surfaces_filed(
    authed_client: TestClient, connector_service_token: str, operator_token: str
) -> None:
    authed_client.post(
        "/v1/identity/links/pending",
        json=_sample_link_body(),
        headers={"Authorization": f"Bearer {connector_service_token}"},
    )
    # Operator UI polls list.
    response = authed_client.get(
        "/v1/identity/links/pending",
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert response.status_code == 200
    assert len(response.json()["links"]) == 1


# --------------------------------------------------------------------------- #
# Approval flow (operator-only)
# --------------------------------------------------------------------------- #


def test_approve_creates_new_person_with_alias(
    authed_client: TestClient,
    connector_service_token: str,
    operator_token: str,
) -> None:
    body = _sample_link_body()
    filed = authed_client.post(
        "/v1/identity/links/pending",
        json=body,
        headers={"Authorization": f"Bearer {connector_service_token}"},
    ).json()
    link_id = filed["linkId"]

    approve = authed_client.post(
        f"/v1/identity/links/pending/{link_id}/approve",
        json={"displayName": "Sarah", "relationshipNote": "my wife"},
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert approve.status_code == 200, approve.text
    payload = approve.json()
    assert payload["link"]["status"] == "approved"
    person = payload["person"]
    assert person["displayName"] == "Sarah"
    assert person["relationshipNote"] == "my wife"
    # Alias from the link is attached to the person.
    aliases = person["aliases"]
    assert any(
        a["platform"] == "discord" and a["accountId"] == "discord-user-123"
        for a in aliases
    )


def test_approve_aliases_onto_existing_person(
    authed_client: TestClient,
    authed_operator_client: TestClient,
    connector_service_token: str,
    operator_token: str,
) -> None:
    # Pre-create a person via the operator-only POST.
    existing = authed_operator_client.post(
        "/v1/identity/persons",
        json={"displayName": "Sarah"},
    ).json()
    pid = existing["personId"]

    body = _sample_link_body()
    filed = authed_client.post(
        "/v1/identity/links/pending",
        json=body,
        headers={"Authorization": f"Bearer {connector_service_token}"},
    ).json()
    link_id = filed["linkId"]

    approve = authed_client.post(
        f"/v1/identity/links/pending/{link_id}/approve",
        json={"linkAsPersonId": pid},
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert approve.status_code == 200, approve.text
    payload = approve.json()
    assert payload["person"]["personId"] == pid
    # Alias from the link is attached to the EXISTING person.
    aliases = payload["person"]["aliases"]
    assert any(a["accountId"] == "discord-user-123" for a in aliases)


def test_approve_400_when_neither_or_both_supplied(
    authed_client: TestClient,
    connector_service_token: str,
    operator_token: str,
) -> None:
    filed = authed_client.post(
        "/v1/identity/links/pending",
        json=_sample_link_body(),
        headers={"Authorization": f"Bearer {connector_service_token}"},
    ).json()
    link_id = filed["linkId"]

    # Neither — 400.
    neither = authed_client.post(
        f"/v1/identity/links/pending/{link_id}/approve",
        json={},
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert neither.status_code == 400

    # Both — 400.
    both = authed_client.post(
        f"/v1/identity/links/pending/{link_id}/approve",
        json={
            "linkAsPersonId": "00000000-0000-0000-0000-000000000003",
            "displayName": "Conflict",
        },
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert both.status_code == 400


def test_approve_rejected_for_service_token(
    authed_client: TestClient, connector_service_token: str
) -> None:
    filed = authed_client.post(
        "/v1/identity/links/pending",
        json=_sample_link_body(),
        headers={"Authorization": f"Bearer {connector_service_token}"},
    ).json()
    response = authed_client.post(
        f"/v1/identity/links/pending/{filed['linkId']}/approve",
        json={"displayName": "Hacker"},
        headers={"Authorization": f"Bearer {connector_service_token}"},
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Reject (operator-only)
# --------------------------------------------------------------------------- #


def test_reject_flips_status(
    authed_client: TestClient,
    connector_service_token: str,
    operator_token: str,
) -> None:
    filed = authed_client.post(
        "/v1/identity/links/pending",
        json=_sample_link_body(),
        headers={"Authorization": f"Bearer {connector_service_token}"},
    ).json()
    response = authed_client.post(
        f"/v1/identity/links/pending/{filed['linkId']}/reject",
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert response.status_code == 204

    # The link is no longer pending — filing again with the same
    # (platform, account) MUST be accepted (the unique-pending index
    # only constrains rows in `status='pending'`).
    refiled = authed_client.post(
        "/v1/identity/links/pending",
        json=_sample_link_body(),
        headers={"Authorization": f"Bearer {connector_service_token}"},
    )
    assert refiled.status_code == 201


def test_reject_unknown_link_returns_404(
    authed_operator_client: TestClient,
) -> None:
    response = authed_operator_client.post(
        "/v1/identity/links/pending/00000000-0000-0000-0000-000000000099/reject"
    )
    assert response.status_code == 404
