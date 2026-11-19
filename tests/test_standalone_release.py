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

PACKAGE = "ccf-qq-relay"


class StandaloneReleaseTests(unittest.TestCase):
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
        (self.source / "config.example.json").write_text("{}\n", encoding="utf-8")

    def fixture(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_new_public_python_tests_are_included_and_state_is_excluded(self):
        self.fixture("tests/test_new_feature.py", "# public regression test\n")
        self.fixture("tests/__init__.py", "")
        for relative in ("config.json", "token.env", "state/relay.sqlite3",
                         "private.log", "tests/private_fixture.json",
                         "tests/__pycache__/test_old.pyc", "docs/核验记录.md"):
            self.fixture(relative, "synthetic private state\n")
        content = package_release.collect(self.source)
        self.assertIn("tests/test_new_feature.py", content)
        self.assertIn("tests/__init__.py", content)
        self.assertNotIn("docs/核验记录.md", content)
        self.assertFalse(any(b"synthetic private state" in data for data in content.values()))

    def test_build_is_reproducible_and_extracted_source_can_rebuild_it(self):
        self.fixture("tests/test_new_feature.py", "# public regression test\n")
        first = package_release.build(self.source, self.directory / "first", "1.1.0")
        second = package_release.build(self.source, self.directory / "second", "1.1.0")
        for a, b in zip(first, second):
            self.assertEqual(a.read_bytes(), b.read_bytes())
        prefix = PACKAGE + "-1.1.0/"
        extracted = self.directory / "extracted"
        with tarfile.open(first[0], "r:gz") as archive:
            names = archive.getnames()
            self.assertTrue(all(name.startswith(prefix) for name in names))
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                path = extracted / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
                path.chmod(member.mode)
        with zipfile.ZipFile(first[1]) as archive:
            self.assertEqual(set(archive.namelist()), set(names))
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))
            self.assertEqual((archive.getinfo(prefix + "install.sh").external_attr >> 16) & 0o777, 0o755)
        release = extracted / (PACKAGE + "-1.1.0")
        self.assertTrue((release / "install.sh").stat().st_mode & 0o100)
        for line in (release / "MANIFEST.sha256").read_text().splitlines():
            digest, relative = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((release / relative).read_bytes()).hexdigest())
        third = package_release.build(release, self.directory / "third", "1.1.0")
        for a, b in zip(first, third):
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_symlinked_public_tests_are_rejected(self):
        target = self.fixture("external.py", "# outside allowlist\n")
        tests = self.source / "tests"
        tests.mkdir()
        (tests / "test_escape.py").symlink_to(target)
        with self.assertRaises(ValueError):
            package_release.collect(self.source)

    def test_symlinked_test_directory_is_rejected(self):
        external = self.directory / "external_tests"
        external.mkdir()
        (self.source / "tests").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            package_release.collect(self.source)

    def test_identifiable_text_and_all_private_key_formats_are_rejected(self):
        relative = "commands.py"
        samples = ("/Users/" + "private-owner/project/", "/home/" + "private-owner/project/", "/Users/" + "示例用户/project/",
                   "user" + "@" + "private.test", "QQ" + chr(65306) + str(246813579),
                   "-----BEGIN " + "OPENSSH PRIVATE KEY-----")
        for sample in samples:
            with self.subTest(sample=sample):
                self.fixture(relative, sample)
                with self.assertRaises(ValueError):
                    package_release.collect(self.source)
        self.fixture(relative, "user@example.invalid\n")
        self.assertIn(relative, package_release.collect(self.source))

    def test_sensitive_example_config_and_invalid_version_are_rejected(self):
        for key in ("allowed_user", "allowed_users", "admin_qq", "username", "password", "onebot_token", "credentials"):
            self.fixture("config.example.json", json.dumps({key: "synthetic"}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                package_release.collect(self.source)
        self.fixture("config.example.json", "[]")
        with self.assertRaises(ValueError):
            package_release.collect(self.source)
        with self.assertRaises(ValueError):
            package_release.build(self.source, self.directory / "output", "../../escape")


if __name__ == "__main__":
    unittest.main()
