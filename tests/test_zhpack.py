"""zhpack / asarpatch 的纯逻辑测试。不需要装 Claude。

运行：python3 -m unittest discover -s tests
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asarpatch  # noqa: E402
import zhpack  # noqa: E402


class IcuCheck(unittest.TestCase):
    def test_args(self):
        self.assertEqual(zhpack.icu_args("Hi {name}, {count, plural, one {# item} other {# items}}"), {"name", "count"})
        # plural 分支里的字面量不是参数
        self.assertEqual(zhpack.icu_args("{n, plural, one {file} other {files{s}}}"), {"n", "s"})
        # 引号里的花括号是字面量
        self.assertEqual(zhpack.icu_args("Use '{braces}' and {x}"), {"x"})

    def test_pair(self):
        self.assertIsNone(zhpack.check_pair("Open {name}", "打开 {name}"))
        self.assertIn("占位符", zhpack.check_pair("Open {name}", "打开 {nom}"))
        self.assertIn("标签", zhpack.check_pair("See <link>docs</link>", "查看文档"))


class Merge(unittest.TestCase):
    def test_fallback_to_english(self):
        en = {"a": "Open", "b": "Hi {name}", "c": "Quit"}
        zh = {"a": "打开", "b": "你好 {名字}"}
        merged, translated, problems = zhpack.merge_catalog(en, zh)
        self.assertEqual(merged, {"a": "打开", "b": "Hi {name}", "c": "Quit"})
        self.assertEqual(translated, 1)
        self.assertEqual(len(problems), 1)

    def test_shell_translations_are_valid(self):
        """仓库里的外壳译文本身不能有占位符 / 标签损坏。"""
        en_path = ROOT / "extracted" / "desktop.en-US.json"
        if not en_path.is_file():
            self.skipTest("先运行 zhpack extract")
        en = zhpack.load_json(en_path)
        zh = zhpack.load_json(zhpack.SHELL_ZH)
        broken = [k for k, v in zh.items() if k in en and v != en[k] and zhpack.check_pair(en[k], v)]
        self.assertEqual(broken, [])


@unittest.skipUnless(shutil.which("csreq"), "需要 macOS 的 csreq")
class Requirement(unittest.TestCase):
    def test_compiles(self):
        upstream = zhpack.canonical_upstream_requirement(zhpack.BUNDLE_ID)
        self.assertTrue(zhpack.is_upstream_requirement(upstream))
        req = f'designated => ({upstream}) or (identifier "{zhpack.BUNDLE_ID}" and {zhpack.ADHOC_BRANCH})'
        self.assertIsNone(zhpack.requirement_syntax_error(req))
        self.assertFalse(zhpack.is_upstream_requirement(req.split("=>", 1)[1]))
        self.assertFalse(zhpack.is_upstream_requirement('cdhash H"00"'))


@unittest.skipUnless(shutil.which("node"), "需要 Node.js")
class Hook(unittest.TestCase):
    def test_hook_is_valid_js(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write("(function(a){\n" + asarpatch.hook_js("a") + "\n})")
        try:
            r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            Path(f.name).unlink()

    def test_patch_chunk_is_idempotent(self):
        src = 'x;a.session.defaultSession.webRequest.onBeforeRequest($a("webrequest:before-request",((e,t)=>{t({})}));'
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "chunk.js"
            p.write_text(src)
            self.assertEqual(asarpatch.patch_chunk(p), "new")
            self.assertEqual(asarpatch.patch_chunk(p), "replaced")
            text = p.read_text()
            self.assertEqual(text.count(asarpatch.BLOCK_BEGIN), 1)
            # 1.x 的反引号写法也认
            p.write_text(src.replace('"webrequest:before-request"', "`webrequest:before-request`"))
            self.assertEqual(asarpatch.patch_chunk(p), "new")

    def test_shim_behaviour(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "shim.test.mjs")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
