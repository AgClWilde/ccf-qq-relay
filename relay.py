#!/usr/bin/env python3
"""CCFolia public-channel metadata notices over OneBot v11."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid

import aiohttp

from bootstrap import load_config
from ccfolia import CCFolia, CCFoliaError, public_message
from commands import HELP, command, parse_url
from notifications import epoch, render, resume, transition
from onebot import OneBot

LOG = logging.getLogger("ccf-qq")


@contextlib.contextmanager
def process_lock(database):
    """Allow one running service for each canonical database path."""
    path = Path(str(Path(database).expanduser().resolve()) + ".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("同一数据库已有 CCF 提醒服务运行。") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def channel_for(channels, requested):
    """Resolve an exact public name/ID; case folding must remain unambiguous."""
    matches = [c for c in channels if requested in (c["id"], c["name"])]
    if not matches:
        matches = [c for c in channels if requested.casefold() in
                   (c["id"].casefold(), c["name"].casefold())]
    if len(matches) != 1:
        raise ValueError("频道不存在或名称重复；可用频道：" + "、".join(channel_label(c) for c in channels))
    return matches[0]


def channel_label(channel):
    return channel["name"] if channel["name"].casefold() == channel["id"].casefold() else f"{channel['name']}（ID: {channel['id']}）"


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, room TEXT NOT NULL,
            token TEXT NOT NULL, refresh_token TEXT NOT NULL, user_id TEXT NOT NULL,
            title TEXT NOT NULL, last_error TEXT, UNIQUE(owner,room));
          CREATE TABLE IF NOT EXISTS bindings (
            id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES accounts(id),
            owner TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
            room TEXT NOT NULL, channel TEXT NOT NULL, channel_title TEXT NOT NULL,
            title TEXT NOT NULL, started REAL NOT NULL,
            notification_state TEXT NOT NULL DEFAULT '{}',
            UNIQUE(target_type,target_id,room,channel));
          CREATE TABLE IF NOT EXISTS seen (
            binding TEXT NOT NULL REFERENCES bindings(id) ON DELETE CASCADE,
            message TEXT NOT NULL, PRIMARY KEY(binding,message));
          CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, binding TEXT NOT NULL REFERENCES bindings(id) ON DELETE CASCADE,
            message TEXT NOT NULL, part INTEGER NOT NULL, text TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
            UNIQUE(binding,message,part));
          CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS registered_accounts (
            owner TEXT PRIMARY KEY, username TEXT NOT NULL,
            token TEXT NOT NULL, refresh_token TEXT NOT NULL, user_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS room_watches (
            id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES accounts(id),
            target_type TEXT NOT NULL, target_id TEXT NOT NULL,
            room TEXT NOT NULL, started REAL NOT NULL,
            UNIQUE(target_type,target_id,room));
          CREATE TABLE IF NOT EXISTS excluded_channels (
            watch TEXT NOT NULL REFERENCES room_watches(id) ON DELETE CASCADE,
            channel TEXT NOT NULL, PRIMARY KEY(watch,channel));
          CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent,binding,created,id);
        """)

    def rows(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params)]

    def accounts(self):
        return self.rows("SELECT * FROM accounts WHERE id IN (SELECT account FROM bindings UNION SELECT account FROM room_watches)")

    def registered(self, owner):
        rows = self.rows("SELECT * FROM registered_accounts WHERE owner=?", (owner,))
        return rows[0] if rows else None

    def register(self, owner, username, auth):
        with self.db:
            self.db.execute("""INSERT INTO registered_accounts VALUES (?,?,?,?,?) ON CONFLICT(owner)
                DO UPDATE SET username=excluded.username,token=excluded.token,
                refresh_token=excluded.refresh_token,user_id=excluded.user_id""",
                (owner, username, auth["token"], auth["refresh_token"], auth["user_id"]))

    def watches(self, account=None):
        return self.rows("SELECT * FROM room_watches" + (" WHERE account=?" if account else ""),
                         (account,) if account else ())

    def watch_room(self, account, kind, target, room, started):
        with self.db:
            prior = self.rows("SELECT * FROM room_watches WHERE target_type=? AND target_id=? AND room=?",
                              (kind, target, room))
            ident = prior[0]["id"] if prior else str(uuid.uuid4())
            self.db.execute("""INSERT INTO room_watches VALUES (?,?,?,?,?,?)
                ON CONFLICT(target_type,target_id,room) DO UPDATE SET account=excluded.account""",
                (ident, account, kind, target, room, started))
            # An explicit full-room subscription restores previously excluded channels.
            self.db.execute("DELETE FROM excluded_channels WHERE watch=?", (ident,))
        return ident

    def sync_watches(self, account, metadata):
        """Materialize new public channels without resetting existing delivery state."""
        for watch in self.watches(account["id"]):
            excluded = {row["channel"] for row in self.rows(
                "SELECT channel FROM excluded_channels WHERE watch=?", (watch["id"],))}
            for channel in metadata["channels"]:
                if channel["id"] not in excluded:
                    # Retain existing credentials, owner, cursor and queued notices.
                    prior = self.rows("SELECT id FROM bindings WHERE target_type=? AND target_id=? AND room=? AND channel=?",
                                      (watch["target_type"], watch["target_id"], watch["room"], channel["id"]))
                    if not prior:
                        self.subscribe(account["owner"], watch["target_type"], watch["target_id"],
                                       watch["room"], channel, metadata, account, watch["started"])

    def include(self, kind, target, room, channel):
        with self.db:
            self.db.execute("""DELETE FROM excluded_channels WHERE channel=? AND watch IN
                (SELECT id FROM room_watches WHERE target_type=? AND target_id=? AND room=?)""",
                (channel, kind, target, room))

    def active(self, account=None):
        return self.rows("SELECT * FROM bindings" + (" WHERE account=?" if account else ""),
                         (account,) if account else ())

    def get(self, binding):
        rows = self.rows("SELECT * FROM bindings WHERE id=?", (binding,))
        return rows[0] if rows else None

    def subscribe(self, owner, kind, target, room, selected, metadata, auth, started):
        """Credential refresh and duplicate subscription preserve delivery state."""
        with self.db:
            existing_account = self.rows("SELECT * FROM accounts WHERE owner=? AND room=?", (owner, room))
            account = existing_account[0]["id"] if existing_account else str(uuid.uuid4())
            self.db.execute("""INSERT INTO accounts(id,owner,room,token,refresh_token,user_id,title)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner,room) DO UPDATE SET
                token=excluded.token,refresh_token=excluded.refresh_token,user_id=excluded.user_id,
                title=excluded.title,last_error=NULL""",
                (account, owner, room, auth["token"], auth["refresh_token"], auth["user_id"], metadata["name"]))
            prior = self.rows("SELECT * FROM bindings WHERE target_type=? AND target_id=? AND room=? AND channel=?",
                              (kind, target, room, selected["id"]))
            if prior and prior[0]["owner"] != owner and kind != "group":
                raise ValueError("这个窗口的该频道已由另一位操作者订阅。请先关闭现有订阅，再重新订阅。")
            ident = prior[0]["id"] if prior else str(uuid.uuid4())
            title = metadata["name"] + "-" + selected["name"]
            self.db.execute("""INSERT INTO bindings(id,account,owner,target_type,target_id,room,channel,
                channel_title,title,started) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(target_type,target_id,room,channel) DO UPDATE SET
                account=excluded.account,channel_title=excluded.channel_title,title=excluded.title""",
                (ident, account, owner, kind, target, room, selected["id"], selected["name"], title, started))
        return ident, account, bool(prior)

    def close(self, bindings, watches=(), exclude=False):
        with self.db:
            for binding in bindings:
                if exclude:
                    self.db.execute("""INSERT OR IGNORE INTO excluded_channels SELECT id,? FROM room_watches
                        WHERE target_type=? AND target_id=? AND room=?""",
                        (binding["channel"], binding["target_type"], binding["target_id"], binding["room"]))
                self.db.execute("DELETE FROM bindings WHERE id=?", (binding["id"],))
            for watch in watches:
                self.db.execute("DELETE FROM room_watches WHERE id=?", (watch["id"],))
            self.db.execute("DELETE FROM accounts WHERE id NOT IN (SELECT account FROM bindings UNION SELECT account FROM room_watches)")

    def ingest(self, account, messages):
        fresh = 0
        with self.db:
            bindings = self.active(account)
            for binding in bindings:
                state = json.loads(binding["notification_state"])
                for message in sorted(messages, key=lambda m: (epoch(m["created"]), m["id"])):
                    if (message.get("channelId") != binding["channel"]
                            or epoch(message["created"]) < binding["started"]):
                        continue
                    inserted = self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)",
                                               (binding["id"], message["id"])).rowcount
                    if not inserted or not public_message(message):
                        continue
                    state, notices = transition(state, message, title=binding["title"])
                    for part, text in enumerate(notices):
                        self.db.execute("INSERT OR IGNORE INTO outbox(binding,message,part,text,created) VALUES (?,?,?,?,?)",
                                        (binding["id"], message["id"], part, text, epoch(message["created"])))
                    fresh += 1
                self.db.execute("UPDATE bindings SET notification_state=? WHERE id=?",
                                (json.dumps(state, ensure_ascii=False), binding["id"]))
        return fresh

    def resume_due(self, account, now, channels=None):
        with self.db:
            for binding in self.active(account):
                if channels is not None and binding["channel"] not in channels:
                    continue
                state, notices = resume(json.loads(binding["notification_state"]), binding["title"], now)
                for text in notices:
                    source = "quiet:" + str(state.get("last_activity"))
                    self.db.execute("INSERT OR IGNORE INTO outbox(binding,message,part,text,created) VALUES (?,?,?,?,?)",
                                    (binding["id"], source, 0, text, now))
                self.db.execute("UPDATE bindings SET notification_state=? WHERE id=?",
                                (json.dumps(state, ensure_ascii=False), binding["id"]))


