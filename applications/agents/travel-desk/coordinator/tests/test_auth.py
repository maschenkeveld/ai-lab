"""Identity and roles: real RSA-signed tokens, a stand-in for Keycloak's key lookup, and the REST API over real HTTP."""
import base64
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from travel_coordinator import api
from travel_coordinator.auth import Authenticator

ISSUER = "http://keycloak.lab:8080/realms/ai-lab"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
auth = Authenticator(enabled=True, issuer=ISSUER, key_resolver=lambda token: KEY.public_key())


def token(user="alice", roles=("desk-requester",), *, azp="travel-desk-web", iss=ISSUER, exp_in=300, key=KEY, **extra) -> str:
    claims = {"iss": iss, "azp": azp, "preferred_username": user, "realm_access": {"roles": list(roles)},
              "iat": int(time.time()), "exp": int(time.time()) + exp_in, **extra}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


def bearer(user="alice", roles=("desk-requester",), **kw) -> dict:
    return {"Authorization": f"Bearer {token(user, roles, **kw)}"}


# ---- the verifier -----------------------------------------------------------------------------------------------


def test_a_good_token_becomes_a_user_with_roles():
    user = auth.verify(token("bob", ("desk-approver",)))
    assert user.username == "bob" and user.is_approver and user.can_request


def test_a_requester_is_not_an_approver():
    user = auth.verify(token("alice", ("desk-requester",)))
    assert user.can_request and not user.is_approver


@pytest.mark.parametrize("bad, why", [
    (lambda: token(exp_in=-60), "expired"),
    (lambda: token(iss="http://evil.example/realms/ai-lab"), "wrong issuer"),
    (lambda: token(azp="some-other-app"), "not issued to a travel desk client"),
    (lambda: token(user=None), "no preferred_username"),
    (lambda: token(key=OTHER_KEY), "signed by someone else"),
    (lambda: "not.a.jwt", "garbage"),
])
def test_bad_tokens_are_rejected(bad, why):
    with pytest.raises(HTTPException) as err:
        auth.verify(bad())
    assert err.value.status_code == 401, why


def test_an_unsigned_token_is_rejected():
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'iss': ISSUER, 'azp': 'travel-desk-web', 'preferred_username': 'bob', 'exp': int(time.time()) + 300})}."
    with pytest.raises(HTTPException) as err:
        auth.verify(forged)
    assert err.value.status_code == 401


def test_algorithm_confusion_is_rejected():
    """The classic attack: sign with HS256 using the realm's public key as the secret. Only RS256 is accepted."""
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    public_pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"iss": ISSUER, "azp": "travel-desk-web", "preferred_username": "bob", "exp": int(time.time()) + 300,
                           "realm_access": {"roles": ["desk-approver"]}}).encode())
    signature = b64(hmac.new(public_pem, f"{head}.{body}".encode(), hashlib.sha256).digest())  # PyJWT refuses to build this; do it by hand
    with pytest.raises(HTTPException) as err:
        auth.verify(f"{head}.{body}.{signature}")
    assert err.value.status_code == 401


def test_the_dependency_needs_a_bearer_token():
    for header in (None, "", "Basic abc", "Bearer"):
        with pytest.raises(HTTPException) as err:
            auth.current(header)
        assert err.value.status_code == 401


def test_with_authentication_off_nobody_is_checked():
    assert Authenticator(enabled=False, key_resolver=lambda t: None).current(None) is None


def test_the_web_app_gets_the_endpoints_it_needs():
    cfg = auth.public_config()
    assert cfg["client_id"] == "travel-desk-web" and cfg["token_endpoint"] == f"{ISSUER}/protocol/openid-connect/token"


# ---- the REST API with authentication on ------------------------------------------------------------------------


