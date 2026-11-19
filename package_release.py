#!/usr/bin/env python3
"""Build reproducible, allowlisted source releases without runtime state."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile


FILES = (
    "relay.py", "ccfolia.py", "onebot.py", "commands.py", "notifications.py",
    "bootstrap.py", "requirements.txt", "config.example.json", "README.md",
    "package_release.py", "docs/安装说明.md", "docs/使用说明.md",
)
OPTIONAL_FILES = ("tools/check_source.py",)
RELOCATED = {
    "install.sh": "packaging/install.sh",
    "ccf-qq-relay.service.in": "packaging/ccf-qq-relay.service.in",
}


def test_files(source):
    """Include public Python tests, without collecting fixtures or state."""
    directory = source / "tests"
    if directory.is_symlink():
        raise ValueError("发行测试目录不能是符号链接。")
    if not directory.exists():
        return ()
    return tuple(sorted(
        path.relative_to(source).as_posix()
        for path in directory.iterdir()
        if path.name == "__init__.py" or re.fullmatch(r"test_[A-Za-z0-9_]+\.py", path.name)
    ))


def validate_public_text(relative, text):
    if re.search(r"/(?:Users|home)/[^/\r\n\"'<>]+/", text):
        raise ValueError("发行文件含个人主目录路径：" + relative)
    emails = re.findall(r"(?<![A-Za-z0-9_.+%:-])[A-Za-z0-9_.+%-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text)
    if any(domain.lower() not in {"example.com", "example.org", "example.net", "example.invalid"} for domain in emails):
        raise ValueError("发行文件含非示例邮箱地址：" + relative)
    if re.search(r"(?:QQ(?:号|号码)?|群号)\s*[:：=]?\s*[\"']?[1-9][0-9]{4,11}(?![0-9])", text, re.IGNORECASE):
        raise ValueError("发行文件含具体 QQ 号码：" + relative)
    if re.search(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----", text):
        raise ValueError("发行文件含私钥：" + relative)


def collect(source):
    source = Path(source)
    content = {}
    selected = {name: name for name in FILES}
    for target, original in RELOCATED.items():
        packaged = source / original
        selected[target] = original if packaged.exists() or packaged.is_symlink() else target
    selected.update({name: name for name in OPTIONAL_FILES if (source / name).exists()})
    selected.update({name: name for name in test_files(source)})
    for target, relative in sorted(selected.items()):
        path = source / relative
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents) or not path.is_file():
            raise ValueError("发行文件缺失或不是普通文件：" + relative)
        data = path.read_bytes()
        text = data.decode("utf-8")
        validate_public_text(relative, text)
        content[target] = data
    example = json.loads(content["config.example.json"])
    public_keys = {"database", "onebot_url", "onebot_token_env", "poll_seconds", "rescan_seconds", "send_interval"}
    if not isinstance(example, dict) or set(example) - public_keys:
        raise ValueError("示例配置必须仅含公开运行参数，不含操作者、账号或令牌。")
    manifest = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in sorted(content.items()))
    content["MANIFEST.sha256"] = manifest.encode("utf-8")
    return content


def build(source, destination, version):
    source, destination = Path(source), Path(destination)
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("版本号格式无效。")
    content = collect(source)
    destination.mkdir(parents=True, exist_ok=True)
    name = "ccf-qq-relay-" + version
    tar_path, zip_path = destination / (name + ".tar.gz"), destination / (name + ".zip")
    # Normalize ownership, permissions and times so a rebuilt release has the
    # same checksum on another machine.
    with tar_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative, data in sorted(content.items()):
                    info = tarfile.TarInfo(name + "/" + relative)
                    info.size = len(data)
                    info.mode = 0o755 if relative == "install.sh" else 0o644
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, data in sorted(content.items()):
            info = zipfile.ZipInfo(name + "/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if relative == "install.sh" else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    sums = "".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n" for path in (tar_path, zip_path))
    (destination / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return tar_path, zip_path


def main():
    parser = argparse.ArgumentParser(description="构建不含本地配置和运行状态的安装包")
    parser.add_argument("--version", default="1.1.0")
    parser.add_argument("--output", default="dist")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    output = Path(args.output)
    if not output.is_absolute():
        output = source / output
    try:
        paths = build(source, output, args.version)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(2, "打包失败：" + str(error) + "\n")
    for path in paths:
        print(path.name)
    print("SHA256SUMS")


if __name__ == "__main__":
    main()
