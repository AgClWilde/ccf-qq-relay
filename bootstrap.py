#!/usr/bin/env python3
"""Validated configuration and a credential-free Linux user installer."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


SERVICE = "ccf-qq-relay.service"
RUNTIME_FILES = (
    "relay.py", "ccfolia.py", "onebot.py", "commands.py", "notifications.py",
    "bootstrap.py", "requirements.txt", "config.example.json",
)
ENV_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """Configuration error whose message never includes credentials."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("配置含有重复字段。")
        result[key] = value
    return result


def read_json(path):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_object)
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("无法读取有效的 JSON 配置。") from None
    if not isinstance(result, dict):
        raise ConfigError("配置必须是 JSON 对象。")
    return result


def validate_url(value):
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        endpoint = urlsplit(value)
        port = endpoint.port if endpoint.port is not None else (443 if endpoint.scheme == "wss" else 80)
        if (endpoint.scheme not in {"ws", "wss"} or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or not 1 <= port <= 65535):
            raise ValueError
    except ValueError:
        raise ConfigError("onebot_url 须为 ws:// 或 wss:// 地址，不能含账号、密码、查询参数或片段。") from None
    return value, port


def _interval(config, key, default):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ConfigError(key + " 必须为有限正数。")
    return float(value)


def validate_config(config, base):
    if not isinstance(config, dict):
        raise ConfigError("配置必须是 JSON 对象。")
    result = dict(config)
    result["onebot_url"], port = validate_url(config.get("onebot_url", "ws://127.0.0.1:3001"))
    # Ignore legacy allowlists so installations made with earlier versions
    # become open to all QQ users without editing their existing config.json.
    result.pop("allowed_users", None)
    result.pop("allowed_user", None)
    database = config.get("database", "state/relay.sqlite3")
    if not isinstance(database, str) or not database.strip() or "\x00" in database or database == ":memory:":
        raise ConfigError("database 须为持久数据库文件路径。")
    database_path = Path(database).expanduser()
    if not database_path.is_absolute():
        database_path = Path(base) / database_path
    if database_path.is_symlink() or (database_path.exists() and not database_path.is_file()):
        raise ConfigError("database 不能指向符号链接或非普通文件。")
    result["database"] = str(database_path.absolute())
    for key, default in (("poll_seconds", 3), ("rescan_seconds", 300), ("send_interval", 1.2)):
        result[key] = _interval(config, key, default)
    env_key = config.get("onebot_token_env", "CCF_ONEBOT_TOKEN")
    if not isinstance(env_key, str) or not ENV_PATTERN.fullmatch(env_key):
        raise ConfigError("onebot_token_env 不是有效的环境变量名称。")
    result["onebot_token_env"] = env_key
    if "onebot_token" in config:
        raise ConfigError("请通过 onebot_token_env 或 napcat_config 提供令牌，不要在配置中直接保存 onebot_token。")
    token = os.environ.get(env_key, "")
    if config.get("napcat_config"):
        value = config["napcat_config"]
        if not isinstance(value, str):
            raise ConfigError("napcat_config 须为配置文件路径。")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(base) / path
        document = read_json(path)
        selected_port = config.get("napcat_ws_port", port)
        if isinstance(selected_port, bool) or not isinstance(selected_port, int) or not 1 <= selected_port <= 65535:
            raise ConfigError("napcat_ws_port 须为 1–65535 的整数。")
        network = document.get("network")
        servers = network.get("websocketServers") if isinstance(network, dict) else None
        if not isinstance(servers, list):
            raise ConfigError("NapCat 配置缺少正向 WebSocket 列表。")
        matches = [server for server in servers if isinstance(server, dict)
                   and server.get("enable") is True and str(server.get("port")) == str(selected_port)]
        if len(matches) != 1 or not isinstance(matches[0].get("token", ""), str):
            raise ConfigError("NapCat 配置须有唯一匹配端口的已启用正向 WebSocket。")
        token = matches[0].get("token", "")
        result["napcat_config"] = str(path.absolute())
        result["napcat_ws_port"] = selected_port
    if any(c in token for c in "\r\n\x00"):
        raise ConfigError("OneBot 令牌含无效控制字符。")
    result["onebot_token"] = token
    return result


