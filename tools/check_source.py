#!/usr/bin/env python3
"""Optional, read-only live source check. Credentials are prompted, never saved."""
import asyncio
import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from ccfolia import CCFolia, CCFoliaError, public_message
from commands import parse_url


async def check(url, email, password):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        api = CCFolia(session)
        await api.login(email, password)
        del password
        room = await api.room(parse_url(url))
        result = {"login": "ok", "channels": [c["name"] for c in room["channels"]]}
        page = await api.latest(room["id"], limit=2)
        result["history_read"] = "ok"
        if page:
            older = await api.latest(room["id"], limit=2, before=page[-1]["pos"])
            assert not {m["id"] for m in page}.intersection(m["id"] for m in older)
            assert all(m["pos"] < page[-1]["pos"] for m in older)
            result["pagination"] = "ok"
        else:
            result["pagination"] = "empty-room"
        result["public_messages_in_sample"] = sum(public_message(m) for m in page)
        for channel in room["channels"]:
            latest = await api.latest(room["id"], channel["id"], limit=1)
            assert all(m["channelId"] == channel["id"] for m in latest)
        result["per_channel_reads"] = "ok"
        await api.refresh()
        await api.room(room["id"])
        result["token_refresh"] = "ok"
        import json
        print(json.dumps(result, ensure_ascii=False))


def main():
    url = input("Room URL: ").strip()
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    try:
        asyncio.run(check(url, email, password))
    except CCFoliaError as error:
        raise SystemExit("Source check failed: " + error.code) from None
    except AssertionError:
        raise SystemExit("Source check failed: unexpected pagination or channel response") from None


if __name__ == "__main__":
    main()