@pytest.fixture
async def client(make_desk):
    desk = await make_desk()
    app = api.build_app(desk.service, lifespan=None, auth=auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.desk = desk
        yield c


A = "/desk/api"
ALICE = bearer("alice", ("desk-requester",))
BOB = bearer("bob", ("desk-approver",))
CAROL = bearer("carol", ("desk-approver",))
DAVE = bearer("dave", ("desk-requester", "desk-approver"))
ERIN = bearer("erin", ("desk-requester",))


async def new_trip(client, headers=ALICE, **body) -> dict:
    r = await client.post(f"{A}/trips", json={"request": "culture in vienna", **body}, headers=headers)
    assert r.status_code == 202, r.text
    return r.json()


async def test_config_is_public_but_everything_else_needs_a_token(client):
    assert (await client.get(f"{A}/config")).json()["auth"]["enabled"] is True
    for path in ("/trips", "/approvals", "/audit", "/me"):
        assert (await client.get(f"{A}{path}")).status_code == 401
    assert (await client.post(f"{A}/trips", json={"request": "vienna"})).status_code == 401
    assert (await client.get(f"{A}/trips", headers={"Authorization": "Bearer garbage"})).status_code == 401
    assert (await client.get(f"{A}/trips", headers=bearer(exp_in=-60))).status_code == 401


async def test_who_you_are_comes_from_the_token_not_the_body(client):
    trip = await new_trip(client, ALICE, requested_by="mallory")
    assert trip["requested_by"] == "alice"
    me = (await client.get(f"{A}/me", headers=BOB)).json()
    assert me == {"authenticated": True, "username": "bob", "roles": ["desk-approver"], "is_approver": True}


async def test_a_requester_cannot_decide_or_see_the_queue(client):
    trip = await new_trip(client)
    approval = await client.desk.pending(trip["id"])
    assert (await client.get(f"{A}/approvals", headers=ALICE)).status_code == 403
    r = await client.post(f"{A}/approvals/{approval['id']}/decision", json={"decision": "approve"}, headers=ALICE)
    assert r.status_code == 403 and "desk-approver" in r.json()["detail"]
    assert (await client.desk.store.get_approval(approval["id"]))["status"] == "pending"


async def test_an_approver_decides_and_the_audit_names_the_token_user(client):
    trip = await new_trip(client)
    approval = await client.desk.pending(trip["id"])
    r = await client.post(f"{A}/approvals/{approval['id']}/decision",
                          json={"decision": "approve", "approver": "mallory", "comment": "fine"}, headers=BOB)
    assert r.status_code == 200 and r.json()["decisions"][0]["approver"] == "bob"   # the claimed name was ignored
    await client.desk.until(trip["id"], "completed")
    audit = (await client.get(f"{A}/audit", params={"trip_id": trip["id"]}, headers=BOB)).json()
    assert next(e for e in audit if e["type"] == "approval_approve")["actor"] == "bob"
    assert not any(e["actor"] == "mallory" for e in audit)


async def test_nobody_decides_on_a_trip_they_requested(client):
    trip = await new_trip(client, DAVE)                  # dave is both a requester and an approver
    approval = await client.desk.pending(trip["id"])
    r = await client.post(f"{A}/approvals/{approval['id']}/decision", json={"decision": "approve"}, headers=DAVE)
    assert r.status_code == 403 and "requested" in r.json()["detail"]
    assert (await client.desk.store.get_approval(approval["id"]))["status"] == "pending"
    kinds = [e["type"] for e in (await client.get(f"{A}/audit", params={"trip_id": trip["id"]}, headers=BOB)).json()]
    assert "approval_refused" in kinds                   # the attempt is on the record
    assert (await client.post(f"{A}/approvals/{approval['id']}/decision", json={"decision": "approve"}, headers=BOB)).status_code == 200


async def test_the_same_approver_cannot_approve_twice_even_with_a_valid_token(client):
    client.desk.stub.price_override["VIE"] = 250.0       # two approvers needed
    trip = await new_trip(client)
    approval = await client.desk.pending(trip["id"])
    url = f"{A}/approvals/{approval['id']}/decision"
    assert (await client.post(url, json={"decision": "approve"}, headers=BOB)).status_code == 200
    assert (await client.post(url, json={"decision": "approve"}, headers=BOB)).status_code == 409
    assert (await client.post(url, json={"decision": "approve"}, headers=CAROL)).status_code == 200


async def test_requesters_only_see_their_own_trips(client):
    mine, theirs = await new_trip(client, ALICE), await new_trip(client, ERIN)
    alices = (await client.get(f"{A}/trips", headers=ALICE)).json()      # the database is shared by the tests in this module
    assert mine["id"] in {t["id"] for t in alices} and theirs["id"] not in {t["id"] for t in alices}
    assert {t["requested_by"] for t in alices} == {"alice"}
    assert {t["id"] for t in (await client.get(f"{A}/trips", headers=BOB)).json()} >= {mine["id"], theirs["id"]}
    assert (await client.get(f"{A}/trips/{theirs['id']}", headers=ALICE)).status_code == 403
    assert (await client.get(f"{A}/audit", params={"trip_id": theirs["id"]}, headers=ALICE)).status_code == 403
    assert (await client.post(f"{A}/trips/{theirs['id']}/cancel", json={}, headers=ALICE)).status_code == 403
    assert (await client.get(f"{A}/trips/{theirs['id']}", headers=BOB)).status_code == 200


async def test_a_token_without_a_desk_role_gets_nothing_useful(client):
    nobody = bearer("zed", ())
    assert (await client.post(f"{A}/trips", json={"request": "vienna"}, headers=nobody)).status_code == 403
    assert (await client.get(f"{A}/approvals", headers=nobody)).status_code == 403
