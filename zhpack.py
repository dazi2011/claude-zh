#!/usr/bin/env python3
"""Claude Desktop（macOS）简体中文补丁。

界面主体直接用 claude.ai 官方的简体中文（zh-Hans）目录；本工具只做三件事：

  1. 往 app.asar 主进程里插一个本地语言钩子（见 asarpatch.py），让桌面端主窗口
     使用 zh-Hans，而账号语言保持不变；
  2. 补上官方没有的桌面外壳中文：Contents/Resources/zh-Hans.json（原生菜单、对话框、
     托盘、桌面专属设置页）和 zh_CN.lproj/Localizable.strings；
  3. ad-hoc 重签名，并写入一条显式的 designated requirement，保住官方自动更新和系统权限。

用法见 README.md，或 `python3 zhpack.py -h`。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import asarpatch

# ---------------------------------------------------------------- 常量

LOCALE = asarpatch.LOCALE  # zh-Hans
FALLBACK = "en-US"

ROOT = Path(__file__).resolve().parent
TRANSLATIONS = ROOT / "translations"
EXTRACTED = ROOT / "extracted"  # 本机生成的中间产物，不进版本库

SHELL_ZH = TRANSLATIONS / f"desktop.{LOCALE}.json"
# 官方从 2.19675.0 起自带外壳 zh-Hans.json。装过补丁之后 App 里那份已被合并改写，
# 所以在 App 还是官方签名时把原版缓存下来，同版本重装时用它。
OFFICIAL_SHELL_CACHE = EXTRACTED / f"desktop.{LOCALE}.official.json"
MENU_ZH = TRANSLATIONS / f"menu.{LOCALE}.strings"
KEEP_EN = TRANSLATIONS / "keep-english.json"

APP_DEFAULT = Path("/Applications/Claude.app")
CONTENTS = Path("Contents")
RESOURCES = CONTENTS / "Resources"
SHELL_EN_REL = RESOURCES / f"{FALLBACK}.json"
SHELL_ZH_REL = RESOURCES / f"{LOCALE}.json"
MENU_EN_REL = RESOURCES / "en.lproj" / "Localizable.strings"
# macOS 的本地化目录名用下划线（与 app 自带的 pt_BR.lproj / es_419.lproj 同风格）
LPROJ = RESOURCES / "zh_CN.lproj"

CONFIG_REL = Path("Library/Application Support/Claude/config.json")
BUNDLE_ID = "com.anthropic.claudefordesktop"
# 官方构建的 Developer ID 团队（Anthropic）。只在读不到原版签名要求时当兜底。
ANTHROPIC_TEAM_ID = "Q6L2SF6YDW"

# 官方原版的备份（uninstall 从这里恢复）和上一版汉化包的快照（出问题时手动回滚用）
BACKUP_PREFIX = "Claude.backup-before-zh-CN-"
SNAPSHOT_PREFIX = "Claude.zh-prev-"

# ---------------------------------------------------------------- 输出

_TTY = sys.stdout.isatty()
MARK, OK, WARN, ERR, DIM, OFF = (
    ("\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")
    if _TTY
    else ("",) * 6
)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"{MARK}▸{OFF} {msg}")


def good(msg: str) -> None:
    say(f"  {OK}✓{OFF} {msg}")


def warn(msg: str) -> None:
    say(f"  {WARN}!{OFF} {msg}")


def bad(msg: str) -> None:
    say(f"  {ERR}✗{OFF} {msg}")


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    say(f"{ERR}✗ {msg}{OFF}")
    raise SystemExit(1)


def ask(question: str, default: bool = True) -> bool:
    """只在交互终端里问；非交互时返回 False，由调用方打印手动命令。"""
    if not sys.stdin.isatty():
        return False
    hint = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"  {question} {hint} ").strip().lower()
    except EOFError:
        return False
    return default if not ans else ans in ("y", "yes", "是")


# ---------------------------------------------------------------- 基础工具


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def app_version(app: Path) -> str:
    try:
        with (app / CONTENTS / "Info.plist").open("rb") as fh:
            return plistlib.load(fh).get("CFBundleShortVersionString", "?")
    except (OSError, plistlib.InvalidFileException):
        return "?"


def is_intact(app: Path) -> bool:
    """备份被别的程序清理过（只剩空目录、缺主程序）就不能拿来恢复。"""
    return (app / CONTENTS / "Info.plist").is_file() and (app / CONTENTS / "MacOS" / "Claude").is_file()


def require_app(app: Path) -> None:
    if not (app / RESOURCES / "app.asar").is_file():
        die(f"这不像一个 Claude.app：{app}")
    if not os.access(app.parent, os.W_OK):
        die(f"没有写权限：{app.parent}")


def trash(path: Path) -> Path:
    """移到废纸篓（可恢复），不做永久删除。"""
    bin_ = Path.home() / ".Trash"
    dest = bin_ / path.name
    n = 1
    while dest.exists():
        dest = bin_ / f"{path.stem} {n}{path.suffix}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest


# ---------------------------------------------------------------- ICU 占位符校验
#
# 译文最容易出的硬伤是把 {name} / {count, plural, ...} 弄坏，界面会报错或显示乱码。
# 这里做一个够用的 ICU MessageFormat 解析：只提取真正的参数引用，
# 不会把 plural 分支里的字面量误判成参数。


def icu_args(msg: str) -> set[str]:
    """提取 ICU 消息中引用的参数名。"""
    args: set[str] = set()
    n = len(msg)

    def skip_quote(i: int) -> int:
        # 撇号规则：'' 是字面量单引号；'{ '} '# 开启一段引用直到下一个 '；其余是普通撇号
        if i + 1 < n and msg[i + 1] == "'":
            return i + 2
        if i + 1 < n and msg[i + 1] in "{}#":
            j = msg.find("'", i + 1)
            return n if j == -1 else j + 1
        return i + 1

    def skip_balanced(i: int) -> int:
        depth = 1
        while i < n and depth:
            if msg[i] == "{":
                depth += 1
            elif msg[i] == "}":
                depth -= 1
            i += 1
        return i

    def parse_body(i: int) -> int:
        while i < n:
            c = msg[i]
            if c == "'":
                i = skip_quote(i)
            elif c == "{":
                i = parse_arg(i)
            elif c == "}":
                return i
            else:
                i += 1
        return i

    def parse_arg(i: int) -> int:
        i += 1
        while i < n and msg[i].isspace():
            i += 1
        start = i
        while i < n and (msg[i].isalnum() or msg[i] in "_$"):
            i += 1
        if msg[start:i]:
            args.add(msg[start:i])
        while i < n and msg[i].isspace():
            i += 1
        if i >= n:
            return i
        if msg[i] == "}":
            return i + 1
        if msg[i] != ",":
            return skip_balanced(i)
        i += 1
        while i < n and msg[i].isspace():
            i += 1
        tstart = i
        while i < n and msg[i].isalpha():
            i += 1
        if msg[tstart:i] in ("plural", "select", "selectordinal"):
            while i < n and msg[i] != "}":
                if msg[i] == "{":
                    i = parse_body(i + 1) + 1
                else:
                    i += 1
            return i + 1 if i < n else i
        return skip_balanced(i)

    parse_body(0)
    return args


TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\s*/?>")


def icu_tags(msg: str) -> list[str]:
    return sorted(TAG_RE.findall(msg))


def check_pair(en: str, zh: str) -> str | None:
    """返回问题描述，没问题返回 None。"""
    if icu_args(en) != icu_args(zh):
        return f"占位符不一致 EN={sorted(icu_args(en))} ZH={sorted(icu_args(zh))}"
    if icu_tags(en) != icu_tags(zh):
        return f"标签不一致 EN={icu_tags(en)} ZH={icu_tags(zh)}"
    return None


# ---------------------------------------------------------------- 外壳字典与原生菜单


def load_keep_en() -> set[str]:
    """有意保留英文的键（产品名、单位、缩写…），免得每次都被当成没翻。"""
    return set(load_json(KEEP_EN)) if KEEP_EN.is_file() else set()


def merge_catalog(en_map: dict, *sources: dict) -> tuple[dict, list[int], list[str]]:
    """以英文为骨架逐键合并，按顺序取第一个可用的来源。

    来源里与英文相同的值视为「有意保留英文」，同样算命中；占位符 / 标签损坏的跳过，
    都没有就保留英文，保证键集完备。返回（合并结果，各来源命中数，问题列表）。
    """
    merged: dict[str, Any] = {}
    counts = [0] * len(sources)
    problems: list[str] = []
    for key, en_val in en_map.items():
        merged[key] = en_val
        if not isinstance(en_val, str):
            continue
        for i, src in enumerate(sources):
            val = src.get(key)
            if not isinstance(val, str):
                continue
            issue = check_pair(en_val, val)
            if issue:
                problems.append(f"[{key}] {issue}")
                continue
            merged[key] = val
            counts[i] += 1
            break
    return merged, counts, problems


def official_shell(app: Path) -> dict:
    """官方自带的外壳中文。官方签名的 App 直接读并缓存；已汉化的 App 只认同版本的缓存。"""
    version = app_version(app)
    if is_official_signed(app):
        src = app / SHELL_ZH_REL
        data = load_json(src) if src.is_file() else {}
        if data:
            save_json(OFFICIAL_SHELL_CACHE, {"version": version, "messages": data})
        return data
    if OFFICIAL_SHELL_CACHE.is_file():
        cache = load_json(OFFICIAL_SHELL_CACHE)
        if cache.get("version") == version:
            return cache.get("messages", {})
    return {}


def install_shell_catalog(app: Path, official: dict) -> None:
    en_src = app / SHELL_EN_REL
    if not en_src.is_file():
        die(f"找不到外壳英文原文：{en_src}")
    ours = load_json(SHELL_ZH) if SHELL_ZH.is_file() else {}
    # 官方优先：和界面主体的官方译文用词、语气一致；官方还没翻的才用社区译文补
    merged, (n_off, n_ours), problems = merge_catalog(load_json(en_src), official, ours)
    for line in problems[:10]:
        warn(f"译文损坏，已跳过 {line}")
    with (app / SHELL_ZH_REL).open("w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, separators=(",", ":"))
    rest = len(merged) - n_off - n_ours
    good(
        f"外壳字典 {SHELL_ZH_REL.name}：官方 {n_off} + 社区补充 {n_ours}"
        + (f" + 英文 {rest}" if rest else "")
        + f" = {len(merged)}"
    )


STRINGS_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)";', re.M)


def read_strings(path: Path) -> str:
    raw = path.read_bytes()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    return raw.decode(enc)


def install_menu(app: Path) -> None:
    if not MENU_ZH.is_file():
        warn("没有原生菜单译文，跳过（菜单栏保持英文）")
        return
    dest = app / LPROJ
    dest.mkdir(parents=True, exist_ok=True)
    # 照抄 en.lproj 的编码最稳（传统上是 UTF-16）
    src = app / MENU_EN_REL
    enc = "utf-8" if src.is_file() and src.read_bytes()[:2] not in (b"\xff\xfe", b"\xfe\xff") else "utf-16"
    (dest / "Localizable.strings").write_text(MENU_ZH.read_text(encoding="utf-8"), encoding=enc)
    good(f"原生菜单 {LPROJ.name}/Localizable.strings")


# ---------------------------------------------------------------- 附带修复：iOS 模拟器沙盒
#
# 与汉化无关的上游问题，顺手修：反正要重签名，改一行 profile 不增加代价。

SIM_SB_REL = RESOURCES / "claude-ios-sim.sb"
SIM_SB_RULE = "(allow system-info)"
SIM_SB_PATCH = f""";; ── claude-zh:metal-system-info ──────────────────────────────
;; macOS 26 起，Metal 在 -[_MTLDevice recordBinaryArchiveUsage:] 里查询 vfs.disk-space。
;; 官方 profile 的 (deny default) 挡掉这个 system-info 查询后返回 nil，nil 被塞进
;; +[NSArray arrayWithObjects:count:] → NSInvalidArgumentException → sidecar abort(134)，
;; 表现为「Claude Code iOS 模拟器正在崩溃后重启」无限循环。官方原版同样会崩。
{SIM_SB_RULE}
"""


def patch_sim_sandbox(app: Path) -> None:
    sb = app / SIM_SB_REL
    if not sb.is_file():
        return
    text = sb.read_text(encoding="utf-8")
    if SIM_SB_RULE in text:  # 上游自己加了、或者已经补过，都不再叠
        return
    sb.write_text(text.rstrip("\n") + "\n\n" + SIM_SB_PATCH, encoding="utf-8")
    good("附带修复：iOS 模拟器沙盒 profile 补上 (allow system-info)")


# ---------------------------------------------------------------- 签名
#
# 改了 Contents 就破坏了签名封条，只能 ad-hoc 重签。重签有三个坑：
#
# 1. team 绑定的 entitlement。ad-hoc 没有 Team ID，保留它们会让 taskgated 在 exec 时
#    直接 SIGKILL（崩溃报告写 "Taskgated Invalid Signature"），必须剔除。
# 2. 自动更新。Squirrel.Mac 装更新前拿「当前运行的 app」的 designated requirement（DR）
#    校验下载下来的新版（SecCodeCopySelf → SecCodeCopyDesignatedRequirement →
#    SecStaticCodeCheckValidityWithErrors）。ad-hoc 默认合成的 DR 是
#    `cdhash H"…"`，官方新版永远对不上，每次都报
#    "Code signature … did not pass validation: code failed to satisfy specified code requirement(s)"。
# 3. 系统权限（TCC）和钥匙串。它们记住的也是 app 的 DR；cdhash 形式的 DR 每重签一次
#    就变一次，之前授予的权限随之失效。
#
# 所以重签时显式写入一条固定的 DR：
#     (官方 Developer ID 那条) or (identifier "com.anthropic.claudefordesktop" and ! anchor apple generic)
# 官方新版满足第一支 → 自动更新照常；汉化包自己满足第二支 → 权限与钥匙串在重签、
# 官方更新、再重装之间都保持有效（在这条 DR 下重新授权一次之后）。

TEAM_BOUND_ENTITLEMENTS = (
    "com.apple.application-identifier",
    "com.apple.developer.team-identifier",
    "keychain-access-groups",
)
ADHOC_BRANCH = "! anchor apple generic"
UPSTREAM_DR_CACHE = EXTRACTED / "upstream.designated-requirement.txt"


def codesign_info(path: Path) -> str:
    r = subprocess.run(["codesign", "-dv", str(path)], capture_output=True, text=True)
    return r.stderr or r.stdout


def signing_identifier(path: Path) -> str:
    for line in codesign_info(path).splitlines():
        if line.startswith("Identifier="):
            return line.split("=", 1)[1].strip()
    return ""


def is_official_signed(app: Path) -> bool:
    return f"TeamIdentifier={ANTHROPIC_TEAM_ID}" in codesign_info(app)


def designated_requirement(path: Path) -> str:
    """读出 DR 表达式（去掉 "designated => "；隐式合成的那条前面带 #，一并接受）。"""
    r = subprocess.run(["codesign", "-d", "-r-", str(path)], capture_output=True, text=True)
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        line = line.strip().lstrip("#").strip()
        if line.startswith("designated =>"):
            return line.split("=>", 1)[1].strip()
    return ""


