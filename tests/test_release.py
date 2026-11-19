import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        for relative in (*package_release.FILES, *package_release.RELOCATED.values()):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic public source\n", encoding="utf-8")
        (self.source / "config.example.json").write_text(json.dumps({}))

    def test_release_is_allowlisted_reproducible_and_manifested(self):
        for relative in ("config.json", "token.env", "state/relay.sqlite3", "private.log", "tests/private_state.json"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SYNTHETIC_PRIVATE_DATA")
        first = package_release.build(self.source, self.directory / "one", "1.0.0")
        second = package_release.build(self.source, self.directory / "two", "1.0.0")
        for a, b in zip(first, second):
            self.assertEqual(a.read_bytes(), b.read_bytes())
        with tarfile.open(first[0], "r:gz") as archive:
            names = archive.getnames()
            self.assertTrue(all(name.startswith("ccf-qq-relay-1.0.0/") for name in names))
            self.assertFalse(any("state/" in name or name.endswith("config.json") or name.endswith("token.env") or name.endswith("private_state.json") for name in names))
            for member in archive.getmembers():
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.uname, "")
                self.assertNotIn(b"SYNTHETIC_PRIVATE_DATA", archive.extractfile(member).read())
            manifest = archive.extractfile("ccf-qq-relay-1.0.0/MANIFEST.sha256").read().decode()
            for line in manifest.splitlines():
                checksum, relative = line.split("  ", 1)
                self.assertEqual(checksum, hashlib.sha256(archive.extractfile("ccf-qq-relay-1.0.0/" + relative).read()).hexdigest())
        with zipfile.ZipFile(first[1]) as archive:
            self.assertEqual(set(archive.namelist()), set(names))

    def test_extracted_release_can_be_rebuilt_identically(self):
        for relative in (*package_release.OPTIONAL_FILES,
                         "tests/test_relay_integration.py", "tests/test_workers.py"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic public verification\n", encoding="utf-8")
        original = package_release.build(self.source, self.directory / "original", "1.0.0")
        extraction = self.directory / "extracted"
        extraction.mkdir()
        with tarfile.open(original[0], "r:gz") as archive:
            for member in archive.getmembers():
                path = extraction / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
                path.chmod(member.mode)
        extracted = extraction / "ccf-qq-relay-1.0.0"
        self.assertTrue((extracted / "install.sh").is_file())
        self.assertFalse((extracted / "packaging").exists())
        for relative in (*package_release.OPTIONAL_FILES,
                         "tests/test_relay_integration.py", "tests/test_workers.py"):
            self.assertTrue((extracted / relative).is_file())
        rebuilt = package_release.build(extracted, self.directory / "rebuilt", "1.0.0")
        for first, second in zip(original, rebuilt):
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_symlink_and_personal_data_are_rejected(self):
        readme = self.source / "README.md"
        for text in ("Contact person" + "@" + "private.test", "/Users/" + "private-owner/project/", "/home/" + "private-owner/project/"):
            readme.write_text(text)
            with self.assertRaises(ValueError):
                package_release.collect(self.source)
        readme.unlink()
        readme.symlink_to(self.source / "requirements.txt")
        with self.assertRaises(ValueError):
            package_release.collect(self.source)

    def test_sensitive_example_config_and_bad_version_rejected(self):
        (self.source / "config.example.json").write_text('{"allowed_users": ["12345"]}')
        with self.assertRaises(ValueError):
            package_release.collect(self.source)
        with self.assertRaises(ValueError):
            package_release.build(self.source, self.directory / "output", "../../escape")


if __name__ == "__main__":
    unittest.main()
