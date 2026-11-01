/**
 * 周报插件匿名调用统计 —— 收集端（Cloudflare Worker + D1）
 *
 * 路由：
 *   POST /v1/events   接收插件上报（字段白名单校验，不落 IP / UA）
 *   GET  /stats?key=  聚合结果 JSON（需要 STATS_KEY）
 *   GET  /            单页看板（前端再拉 /stats）
 *
 * 部署：
 *   wrangler d1 create mimetics-telemetry        → 把 database_id 填进 wrangler.toml
 *   wrangler d1 execute mimetics-telemetry --remote --file=./schema.sql
 *   wrangler secret put STATS_KEY
 *   wrangler deploy
 */

const MAX_BODY_BYTES = 2048;
const HEX16 = /^[0-9a-f]{16}$/;
const VERSION_RE = /^[0-9A-Za-z.\-+]{1,32}$/;
const ERROR_KIND_RE = /^[a-z_]{1,32}$/;
const VALID_EVENTS = new Set(["install", "call"]);
const VALID_OUTCOMES = new Set(["ok", "gate", "error"]);
const VALID_OS = new Set(["win32", "darwin", "linux", "aix", "freebsd", "openbsd", "android", "sunos", "unknown"]);

function json(data, status, headers) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}

function cleanString(value, maxLen, re) {
  if (typeof value !== "string") return null;
  const s = value.slice(0, maxLen);
  return re.test(s) ? s : null;
}

function intOrNull(value, min, max) {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const n = Math.round(value);
  return n >= min && n <= max ? n : null;
}

async function handleEvent(request, env, cors) {
  const type = request.headers.get("content-type") || "";
  if (!type.includes("application/json")) return json({ error: "unsupported_media_type" }, 415, cors);

  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) return json({ error: "payload_too_large" }, 413, cors);

  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return json({ error: "invalid_json" }, 400, cors);
  }
  if (!body || typeof body !== "object") return json({ error: "invalid_body" }, 400, cors);

  const entry = {
    received_at: Date.now(),
    schema: intOrNull(body.schema, 1, 99) || 0,
    instance: cleanString(body.instance, 16, HEX16),
    event: VALID_EVENTS.has(body.event) ? body.event : null,
    version: cleanString(body.version, 32, VERSION_RE),
    outcome: VALID_OUTCOMES.has(body.outcome) ? body.outcome : null,
    duration_ms: intOrNull(body.duration_ms, 0, 600000),
    materials: intOrNull(body.materials, 0, 1000),
    error_kind: cleanString(body.error_kind, 32, ERROR_KIND_RE),
    node: cleanString(body.node, 4, /^[0-9]{1,3}$/),
    os: VALID_OS.has(body.os) ? body.os : "unknown",
  };

  if (!entry.instance || !entry.event) return json({ error: "missing_required_fields" }, 400, cors);

  await env.DB.prepare(
    "INSERT INTO events (received_at, schema, instance, event, version, outcome, duration_ms, materials, error_kind, node, os) " +
      "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
  )
    .bind(
      entry.received_at,
      entry.schema,
      entry.instance,
      entry.event,
      entry.version,
      entry.outcome,
      entry.duration_ms,
      entry.materials,
      entry.error_kind,
      entry.node,
      entry.os,
    )
    .run();

  return json({ ok: true }, 202, cors);
}