def is_upstream_requirement(expr: str) -> bool:
    """像官方 Developer ID 签名的 DR（不是 cdhash 白名单，也不是我们写的组合式）。"""
    return (
        bool(expr)
        and "cdhash" not in expr
        and ADHOC_BRANCH not in expr
        and "anchor apple generic" in expr
        and "certificate leaf[subject.OU]" in expr
    )


def canonical_upstream_requirement(ident: str) -> str:
    """codesign 给 Developer ID 签名合成的标准 DR（与官方安装包读出来的逐字一致）。"""
    return (
        f'identifier "{ident}" and anchor apple generic '
        "and certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */ "
        "and certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ "
        f"and certificate leaf[subject.OU] = {ANTHROPIC_TEAM_ID}"
    )


def upstream_requirement(app: Path) -> str:
    """官方 DR：app 还是官方签名就直接读，否则用上次缓存的，再不行按标准形状拼。"""
    expr = designated_requirement(app)
    if is_upstream_requirement(expr):
        if not UPSTREAM_DR_CACHE.is_file() or UPSTREAM_DR_CACHE.read_text().strip() != expr:
            UPSTREAM_DR_CACHE.parent.mkdir(parents=True, exist_ok=True)
            UPSTREAM_DR_CACHE.write_text(expr + "\n", encoding="utf-8")
        return expr
    if UPSTREAM_DR_CACHE.is_file():
        cached = UPSTREAM_DR_CACHE.read_text(encoding="utf-8").strip()
        if is_upstream_requirement(cached):
            return cached
    return canonical_upstream_requirement(signing_identifier(app) or BUNDLE_ID)


