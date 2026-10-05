// 页面脚本（asarpatch.shim_js）的行为测试。不依赖 claude.ai，也不需要登录。
// 运行：node tests/shim.test.mjs
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const SHIM = execFileSync("python3", ["-c", "import asarpatch; print(asarpatch.shim_js())"], {
  cwd: root,
  encoding: "utf8",
});

const djb2 = (s) => {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h << 5) - h + s.charCodeAt(i);
    h = h & h;
  }
  return String(h >>> 0);
};
const GATE = djb2("witty_scone_main");

let failed = 0;
const check = (cond, msg) => {
  console.log(`${cond ? "ok  " : "FAIL"} ${msg}`);
  if (!cond) failed++;
};

// 每个场景一个干净的「页面」：伪造 location / localStorage / fetch，然后执行页面脚本
function page({ pref, accountLocale = "en-US", released = ["en-US", "fr-FR"], hostname = "claude.ai" } = {}) {
  const store = new Map();
  if (pref) store.set("claude-zh:locale", pref);
  const sent = [];
  globalThis.window = globalThis;
  delete globalThis.__claude_zh_locale__;
  globalThis.location = { hostname, href: `https://${hostname}/new` };
  globalThis.document = { readyState: "loading" };
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  globalThis.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    sent.push({ url, init });
    const path = new URL(url, "https://claude.ai").pathname;
    const json = (o, extra = {}) =>
      new Response(JSON.stringify(o), {
        status: 200,
        headers: { "content-type": "application/json", "content-length": "1", date: "Sat, 26 Sep 2026 08:00:00 GMT", ...extra },
      });
    if (/^\/(edge-)?api\/bootstrap/.test(path))
      return json({
        account: { uuid: "u" },
        locale: accountLocale,
        gated_messages: { locale: accountLocale },
        growthbook: { hashing_algorithm: "djb2", features: released ? { [GATE]: { defaultValue: { released: [...released] } } } : {} },
      });
    if (path === "/api/account_profile") return new Response('{"error":"not permitted"}', { status: 400 });
    return json({ locale: accountLocale });
  };
  const result = eval(SHIM);
  return { result, store, sent };
}

const boot = async () => (await fetch("/edge-api/bootstrap/org/app_start?statsig_hashing_algorithm=djb2")).json();
const put = (locale) =>
  fetch("/api/account_profile", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ locale }) });

// ---- 默认（从没选过语言）
{
  const { result, store } = page();
  check(result === "ok", "安装返回 ok");
  check(store.get("spa:locale") === "zh-Hans", "首屏：spa:locale 预置为 zh-Hans");
  check(window.__claude_zh_locale_at__ === "loading", "记录了 readyState=loading");
  const b = await boot();
  check(b.locale === "zh-Hans", "启动数据顶层 locale → zh-Hans");
  check(b.gated_messages.locale === "en-US", "嵌套的 locale 不动");
  check(b.account.uuid === "u", "其它字段原样");
  check(b.growthbook.features[GATE].defaultValue.released.includes("zh-Hans"), "放量名单补上 zh-Hans");
  const r = await fetch("/edge-api/bootstrap?x=1");
  check(r.headers.get("date") !== null && r.headers.get("content-length") === null, "保留 Date 头、去掉过期的 content-length");
  const r2 = await fetch(new Request("https://claude.ai/edge-api/bootstrap/org/app_start"));
  check((await r2.json()).locale === "zh-Hans", "Request 对象形式也命中");
  check((await (await fetch("/api/bootstrap/org/app_start")).json()).locale === "zh-Hans", "/api/bootstrap 形式也命中");
  check((await (await fetch("/api/other")).json()).locale === "en-US", "其它接口的响应不动");
  check((await (await fetch("/edge-api/bootstrapper")).json()).locale === "en-US", "相似路径不误伤");
  check(eval(SHIM) === "skip:dup", "重复注入被挡掉");
}

// ---- 放量名单里本来没有这个特性：补一个只含 zh-Hans 的
{
  page({ released: null });
  const b = await boot();
  check(JSON.stringify(b.growthbook.features[GATE]) === '{"defaultValue":{"released":["zh-Hans"]}}', "缺特性时新建放量名单");
}

// ---- 在选择器里选中文：本地应答成功，不打到服务端
{
  const { store, sent } = page({ pref: "en-US" });
  const r = await put("zh-Hans");
  check(r.status === 200, "选中文：本地返回 200");
  check(!sent.some((s) => s.url.includes("account_profile")), "选中文：请求没有发往服务端");
  check(store.get("claude-zh:locale") === "zh-Hans" && store.get("spa:locale") === "zh-Hans", "选中文：记住选择");
  check((await boot()).locale === "zh-Hans", "选中文后启动数据跟着变中文");
}

// ---- 在选择器里选英文：照常提交，之后不再强制中文
{
  const { store, sent } = page();
  await put("en-US");
  check(sent.some((s) => s.url.includes("account_profile")), "选英文：请求照常发往服务端");
  check(store.get("claude-zh:locale") === "en-US", "选英文：记住选择");
  const b = await boot();
  check(b.locale === "en-US", "选英文后启动数据保持账号语言");
  check(b.growthbook.features[GATE].defaultValue.released.includes("zh-Hans"), "选英文后中文仍留在选择器里");
}

// ---- 重启后仍尊重英文选择
{
  const { store } = page({ pref: "en-US" });
  check(store.get("spa:locale") === undefined, "偏好是英文时不预置 spa:locale");
  check((await boot()).locale === "en-US", "偏好是英文时不改启动数据");
}

// ---- 其它站点不碰
{
  const { result } = page({ hostname: "example.com" });
  check(result === "skip:host", "非 claude.ai 页面直接跳过");
}

console.log(failed ? `\n${failed} 项失败` : "\n全部通过");
process.exit(failed ? 1 : 0);
