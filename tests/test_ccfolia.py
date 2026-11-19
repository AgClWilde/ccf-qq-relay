"""Offline contract checks; all accounts, rooms, tokens, and text are fixtures."""

import asyncio
import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ccfolia import (CCFolia, CCFoliaError, DOCUMENT_ROOT, canonical_timestamp,
                     decode_document, decode_value, public_message)


def value(data):
    if data is None:
        return {"nullValue": None}
    if isinstance(data, bool):
        return {"booleanValue": data}
    if isinstance(data, str):
        return {"stringValue": data}
    if isinstance(data, list):
        return {"arrayValue": {"values": [value(item) for item in data]}}
    if isinstance(data, dict):
        return {"mapValue": {"fields": {key: value(item) for key, item in data.items()}}}
    raise AssertionError("unsupported test fixture")


def document(identifier, data, *, room="fixture-room", message=False):
    path = f"{DOCUMENT_ROOT}/rooms/{room}"
    if message:
        path += "/messages/" + identifier
    return {"name": path, "fields": {key: value(item) for key, item in data.items()}}


class Response:
    def __init__(self, result, status=200):
        self.result, self.status = result, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class Session:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result if isinstance(result, Response) else Response(result)


AUTH = {"idToken": "fixture-id", "refreshToken": "fixture-refresh", "localId": "fixture-user", "expiresIn": "3600"}
REFRESH = {"id_token": "fixture-renewed", "refresh_token": "fixture-rotated", "user_id": "fixture-user", "expires_in": "3600"}