async function handleStats(url, env, cors) {
  const key = url.searchParams.get("key") || "";
  if (!env.STATS_KEY) return json({ error: "server_misconfigured", hint: "wrangler secret put STATS_KEY" }, 500, cors);
  if (key !== env.STATS_KEY) return json({ error: "unauthorized" }, 401, cors);

  const now = Date.now();
  const since24h = now - 24 * 3600 * 1000;
  const since30d = now - 30 * 24 * 3600 * 1000;

  const totalsSql =
    "SELECT " +
    "SUM(CASE WHEN event='call' THEN 1 ELSE 0 END) AS calls, " +
    "SUM(CASE WHEN event='call' AND outcome='ok' THEN 1 ELSE 0 END) AS ok, " +
    "SUM(CASE WHEN event='call' AND outcome='gate' THEN 1 ELSE 0 END) AS gate, " +
    "SUM(CASE WHEN event='call' AND outcome='error' THEN 1 ELSE 0 END) AS errors, " +
    "SUM(CASE WHEN event='install' THEN 1 ELSE 0 END) AS installs, " +
    "COUNT(DISTINCT CASE WHEN event='call' THEN instance END) AS callers, " +
    "AVG(CASE WHEN event='call' THEN duration_ms END) AS avg_ms, " +
    "MAX(received_at) AS last_ts " +
    "FROM events";

  const dailySql =
    "SELECT date(received_at/1000,'unixepoch') AS day, " +
    "SUM(CASE WHEN event='call' THEN 1 ELSE 0 END) AS calls, " +
    "COUNT(DISTINCT CASE WHEN event='call' THEN instance END) AS instances, " +
    "SUM(CASE WHEN event='call' AND outcome='error' THEN 1 ELSE 0 END) AS errors " +
    "FROM events WHERE received_at >= ?1 GROUP BY day ORDER BY day";

  const [totals, recent, daily, versions, errors, os, recent24] = await Promise.all([
    env.DB.prepare(totalsSql).first(),
    env.DB.prepare("SELECT SUM(CASE WHEN event='call' THEN 1 ELSE 0 END) AS calls, COUNT(DISTINCT CASE WHEN event='call' THEN instance END) AS instances FROM events WHERE received_at >= ?1")
      .bind(since24h)
      .first(),
    env.DB.prepare(dailySql).bind(since30d).all(),
    env.DB.prepare("SELECT version, COUNT(*) AS calls FROM events WHERE event='call' GROUP BY version ORDER BY calls DESC LIMIT 10").all(),
    env.DB.prepare("SELECT error_kind, COUNT(*) AS count FROM events WHERE event='call' AND outcome='error' GROUP BY error_kind ORDER BY count DESC LIMIT 10").all(),
    env.DB.prepare("SELECT os, COUNT(DISTINCT instance) AS instances FROM events GROUP BY os ORDER BY instances DESC").all(),
    env.DB.prepare("SELECT instance FROM events WHERE event='call' AND received_at >= ?1 GROUP BY instance").bind(since30d).all(),
  ]);

  const calls = totals?.calls || 0;
  const ok = totals?.ok || 0;
  const gate = totals?.gate || 0;

  return json(
    {
      generated_at: now,
      totals: {
        calls,
        ok,
        gate,
        errors: totals?.errors || 0,
        installs: totals?.installs || 0,
        callers: totals?.callers || 0,
        success_rate: calls > 0 ? Math.round(((ok + gate) / calls) * 1000) / 10 : 0,
        avg_ms: totals?.avg_ms ? Math.round(totals.avg_ms) : 0,
        last_ts: totals?.last_ts || 0,
      },
      last_24h: { calls: recent?.calls || 0, instances: recent?.instances || 0 },
      active_instances_30d: recent24?.results?.length || 0,
      daily: daily?.results || [],
      versions: versions?.results || [],
      errors: errors?.results || [],
      os: os?.results || [],
    },
    200,
    cors,
  );
}

