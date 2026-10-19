import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tradebot.app import same_origin


def request(origin, host="tradebot-managed-staging.onrender.com", scheme="http"):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "scheme": scheme, "server": (host, 80), "path": "/", "headers": headers})


@pytest.mark.parametrize("origin", [
    "https://tradebot-managed-staging.onrender.com",  # browser behind Render's TLS proxy
    "http://tradebot-managed-staging.onrender.com",
    None,  # same-origin requests without an Origin header
])
def test_same_host_is_allowed_whatever_the_scheme(origin):
    same_origin(request(origin))


@pytest.mark.parametrize("origin", ["https://evil.example", "https://tradebot-managed-staging.onrender.com.evil.example", "null"])
def test_other_hosts_are_rejected(origin):
    with pytest.raises(HTTPException) as exc:
        same_origin(request(origin))
    assert exc.value.status_code == 403
