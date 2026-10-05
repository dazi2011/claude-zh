#!/usr/bin/env python3
"""app.asar 补丁：让桌面端主窗口使用 claude.ai 官方的简体中文，而不改账号语言。

背景
----
claude.ai 的页面 meta（<meta name="i18n-catalogs">）已经列出官方 zh-Hans 目录，
SPA 只要当前 locale 是 zh-Hans，就会自己去拉 /i18n/zh-Hans.json。但这门语言在灰度里：

  * 语言选择器列出哪些语言，由 GrowthBook 特性 witty_scone_main 的 released 列表决定；
  * 账号语言设不成它：PUT /api/account_profile {"locale":"zh-Hans"} 会返回 400
    "locale: Input is not one of the permitted values."

SPA 登录后按启动数据（/edge-api/bootstrap…）顶层的 locale 决定界面语言，并写进
localStorage["spa:locale"]。所以只需在主窗口里、任何页面脚本之前，包一层 window.fetch：

  1. 启动数据回来时，把顶层 locale 改成 zh-Hans（用户选了别的语言时不改）；
  2. 往 witty_scone_main 的 released 里补上 zh-Hans，让它常驻语言选择器；
  3. 用户在选择器里选 zh-Hans 时，账号接口会拒绝，这一次提交在本地直接应答成功，
     并把选择记在 localStorage["claude-zh:locale"]；选其它语言照常提交给服务端。

译文全部来自官方目录，本补丁不改写任何界面文字、不拦 i18n 请求。

注入点
------
主进程里官方自己的 `session.defaultSession.webRequest.onBeforeRequest(…"webrequest:before-request"…)`
回调的最前面。不另外注册监听器：Electron 同一 session 只保留最后一个 onBeforeRequest，
另起一个会把官方逻辑顶掉。锚点用结构正则匹配，要求全局唯一命中，不写死混淆变量名。
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

LOCALE = "zh-Hans"
# 语言选择器的放量名单（GrowthBook 特性名；启动数据里按 djb2 哈希作键）
GATE_FEATURE = "witty_scone_main"
# 用户在桌面端语言选择器里的选择，只存在桌面端的 claude.ai 本地存储里
PREF_KEY = "claude-zh:locale"

MARKER = "__claude_zh_locale_hook__"
# 注入块首尾各打一个注释标记，重装时整块摘掉再插新的，不必从干净 asar 重来
BLOCK_BEGIN = "/*claude-zh-locale:begin*/"
BLOCK_END = "/*claude-zh-locale:end*/"
# 早期「借法语当载体」方案留下的标志位；碰到就拒绝叠加
LEGACY_MARKER = "__claude_zh_i18n_hook__"

LOG_FILE = "/tmp/claude-zh-locale.log"

# 默认 session 的 onBeforeRequest。标签字面量的引号跟着压缩器走：
# 1.x 是反引号，2.x 起是双引号，两种都认。
RE_BEFORE_REQUEST = re.compile(
    r"(?P<ns>[A-Za-z_$][\w$]*)\.session\.defaultSession\.webRequest\.onBeforeRequest\("
    r"(?P<wrap>[A-Za-z_$][\w$]*)\((?P<q>[`\"])webrequest:before-request(?P=q),\(\("
    r"(?P<det>[A-Za-z_$][\w$]*),(?P<cb>[A-Za-z_$][\w$]*)\)=>\{"
)


# ---------------------------------------------------------------- 页面脚本


def shim_js(locale: str = LOCALE) -> str:
    """页面侧脚本，由 Page.addScriptToEvaluateOnNewDocument 在任何页面脚本之前运行。

    启动数据有两条来路，都走 window.fetch：HTML 里内联的预加载脚本
    （window.__BOOTSTRAP_PRELOAD__）和 SPA 自己的请求。包在页面这一层，
    HTTP 缓存命中也照样经过这里。解析失败一律放行原响应。

    window.__claude_zh_locale_at__ 记下脚本运行时文档的 readyState：
    "loading" 说明赶在了页面脚本之前，主进程据此判断要不要补一次重载。
    """
    cfg = json.dumps({"L": locale, "PK": PREF_KEY, "GATE": GATE_FEATURE})
    return (
        "(function(){try{"
        "if(!/(^|\\.)claude\\.(ai|com)$/.test(location.hostname))return 'skip:host';"
        "if(window.__claude_zh_locale__)return 'skip:dup';"
        "window.__claude_zh_locale__=1;"
        "window.__claude_zh_locale_at__=document.readyState;"
        f"var C={cfg},L=C.L;"
        "var ls=function(k,v){try{if(v===void 0)return localStorage.getItem(k);"
        "localStorage.setItem(k,v)}catch(e){return null}};"
        # 没选过、或选的就是中文 → 强制中文；选过别的语言就尊重用户
        "var on=function(){var p=ls(C.PK);return !p||p===L};"
        "if(on())ls('spa:locale',L);"
        "var of=window.fetch;if(typeof of!=='function')return 'skip:nofetch';"
        "var pathOf=function(i){try{var u=typeof i==='string'?i:(i&&i.url)||String(i);"
        "return new URL(u,location.href).pathname}catch(e){return ''}};"
        "var djb2=function(s){var h=0;for(var i=0;i<s.length;i++){h=(h<<5)-h+s.charCodeAt(i);h=h&h}"
        "return String(h>>>0)};"
        "var addRel=function(o){var n=0;(function w(x){"
        "if(!x||typeof x!=='object')return;"
        "if(Array.isArray(x.released)&&x.released.indexOf(L)<0){x.released.push(L);n++}"
        "for(var k in x)if(Object.prototype.hasOwnProperty.call(x,k)&&x[k]&&typeof x[k]==='object')w(x[k])"
        "})(o);return n};"
        "var json=function(b,r){"
        "var h=new Headers();if(r)r.headers.forEach(function(v,k){"
        "if(!/^(content-length|content-encoding)$/i.test(k))h.append(k,v)});"
        "else h.set('content-type','application/json');"
        "var n=new Response(JSON.stringify(b),{status:r?r.status:200,statusText:r?r.statusText:'OK',headers:h});"
        "if(r){try{Object.defineProperty(n,'url',{value:r.url})}catch(e){}"
        "try{Object.defineProperty(n,'redirected',{value:r.redirected})}catch(e){}}"
        "return n};"
        "var fix=function(r){"
        "if(!r||!r.ok)return r;"
        "var ct=(r.headers&&r.headers.get('content-type'))||'';"
        "if(ct.indexOf('json')<0)return r;"
        "return r.clone().text().then(function(t){"
        "var j=JSON.parse(t),hit=0;"
        "if(!j||typeof j!=='object'||Array.isArray(j))return r;"
        "if(on()&&typeof j.locale==='string'&&j.locale!==L){j.locale=L;hit++}"
        "var gb=j.growthbook,f=gb&&gb.features;"
        "if(f&&typeof f==='object'){"
        "var k=gb.hashing_algorithm==='djb2'?djb2(C.GATE):C.GATE;"
        "if(f[k])hit+=addRel(f[k]);else{f[k]={defaultValue:{released:[L]}};hit++}}"
        "if(!hit)return r;"
        "window.__claude_zh_locale_fixed__=(window.__claude_zh_locale_fixed__||0)+1;"
        "return json(j,r);"
        "}).catch(function(){return r})};"
        "var wrapped=function(input,init){"
        "var p=pathOf(input);"
        "try{"
        "var m=String((init&&init.method)||(input&&input.method)||'GET').toUpperCase();"
        "if(p==='/api/account_profile'&&m==='PUT'&&init&&typeof init.body==='string'){"
        "var b=JSON.parse(init.body);"
        "if(b&&typeof b.locale==='string'){"
        "ls(C.PK,b.locale);"
        "if(b.locale===L){"
        "ls('spa:locale',L);delete b.locale;"
        "if(!Object.keys(b).length)return Promise.resolve(json({locale:L}));"
        "return of.call(window,input,Object.assign({},init,{body:JSON.stringify(b)}))}}}"
        "}catch(e){}"
        "var q=of.apply(window,arguments);"
        # 与官方自己的判断一致：/edge-api/bootstrap 与 /api/bootstrap 都算启动数据
        "try{if(/^\\/(?:edge-)?api\\/bootstrap(\\/|$)/.test(p))return q.then(fix)}catch(e){}"
        "return q};"
        "try{Object.defineProperty(wrapped,'name',{value:'fetch'})}catch(e){}"
        "window.fetch=wrapped;"
        "return 'ok';"
        "}catch(e){return 'err:'+(e&&e.message)}})();"
    )


# ---------------------------------------------------------------- 主进程注入


def hook_js(ns: str, locale: str = LOCALE) -> str:
    """主进程侧：给主窗口挂上页面脚本。

    只动默认 session 上、导航到 claude.ai 的 webContents。内置浏览器面板、预览窗口
    这些 App 自己也要挂调试器，我们抢先 attach 会把它们挤掉，所以一概不碰。
    did-start-navigation 发生在网络响应回来之前，addScript 赶得上这次提交。

    官方是先发主窗口的加载、后注册 onBeforeRequest，所以主窗口的第一篇文档必然
    早于我们。武装后 3 秒检查页面里的脚本是不是「赶在前面」跑的，不是就重载一次
    （每个 webContents 最多一次）。
    """
    return (
        f"{BLOCK_BEGIN}"
        f"if(!globalThis.{MARKER}){{globalThis.{MARKER}=1;try{{"
        "const _lg=(m)=>{try{require('node:fs').appendFileSync("
        f"{json.dumps(LOG_FILE)},new Date().toISOString()+' '+m+'\\n')}}catch(e){{}}}};"
        f"const _src={json.dumps(shim_js(locale))};"
        f"const _ds=()=>{{try{{return {ns}.session.defaultSession}}catch(e){{return null}}}};"
        "const _host=(u)=>{try{return /(^|\\.)claude\\.(ai|com)$/.test(new URL(u).hostname)}"
        "catch(e){return false}};"
        "const _check=(w)=>{setTimeout(()=>{try{"
        f"if(w.isDestroyed()||w.{MARKER}_r||!_host(w.getURL()))return;"
        "w.debugger.sendCommand('Runtime.evaluate',{expression:"
        "'window.__claude_zh_locale_at__===\"loading\"',returnByValue:!0})"
        ".then((r)=>{if(r&&r.result&&r.result.value===!0)return;"
        f"w.{MARKER}_r=1;_lg('first document missed, reloading');"
        "try{w.reload()}catch(e){}}).catch(()=>{});"
        "}catch(e){}},3000)};"
        "const _arm=(w)=>{try{"
        f"if(!w||w.isDestroyed()||w.{MARKER}_w)return;"
        "const s=_ds();if(s&&w.session!==s)return;"
        f"w.{MARKER}_w=1;const d=w.debugger;"
        "try{if(!d.isAttached())d.attach('1.3')}catch(e){_lg('attach failed: '+(e&&e.message));return}"
        "d.sendCommand('Page.enable').catch(()=>{});"
        "d.sendCommand('Page.addScriptToEvaluateOnNewDocument',{source:_src})"
        ".then(()=>{_lg('armed '+String(w.getURL()).slice(0,60));_check(w)})"
        ".catch((e)=>_lg('arm failed: '+(e&&e.message)));"
        "}catch(e){_lg('arm error: '+(e&&e.message))}};"
        "const _watch=(w)=>{try{"
        "if(!w||w.isDestroyed())return;const s=_ds();if(s&&w.session!==s)return;"
        "if(_host(w.getURL()))_arm(w);"
        "w.on('did-start-navigation',(_e,url,_ip,main)=>{if(main&&_host(url))_arm(w)});"
        "}catch(e){}};"
        f"try{{{ns}.webContents.getAllWebContents().forEach(_watch)}}catch(e){{}}"
        f"try{{{ns}.app.on('web-contents-created',(_ev,w)=>_watch(w))}}catch(e){{}}"
        f"_lg('hook wired, locale {locale}');"
        "}catch(e){}}"
        f"{BLOCK_END}"
    )


def find_hook_chunk(build_dir: Path) -> Path:
    """onBeforeRequest 锚点必须恰好在一个文件里唯一命中。"""
    hits = []
    for js in sorted(build_dir.glob("*.js")):
        n = len(RE_BEFORE_REQUEST.findall(js.read_text(encoding="utf-8", errors="replace")))
        if n:
            hits.append((js, n))
    if len(hits) != 1 or hits[0][1] != 1:
        detail = "\n".join(f"    {p.name}: {n} 处" for p, n in hits) or "    无任何命中"
        raise SystemExit(f"定位 onBeforeRequest 锚点失败（需要恰好一处）：\n{detail}")
    return hits[0][0]


def patch_chunk(path: Path, locale: str = LOCALE) -> str:
    """插入（或整块替换）注入代码。返回 "new" / "replaced"。"""
    text = path.read_text(encoding="utf-8")
    if LEGACY_MARKER in text:
        raise SystemExit("这份 app.asar 带着旧版「法语载体」补丁，请先装回官方版")
    mode = "new"
    b = text.find(BLOCK_BEGIN)
    if b >= 0:
        e = text.find(BLOCK_END, b)
        if e < 0:
            raise SystemExit("找到了注入块的开头标记却没有结尾，拒绝盲改")
        text = text[:b] + text[e + len(BLOCK_END) :]
        mode = "replaced"
    m = RE_BEFORE_REQUEST.search(text)
    if not m:
        raise SystemExit("onBeforeRequest 锚点失配")
    text = text[: m.end()] + hook_js(m.group("ns"), locale) + text[m.end() :]
    path.write_text(text, encoding="utf-8")
    return mode


# ---------------------------------------------------------------- asar 完整性


def asar_header_hash(asar: Path) -> str:
    """Electron 校验的是 asar 的 header JSON，不是整个文件。

    格式（Chromium Pickle 套两层）：
        [0:4]   u32 = 4          第一个 pickle 的 payload 长度
        [4:8]   u32 = headerPickleSize
        [8:12]  u32 = payload 长度
        [12:16] u32 = header 字符串长度 N
        [16:16+N]                header JSON
    算整个文件的哈希是错的，启动时会 "Integrity check failed for asar archive" 直接崩。
    """
    with asar.open("rb") as fh:
        head = fh.read(16)
        if len(head) < 16:
            raise SystemExit(f"app.asar 太小，无法解析头部：{asar}")
        length = int.from_bytes(head[12:16], "little")
        payload = fh.read(length)
    if len(payload) != length:
        raise SystemExit("app.asar 头部长度与实际不符，可能已损坏")
    return hashlib.sha256(payload).hexdigest()


def update_asar_integrity(app: Path, asar: Path) -> tuple[int, str]:
    """把新哈希写进所有带 ElectronAsarIntegrity 的 Info.plist。"""
    digest = asar_header_hash(asar)
    updated = 0
    for plist in sorted((app / "Contents").rglob("Info.plist")):
        try:
            data = plistlib.loads(plist.read_bytes())
        except Exception:
            continue
        entry = data.get("ElectronAsarIntegrity")
        if not isinstance(entry, dict):
            continue
        changed = False
        for val in entry.values():
            if isinstance(val, dict) and "hash" in val:
                val["hash"] = digest
                val["algorithm"] = "SHA256"
                changed = True
        if changed:
            plist.write_bytes(plistlib.dumps(data))
            updated += 1
    return updated, digest


def integrity_mismatches(app: Path) -> list[str]:
    """Info.plist 里记的 asar 哈希和实际不符的那些文件。"""
    asar = app / "Contents" / "Resources" / "app.asar"
    if not asar.is_file():
        return []
    digest = asar_header_hash(asar)
    bad = []
    for plist in sorted((app / "Contents").rglob("Info.plist")):
        try:
            data = plistlib.loads(plist.read_bytes())
        except Exception:
            continue
        entry = data.get("ElectronAsarIntegrity")
        if isinstance(entry, dict) and any(
            isinstance(v, dict) and v.get("hash") != digest for v in entry.values()
        ):
            bad.append(str(plist.relative_to(app)))
    return bad


# ---------------------------------------------------------------- unpacked 权限位

# Mach-O thin（两种字节序）+ universal fat 的魔数
MACHO_MAGIC = frozenset(
    {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
)


def _head4(path: Path) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(4)
    except OSError:
        return b""


def is_macho(path: Path) -> bool:
    """按文件头判断是不是 Mach-O，不看后缀也不看权限位。"""
    return not path.is_symlink() and path.is_file() and _head4(path) in MACHO_MAGIC


def needs_exec_bit(path: Path) -> bool:
    """本来就该能 exec 的文件（Mach-O 或带 shebang 的脚本）。dylib / *.node 是 dlopen 的，不算。"""
    if path.is_symlink() or not path.is_file() or path.suffix in {".dylib", ".node", ".so"}:
        return False
    head = _head4(path)
    return head in MACHO_MAGIC or head.startswith(b"#!")


def restore_unpacked_modes(unpacked: Path, modes: dict[str, int]) -> int:
    """重打包后把 app.asar.unpacked 的权限位还原成官方 app 里的样子。

    `asar extract` 不保留 unix 权限位，解出来一律 644。原样带回去的话 spawn-helper
    这类要 exec 的文件会丢掉执行位：终端起不来（posix_spawn EACCES），MCP 起不来。
    """
    changed = 0
    for f in unpacked.rglob("*"):
        if f.is_symlink() or not f.is_file():
            continue
        rel = str(f.relative_to(unpacked))
        cur = f.stat().st_mode & 0o7777
        want = modes.get(rel)
        if want is None:
            if not needs_exec_bit(f):
                continue
            want = cur | 0o111
        if want != cur:
            os.chmod(f, want)
            changed += 1
    return changed


def ensure_unpacked_exec_bits(app: Path) -> list[str]:
    """兜底：扫出 app.asar.unpacked 里丢了执行位的可执行文件并补上。"""
    unpacked = app / "Contents" / "Resources" / "app.asar.unpacked"
    if not unpacked.is_dir():
        return []
    fixed: list[str] = []
    for f in unpacked.rglob("*"):
        if needs_exec_bit(f) and not f.stat().st_mode & 0o111:
            os.chmod(f, (f.stat().st_mode & 0o7777) | 0o111)
            fixed.append(str(f.relative_to(unpacked)))
    return fixed


# ---------------------------------------------------------------- 总入口


def _asar(*args: str) -> None:
    subprocess.run(
        ["npx", "--yes", "@electron/asar", *args], check=True, capture_output=True, text=True
    )


def patch_app(app: Path, locale: str = LOCALE) -> str:
    """解包 → 注入 → 重打包 → 还原 unpacked 权限位 → 更新完整性哈希。返回一句说明。"""
    res = app / "Contents" / "Resources"
    asar, unpacked = res / "app.asar", res / "app.asar.unpacked"
    if not asar.is_file():
        raise SystemExit(f"找不到 app.asar：{asar}")

    work = Path(tempfile.mkdtemp(prefix="claude-zh-asar."))
    try:
        extracted = work / "app"
        _asar("extract", str(asar), str(extracted))
        chunk = find_hook_chunk(extracted / ".vite" / "build")
        mode = patch_chunk(chunk, locale)

        # 官方 app.asar.unpacked 里不只有 *.node，还有可执行文件和 *.dylib，
        # 必须留在真实文件系统上。unpack 规则从实际解包目录推导，并记下每个文件的权限位。
        original: set[str] = set()
        modes: dict[str, int] = {}
        if unpacked.is_dir():
            for f in unpacked.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(unpacked))
                    original.add(rel)
                    if not f.is_symlink():
                        modes[rel] = f.stat().st_mode & 0o7777
        patterns = {
            f"**/*{Path(r).suffix}" if Path(r).suffix else f"**/{Path(r).name}"
            for r in original
        }

        repacked = work / "app.asar"
        cmd = ["pack", str(extracted), str(repacked)]
        if patterns:
            cmd += ["--unpack", "{" + ",".join(sorted(patterns)) + "}"]
        _asar(*cmd)

        new_dir = work / "app.asar.unpacked"
        new = (
            {str(f.relative_to(new_dir)) for f in new_dir.rglob("*") if f.is_file()}
            if new_dir.is_dir()
            else set()
        )
        missing = original - new
        if missing:
            raise SystemExit(
                f"重打包后这些原本在 asar 外的文件没能留在外面，装上去会起不来：{sorted(missing)[:6]}"
            )

        shutil.copy2(repacked, asar)
        restored = 0
        if unpacked.is_dir() and new_dir.is_dir():
            shutil.rmtree(unpacked)
            shutil.copytree(new_dir, unpacked, symlinks=True)
            restored = restore_unpacked_modes(unpacked, modes)
        count, digest = update_asar_integrity(app, asar)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    what = "新装" if mode == "new" else "替换旧版"
    detail = f"完整性哈希写入 {count} 个 Info.plist，SHA256 {digest[:12]}…"
    if restored:
        detail += f"，还原 {restored} 个 unpacked 文件的权限位"
    return f"{chunk.name}（{what}；{detail}）"
