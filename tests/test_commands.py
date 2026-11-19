"""Synthetic command and URL cases; no account or network required."""

import unittest

from commands import command, parse_url


ROOM = "https://ccfolia.com/rooms/exampleRoom_1"


class URLTests(unittest.TestCase):
    def test_room_id_is_preserved(self):
        for url in (ROOM, ROOM + "/", "<" + ROOM + ">", "【" + ROOM + "】",
                    ROOM.replace(".com/", ".com:443/")):
            with self.subTest(url=url):
                self.assertEqual(parse_url(url), "exampleRoom_1")

    def test_foreign_origin_userinfo_schemes_and_ports_are_rejected(self):
        for url in (
            ROOM.replace("https:", "http:"),
            ROOM.replace("ccfolia.com", "ccfolia.com.example.invalid"),
            ROOM.replace("ccfolia.com", "example.invalid"),
            ROOM.replace("ccfolia.com", "user:password@ccfolia.com"),
            ROOM.replace("ccfolia.com", "@ccfolia.com"),
            ROOM.replace("ccfolia.com", "ccfolia.com:8443"),
            ROOM.replace("ccfolia.com", "ccfolia.com:bad"),
            ROOM.replace("ccfolia.com", "ccfolia.com:99999"),
            ROOM.replace("ccfolia.com", "ccfolia.com."),
            ROOM.replace("https://", "https://["),
            ROOM.replace("https://", "https:\n//"),
            "https://ccfolia.com\\@example.invalid/rooms/exampleRoom_1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_url(url)

    def test_nonroom_and_ambiguous_paths_are_rejected(self):
        for url in (None, "", "https://ccfolia.com", "https://ccfolia.com/rooms/",
                    ROOM + "/other", ROOM + "?room=other", ROOM + "#other",
                    ROOM.replace("rooms", "room"), ROOM.replace("exampleRoom_1", "a%2Fb"),
                    ROOM.replace("exampleRoom_1", "a.b"), ROOM.replace("exampleRoom_1", "a" * 129)):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_url(url)


class CommandTests(unittest.TestCase):
    def test_default_main_attach_and_prefix_variants(self):
        for prefix in (".ccf提醒", "/ccf提醒", "。ccf提醒", "ccf提醒", ".CCF提醒"):
            with self.subTest(prefix=prefix):
                self.assertEqual(command(prefix + " " + ROOM),
                                 {"action": "attach", "url": ROOM, "channel": "MAIN"})

    def test_explicit_channel_before_or_after_url(self):
        for channel in ("MAIN", "INFO", "OTHER", "秘密频道"):
            expected = {"action": "attach", "url": ROOM, "channel": channel}
            self.assertEqual(command(f".ccf提醒 {channel} {ROOM}"), expected)
            self.assertEqual(command(f".ccf提醒 {ROOM} {channel}"), expected)

    def test_bind_default_and_explicit_channel_with_multiline_credentials(self):
        expected = {"action": "bind", "url": ROOM, "channel": "MAIN",
                    "username": "member@example.invalid", "password": "pass word"}
        self.assertEqual(command(f".ccf提醒\n{ROOM}\nmember@example.invalid\npass word"), expected)
        self.assertEqual(command(f".ccf提醒 INFO {ROOM} member@example.invalid pass word"),
                         {**expected, "channel": "INFO"})

    def test_password_is_not_reinterpreted(self):
        for password in ("MAIN", "INFO", "关闭", "pass  word", "pass word OTHER", "p&=#!?%[]", "\"quoted\" value"):
            for prefix in (".ccf提醒 ", ".ccf提醒 OTHER "):
                self.assertEqual(command(prefix + ROOM + " member@example.invalid " + password)["password"],
                                 password)

    def test_bare_url_attach_and_binding(self):
        self.assertEqual(command(ROOM), {"action": "attach", "url": ROOM, "channel": "MAIN"})
        self.assertEqual(command(ROOM + "\nmember@example.invalid\npassword"), {
            "action": "bind", "url": ROOM, "channel": "MAIN",
            "username": "member@example.invalid", "password": "password"})

    def test_help(self):
        for text in (".ccf提醒", ".ccf提醒 help", ".ccf提醒 帮助"):
            self.assertEqual(command(text), {"action": "help"})

    def test_actions_with_optional_room_and_channel(self):
        for token, action in (("状态", "status"), ("关闭", "off"),
                              ("频道", "channels"), ("off", "off"), ("channels", "channels")):
            self.assertEqual(command(f".ccf提醒 {token}"),
                             {"action": action, "url": "", "channel": None})
            self.assertEqual(command(f".ccf提醒 {token} {ROOM}"),
                             {"action": action, "url": ROOM, "channel": None})
            self.assertEqual(command(f".ccf提醒 {token} INFO"),
                             {"action": action, "url": "", "channel": "INFO"})
            for target in ("INFO " + ROOM, ROOM + " INFO"):
                self.assertEqual(command(f".ccf提醒 {token} {target}"),
                                 {"action": action, "url": ROOM, "channel": "INFO"})

    def test_test_command_and_registration(self):
        for alias in ("测试", "test", "TEST"):
            self.assertEqual(command(f".ccf提醒 {alias} {ROOM}"),
                             {"action": "test", "url": ROOM, "channel": None})
            self.assertEqual(command(f".ccf提醒 {alias} INFO {ROOM}")["channel"], "INFO")
            with self.assertRaises(ValueError):
                command(f".ccf提醒 {alias}")
        self.assertEqual(command("。ccf提醒 登记"), {"action": "register"})
        self.assertEqual(command("。ccf提醒 登记 member@example.invalid pass  word"),
                         {"action": "register", "username": "member@example.invalid", "password": "pass  word"})
        for text in (f".ccf提醒 全频道 {ROOM}", f".ccf提醒 {ROOM} 全频道"):
            self.assertEqual(command(text), {"action": "attach", "url": ROOM, "channel": "全频道"})

    def test_unrelated_text_is_ignored(self):
        for text in (None, ".ccf", ".ccf提醒extra 状态", "ordinary QQ message", "http://ccfolia.com/rooms/example",
                     "https://ccfolia.com.example.invalid/rooms/example", ".boluo提醒 " + ROOM):
            self.assertIsNone(command(text))

    def test_malformed_bindings_and_targets_fail(self):
        for text in (f".ccf提醒 {ROOM} member@example.invalid",
                     f".ccf提醒 INFO {ROOM} member@example.invalid",
                     f".ccf提醒 {ROOM} INFO member@example.invalid password",
                     f".ccf提醒 {ROOM} member password", ".ccf提醒 INFO",
                     ".ccf提醒 https://example.invalid/rooms/room", ".ccf提醒 测试 https://example.invalid/",
                     f".ccf提醒 测试 {ROOM} {ROOM}",
                     ".ccf提醒 INFO invalid", f".ccf提醒 {ROOM} " + "x" * 129):
            with self.subTest(text=text), self.assertRaises(ValueError):
                command(text)


if __name__ == "__main__":
    unittest.main()
