"""Minimal Open Library API client.

A trimmed port of `openlibrary.api.OpenLibrary` covering only what shelfie
uses. The wire contract (login form, cookie shape, /api/import payload,
/query.json response) is preserved exactly so shelfie keeps working
against an unmodified Open Library web app.
"""

import datetime
import json
import re

import requests


class OLError(Exception):
    def __init__(self, e):
        self.code = e.response.status_code
        self.headers = e.response.headers
        self.text = e.response.text
        Exception.__init__(self, f"{e}. Response: {self.text}")


class OLClient:
    def __init__(self, base_url="https://openlibrary.org"):
        self.base_url = base_url.rstrip("/") if base_url else "https://openlibrary.org"
        self.cookie = None

    def _request(
        self,
        path,
        method="GET",
        data=None,
        headers=None,
        params=None,
        allow_redirects=True,
    ):
        url = self.base_url + path
        headers = dict(headers or {})
        params = params or {}
        if self.cookie:
            headers["Cookie"] = self.cookie

        try:
            response = requests.request(
                method,
                url,
                data=data,
                headers=headers,
                params=params,
                allow_redirects=allow_redirects,
            )
            response.raise_for_status()
            return response
        except requests.HTTPError as e:
            raise OLError(e)

    def login(self, username, password):
        """POST /account/login. OL reads credentials from query params, not
        the form body — keep that quirk."""
        try:
            response = self._request(
                "/account/login",
                method="POST",
                params={"username": username, "password": password},
                allow_redirects=False,
            )
        except OLError as e:
            response = e

        if "Set-Cookie" in response.headers:
            cookies = response.headers["Set-Cookie"].split(",")
            self.cookie = ";".join(c.split(";")[0] for c in cookies)

    def get(self, key, v=None):
        response = self._request(key + ".json", params={"v": v} if v else {})
        return unmarshal(response.json())

    def query(self, q=None, **kw):
        q = dict(q or {})
        q.update(kw)
        q = marshal(q)
        response = self._request("/query.json", params={"query": json.dumps(q)})
        return unmarshal(response.json())

    def import_data(self, data):
        return self._request("/api/import", method="POST", data=data).text


def marshal(data):
    if isinstance(data, list):
        return [marshal(d) for d in data]
    if isinstance(data, dict):
        return {k: marshal(v) for k, v in data.items()}
    if isinstance(data, datetime.datetime):
        return {"type": "/type/datetime", "value": data.isoformat()}
    if isinstance(data, Text):
        return {"type": "/type/text", "value": str(data)}
    if isinstance(data, Reference):
        return {"key": str(data)}
    return data


def unmarshal(d):
    if isinstance(d, list):
        return [unmarshal(v) for v in d]
    if isinstance(d, dict):
        if "key" in d and len(d) == 1:
            return Reference(d["key"])
        if "value" in d and "type" in d:
            if d["type"] == "/type/text":
                return Text(d["value"])
            if d["type"] == "/type/datetime":
                return parse_datetime(d["value"])
            return d["value"]
        return {k: unmarshal(v) for k, v in d.items()}
    return d


def parse_datetime(value):
    if isinstance(value, datetime.datetime):
        return value
    tokens = re.split(r"-|T|:|\.| ", value)
    return datetime.datetime(*map(int, tokens))


class Text(str):
    __slots__ = ()

    def __repr__(self):
        return "<text: %s>" % str.__repr__(self)


class Reference(str):
    __slots__ = ()

    def __repr__(self):
        return "<ref: %s>" % str.__repr__(self)