def load_config(path):
    path = Path(path).expanduser().absolute()
    return validate_config(read_json(path), path.parent)


def _safe_path(value):
    path = Path(value).expanduser().absolute()
    # systemd has expansions even inside quoted values.
    if (str(path) != str(path).strip() or any(ord(c) < 32 for c in str(path))
            or any(c in str(path) for c in '\\"%$*?[]')):
        raise ConfigError("安装路径不能包含首尾空白、控制字符、反斜杠、双引号、%、$ 或通配符。")
    return path


def render_unit(template, prefix):
    fields = {"WORKING_DIRECTORY": prefix, "PYTHON": prefix / ".venv/bin/python",
              "RELAY": prefix / "relay.py", "CONFIG": prefix / "config.json",
              "ENVIRONMENT_FILE": prefix / "token.env"}
    unit = template
    for key, value in fields.items():
        # ExecStart tokenizes words and removes quotes. WorkingDirectory and
        # EnvironmentFile parse a single raw path; quotes become literal bytes.
        rendered = '"' + str(value) + '"' if key in {"PYTHON", "RELAY", "CONFIG"} else str(value)
        unit = unit.replace("@" + key + "@", rendered)
    if re.search(r"@[A-Z_]+@", unit):
        raise ConfigError("服务模板含未识别字段。")
    return unit


def _run(command):
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise ConfigError("安装命令失败；修复环境后可重新运行安装器。") from None