def update_safe_requirement(app: Path) -> str:
    ident = signing_identifier(app) or BUNDLE_ID
    return f'designated => ({upstream_requirement(app)}) or (identifier "{ident}" and {ADHOC_BRANCH})'


def requirement_syntax_error(text: str) -> str | None:
    """先用 csreq 编译一遍，语法错就别留到 codesign 才炸。"""
    with tempfile.TemporaryDirectory(prefix="claude-zh-req.") as d:
        src = Path(d) / "req.txt"
        src.write_text(text + "\n", encoding="utf-8")
        r = subprocess.run(
            ["csreq", "-r", str(src), "-b", str(Path(d) / "req.bin")], capture_output=True, text=True
        )
        return None if r.returncode == 0 else (r.stdout + r.stderr).strip()[:300]


def satisfies(app: Path, requirement: str) -> bool:
    r = subprocess.run(
        ["codesign", "--verify", f"-R={requirement}", str(app)], capture_output=True, text=True
    )
    return r.returncode == 0


def load_entitlements(path: Path) -> dict[str, Any]:
    r = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(path)], capture_output=True, check=False
    )
    out = r.stdout.rstrip(b"\x00")
    if r.returncode != 0 or not out.strip():
        return {}
    try:
        data = plistlib.loads(out)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def is_signable_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if path.suffix in {".dylib", ".node", ".so"} or os.access(path, os.X_OK):
        return True
    # 光看 X_OK 不够：asar 重打包会把 unpacked 里可执行文件的权限位抹掉
    return asarpatch.is_macho(path)


