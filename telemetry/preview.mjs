/**
 * 把看板渲染成离线预览页：用示例数据替换掉 fetch('/stats')，其余样式与逻辑完全一致。
 *
 * 用法：node telemetry/preview.mjs <输出路径>
 */
import { writeFileSync } from 'node:fs'
import { DASHBOARD_HTML } from './worker/src/index.js'

const now = new Date('2026-11-01T10:00:00Z').getTime()
const days = []
let calls = 2
for (let i = 29; i >= 0; i -= 1) {
  const d = new Date(now - i * 86400000)
  calls = Math.max(0, calls + Math.round(Math.sin(i / 3) * 3))
  days.push({
    day: d.toISOString().slice(0, 10),
    calls: i > 8 ? 0 : calls,
    instances: 1,
    errors: i % 9 === 0 ? 1 : 0,
  })
}
const totalCalls = days.reduce((sum, d) => sum + d.calls, 0)

const sample = {
  generated_at: now,
  totals: {
    calls: totalCalls + 138,
    ok: totalCalls + 122,
    gate: 11,
    errors: 5,
    installs: 23,
    callers: 14,
    success_rate: 96.6,
    avg_ms: 418,
    last_ts: now - 3600000,
  },
  last_24h: { calls: 7, instances: 4 },
  active_instances_30d: 12,
  daily: days,
  versions: [
    { version: '1.3.1', calls: totalCalls + 130 },
    { version: '1.4.0', calls: 8 },
  ],
  errors: [
    { error_kind: 'python_missing', count: 3 },
    { error_kind: 'timeout', count: 1 },
    { error_kind: 'pipeline_error', count: 1 },
  ],
  os: [
    { os: 'win32', instances: 10 },
    { os: 'darwin', instances: 3 },
    { os: 'linux', instances: 1 },
  ],
}

const needle = 'fetch("/stats"+q).then(function(r){return r.json()})'
if (!DASHBOARD_HTML.includes(needle)) {
  console.error('预览失败：看板脚本结构已变化，需同步更新 selector')
  process.exit(1)
}

const html = DASHBOARD_HTML.replace(needle, `Promise.resolve(${JSON.stringify(sample)})`)
  .replace('<div class="sub" id="sub">读取中…</div>', '<div class="sub" id="sub">读取中…</div><div style="font-size:12px;color:#993C1D;margin:-12px 0 16px">示例数据预览（非真实采集结果）</div>')

const out = process.argv[2] || 'telemetry/preview.html'
writeFileSync(out, html, 'utf8')
console.log(`已生成 ${out}（${html.length} 字节）`)
