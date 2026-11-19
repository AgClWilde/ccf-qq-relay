"""Read-only CCFolia client using the site's Firebase Auth / Firestore backend.

No room, member, chat, or account mutations are exposed. Public configuration
and field names are from CCFolia 1.37.6's own application bundle (2026-10-15).
Firebase API keys identify a public application; user tokens remain private.
Errors intentionally contain no request/response bodies, credentials, or URLs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
import re
import time
from typing import Any

import aiohttp


API_KEY = "AIzaSyAMlcPs4ekVSBdzpRdEloqQ8lIgP9lEnRI"
PROJECT_ID = "ccfolia-160aa"
AUTH_ORIGIN = "https://identitytoolkit.googleapis.com"
TOKEN_ORIGIN = "https://securetoken.googleapis.com"
FIRESTORE_ORIGIN = "https://firestore.googleapis.com"
DOCUMENT_ROOT = f"projects/{PROJECT_ID}/databases/(default)/documents"
_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=15)
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,1500}\Z")
_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z\Z")
_SAFE_CODES = frozenset({
    "BAD_REQUEST", "UNAUTHENTICATED", "NO_PERMISSION", "NOT_FOUND",
    "INVALID_LOGIN", "PASSWORD_LOGIN_DISABLED", "REAUTH_REQUIRED",
    "LIMIT_EXCEEDED", "SERVICE_UNAVAILABLE", "REQUEST_TIMEOUT",
    "NETWORK_ERROR", "INVALID_RESPONSE", "INDEX_REQUIRED", "API_ERROR",
})
_STATUS_CODES = {
    400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "NO_PERMISSION",
    404: "NOT_FOUND", 408: "REQUEST_TIMEOUT", 429: "LIMIT_EXCEEDED",
    500: "SERVICE_UNAVAILABLE", 502: "SERVICE_UNAVAILABLE",
    503: "SERVICE_UNAVAILABLE", 504: "SERVICE_UNAVAILABLE",
}
_AUTH_CODES = {
    "EMAIL_NOT_FOUND": "INVALID_LOGIN", "INVALID_PASSWORD": "INVALID_LOGIN",
    "INVALID_LOGIN_CREDENTIALS": "INVALID_LOGIN", "USER_DISABLED": "REAUTH_REQUIRED",
    "USER_NOT_FOUND": "REAUTH_REQUIRED", "TOKEN_EXPIRED": "REAUTH_REQUIRED",
    "INVALID_REFRESH_TOKEN": "REAUTH_REQUIRED", "INVALID_ID_TOKEN": "UNAUTHENTICATED",
    "MISSING_REFRESH_TOKEN": "REAUTH_REQUIRED", "TOO_MANY_ATTEMPTS_TRY_LATER": "LIMIT_EXCEEDED",
    "OPERATION_NOT_ALLOWED": "PASSWORD_LOGIN_DISABLED", "UNAUTHENTICATED": "UNAUTHENTICATED",
    "PERMISSION_DENIED": "NO_PERMISSION", "NOT_FOUND": "NOT_FOUND",
    "RESOURCE_EXHAUSTED": "LIMIT_EXCEEDED", "UNAVAILABLE": "SERVICE_UNAVAILABLE",
    "FAILED_PRECONDITION": "INDEX_REQUIRED",
}


class CCFoliaError(Exception):
    """A credential-free error suitable for logs and QQ status messages."""

    def __init__(self, code: str = "API_ERROR", status: int | None = None):
        self.code = code if isinstance(code, str) and code in _SAFE_CODES else "API_ERROR"
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        super().__init__(f"CCFolia: {self.code}")


def _segment(value: Any) -> str:
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
        raise CCFoliaError("BAD_REQUEST")
    return value


def canonical_timestamp(value: Any) -> str:
    """Normalize Firestore timestamps without losing nanosecond cursor precision."""
    if not isinstance(value, str):
        raise CCFoliaError("INVALID_RESPONSE")
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise CCFoliaError("INVALID_RESPONSE")
    try:
        datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise CCFoliaError("INVALID_RESPONSE") from None
    return match[1] + "." + (match[2] or "").ljust(9, "0") + "Z"


def public_message(message: Mapping[str, Any]) -> bool:
    """Exclude direct messages, private channels, secret dice, and removals.

    A raw Firestore room page can include DMs. Never infer publicity merely from
    the account being able to read it. Call again at persistence/delivery edges.
    """
    if message.get("_hidden") or message.get("_removed") or message.get("removed") or message.get("to"):
        return False
    extend = message.get("extend")
    if isinstance(extend, Mapping):
        roll = extend.get("roll")
        if isinstance(roll, Mapping) and roll.get("secret"):
            return False
    return True


def decode_value(value: Any, *, _depth: int = 0) -> Any:
    """Decode only documented Firestore Value variants, with bounded nesting."""
    if not isinstance(value, dict) or len(value) != 1 or _depth > 30:
        raise CCFoliaError("INVALID_RESPONSE")
    kind, content = next(iter(value.items()))
    if kind == "nullValue":
        return None
    if kind in {"stringValue", "timestampValue", "referenceValue", "bytesValue"}:
        if not isinstance(content, str):
            raise CCFoliaError("INVALID_RESPONSE")
        return content
    if kind == "booleanValue" and isinstance(content, bool):
        return content
    if kind == "integerValue":
        try:
            if isinstance(content, bool):
                raise ValueError
            return int(content)
        except (ValueError, TypeError):
            raise CCFoliaError("INVALID_RESPONSE") from None
    if kind == "doubleValue" and isinstance(content, (int, float, str)):
        try:
            return float(content)
        except ValueError:
            raise CCFoliaError("INVALID_RESPONSE") from None
    if kind == "arrayValue" and isinstance(content, dict):
        values = content.get("values", [])
        if isinstance(values, list):
            return [decode_value(item, _depth=_depth + 1) for item in values]
    if kind == "mapValue" and isinstance(content, dict):
        fields = content.get("fields", {})
        if isinstance(fields, dict):
            return {key: decode_value(item, _depth=_depth + 1) for key, item in fields.items()}
    if kind == "geoPointValue" and isinstance(content, dict):
        return content
    raise CCFoliaError("INVALID_RESPONSE")


def decode_document(document: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("name"), str):
        raise CCFoliaError("INVALID_RESPONSE")
    fields = document.get("fields", {})
    if not isinstance(fields, dict):
        raise CCFoliaError("INVALID_RESPONSE")
    identifier = document["name"].rsplit("/", 1)[-1]
    try:
        _segment(identifier)
    except CCFoliaError:
        raise CCFoliaError("INVALID_RESPONSE") from None
    return identifier, {key: decode_value(value) for key, value in fields.items()}


class CCFolia:
    def __init__(self, session: aiohttp.ClientSession, token: str = "", refresh_token: str = ""):
        self.session = session
        self.token = token
        self.refresh_token = refresh_token
        self.user_id = ""
        self._expires_at = 0.0
        self._refresh_lock = asyncio.Lock()
        self._channels: dict[str, dict[str, str]] = {}

    def __repr__(self) -> str:
        return "<CCFolia authenticated>" if self.token or self.refresh_token else "<CCFolia unauthenticated>"

    async def _request(
        self, method: str, url: str, *, payload: Any = None,
        form: Mapping[str, str] | None = None, authenticated: bool = False,
    ) -> Any:
        # These URL values are assembled internally. Never follow redirects with
        # a password or Authorization header, nor inherit session-wide tokens.
        headers = {"Accept": "application/json", "Origin": "https://ccfolia.com",
                   "Referer": "https://ccfolia.com/",
                   "Authorization": f"Bearer {self.token}" if authenticated and self.token else ""}
        try:
            async with self.session.request(
                method, url, json=payload, data=form, headers=headers,
                allow_redirects=False, timeout=_TIMEOUT,
            ) as response:
                status = response.status
                try:
                    result = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    raise CCFoliaError(_STATUS_CODES.get(status, "INVALID_RESPONSE"), status) from None
                error = result.get("error") if isinstance(result, dict) else None
                if isinstance(error, dict) or not 200 <= status < 300:
                    code = error.get("status", "") if isinstance(error, dict) else ""
                    if not isinstance(code, str):
                        code = ""
                    message = error.get("message", "") if isinstance(error, dict) else ""
                    if isinstance(message, str):
                        code = message.split(" : ", 1)[0] if message.split(" : ", 1)[0] in _AUTH_CODES else code
                    raise CCFoliaError(_AUTH_CODES.get(code, _STATUS_CODES.get(status, "API_ERROR")), status)
                if not isinstance(result, (dict, list)):
                    raise CCFoliaError("INVALID_RESPONSE", status)
                return result
        except CCFoliaError:
            raise
        except asyncio.TimeoutError:
            raise CCFoliaError("REQUEST_TIMEOUT") from None
        except (aiohttp.ClientError, OSError):
            raise CCFoliaError("NETWORK_ERROR") from None

    def _accept_auth(self, result: Any, *, refreshed: bool = False) -> dict[str, str]:
        if not isinstance(result, dict):
            raise CCFoliaError("INVALID_RESPONSE")
        token = result.get("id_token" if refreshed else "idToken")
        refresh = result.get("refresh_token" if refreshed else "refreshToken")
        user = result.get("user_id" if refreshed else "localId")
        if not all(isinstance(item, str) and item for item in (token, refresh, user)):
            raise CCFoliaError("INVALID_RESPONSE")
        try:
            expires = int(result.get("expires_in" if refreshed else "expiresIn", "3600"))
        except (ValueError, TypeError):
            raise CCFoliaError("INVALID_RESPONSE") from None
        if not 0 < expires <= 86400:
            raise CCFoliaError("INVALID_RESPONSE")
        self.token, self.refresh_token, self.user_id = token, refresh, user
        self._expires_at = time.monotonic() + max(0, expires - 60)
        return {"token": token, "refresh_token": refresh, "user_id": user}

    async def login(self, email: str, password: str) -> dict[str, str]:
        if not isinstance(email, str) or not email or not isinstance(password, str) or not password:
            raise CCFoliaError("BAD_REQUEST")
        result = await self._request(
            "POST", AUTH_ORIGIN + "/v1/accounts:signInWithPassword?key=" + API_KEY,
            payload={"email": email, "password": password, "returnSecureToken": True},
        )
        return self._accept_auth(result)

    async def refresh(self, *, force: bool = True, old_token: str | None = None) -> dict[str, str]:
        async with self._refresh_lock:
            if old_token is not None and self.token and self.token != old_token:
                return {"token": self.token, "refresh_token": self.refresh_token, "user_id": self.user_id}
            if not force and self.token and (not self._expires_at or time.monotonic() < self._expires_at):
                return {"token": self.token, "refresh_token": self.refresh_token, "user_id": self.user_id}
            if not self.refresh_token:
                raise CCFoliaError("REAUTH_REQUIRED")
            result = await self._request(
                "POST", TOKEN_ORIGIN + "/v1/token?key=" + API_KEY,
                form={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            )
            return self._accept_auth(result, refreshed=True)

    async def _read(self, method: str, path: str, *, payload: Any = None) -> Any:
        if self.refresh_token and (not self.token or self._expires_at and time.monotonic() >= self._expires_at):
            await self.refresh(force=False)
        if not self.token:
            raise CCFoliaError("UNAUTHENTICATED")
        old_token = self.token
        url = FIRESTORE_ORIGIN + "/v1/" + DOCUMENT_ROOT + path
        try:
            return await self._request(method, url, payload=payload, authenticated=True)
        except CCFoliaError as error:
            if error.code != "UNAUTHENTICATED" or not self.refresh_token:
                raise
        await self.refresh(old_token=old_token)
        return await self._request(method, url, payload=payload, authenticated=True)

    async def room(self, room_id: str) -> dict[str, Any]:
        room_id = _segment(room_id)
        result = await self._read("GET", "/rooms/" + room_id)
        identifier, data = decode_document(result)
        if identifier != room_id:
            raise CCFoliaError("INVALID_RESPONSE")
        channels = {"main": "MAIN", "info": "INFO", "other": "OTHER"}
        groups = data.get("messageGroups", [])
        legacy = data.get("messageChannels", [])
        if isinstance(legacy, list):
            for name in legacy:
                if isinstance(name, str) and name and "/" not in name:
                    channels.setdefault(name, name)
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict):
                    channel = group.get("id")
                    name = group.get("name")
                    if isinstance(channel, str) and channel and "/" not in channel:
                        # Modern metadata overrides any legacy entry, including
                        # builtin collisions. A private/unknown kind fails shut.
                        if group.get("kind") == "public":
                            channels[channel] = name if isinstance(name, str) and name else channel
                        else:
                            channels.pop(channel, None)
        self._channels[room_id] = channels
        name = data.get("name")
        return {"id": room_id, "name": name if isinstance(name, str) else "",
                "owner": data.get("owner", ""),
                "channels": [{"id": key, "name": value} for key, value in channels.items()]}

    @staticmethod
    def _cursor(cursor: Any, room_id: str) -> list[dict[str, str]]:
        if isinstance(cursor, Mapping):
            created, identifier = cursor.get("created", cursor.get("createdAt")), cursor.get("id")
        elif isinstance(cursor, str) and "|" in cursor:
            created, identifier = cursor.rsplit("|", 1)
        else:
            raise CCFoliaError("BAD_REQUEST")
        try:
            created = canonical_timestamp(created)
            identifier = _segment(identifier)
        except CCFoliaError:
            raise CCFoliaError("BAD_REQUEST") from None
        return [{"timestampValue": created},
                {"referenceValue": DOCUMENT_ROOT + f"/rooms/{room_id}/messages/{identifier}"}]

    async def latest(
        self, room_id: str, channel_id: str | None = None, limit: int = 256,
        before: str | Mapping[str, Any] | None = None,
        after: str | Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a complete newest-first raw page with privacy annotations.

        ``before`` and ``after`` are exclusive opaque ``pos`` cursors. A page
        includes hidden messages so its last item always advances pagination.
        Call public_message before storing content or delivering it externally.
        """
        room_id = _segment(room_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise CCFoliaError("BAD_REQUEST")
        if channel_id is not None and (not isinstance(channel_id, str) or not channel_id or "/" in channel_id):
            raise CCFoliaError("BAD_REQUEST")
        if room_id not in self._channels:
            await self.room(room_id)
        query: dict[str, Any] = {
            "from": [{"collectionId": "messages"}], "limit": limit,
            "orderBy": [{"field": {"fieldPath": "createdAt"}, "direction": "DESCENDING"},
                        {"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"}],
        }
        if channel_id is not None:
            query["where"] = {"fieldFilter": {"field": {"fieldPath": "channel"},
                                               "op": "EQUAL", "value": {"stringValue": channel_id}}}
        if before is not None:
            query["startAt"] = {"values": self._cursor(before, room_id), "before": False}
        if after is not None:
            query["endAt"] = {"values": self._cursor(after, room_id), "before": True}
        result = await self._read("POST", f"/rooms/{room_id}:runQuery", payload={"structuredQuery": query})
        if not isinstance(result, list):
            raise CCFoliaError("INVALID_RESPONSE")
        messages: list[dict[str, Any]] = []
        prefix = DOCUMENT_ROOT + f"/rooms/{room_id}/messages/"
        for item in result:
            if not isinstance(item, dict):
                raise CCFoliaError("INVALID_RESPONSE")
            if "error" in item:
                raise CCFoliaError("API_ERROR")
            if "document" not in item:
                continue  # Empty queries contain a readTime-only result.
            document = item["document"]
            if not isinstance(document, dict) or not isinstance(document.get("name"), str) or not document["name"].startswith(prefix):
                raise CCFoliaError("INVALID_RESPONSE")
            identifier, data = decode_document(document)
            created = canonical_timestamp(data.get("createdAt"))
            channel = data.get("channel", "main")
            if not isinstance(channel, str):
                raise CCFoliaError("INVALID_RESPONSE")
            data.update(id=identifier, roomId=room_id, created=created, createdAt=created,
                        channelId=channel, channel=channel, pos=created + "|" + identifier)
            data["_removed"] = bool(data.get("removed"))
            data["_hidden"] = channel not in self._channels[room_id] or not public_message(data)
            messages.append(data)
        return messages