def collect_sign_targets(app: Path) -> tuple[list[Path], list[Path]]:
    """(可执行文件, 嵌套 bundle)。必须覆盖整个 Contents，漏签会导致混合签名被拒。"""
    files: list[Path] = []
    bundles: list[Path] = []
    for root, dirs, names in os.walk(app / CONTENTS):
        rp = Path(root)
        bundles += [rp / d for d in dirs if Path(d).suffix in {".app", ".framework"}]
        files += [rp / n for n in names if is_signable_file(rp / n)]
    return files, bundles


def sign_one(path: Path, tmp: Path, requirement: str | None = None) -> None:
    ent = load_entitlements(path)
    if ent:
        for key in TEAM_BOUND_ENTITLEMENTS:
            ent.pop(key, None)
        # 没有 Team ID，hardened runtime 下加载自带 framework 会栽在 library validation 上
        ent["com.apple.security.cs.disable-library-validation"] = True
    cmd = [
        "codesign", "--force", "--sign", "-", "--timestamp=none", "--options", "runtime",
        # 不保留 identifier 的话，ad-hoc 签名的标识符会变空
        "--preserve-metadata=identifier,flags",
    ]
    tag = abs(hash(path.as_posix()))
    if ent:
        f = tmp / f"ent-{tag}.plist"
        f.write_bytes(plistlib.dumps(ent, fmt=plistlib.FMT_XML))
        cmd += ["--entitlements", str(f)]
    if requirement:
        f = tmp / f"req-{tag}.txt"
        f.write_text(requirement + "\n", encoding="utf-8")
        cmd += ["-r", str(f)]
    r = subprocess.run(cmd + [str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"重签名失败 {path}: {r.stderr.strip()}")


def resign(app: Path) -> None:
    """由内向外完整重签。DR 必须在动手之前读：签主可执行文件会把 bundle 层的签名冲掉。"""
    requirement = update_safe_requirement(app)
    err = requirement_syntax_error(requirement)
    if err:
        die(f"DR 表达式编译不过：{err}")
    files, bundles = collect_sign_targets(app)
    with tempfile.TemporaryDirectory(prefix="claude-zh-sign.") as d:
        tmp = Path(d)
        for f in sorted(files, key=lambda p: len(p.parts), reverse=True):
            sign_one(f, tmp)
        for b in sorted(bundles, key=lambda p: len(p.parts), reverse=True):
            sign_one(b, tmp)
        # 只有最外层 bundle 的 DR 会被 Squirrel / TCC 读到
        sign_one(app, tmp, requirement)
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app)], capture_output=True)
    good(f"ad-hoc 重签名完成（{len(files)} 个可执行文件 + {len(bundles) + 1} 个 bundle，DR 保留官方分支）")


# ---------------------------------------------------------------- 系统权限（TCC）
#
# 签名一变，macOS 记着的「辅助功能 / 屏幕录制」等授权就对不上了：设置里开关还亮着，
# 实际不生效。这里只读系统 TCC 数据库，逐条检查记录的签名要求，当前 app 不满足的
# 才用 tccutil 重置（需要时弹系统授权框），再打开对应的设置页让用户重新勾选。
# 不直接改 TCC.db：系统库受 SIP 保护，绕过它等于绕过 macOS 的安全机制。

TCC_DB = Path("/Library/Application Support/com.apple.TCC/TCC.db")
TCC_NAMES = {
    "Accessibility": ("辅助功能", "Privacy_Accessibility"),
    "ScreenCapture": ("屏幕录制", "Privacy_ScreenCapture"),
    "ListenEvent": ("输入监控", "Privacy_ListenEvent"),
    "PostEvent": ("辅助功能（事件）", "Privacy_Accessibility"),
    "SystemPolicyAllFiles": ("完全磁盘访问", "Privacy_AllFiles"),
    "Microphone": ("麦克风", "Privacy_Microphone"),
    "Camera": ("摄像头", "Privacy_Camera"),
    "AppleEvents": ("自动化", "Privacy_Automation"),
}