/** 看板 HTML。导出以便静态预览与自测（不影响 Worker 运行）。 */
export const DASHBOARD_HTML = [
  '<!DOCTYPE html>',
  '<html lang="zh-CN"><head><meta charset="utf-8">',
  '<meta name="viewport" content="width=device-width,initial-scale=1">',
  '<title>周报插件 · 调用统计</title>',
  '<style>',
  'body{margin:0;padding:24px 20px 48px;background:#FAF9F5;color:#2C2C2A;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}',
  '.wrap{max-width:880px;margin:0 auto}',
  'h1{font-size:18px;font-weight:500;margin:0 0 4px}',
  '.sub{color:#5F5E5A;font-size:12px;margin-bottom:20px}',
  '.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}',
  '.card{background:#fff;border:1px solid #E5E3DC;border-radius:12px;padding:14px 16px}',
  '.k{font-size:12px;color:#5F5E5A}',
  '.v{font-size:24px;font-weight:500;margin-top:2px}',
  '.u{font-size:12px;color:#888780;margin-left:2px}',
  '.panel{background:#fff;border:1px solid #E5E3DC;border-radius:12px;padding:16px;margin-bottom:16px}',
  '.pt{font-size:13px;font-weight:500;margin:0 0 12px}',
  '.bars{display:flex;align-items:flex-end;gap:4px;height:120px}',
  '.bar{flex:1;background:#1D9E75;border-radius:3px 3px 0 0;min-height:2px}',
  '.axis{display:flex;justify-content:space-between;font-size:11px;color:#888780;margin-top:6px}',
  'table{width:100%;border-collapse:collapse;font-size:13px}',
  'td{padding:6px 0;border-bottom:1px solid #F1EFE8}',
  'td:last-child{text-align:right;color:#5F5E5A}',
  '.empty{color:#888780;font-size:12px}',
  '.note{font-size:12px;color:#5F5E5A;line-height:1.7}',
  '@media(max-width:640px){.cards{grid-template-columns:repeat(2,1fr)}}',
  '</style></head><body><div class="wrap">',
  '<h1>周报插件 · 调用统计</h1>',
  '<div class="sub" id="sub">读取中…</div>',
  '<div class="cards">',
  '<div class="card"><div class="k">累计调用</div><div class="v" id="calls">–</div></div>',
  '<div class="card"><div class="k">近 24 小时</div><div class="v" id="calls24">–</div></div>',
  '<div class="card"><div class="k">活跃实例（30 天）</div><div class="v" id="inst">–</div></div>',
  '<div class="card"><div class="k">成功率</div><div class="v" id="rate">–<span class="u">%</span></div></div>',
  '</div>',
  '<div class="panel"><p class="pt">每日调用（近 30 天，UTC 日）</p><div class="bars" id="bars"></div><div class="axis"><span id="d0"></span><span id="d1"></span></div></div>',
  '<div class="panel"><p class="pt">版本分布</p><table id="ver"></table></div>',
  '<div class="panel"><p class="pt">失败归类</p><table id="err"></table></div>',
  '<div class="panel"><p class="pt">平台分布（按实例）</p><table id="os"></table></div>',
  '<div class="note">口径说明：<br>',
  '· <b>调用</b> = 用户真正执行了一次 <code>generate_weekly_report</code>；安装量请看「累计调用」上方的 installs 字段。<br>',
  '· <b>成功率</b> = (正常产出 + R0 闸门) / 总调用；闸门是无素材时的正常业务提示，不计为失败。<br>',
  '· <b>实例</b> 是本机随机 ID 的哈希，不含账号、IP、硬件信息；同一人多机算多个实例。<br>',
  '· 关闭上报：设置环境变量 <code>WEEKLY_REPORT_TELEMETRY=0</code>，或在插件配置里写 <code>telemetryEnabled: false</code>。',
  '</div>',
  '</div>',
  '<script>',
  '(function(){',
  'var q=location.search;',
  'function el(id){return document.getElementById(id)}',
  'function rows(tbody,list,label,key){',
  '  if(!list||!list.length){tbody.innerHTML="<tr><td class=empty>暂无数据</td></tr>";return}',
  '  tbody.innerHTML=list.map(function(it){return "<tr><td>"+(it[key]||label)+"</td><td>"+it[label]+"</td></tr>"}).join("")',
  '}',
  'fetch("/stats"+q).then(function(r){return r.json()}).then(function(d){',
  '  if(d.error){el("sub").textContent="读取失败："+d.error;return}',
  '  el("calls").textContent=d.totals.calls;',
  '  el("calls24").textContent=d.last_24h.calls;',
  '  el("inst").textContent=d.active_instances_30d;',
  '  el("rate").innerHTML=d.totals.success_rate+"<span class=u>%</span>";',
  '  el("sub").textContent="安装 "+d.totals.installs+" 次 · 累计实例 "+d.totals.callers+" 个 · 平均耗时 "+d.totals.avg_ms+" ms · 更新于 "+new Date(d.generated_at).toLocaleString();',
  '  var daily=d.daily||[];var max=1;daily.forEach(function(x){if(x.calls>max)max=x.calls});',
  '  el("bars").innerHTML=daily.map(function(x){var h=Math.round(x.calls/max*100);return "<div class=bar style=\\"height:"+h+"%\\" title=\\""+x.day+" · "+x.calls+" 次\\"></div>"}).join("");',
  '  if(daily.length){el("d0").textContent=daily[0].day;el("d1").textContent=daily[daily.length-1].day}',
  '  rows(el("ver"),d.versions,"calls","version");',
  '  rows(el("err"),d.errors,"count","error_kind");',
  '  rows(el("os"),d.os,"instances","os");',
  '}).catch(function(e){el("sub").textContent="读取失败："+e.message})',
  '})();',
  '</script>',
  '</body></html>',
].join("\n");

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET,POST,OPTIONS",
      "access-control-allow-headers": "content-type",
    };

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });

    if (url.pathname === "/v1/events" && request.method === "POST") {
      try {
        return await handleEvent(request, env, cors);
      } catch (error) {
        return json({ error: "internal_error", message: String(error && error.message) }, 500, cors);
      }
    }
    if (url.pathname === "/stats" && request.method === "GET") {
      try {
        return await handleStats(url, env, cors);
      } catch (error) {
        return json({ error: "internal_error", message: String(error && error.message) }, 500, cors);
      }
    }
    if ((url.pathname === "/" || url.pathname === "/index.html") && request.method === "GET") {
      return new Response(DASHBOARD_HTML, { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    return json({ error: "not_found" }, 404, cors);
  },
};