def install(arguments, source=None):
    parser = argparse.ArgumentParser(description="安装 CCF QQ 提醒（Linux / systemd 用户服务）")
    parser.add_argument("--prefix", default=str(Path.home() / ".local/share/ccf-qq-relay"))
    # Accept legacy installer arguments, but do not restrict command access.
    parser.add_argument("--admin-qq", action="append", help=argparse.SUPPRESS)
    parser.add_argument("--onebot-url", default="ws://127.0.0.1:3001")
    parser.add_argument("--napcat-config", metavar="PATH", help="只读自己的 NapCat 配置")
    parser.add_argument("--napcat-port", type=int)
    parser.add_argument("--enable", action="store_true", help="安装后立即启动并启用用户服务")
    parser.add_argument("--dry-run", action="store_true", help="仅检查；不写文件、不联网、不启动服务")
    args = parser.parse_args(arguments)
    if sys.version_info < (3, 10):
        raise ConfigError("需要 Python 3.10 或更高版本。")
    if not args.dry_run and not sys.platform.startswith("linux"):
        raise ConfigError("自动安装仅支持 Linux；其他系统可手动安装或用 --dry-run 检查。")
    prefix = _safe_path(args.prefix)
    unit_dir = _safe_path(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user")
    unit_path = unit_dir / SERVICE
    source = Path(source) if source is not None else Path(__file__).resolve().parent
    if prefix.is_symlink():
        raise ConfigError("安装目录不能是符号链接。")
    config = {"database": "state/relay.sqlite3", "onebot_url": args.onebot_url,
              "onebot_token_env": "CCF_ONEBOT_TOKEN",
              "poll_seconds": 3, "rescan_seconds": 300, "send_interval": 1.2}
    _, port = validate_url(args.onebot_url)
    if args.napcat_port is not None and not args.napcat_config:
        raise ConfigError("--napcat-port 需要同时提供 --napcat-config。")
    if args.napcat_config:
        config["napcat_config"] = str(Path(args.napcat_config).expanduser().absolute())
        config["napcat_ws_port"] = args.napcat_port if args.napcat_port is not None else port
    validate_config(config, prefix)
    template = source / "ccf-qq-relay.service.in"
    if not template.is_file():
        template = source / "packaging/ccf-qq-relay.service.in"
    if not template.is_file() or template.is_symlink():
        raise ConfigError("发行包缺少服务模板。")
    files = {name: source / name for name in RUNTIME_FILES}
    files["ccf-qq-relay.service.in"] = template
    for name, original in files.items():
        target = prefix / name
        if not original.is_file() or original.is_symlink():
            raise ConfigError("发行包缺少正常文件：" + name)
        if target.is_symlink() or (target.exists() and (not target.is_file() or target.read_bytes() != original.read_bytes())):
            raise ConfigError("安装目录已有不同内容：" + name + "；请备份旧版后选择新目录。")
    for name in ("config.json", "token.env"):
        path = prefix / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConfigError("已有配置不是普通文件：" + name)
    config_path = prefix / "config.json"
    if config_path.exists():
        load_config(config_path)
    for name in ("state", ".venv"):
        path = prefix / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ConfigError("已有 " + name + " 不是普通目录。")
    if (prefix / ".venv").exists() and not (prefix / ".venv/pyvenv.cfg").is_file():
        raise ConfigError("已有 .venv 不是虚拟环境。")
    unit = render_unit(template.read_text(encoding="utf-8"), prefix)
    if unit_path.is_symlink() or (unit_path.exists() and (not unit_path.is_file() or unit_path.read_text(encoding="utf-8") != unit)):
        raise ConfigError("已有同名用户服务指向其他内容；请检查自己的 systemd 用户服务。")
    if args.dry_run:
        print("安装计划检查通过；未写文件、安装依赖或启动服务。")
        print("安装目录：" + str(prefix))
        print("用户服务：" + str(unit_path))
        return
    if shutil.which("systemctl") is None:
        raise ConfigError("找不到 systemctl；可按安装说明手动运行。")
    os.umask(0o077)
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(prefix, 0o700)
    (prefix / "state").mkdir(mode=0o700, exist_ok=True)
    os.chmod(prefix / "state", 0o700)
    for name, original in files.items():
        if not (prefix / name).exists():
            shutil.copyfile(original, prefix / name)
    if not config_path.exists():
        with config_path.open("x", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    else:
        print("保留已有 config.json 和订阅状态；本次参数不会改写已有配置。")
    os.chmod(config_path, 0o600)
    if not (prefix / ".venv/bin/python").exists():
        _run([sys.executable, "-m", "venv", str(prefix / ".venv")])
    _run([str(prefix / ".venv/bin/python"), "-m", "pip", "--isolated", "install", "--index-url", "https://pypi.org/simple", "-r", str(prefix / "requirements.txt")])
    unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not unit_path.exists():
        with unit_path.open("x", encoding="utf-8") as handle:
            handle.write(unit)
    _run(["systemctl", "--user", "daemon-reload"])
    if args.enable:
        _run(["systemctl", "--user", "enable", "--now", SERVICE])
        print("安装完成，用户服务已启用。")
    else:
        print("安装完成。核对连接配置后运行：systemctl --user enable --now " + SERVICE)
    if not config.get("napcat_config"):
        print("若 OneBot 设有访问令牌，请在安装目录的 token.env 中填写 CCF_ONEBOT_TOKEN，并将权限设为 600。")


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "check":
            parser = argparse.ArgumentParser(description="检查 CCF QQ 提醒配置（不连接服务）")
            parser.add_argument("--config", default="config.json")
            args = parser.parse_args(sys.argv[2:])
            config = load_config(args.config)
            print("配置检查通过；所有 QQ 用户均可使用 .ccf提醒 指令。")
        elif len(sys.argv) > 1 and sys.argv[1] == "install":
            install(sys.argv[2:])
        else:
            raise ConfigError("用法：python3 bootstrap.py install [--onebot-url URL]；或 check --config config.json")
    except (ConfigError, OSError):
        # OSError may embed file names; never emit exception internals.
        error = sys.exc_info()[1]
        print("操作失败：" + (str(error) if isinstance(error, ConfigError) else "文件操作失败；请检查路径与权限。"), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