def tcc_entries() -> list[tuple[str, int, bytes | None]] | None:
    """[(服务名, auth_value, csreq)]；读不了（没有完全磁盘访问权限）返回 None。"""
    try:
        con = sqlite3.connect(f"file:{TCC_DB}?mode=ro", uri=True)
        rows = con.execute(
            "select service, auth_value, csreq from access where client=?", (BUNDLE_ID,)
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    return [(s.removeprefix("kTCCService"), int(v), c) for s, v, c in rows]


def csreq_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".csreq") as f:
        f.write(blob)
        f.flush()
        r = subprocess.run(["csreq", "-r", f.name, "-t"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def stale_permissions(app: Path) -> tuple[list[str], list[str]] | None:
    """(已失效的服务, 仍有效的服务)；只看状态为「允许」的条目。读不了库返回 None。"""
    entries = tcc_entries()
    if entries is None:
        return None
    stale, fine = [], []
    for service, value, blob in entries:
        if value != 2:  # 2 = 允许
            continue
        req = csreq_text(blob)
        (fine if req and satisfies(app, req) else stale).append(service)
    return stale, fine


def label(service: str) -> str:
    if service == "All":
        return "全部授权"
    return TCC_NAMES.get(service, (service, ""))[0]


def reset_permissions(services: list[str]) -> bool:
    """逐项 tccutil reset；普通身份失败时改用管理员身份（系统弹密码 / Touch ID 框）。"""
    cmds = [["tccutil", "reset", s, BUNDLE_ID] for s in services]
    failed = [c for c in cmds if subprocess.run(c, capture_output=True).returncode != 0]
    if not failed:
        return True
    script = " && ".join(" ".join(c) for c in failed)
    r = subprocess.run(
        ["osascript", "-e", f'do shell script "{script}" with administrator privileges'],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def open_privacy_panes(services: list[str]) -> None:
    if "All" in services:
        services = ["Accessibility", "ScreenCapture"]
    panes = []
    for s in services:
        pane = TCC_NAMES.get(s, ("", ""))[1]
        if pane and pane not in panes:
            panes.append(pane)
    for pane in panes:
        subprocess.run(
            ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
            capture_output=True,
        )


def fix_permissions(app: Path, services: list[str], confirm: bool = True) -> None:
    names = "、".join(label(s) for s in services)
    if confirm and not ask(f"重置失效的授权（{names}）并打开设置页重新授权？"):
        say(f"  稍后可以运行：python3 {Path(__file__).name} permissions --reset")
        return
    if not reset_permissions(services):
        bad("重置失败（取消了授权框？）")
        return
    good(f"已重置：{names}")
    open_privacy_panes(services)
    say("  接下来：⌘Q 退出 Claude 再打开；在刚打开的设置页里把 Claude 重新打开")
    say("  （列表里没有 Claude 就点「+」把 /Applications/Claude.app 加进去）。")
    say("  这次授权记住的是固定的签名规则，以后重装汉化、官方自动更新都不会再失效。")


def cmd_permissions(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    result = stale_permissions(app)
    if result is None:
        warn("读不了系统 TCC 数据库（运行它的终端需要「完全磁盘访问」权限），无法逐项诊断。")
        if args.reset:
            fix_permissions(app, ["Accessibility", "ScreenCapture"], confirm=not args.yes)
        return 0
    stale, fine = result
    for s in fine:
        good(f"{label(s)}：有效")
    for s in stale:
        bad(f"{label(s)}：已失效（记住的是旧签名）")
    if not stale and not fine:
        say("  Claude 目前没有辅助功能、屏幕录制等系统授权。")
    if args.all:
        # 麦克风、摄像头这类记在用户级 TCC 库里，读不到，只能整体重置
        fix_permissions(app, ["All"], confirm=not args.yes)
    elif stale and (args.reset or ask("现在修复？")):
        fix_permissions(app, stale, confirm=False)
    elif stale:
        say(f"  修复：python3 {Path(__file__).name} permissions --reset")
    return 0


# ---------------------------------------------------------------- 校验与冒烟测试


def asar_hooked(app: Path) -> bool:
    asar = app / RESOURCES / "app.asar"
    return asar.is_file() and asarpatch.MARKER.encode() in asar.read_bytes()


def asar_has_legacy(app: Path) -> bool:
    asar = app / RESOURCES / "app.asar"
    return asar.is_file() and asarpatch.LEGACY_MARKER.encode() in asar.read_bytes()


def is_localized(app: Path) -> bool:
    return asar_hooked(app) or asar_has_legacy(app) or not is_official_signed(app)


def zh_ratio(path: Path) -> tuple[int, int]:
    data = load_json(path)
    han = re.compile(r"[一-鿿]")
    return sum(1 for v in data.values() if isinstance(v, str) and han.search(v)), len(data)


def verify_app(app: Path) -> list[str]:
    problems: list[str] = []
    if not asar_hooked(app):
        problems.append("app.asar 里没有本地语言钩子")
    if asar_has_legacy(app):
        problems.append("app.asar 里还有旧版「法语载体」补丁")
    shell = app / SHELL_ZH_REL
    if shell.is_file():
        zh, total = zh_ratio(shell)
        say(f"    外壳字典 {zh}/{total} 条是中文（{zh / total * 100:.1f}%）")
    else:
        problems.append(f"缺外壳字典 {SHELL_ZH_REL}")
    bad_integrity = asarpatch.integrity_mismatches(app)
    if bad_integrity:
        problems.append(f"asar 完整性哈希与实际不符：{bad_integrity[:3]}")
    unpacked = app / RESOURCES / "app.asar.unpacked"
    if unpacked.is_dir():
        noexec = [
            str(f.relative_to(unpacked))
            for f in unpacked.rglob("*")
            if asarpatch.needs_exec_bit(f) and not f.stat().st_mode & 0o111
        ]
        if noexec:
            problems.append(f"app.asar.unpacked 里 {len(noexec)} 个可执行文件丢了执行位：{noexec[:3]}")

    r = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True
    )
    if r.returncode != 0:
        problems.append(f"签名校验失败：{r.stderr.strip().splitlines()[:1]}")
    if not signing_identifier(app):
        problems.append("签名标识符为空")
    if "adhoc" in codesign_info(app):
        leftover = [k for k in TEAM_BOUND_ENTITLEMENTS if k in load_entitlements(app)]
        if leftover:
            problems.append(f"ad-hoc 签名仍带 team 绑定 entitlement，启动即被 SIGKILL：{leftover}")
    dr = designated_requirement(app)
    if "cdhash" in dr or "certificate leaf[subject.OU]" not in dr:
        problems.append("DR 没有官方证书分支，Squirrel 会拒掉官方新版（自动更新失效）")
    elif r.returncode == 0 and not satisfies(app, dr):
        problems.append("app 不满足自己的 DR，系统权限和钥匙串授权会反复失效")
    return problems


def smoke_launch(app: Path, seconds: int = 10) -> str | None:
    """真的把 app 跑起来，确认不会被内核以签名理由杀掉（静态校验抓不到这一类）。

    --use-mock-keychain：ad-hoc 签名和钥匙串条目的访问控制对不上，不加的话每次冒烟都会弹
    「Claude Safe Storage」要登录密码，没人输就一直卡着。
    """
    exe = app / CONTENTS / "MacOS" / "Claude"
    profile = Path(tempfile.mkdtemp(prefix="claude-zh-smoke."))
    proc = subprocess.Popen(
        [str(exe), f"--user-data-dir={profile}", "--use-mock-keychain"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        rc = proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if rc == -9:
        return "启动即被 SIGKILL —— 内核拒绝了这个签名"
    err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    return f"启动后立即退出，返回码 {rc}" + (f"：{err[:200]}" if err else "")


# ---------------------------------------------------------------- 安装 / 卸载


def set_config_locale(home: Path, locale: str) -> None:
    """外壳启动时的初始语言。之后 SPA 会通过 requestLocaleChange 让外壳跟着它走。"""
    cfg = home / CONFIG_REL
    if not cfg.is_file():
        return
    data = load_json(cfg)
    if data.get("locale") != locale:
        data["locale"] = locale
        save_json(cfg, data)


def backups(app: Path, prefix: str) -> list[Path]:
    return sorted(app.parent.glob(f"{prefix}*.app"))


def prune_backups(app: Path) -> None:
    """官方原版只留最新一份，汉化快照只留最新一份，其余（含已损坏的）移到废纸篓。"""
    official = [b for b in backups(app, BACKUP_PREFIX) if is_intact(b) and is_official_signed(b)]
    snapshots = [b for b in backups(app, SNAPSHOT_PREFIX) if is_intact(b)]
    keep = set(official[-1:]) | set(snapshots[-1:])
    for b in backups(app, BACKUP_PREFIX) + backups(app, SNAPSHOT_PREFIX):
        if b not in keep:
            what = "旧" if is_intact(b) else "已损坏的"  # 移走之前判断，移走之后路径就不存在了
            trash(b)
            good(f"{what}备份移到废纸篓：{b.name}")


def cmd_install(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    say(f"{MARK}=== 安装 Claude 简体中文（官方 {LOCALE} + 外壳汉化）==={OFF}")
    say(f"目标 : {app}  (版本 {app_version(app)})")
    if args.dry_run:
        say(f"{WARN}模式 : dry-run（不替换正式 app）{OFF}")
    say()
    if asar_has_legacy(app):
        die("这份 app 还带着旧版「法语载体」补丁。先装回官方版（让它自动更新一次，"
            "或从 https://claude.ai/download 覆盖安装）再来。")

    # 必须在复制、改写之前从原 App 读：装过补丁的 App 里那份已被合并改写
    official = official_shell(app)
    if not official and not is_official_signed(app):
        warn("App 已汉化过且没有同版本的官方外壳缓存，外壳只用社区译文")

    work_root = Path(tempfile.mkdtemp(prefix="claude-zh-"))
    work = work_root / "Claude.app"
    try:
        step("[1/7] 复制 app 到临时目录")
        run(["ditto", str(app), str(work)])

        step("[2/7] 写入外壳字典与原生菜单")
        install_shell_catalog(work, official)
        install_menu(work)

        step(f"[3/7] 注入本地语言钩子（app.asar，强制 {LOCALE}，账号不动）")
        try:
            good(asarpatch.patch_app(work, LOCALE))
        except subprocess.CalledProcessError as exc:
            die(f"asar 打包失败（需要 Node.js / npx）：{(exc.stderr or '').strip()[:200]}")
        fixed = asarpatch.ensure_unpacked_exec_bits(work)
        if fixed:
            good(f"补回 {len(fixed)} 个 unpacked 文件的执行位")
        patch_sim_sandbox(work)

        step("[4/7] 重新签名")
        resign(work)

        step("[5/7] 校验")
        problems = verify_app(work)
        if problems:
            for p in problems:
                bad(p)
            die("校验未通过，正式 app 未被改动。")
        good("通过")

        step("[6/7] 启动冒烟测试")
        launch_problem = smoke_launch(work)
        if launch_problem:
            die(f"{launch_problem}。正式 app 未被改动。")
        good("能正常启动")

        step("[7/7] 就位")
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = BACKUP_PREFIX if is_official_signed(app) else SNAPSHOT_PREFIX
        backup = app.parent / f"{prefix}{stamp}.app"
        if args.dry_run:
            say(f"    [dry-run] 会把原 app 移到 {backup.name}，产物在 {work}")
            work_root = None  # 留着给人看
            say(f"{OK}dry-run 完成，正式 app 未被改动。{OFF}")
            return 0
        # 换包用 mv：正在运行的旧实例持有旧 inode，继续跑不受影响（本工具可能就跑在
        # Claude 自己的终端里，所以绝不去杀进程），什么时候重启由用户决定。
        shutil.move(str(app), str(backup))
        shutil.move(str(work), str(app))
        good(f"原 app 移到 {backup.name}")
        set_config_locale(args.user_home, LOCALE)
        prune_backups(app)
    finally:
        if work_root is not None:
            shutil.rmtree(work_root, ignore_errors=True)

    say()
    step("系统权限")
    result = stale_permissions(app)
    if result is None:
        say("    读不了系统 TCC 数据库，跳过诊断。辅助功能 / 屏幕录制不好用时运行：")
        say(f"    python3 {Path(__file__).name} permissions --reset")
    elif result[0]:
        for s in result[0]:
            bad(f"{label(s)}：已失效")
        if args.no_permissions:
            say(f"    修复：python3 {Path(__file__).name} permissions --reset")
        else:
            fix_permissions(app, result[0])
    else:
        good("没有失效的授权")

    say()
    say(f"{OK}安装完成。{OFF}⌘Q 退出 Claude 再打开：")
    say("  · 启动时如果弹「Claude Safe Storage」要登录密码，输完选「始终允许」；")
    say("  · 窗口会自动刷新一次，之后就是官方简体中文；")
    say("  · 语言可以在「设置 → 常规 → 语言」里随时切换，选中文只在本机生效，不改账号。")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    app: Path = args.app
    official = [b for b in backups(app, BACKUP_PREFIX) if is_intact(b) and is_official_signed(b)]
    if not official:
        die("找不到完好的官方原版备份。直接从 https://claude.ai/download 下载覆盖安装即可。")
    src = official[-1]
    say(f"用官方备份恢复：{src.name}（版本 {app_version(src)}）")
    if args.dry_run:
        say("[dry-run] 未执行")
        return 0
    if app.exists():
        good(f"汉化版移到废纸篓：{trash(app).name}")
    shutil.move(str(src), str(app))
    good("官方版已恢复")
    set_config_locale(args.user_home, FALLBACK)
    say("⌘Q 退出 Claude 再打开即可。")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    say(f"校验 {app}  (版本 {app_version(app)})")
    problems = verify_app(app)
    for p in problems:
        bad(p)
    if not problems:
        good("完整")
    return 1 if problems else 0


def cmd_status(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    official = is_official_signed(app)
    say(f"Claude       : {app_version(app)}  ({app})")
    say(f"签名         : {'Anthropic 官方' if official else 'ad-hoc（已汉化）'}")
    say(f"本地语言钩子 : {'已安装' if asar_hooked(app) else '未安装'}")
    shell = app / SHELL_ZH_REL
    if shell.is_file():
        zh, total = zh_ratio(shell)
        say(f"外壳字典     : 已安装（{zh}/{total} 条中文）")
    else:
        say("外壳字典     : 未安装")
    say(f"原生菜单     : {'已安装' if (app / LPROJ / 'Localizable.strings').is_file() else '未安装'}")
    dr = designated_requirement(app)
    auto = "可用" if official or ("certificate leaf[subject.OU]" in dr and "cdhash" not in dr) else "失效"
    say(f"官方自动更新 : {auto}")
    result = stale_permissions(app)
    if result is None:
        say("系统权限     : 无法读取（需要完全磁盘访问）")
    else:
        stale, fine = result
        parts = [f"{label(s)} ✓" for s in fine] + [f"{label(s)} ✗ 已失效" for s in stale]
        say(f"系统权限     : {'，'.join(parts) or '无'}")
    for prefix, what in ((BACKUP_PREFIX, "官方备份"), (SNAPSHOT_PREFIX, "汉化快照")):
        for b in backups(app, prefix):
            if not is_intact(b):
                tag = "（已损坏，下次 install 会移到废纸篓）"
            elif prefix == BACKUP_PREFIX and not is_official_signed(b):
                tag = "（不是官方签名）"
            else:
                tag = ""
            say(f"{what}     : {b.name}  {app_version(b)}{tag}")
    en = app / SHELL_EN_REL
    if en.is_file():
        en_map = load_json(en)
        official = official_shell(app)
        todo = pending(en_map, official, load_json(SHELL_ZH) if SHELL_ZH.is_file() else {}, load_keep_en())
        done = len(en_map) - len(todo)
        src = f"官方 {len(set(official) & set(en_map))} 条，其余社区译文" if official else "社区译文"
        say(f"外壳译文覆盖 : {done}/{len(en_map)}（{src}，待翻 {len(todo)}）")
    return 0


# ---------------------------------------------------------------- 翻译工作流（仅外壳）


def pending(en_map: dict, official: dict, ours: dict, keep: set[str]) -> dict[str, str]:
    """官方和社区译文都没覆盖的外壳文案。"""
    return {
        k: v
        for k, v in en_map.items()
        if isinstance(v, str) and k not in keep and k not in official and (k not in ours or ours[k] == v)
    }


def cmd_extract(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    step(f"从 Claude {app_version(app)} 抽取外壳英文原文")
    en_map = load_json(app / SHELL_EN_REL)
    save_json(EXTRACTED / f"desktop.{FALLBACK}.json", en_map)
    zh_map = load_json(SHELL_ZH) if SHELL_ZH.is_file() else {}
    official = official_shell(app)
    todo = pending(en_map, official, zh_map, load_keep_en())
    save_json(EXTRACTED / "todo.desktop.json", todo)
    stale = len(set(zh_map) - set(en_map))
    if official:
        good(f"官方自带外壳中文 {len(set(official) & set(en_map))} 条")
    good(f"外壳 {len(en_map)} 条，待翻 {len(todo)} 条 → extracted/todo.desktop.json")
    if stale:
        say(f"    译文里有 {stale} 条键官方已删除，安装时自动忽略")

    menu_src = app / MENU_EN_REL
    if menu_src.is_file():
        text = read_strings(menu_src)
        (EXTRACTED / f"menu.{FALLBACK}.strings").write_text(text, encoding="utf-8")
        have = dict(STRINGS_RE.findall(MENU_ZH.read_text(encoding="utf-8"))) if MENU_ZH.is_file() else {}
        missing = [k for k in dict(STRINGS_RE.findall(text)) if k not in have]
        good(f"原生菜单待翻 {len(missing)} 条" + (f"：{missing[:5]}" if missing else ""))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    en_map = load_json(app / SHELL_EN_REL)
    zh_map = load_json(SHELL_ZH) if SHELL_ZH.is_file() else {}
    todo = sorted(
        pending(en_map, official_shell(app), zh_map, load_keep_en()).items(),
        key=lambda kv: (len(kv[1]), kv[0]),
    )
    chunk = dict(todo[args.start : args.start + args.size])  # 短的先翻：按钮/菜单性价比最高
    out = EXTRACTED / f"batch.desktop.{args.start}-{args.start + len(chunk)}.json"
    save_json(out, chunk)
    good(f"切出 {len(chunk)} 条 → {out.relative_to(ROOT)}（剩余 {len(todo) - args.start - len(chunk)}）")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    en_map = load_json(app / SHELL_EN_REL)
    zh_map = load_json(SHELL_ZH) if SHELL_ZH.is_file() else {}
    keep = load_keep_en()
    added = unknown = rejected = 0
    for path in args.files:
        for key, val in load_json(path).items():
            en_val = en_map.get(key)
            if en_val is None or not isinstance(val, str):
                unknown += 1
            elif val == en_val:
                keep.add(key)  # 与英文相同 = 有意保留英文，记账，不再重复派发
            elif issue := check_pair(en_val, val):
                warn(f"拒绝 [{key}] {issue}")
                rejected += 1
            else:
                zh_map[key] = val
                added += 1
    save_json(SHELL_ZH, zh_map)
    save_json(KEEP_EN, sorted(keep))
    good(f"并入 {added} 条，无效键 {unknown} 条，拒绝 {rejected} 条 → {SHELL_ZH.relative_to(ROOT)}")
    return 1 if rejected else 0


def cmd_check(args: argparse.Namespace) -> int:
    app: Path = args.app
    require_app(app)
    en_map = load_json(app / SHELL_EN_REL)
    zh_map = load_json(SHELL_ZH) if SHELL_ZH.is_file() else {}
    broken = [
        (k, issue)
        for k, v in zh_map.items()
        if isinstance(en_map.get(k), str) and v != en_map[k] and (issue := check_pair(en_map[k], v))
    ]
    values = [v for v in zh_map.values() if isinstance(v, str)]
    variants = [v for v in values if re.search(r"帐号|帐单|帐户|软体|程式|档案|资讯|伺服器|网路|登入", v)]
    informal = [v for v in values if "你" in v]
    say(f"外壳译文 {len(zh_map)} 条，官方已删除的键 {len(set(zh_map) - set(en_map))} 条")
    (bad if broken else good)(f"占位符 / 标签损坏：{len(broken)}")
    for k, issue in broken[:15]:
        say(f"{DIM}    [{k}] {issue}{OFF}")
    if variants:
        warn(f"港台 / 异体用词 {len(variants)} 条：{variants[:3]}")
    if informal:
        warn(f"用了「你」{len(informal)} 条（与官方一致，约定用「您」）")
    return 1 if broken else 0


# ---------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="zhpack", description="Claude Desktop（macOS）简体中文补丁（非官方）"
    )
    ap.add_argument("--app", type=Path, default=APP_DEFAULT, help="Claude.app 路径")
    ap.add_argument("--user-home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="安装 / 重装（App 更新后重跑一次）")
    p.add_argument("--dry-run", action="store_true", help="演练，不替换正式 app")
    p.add_argument("--no-permissions", action="store_true", help="装完不检查系统权限")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("status", help="安装状态、自动更新、系统权限、备份")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify", help="校验已安装的补丁")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("permissions", help="检查 / 修复因重签名失效的系统权限")
    p.add_argument("--reset", action="store_true", help="重置失效的授权并打开设置页")
    p.add_argument("--all", action="store_true", help="重置 Claude 的全部授权（含麦克风、摄像头等）")
    p.add_argument("-y", "--yes", action="store_true", help="不再二次确认")
    p.set_defaults(func=cmd_permissions)

    p = sub.add_parser("uninstall", help="从官方原版备份恢复")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("extract", help="抽取外壳英文原文，生成待翻清单")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("batch", help="切出一批待翻的外壳文案")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--size", type=int, default=200)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("merge", help="把翻完的批次并回外壳译文（逐条校验占位符）")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("check", help="体检外壳译文（占位符 / 标签 / 用词）")
    p.set_defaults(func=cmd_check)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