class DecodeTests(unittest.TestCase):
    def test_firestore_value_types(self):
        self.assertEqual(decode_value({"mapValue": {"fields": {"a": {"arrayValue": {"values": [
            {"integerValue": "12"}, {"booleanValue": False}, {"nullValue": None}, {"doubleValue": 1.5}]}}}}}),
            {"a": [12, False, None, 1.5]})
        self.assertEqual(decode_value({"arrayValue": {}}), [])
        self.assertEqual(decode_value({"mapValue": {}}), {})
        self.assertEqual(decode_value({"timestampValue": "2026-01-02T03:04:05Z"}), "2026-01-02T03:04:05Z")

    def test_nanosecond_cursor_does_not_round(self):
        self.assertEqual(canonical_timestamp("2026-01-02T03:04:05.123456789Z"), "2026-01-02T03:04:05.123456789Z")
        self.assertEqual(canonical_timestamp("2026-01-02T03:04:05Z"), "2026-01-02T03:04:05.000000000Z")
        self.assertEqual(canonical_timestamp("2026-01-02T03:04:05.1Z"), "2026-01-02T03:04:05.100000000Z")
        for invalid in (None, "", "2026-02-30T00:00:00Z", "2026-01-02T03:04:05.1234567890Z"):
            with self.subTest(invalid=invalid), self.assertRaises(CCFoliaError):
                canonical_timestamp(invalid)

    def test_decode_rejects_malformed_documents(self):
        for invalid in ({"name": "x", "fields": []}, {"name": "x", "fields": {"foo": {"unexpected": "x"}}}, {}):
            with self.subTest(invalid=invalid), self.assertRaises(CCFoliaError):
                decode_document(invalid)

    def test_privacy_does_not_depend_only_on_annotation(self):
        self.assertTrue(public_message({"text": "fixture public"}))
        for message in ({"to": "fixture-recipient"}, {"extend": {"roll": {"secret": True}}},
                        {"removed": True}, {"_removed": True}, {"_hidden": True}):
            with self.subTest(message=message):
                self.assertFalse(public_message(message))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_and_explicit_refresh_rotate_token(self):
        session = Session(AUTH, REFRESH)
        client = CCFolia(session)
        login = await client.login("fixture@example.invalid", "fixture-password")
        self.assertEqual(login["user_id"], "fixture-user")
        self.assertEqual(client.token, "fixture-id")
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "")
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertEqual(session.calls[0][2]["json"]["returnSecureToken"], True)
        await client.refresh()
        self.assertEqual(client.token, "fixture-renewed")
        self.assertEqual(client.refresh_token, "fixture-rotated")
        self.assertEqual(session.calls[1][2]["data"], {"grant_type": "refresh_token", "refresh_token": "fixture-refresh"})
        self.assertNotIn("fixture", repr(client))

    async def test_room_includes_only_public_groups_and_builtins(self):
        room = document("fixture-room", {"name": "Fixture Room", "owner": "fixture-owner",
            "messageChannels": ["legacy"], "messageGroups": [
                {"id": "public-1", "name": "Public", "kind": "public"},
                {"id": "private-1", "name": "Private", "kind": "private", "uids": ["fixture-user"]}]})
        client = CCFolia(Session(room), token="fixture-id")
        result = await client.room("fixture-room")
        self.assertEqual(result["name"], "Fixture Room")
        self.assertEqual(result["channels"], [{"id": "main", "name": "MAIN"}, {"id": "info", "name": "INFO"},
            {"id": "other", "name": "OTHER"}, {"id": "legacy", "name": "legacy"}, {"id": "public-1", "name": "Public"}])

    async def test_latest_complete_page_with_privacy_annotations(self):
        base = {"createdAt": "2026-01-02T03:04:05.123456789Z", "name": "Fixture", "channel": "main", "text": "fixture text"}
        entries = [("public", {}), ("dm", {"to": "fixture-user"}), ("secret", {"extend": {"roll": {"secret": True}}}),
                   ("private", {"channel": "private-1"}), ("deleted", {"removed": True})]
        session = Session([{"document": document(identifier, {**base, **extra}, message=True)} for identifier, extra in entries])
        client = CCFolia(session, token="fixture-id")
        client._channels["fixture-room"] = {"main": "MAIN", "info": "INFO", "other": "OTHER"}
        messages = await client.latest("fixture-room")
        self.assertEqual(len(messages), 5)
        self.assertEqual([public_message(message) for message in messages], [True, False, False, False, False])
        self.assertEqual(messages[0]["created"], base["createdAt"])
        self.assertEqual(messages[0]["channelId"], "main")
        self.assertEqual(messages[0]["pos"], base["createdAt"] + "|public")
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer fixture-id")

    async def test_private_metadata_overrides_legacy_and_builtin_collisions(self):
        room = document("fixture-room", {"name": "Fixture", "messageChannels": ["legacy"],
            "messageGroups": [{"id": "legacy", "name": "Private", "kind": "private"},
                              {"id": "main", "name": "Private Main", "kind": "private"},
                              {"id": "info", "name": "Unknown Info", "kind": "future-unknown"}]})
        client = CCFolia(Session(room), token="fixture-id")
        result = await client.room("fixture-room")
        self.assertEqual(result["channels"], [{"id": "other", "name": "OTHER"}])

    async def test_pagination_has_document_tiebreaker_and_exclusive_cursors(self):
        session = Session([{"readTime": "2026-01-02T00:00:00Z"}])
        client = CCFolia(session, token="fixture-id")
        client._channels["fixture-room"] = {"main": "MAIN"}
        before = "2026-01-02T03:04:05.123456789Z|fixture-document"
        after = "2026-01-01T00:00:00Z|fixture-old"
        self.assertEqual(await client.latest("fixture-room", "main", limit=10, before=before, after=after), [])
        query = session.calls[0][2]["json"]["structuredQuery"]
        self.assertEqual(query["orderBy"][-1], {"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"})
        self.assertEqual(query["startAt"]["before"], False)
        self.assertEqual(query["endAt"]["before"], True)
        self.assertTrue(query["startAt"]["values"][1]["referenceValue"].endswith("/fixture-document"))
        self.assertEqual(query["where"]["fieldFilter"]["value"], {"stringValue": "main"})

    async def test_expired_token_refreshes_once_then_retries(self):
        session = Session(Response({"error": {"status": "UNAUTHENTICATED", "message": "sensitive response"}}, 401),
                          REFRESH, document("fixture-room", {"name": "Fixture"}))
        client = CCFolia(session, token="fixture-id", refresh_token="fixture-refresh")
        await client.room("fixture-room")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[2][2]["headers"]["Authorization"], "Bearer fixture-renewed")

    async def test_known_expiry_refreshes_before_request(self):
        session = Session(REFRESH, document("fixture-room", {"name": "Fixture"}))
        client = CCFolia(session, token="fixture-id", refresh_token="fixture-refresh")
        client._expires_at = time.monotonic() - 1
        await client.room("fixture-room")
        self.assertIn("securetoken.googleapis.com", session.calls[0][1])

    async def test_refresh_only_session_works(self):
        session = Session(REFRESH, document("fixture-room", {"name": "Fixture"}))
        client = CCFolia(session, refresh_token="fixture-refresh")
        await client.room("fixture-room")
        self.assertEqual(client.token, "fixture-renewed")

    async def test_concurrent_refresh_deduplicates_retry(self):
        client = CCFolia(Session(REFRESH), token="fixture-id", refresh_token="fixture-refresh")
        await asyncio.gather(client.refresh(old_token="fixture-id"), client.refresh(old_token="fixture-id"))
        self.assertEqual(len(client.session.calls), 1)

    async def test_errors_hide_server_bodies_and_credentials(self):
        for response, expected in (
            (Response({"error": {"message": "INVALID_LOGIN_CREDENTIALS"}}, 400), "INVALID_LOGIN"),
            (Response({"error": {"message": "fixture-password fixture-token"}}, 403), "NO_PERMISSION"),
            (Response(ValueError("fixture-token")), "INVALID_RESPONSE"),
            (Response({"error": {"status": "FAILED_PRECONDITION", "message": "fixture-private-url"}}, 400), "INDEX_REQUIRED"),
        ):
            client = CCFolia(Session(response))
            with self.subTest(expected=expected), self.assertRaises(CCFoliaError) as caught:
                await client.login("fixture@example.invalid", "fixture-password")
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("fixture", str(caught.exception))
            self.assertNotIn("fixture", repr(caught.exception))

    async def test_bad_paths_never_make_network_requests(self):
        session = Session()
        client = CCFolia(session, token="fixture-id")
        for room in ("../../bad", "room?key=secret", "https://other.invalid", "", "room\\bad"):
            with self.subTest(room=room), self.assertRaises(CCFoliaError):
                await client.room(room)
        self.assertEqual(session.calls, [])

    async def test_malformed_page_fails_instead_of_partial_return(self):
        session = Session([{"document": document("fixture", {"channel": "main"}, message=True)}])
        client = CCFolia(session, token="fixture-id")
        client._channels["fixture-room"] = {"main": "MAIN"}
        with self.assertRaises(CCFoliaError) as caught:
            await client.latest("fixture-room")
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

    async def test_unauthenticated_read_does_not_send_request(self):
        client = CCFolia(Session())
        with self.assertRaises(CCFoliaError) as caught:
            await client.room("fixture-room")
        self.assertEqual(caught.exception.code, "UNAUTHENTICATED")
        self.assertEqual(client.session.calls, [])


if __name__ == "__main__":
    unittest.main()