class Relay:
    def __init__(self, config):
        self.config = config
        self.store = Store(config["database"])
        self.onebot = OneBot(config["onebot_url"], config.get("onebot_token", ""), self.on_event)
        self.workers = {}
        self.source_states = {}
        self.command_lock = asyncio.Lock()
        self.registration_pending = {}
        self.event_fingerprints = {}
        self.session = None

    async def send(self, kind, target, text):
        params = {"message": [{"type": "text", "data": {"text": text}}]}
        params["group_id" if kind == "group" else "user_id"] = int(target)
        return await self.onebot.call("send_group_msg" if kind == "group" else "send_private_msg", params)

    async def on_event(self, event):
        if (event.get("post_type") != "message"
                or event.get("message_type") not in ("private", "group")
                or str(event.get("user_id")) == str(event.get("self_id"))):
            return
        owner = str(event.get("user_id"))
        segments = event.get("message")
        text = ("".join(s.get("data", {}).get("text", "") for s in segments
                        if isinstance(s, dict) and s.get("type") == "text")
                if isinstance(segments, list) else str(event.get("raw_message") or segments or ""))
        kind = event["message_type"]
        target = str(event.get("group_id") if kind == "group" else owner)
        # Some adapters omit message_id. Suppress immediate duplicate delivery
        # of the same event while keeping credential text out of dedup storage.
        if event.get("message_id") is None:
            now = time.monotonic()
            self.event_fingerprints = {key: expiry for key, expiry in self.event_fingerprints.items() if expiry > now}
            fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if fingerprint in self.event_fingerprints:
                return
            self.event_fingerprints[fingerprint] = now + 5
        try:
            parsed = command(text)
            if parsed is None and kind == "private" and self.registration_pending.get(owner, 0) > time.monotonic():
                parsed = command(".ccf提醒 登记 " + text)
        except (ValueError, TypeError):
            await self.send(kind, target, "CCF 指令格式错误。发送 .ccf提醒 查看用法。")
            return
        if parsed is None:
            return
        async with self.command_lock:
            if event.get("message_id") is not None:
                ident = f"{event.get('self_id')}:{kind}:{target}:{event['message_id']}:{owner}"
                legacy_ident = f"{event.get('self_id')}:{event['message_id']}:{owner}"
                with self.store.db:
                    if self.store.rows("SELECT id FROM commands WHERE id=?", (legacy_ident,)):
                        return
                    if not self.store.db.execute("INSERT OR IGNORE INTO commands VALUES (?,?)", (ident, time.time())).rowcount:
                        return
            try:
                await self.handle(parsed, owner, kind, target)
            except ValueError as error:
                # Only locally authored validation errors reach this branch.
                await self.send(kind, target, str(error))
            except CCFoliaError:
                await self.send(kind, target, "CCF 登录或访问失败。请核对账号、密码和房间权限后重新绑定。")
            except Exception as error:
                LOG.warning("command_failed class=%s", type(error).__name__)
                with contextlib.suppress(Exception):
                    await self.send(kind, target, "操作暂未完成，请稍后重试。")
            finally:
                parsed.pop("password", None)

    def identity(self, owner, kind, target, room):
        accounts = self.store.accounts()
        # A group binding delegates management of that room to the group.
        if kind == "group":
            ids = {b["account"] for b in self.store.active() if b["target_type"] == kind
                   and b["target_id"] == target and b["room"] == room}
            ids.update(w["account"] for w in self.store.watches() if w["target_type"] == kind
                       and w["target_id"] == target and w["room"] == room)
            matches = [a for a in accounts if a["id"] in ids and not a["last_error"]]
            if matches:
                return matches[0]
        registered = self.store.registered(owner)
        if registered:
            return registered
        matches = [a for a in accounts if a["owner"] == owner and a["room"] == room]
        matches = matches or [a for a in accounts if a["owner"] == owner]
        if matches:
            return matches[0]
        raise ValueError("请先私聊发送 .ccf提醒 登记 <邮箱> <密码>，再在目标窗口订阅房间网址。")

    def save_identity(self, identity, api):
        with self.store.db:
            if identity.get("id"):
                self.store.db.execute("UPDATE accounts SET token=?,refresh_token=? WHERE id=?",
                                      (api.token, api.refresh_token, identity["id"]))
            self.store.db.execute("""UPDATE registered_accounts SET token=?,refresh_token=?
                WHERE owner=? AND user_id=?""", (api.token, api.refresh_token,
                                                identity["owner"], api.user_id or identity["user_id"]))

    async def handle(self, cmd, owner, kind, target):
        action = cmd["action"]
        if action == "help":
            return await self.send(kind, target, HELP)
        if action == "register":
            if kind != "private":
                raise ValueError("账号登记仅限私聊。请私聊发送 .ccf提醒 登记 <邮箱> <密码>。")
            if not cmd.get("username"):
                self.registration_pending[owner] = time.monotonic() + 600
                return await self.send(kind, target, "请在 10 分钟内私聊发送邮箱和密码（以空格或换行分隔），或发送 .ccf提醒 登记 <邮箱> <密码>。")
            self.registration_pending.pop(owner, None)
            api = CCFolia(self.session)
            auth = await api.login(cmd["username"], cmd["password"])
            cmd.pop("password", None)
            self.store.register(owner, cmd["username"], auth)
            return await self.send(kind, target, "CCF 账号已登记。此后可在任意聊天窗口发送 .ccf提醒 [频道或全频道] <房间网址>。")
        room = parse_url(cmd["url"]) if cmd.get("url") else None
        channel_name = cmd.get("channel")
        full_room = channel_name == "全频道"
        selected = [b for b in self.store.active() if b["target_type"] == kind and b["target_id"] == target
                    and (room is None or room == b["room"])
                    and (not channel_name or full_room or channel_name.casefold() in
                         (b["channel"].casefold(), b["channel_title"].casefold()))]
        watches = [w for w in self.store.watches() if w["target_type"] == kind and w["target_id"] == target
                   and (room is None or room == w["room"])]
        if action == "status":
            accounts = {a["id"]: a for a in self.store.accounts()}
            lines = []
            for watch in watches if not channel_name or full_room else []:
                exclusions = self.store.rows("SELECT channel FROM excluded_channels WHERE watch=?", (watch["id"],))
                lines.append(f"{accounts[watch['account']]['title']}：全频道，自动发现新公开频道；已排除 {len(exclusions)} 个频道\nhttps://ccfolia.com/rooms/{watch['room']}")
            for binding in selected:
                pending = self.store.rows("SELECT count(*) n FROM outbox WHERE binding=? AND sent=0", (binding["id"],))[0]["n"]
                account = accounts[binding["account"]]
                state = account["last_error"] or self.source_states.get(account["id"], "连接中")
                pause = "；高频暂停中" if json.loads(binding["notification_state"]).get("phase") == "muted" else ""
                lines.append(f"{binding['title']}：{state}{pause}；待发 {pending} 条\nhttps://ccfolia.com/rooms/{binding['room']}")
            return await self.send(kind, target, "\n".join(lines) or "本窗口尚无匹配的 CCF 订阅。")
        if action == "off":
            if channel_name and not full_room and len(selected) > 1:
                raise ValueError("频道名匹配多个订阅；请附房间网址或使用频道 ID，未修改订阅。")
            self.store.close(selected, watches if not channel_name or full_room else (),
                             exclude=bool(channel_name and not full_room))
            await self.reconcile()
            return await self.send(kind, target, f"已关闭 {len(selected)} 个 CCF 频道订阅。")
        if action in ("channels", "test"):
            if not room:
                choices = {b["room"] for b in selected}
                choices.update(w["room"] for w in watches)
                if len(choices) != 1:
                    raise ValueError("请指定一个房间网址。")
                room = choices.pop()
            identity = self.identity(owner, kind, target, room)
            api = self.api(identity)
            metadata = await api.room(room)
            if action == "channels":
                self.save_identity(identity, api)
                return await self.send(kind, target, "「" + metadata["name"] + "」可订阅频道：" + "、".join(channel_label(c) for c in metadata["channels"]))
            channel = channel_for(metadata["channels"], channel_name) if channel_name else None
            before = None
            while True:
                page = await api.latest(room, channel["id"] if channel else None, limit=64, before=before)
                latest = next((m for m in page if public_message(m)), None)
                if latest or not page or len(page) < 64:
                    break
                cursor = page[-1]["pos"]
                if cursor == before:
                    raise RuntimeError("pagination_stalled")
                before = cursor
            self.save_identity(identity, api)
            if not latest:
                return await self.send(kind, target, "CCF 访问成功；该范围尚无可用于测试的公开消息。")
            channel = channel or channel_for(metadata["channels"], latest["channelId"])
            return await self.send(kind, target, render(latest, metadata["name"] + "-" + channel["name"])[0])
        if action not in ("bind", "attach"):
            raise ValueError("指令不存在。发送 .ccf提醒 查看用法。")
        if action == "bind" and kind == "group":
            raise ValueError("请先私聊发送 .ccf提醒 登记 <邮箱> <密码>。随后在群内发送 .ccf提醒 [频道或全频道] <房间网址>。")
        started = time.time()
        identity = None
        source_owner = owner
        if action == "bind":
            api = CCFolia(self.session)
            auth = await api.login(cmd["username"], cmd["password"])
            cmd.pop("password", None)
        else:
            identity = self.identity(owner, kind, target, room)
            source_owner = identity["owner"]
            api = self.api(identity)
            auth = identity
        metadata = await api.room(room)
        channels = metadata["channels"] if full_room else [channel_for(metadata["channels"], channel_name or "MAIN")]
        if not channels:
            raise ValueError("房间没有可订阅的公开频道。")
        # Verify source access before committing any subscription.
        await api.latest(room, None if full_room else channels[0]["id"], limit=1)
        auth = {"token": api.token, "refresh_token": api.refresh_token,
                "user_id": api.user_id or auth["user_id"]}
        if identity:
            self.save_identity(identity, api)
        duplicate = True
        affected = set()
        for channel in channels:
            previous = self.store.rows("SELECT account FROM bindings WHERE target_type=? AND target_id=? AND room=? AND channel=?",
                                       (kind, target, room, channel["id"]))
            affected.update(b["account"] for b in previous)
            _, account, prior = self.store.subscribe(source_owner, kind, target, room, channel, metadata, auth, started)
            affected.add(account)
            duplicate = duplicate and prior
            self.store.include(kind, target, room, channel["id"])
        if full_room:
            self.store.watch_room(account, kind, target, room, started)
        for ident in affected:
            await self.restart(ident)
        await self.reconcile()
        lead = "已订阅" if not duplicate else "已保留订阅"
        label = "全频道" if full_room else channels[0]["name"]
        suffix = "；新公开频道将自动加入。" if full_room else "。"
        return await self.send(kind, target, f"{lead}「{metadata['name']}-{label}」{suffix}")

    def api(self, account):
        api = CCFolia(self.session, account["token"], account["refresh_token"])
        api.user_id = account["user_id"]
        return api

    def save_auth(self, ident, api):
        matches = self.store.rows("SELECT * FROM accounts WHERE id=?", (ident,))
        if matches:
            self.save_identity(matches[0], api)

    async def catchup(self, api, account, since):
        """Traverse creation-time pages, including all equal-time document IDs."""
        messages, before = [], None
        while True:
            page = await api.latest(account["room"], limit=256, before=before)
            messages.extend(m for m in page if epoch(m["created"]) >= since)
            if not page or len(page) < 256 or epoch(page[-1]["created"]) < since:
                break
            next_before = page[-1]["pos"]
            if next_before == before:
                raise RuntimeError("pagination_stalled")
            before = next_before
        self.store.ingest(account["id"], messages)
        return max((epoch(m["created"]) for m in messages), default=since)

    async def watch(self, account):
        api = self.api(account)
        delay, warned_auth, highwater, next_full = 2, False, None, 0
        public_channels = set()
        while True:
            try:
                bindings = self.store.active(account["id"])
                watches = self.store.watches(account["id"])
                if not bindings and not watches:
                    return
                self.source_states[account["id"]] = "正在补收"
                started = min(b["started"] for b in bindings + watches)
                full = time.monotonic() >= next_full or highwater is None
                # Room metadata is refreshed on every poll: custom channels can
                # become private between scans. Never rely on stale publicity.
                metadata = await api.room(account["room"])
                public_channels = {c["id"] for c in metadata["channels"]}
                before_bindings = {b["id"] for b in bindings}
                self.store.sync_watches({**account, "token": api.token, "refresh_token": api.refresh_token,
                                         "user_id": api.user_id or account["user_id"]}, metadata)
                bindings = self.store.active(account["id"])
                if any(b["id"] not in before_bindings for b in bindings):
                    full = True
                if full:
                    names = {c["id"]: c["name"] for c in metadata["channels"]}
                    with self.store.db:
                        for b in bindings:
                            title = metadata["name"] + "-" + names.get(b["channel"], b["channel_title"])
                            self.store.db.execute("UPDATE bindings SET title=?,channel_title=? WHERE id=?",
                                                  (title, names.get(b["channel"], b["channel_title"]), b["id"]))
                        self.store.db.execute("UPDATE accounts SET title=? WHERE id=?", (metadata["name"], account["id"]))
                    next_full = time.monotonic() + self.config.get("rescan_seconds", 300)
                # Overlap tolerates ordinary delayed reads; periodic full scans
                # also discover older late inserts. No position assumes edit order.
                since = started if full else max(started, highwater - 300)
                highwater = max(highwater or started, await self.catchup(api, account, since))
                self.save_auth(account["id"], api)
                with self.store.db:
                    self.store.db.execute("UPDATE accounts SET last_error=NULL WHERE id=?", (account["id"],))
                self.source_states[account["id"]] = "轮询监听中"
                # Resume only after a successful source scan; no timer while offline.
                self.store.resume_due(account["id"], time.time(), public_channels)
                delay, warned_auth = 2, False
                await asyncio.sleep(self.config.get("poll_seconds", 3))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.save_auth(account["id"], api)
                auth_failed = isinstance(error, CCFoliaError) and error.code in ("UNAUTHENTICATED", "NO_PERMISSION", "REAUTH_REQUIRED", "INVALID_LOGIN")
                state = "登录失效或权限不足，请重新绑定" if auth_failed else "连接中断，自动重试"
                self.source_states[account["id"]] = state
                LOG.warning("source_retry account=%s class=%s", account["id"], type(error).__name__)
                with self.store.db:
                    self.store.db.execute("UPDATE accounts SET last_error=? WHERE id=?", (state, account["id"]))
                if auth_failed and not warned_auth:
                    with contextlib.suppress(Exception):
                        await self.send("private", account["owner"], "一个 CCF 房间登录失效或权限不足，请私聊重新绑定。")
                    warned_auth = True
                highwater, next_full = None, 0
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def restart(self, account):
        task = self.workers.pop(account, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def reconcile(self):
        accounts = {a["id"]: a for a in self.store.accounts()}
        for ident in list(self.workers):
            if ident not in accounts:
                await self.restart(ident)
                self.source_states.pop(ident, None)
        for ident, account in accounts.items():
            if ident not in self.workers or self.workers[ident].done():
                self.workers[ident] = asyncio.create_task(self.watch(account))

    async def deliver(self):
        retry_after = {}
        while True:
            await self.onebot.ready.wait()
            rows = self.store.rows("""SELECT o.*,b.target_type,b.target_id FROM outbox o
                JOIN bindings b ON b.id=o.binding WHERE o.sent=0 AND
                o.id=(SELECT id FROM outbox WHERE binding=b.id AND sent=0 ORDER BY created,id LIMIT 1)
                ORDER BY o.created,o.id""")
            ready = [r for r in rows if retry_after.get(r["binding"], 0) <= time.monotonic()]
            if not ready:
                await asyncio.sleep(0.5)
                continue
            item = ready[0]
            try:
                await self.send(item["target_type"], item["target_id"], item["text"])
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET sent=1,text='' WHERE id=?", (item["id"],))
                await asyncio.sleep(self.config.get("send_interval", 1.2))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("delivery_retry class=%s", type(error).__name__)
                retry_after[item["binding"]] = time.monotonic() + 10

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:
            self.session = session
            await self.reconcile()
            tasks = [asyncio.create_task(self.onebot.run()), asyncio.create_task(self.deliver())]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks + list(self.workers.values()):
                    task.cancel()
                await self.onebot.close()
                await asyncio.gather(*tasks, *self.workers.values(), return_exceptions=True)
                self.store.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    try:
        config = load_config(args.config)
    except (ValueError, OSError, KeyError):
        raise SystemExit("配置无效，请参照安装说明检查配置文件和 OneBot 令牌来源。") from None
    with process_lock(config["database"]):
        asyncio.run(Relay(config).run())


if __name__ == "__main__":
    main()
