"""Parse CCfolia reminder commands without retaining credential copies."""

import re
from urllib.parse import urlsplit


COMMAND = ".ccf提醒"
HELP = (
    "CCF提醒：\n"
    ".ccf提醒 登记 <邮箱> <密码>（仅私聊；以后可直接订阅其他房间）\n"
    ".ccf提醒 [频道] <房间网址> <邮箱> <密码>（仅私聊绑定账号）\n"
    ".ccf提醒 [频道] <房间网址>（在当前聊天窗口订阅）\n"
    ".ccf提醒 <房间网址> [频道]（同上）\n"
    "频道默认为 MAIN；全频道 <网址> 或 <网址> 全频道会自动订阅新公开频道。\n"
    "带邮箱密码绑定时，频道须放在网址前；邮箱后的全部内容作为密码。\n"
    "邮箱、密码可另起一行发送；密码内的空格会保留。\n"
    ".ccf提醒 频道 [房间网址]\n"
    ".ccf提醒 测试 [频道] <房间网址>（用最新现有消息核对提醒）\n"
    ".ccf提醒 状态 [房间网址]\n"
    ".ccf提醒 关闭 [频道] [房间网址]\n"
    "关闭不带参数时，取消当前会话的全部订阅；指定房间时取消该房间订阅。\n"
    "群内所有成员可管理已绑定房间；新房间可使用自己的登记账号。"
)
_PREFIX = re.compile(r"^[.。/]?ccf提醒(?:\s+|$)(.*)", re.I | re.S)
_ROOM = re.compile(r"/rooms/([A-Za-z0-9_-]{1,128})/?")
_BARE = re.compile(r"^https://ccfolia\.com(?:[/:]|$)", re.I)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_ACTIONS = {
    "状态": "status", "status": "status",
    "关闭": "off", "关": "off", "off": "off",
    "频道": "channels", "channels": "channels",
    "测试": "test", "test": "test",
}


def parse_url(value):
    """Return the room ID for an unambiguous, genuine CCfolia HTTPS URL."""
    if not isinstance(value, str):
        raise ValueError("请提供 CCF 房间网址。")
    value = value.strip().strip("<>【】")
    if not value or len(value) > 2048 or re.search(r"\s", value):
        raise ValueError("CCF 房间网址格式错误。")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "ccfolia.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        raise ValueError("CCF 房间网址格式错误。") from None
    if not valid:
        raise ValueError("只接受 https://ccfolia.com/rooms/房间ID 的网址。")
    match = _ROOM.fullmatch(parsed.path)
    if match is None or parsed.query or parsed.fragment:
        raise ValueError("CCF 房间网址须为 https://ccfolia.com/rooms/房间ID。")
    return match.group(1)


def _is_url(value):
    return value.strip("<>【】").lower().startswith("https://")


def _channel(value):
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError("频道名格式错误。")
    if _is_url(value) or "@" in value:
        raise ValueError("请提供频道名称；绑定账号还需要邮箱和密码。")
    return value


def _target(tail):
    """Optional action target: room, channel, or the two in either order."""
    parts = tail.split()
    if not parts:
        return "", None
    urls = [part for part in parts if _is_url(part)]
    if len(urls) > 1:
        raise ValueError("请只提供一个房间网址。")
    url = urls[0] if urls else ""
    if url:
        parse_url(url)
    name = tail.replace(url, "", 1).strip() if url else tail.strip()
    channel = _channel(name) if name else None
    return url, channel


def command(text):
    """Return a command dictionary or ``None`` for unrelated QQ messages.

    Subscription defaults to MAIN. For a credential binding, an explicit
    channel precedes the URL so all text after the email remains a password.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    match = _PREFIX.match(text)
    if match is None:
        if not _BARE.match(text):
            return None
        tail = text
    else:
        tail = match.group(1).strip()
    if not tail or tail.lower() in ("帮助", "help"):
        return {"action": "help"}

    head = tail.split(None, 1)
    if head[0].casefold() in ("登记", "register"):
        if len(head) == 1:
            return {"action": "register"}
        credentials = head[1].split(None, 1)
        if len(credentials) != 2 or not _EMAIL.fullmatch(credentials[0]):
            raise ValueError("请在私聊中提供邮箱和密码。")
        return {"action": "register", "username": credentials[0], "password": credentials[1]}
    action = _ACTIONS.get(head[0].lower())
    if action:
        url, channel = _target(head[1] if len(head) == 2 else "")
        if action == "test" and not url:
            raise ValueError("测试请提供房间网址，可另加频道名。")
        return {"action": action, "url": url, "channel": channel}

    channel = "MAIN"
    explicit_channel = False
    if not _is_url(head[0]):
        channel = _channel(head[0])
        explicit_channel = True
        if len(head) != 2:
            raise ValueError("请提供 CCF 房间网址。")
        tail = head[1]
    # The final split is the only credential split. Password tokens are never
    # reparsed as channels, commands, URLs, or options.
    parts = tail.split(None, 2)
    url = parts[0]
    parse_url(url)
    if len(parts) == 1:
        return {"action": "attach", "url": url, "channel": channel}
    if len(parts) == 2 and not explicit_channel:
        return {"action": "attach", "url": url, "channel": _channel(parts[1])}
    if len(parts) != 3 or not _EMAIL.fullmatch(parts[1]):
        raise ValueError("绑定账号请提供房间网址、邮箱和密码；指定频道须放在网址前。")
    return {"action": "bind", "url": url, "channel": channel,
            "username": parts[1], "password": parts[2]}
