"""Metadata-only formatting and pure per-channel burst/silence transitions."""

import copy
import datetime as dt
import json
import unittest

from notifications import epoch, render, resume, transition


TITLE = "示例房间-MAIN"


def message(at, name="昵称", **extra):
    value = {"created": dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(),
             "name": name, "id": "example-message", "channelId": "main",
             "text": "正文绝不能进入提醒[CQ:image,file=private-example]"}
    value.update(extra)
    return value


def muted():
    state = None
    for at in (0, 90, 180):
        state, _ = transition(state, message(at), TITLE)
    return state


class RenderTests(unittest.TestCase):
    def test_exact_metadata_and_beijing_clock(self):
        self.assertEqual(render(message(0), TITLE), [f"「{TITLE}」[08:00:00] 昵称回复了。"])

    def test_never_emits_body_entities_or_attachments(self):
        for extra in ({}, {"text": ""}, {"mediaId": "private-attachment"},
                      {"entities": [{"text": "hidden entity text"}]}, {"extension": {"secret": "body"}}):
            with self.subTest(extra=extra):
                self.assertEqual(render(message(0, **extra), TITLE),
                                 [f"「{TITLE}」[08:00:00] 昵称回复了。"])

    def test_unknown_sender_fallback_and_single_line(self):
        self.assertEqual(render(message(0, name="", senderId="user-id"), TITLE),
                         [f"「{TITLE}」[08:00:00] user-id回复了。"])
        self.assertEqual(render(message(0, name=""), TITLE),
                         [f"「{TITLE}」[08:00:00] 未知发言者回复了。"])
        self.assertNotIn("\n", render(message(0, name="A\nB"), "房间\r频道")[0])

    def test_timestamp_precision_offset_and_validation(self):
        self.assertEqual(epoch("2026-10-15T08:34:56.123456789+08:00"),
                         epoch("2026-10-15T00:34:56.123456Z"))
        self.assertEqual(epoch("2026-10-15t00:34:56.12345z"),
                         epoch("2026-10-15T00:34:56.123450Z"))
        for value in (None, 100, "2026-10-15T08:34:56", "2026-13-15T08:34:56Z", "2026-10-15T08:34:56+24:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                epoch(value)


class TransitionTests(unittest.TestCase):
    def test_third_warns_at_ninety_second_boundary(self):
        state = None
        for index, at in enumerate((0, 90, 180)):
            state, notices = transition(state, message(at, name=str(index)), TITLE)
            self.assertEqual(len(notices), 1)
            if index < 2:
                self.assertIn("回复了。", notices[0])
            else:
                self.assertEqual(notices, [
                    f"监测到「{TITLE}」高频回复。超过5分钟无人回复前，不再提醒该频道消息。"])
        self.assertEqual(state["phase"], "muted")

    def test_larger_gap_restarts_streak(self):
        state = None
        for at in (0, 90, 180.001):
            state, notices = transition(state, message(at), TITLE)
        self.assertEqual(state["phase"], "normal")
        self.assertEqual(state["streak"], 1)
        self.assertIn("回复了。", notices[0])

    def test_muted_messages_update_latest_speaker_without_notices(self):
        state, notices = transition(muted(), message(400, "后者"), TITLE)
        self.assertEqual(notices, [])
        self.assertEqual(state["last_qualifying"], {"created": 400, "name": "后者"})
        self.assertEqual(resume(state, TITLE, 699.999)[1], [])
        self.assertIn("后者", resume(state, TITLE, 700)[1][0])

    def test_timer_silence_boundary_and_no_duplicate_recovery(self):
        state, notices = resume(muted(), TITLE, 479.999)
        self.assertEqual(notices, [])
        state, notices = resume(state, TITLE, 480)
        self.assertEqual(notices, [
            f"「{TITLE}」最后一条消息由[08:03:00] 昵称 回复。重新开始提醒该频道消息。"])
        self.assertEqual(state["phase"], "normal")
        self.assertEqual(state["streak"], 0)
        self.assertEqual(resume(state, TITLE, 1000)[1], [])

    def test_new_message_after_quiet_resumes_then_reports(self):
        state, notices = transition(muted(), message(480, "新者"), TITLE)
        self.assertEqual(len(notices), 2)
        self.assertIn("重新开始提醒", notices[0])
        self.assertEqual(notices[1], f"「{TITLE}」[08:08:00] 新者回复了。")
        self.assertEqual(state["streak"], 1)

    def test_every_visible_message_counts_even_empty_content(self):
        state = None
        for at in (0, 20, 40):
            state, notices = transition(state, message(at, text=""), TITLE)
        self.assertEqual(state["phase"], "muted")
        self.assertIn("高频回复", notices[0])

    def test_hidden_removed_messages_have_no_effect(self):
        for flag in ("_hidden", "_removed"):
            original = muted()
            state, notices = transition(original, {flag: True}, TITLE)
            self.assertEqual(state, original)
            self.assertEqual(notices, [])
            self.assertIn("重新开始提醒", resume(state, TITLE, 480)[1][0])

    def test_json_roundtrip_restart_and_purity(self):
        original = muted()
        before = copy.deepcopy(original)
        state, notices = transition(json.loads(json.dumps(original)), message(200, "重启后"), TITLE)
        self.assertEqual(original, before)
        self.assertEqual(notices, [])
        restarted = json.loads(json.dumps(state))
        self.assertIn("重启后", resume(restarted, TITLE, 500)[1][0])
        self.assertNotIn("正文", json.dumps(restarted, ensure_ascii=False))

    def test_ingestion_time_does_not_manufacture_burst(self):
        state = None
        for at in (0, 100, 200):
            state, notices = transition(state, message(at), TITLE, now=1000)
        self.assertEqual(state["phase"], "normal")
        self.assertIn("回复了。", notices[0])

    def test_delayed_older_message_does_not_move_clock_or_speaker(self):
        state, _ = transition(None, message(100), TITLE)
        state, notices = transition(state, message(50, "迟到者"), TITLE)
        self.assertEqual(state["last_source_time"], 100)
        self.assertEqual(state["last_activity"], 100)
        self.assertEqual(state["streak"], 1)
        self.assertNotEqual(state["last_qualifying"]["name"], "迟到者")
        self.assertIn("迟到者", notices[0])

    def test_unsupported_state_fields_are_not_retained(self):
        state, _ = transition({"text": "private-example", "streak": True,
                               "last_activity": float("inf"), "last_qualifying": {"created": "bad"}},
                              message(100), TITLE)
        self.assertNotIn("text", state)
        self.assertEqual(state["streak"], 1)
        self.assertEqual(state["last_activity"], 100)

    def test_invalid_current_time_is_rejected(self):
        for now in (None, True, float("inf"), "300"):
            with self.subTest(now=now), self.assertRaises(ValueError):
                resume(muted(), TITLE, now)

    def test_channels_have_independent_state(self):
        main, _ = transition(None, message(0), "Room-MAIN")
        info, _ = transition(None, message(1), "Room-INFO")
        main, _ = transition(main, message(2), "Room-MAIN")
        main, _ = transition(main, message(3), "Room-MAIN")
        info, notices = transition(info, message(4), "Room-INFO")
        self.assertEqual(main["phase"], "muted")
        self.assertEqual(info["phase"], "normal")
        self.assertIn("Room-INFO", notices[0])


if __name__ == "__main__":
    unittest.main()
