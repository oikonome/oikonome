"""Test helper: fetch /export or /export/dump the way a client must.

The data downloads redeem a one-shot ticket minted by POST /api/export/token
after a password step-up (a bare session cookie rides cross-site GETs, so it
is not enough on its own). Every test that reads the export ZIP goes through
here so the step-up shape lives in one place.
"""

DEFAULT_PW = "correct-horse-battery"


def export_get(client, path: str = "/export", password: str = DEFAULT_PW,
               totp_code: str = "", **kw):
    kind = "dump" if path.startswith("/export/dump") else "zip"
    r = client.post("/api/export/token",
                    json={"kind": kind, "password": password,
                          "totp_code": totp_code})
    if r.status_code != 200:
        return r
    return client.get(r.json()["url"], **kw)
