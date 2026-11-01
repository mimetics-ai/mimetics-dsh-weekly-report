#!/usr/bin/env node
/**
 * local-stats.mjs —— 读本地调用日志，出一份你自己的使用统计。
 *
 * 数据来源：`~/.mimetics/weekly-report/events.jsonl`（每次调用一行，纯本地，不上传）
 * 用法：
 *   node scripts/local-stats.mjs            # 看汇总
 *   node scripts/local-stats.mjs --json     # 输出 JSON（便于二次处理）
 *   node scripts/local-stats.mjs --days 7   # 只看最近 7 天
 */
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const args = process.argv.slice(2)
const asJson = args.includes('--json')
const daysIdx = args.indexOf('--days')
const days = daysIdx >= 0 ? Number(args[daysIdx + 1]) || 0 : 0

const home = process.env.WEEKLY_REPORT_TELEMETRY_HOME?.trim()
const dir = home ? join(home, 'weekly-report') : join(homedir(), '.mimetics', 'weekly-report')
const file = join(dir, 'events.jsonl')

if (!existsSync(file)) {
  console.log('还没有本地日志：' + file)
  console.log('生成一次周报之后再看（每次调用会自动追加一行）。')
  process.exit(0)
}

const cutoff = days > 0 ? Date.now() - days * 86400_000 : 0
const events = []
for (const line of readFileSync(file, 'utf8').split('\n')) {
  const t = line.trim()
  if (!t) continue
  try {
    const e = JSON.parse(t)
    if (cutoff && typeof e.ts === 'number' && e.ts < cutoff) continue
    events.push(e)
  } catch {
    /* 半行/损坏行：跳过 */
  }
}

const calls = events.filter((e) => e.event === 'call')
const installs = events.filter((e) => e.event === 'install')
const ok = calls.filter((e) => e.outcome === 'ok').length
const gate = calls.filter((e) => e.outcome === 'gate').length
const err = calls.filter((e) => e.outcome === 'error').length

const durations = calls.map((e) => e.duration_ms).filter((n) => typeof n === 'number').sort((a, b) => a - b)
const pct = (p) => (durations.length ? durations[Math.min(durations.length - 1, Math.floor(durations.length * p))] : null)

const byDay = {}
for (const e of calls) {
  const d = new Date(e.ts).toISOString().slice(0, 10)
  byDay[d] = (byDay[d] || 0) + 1
}
const errKinds = {}
for (const e of calls) {
  if (e.outcome === 'error' && e.error_kind) errKinds[e.error_kind] = (errKinds[e.error_kind] || 0) + 1
}

const summary = {
  log: file,
  window_days: days || 'all',
  events: events.length,
  installs: installs.length,
  calls: calls.length,
  ok,
  gate,
  error: err,
  success_rate: calls.length ? Math.round(((ok + gate) / calls.length) * 1000) / 10 : 0,
  duration_ms: { avg: durations.length ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length) : null, p50: pct(0.5), p90: pct(0.9) },
  by_day: byDay,
  error_kinds: errKinds,
  first: events.length ? new Date(Math.min(...events.map((e) => e.ts))).toISOString() : null,
  last: events.length ? new Date(Math.max(...events.map((e) => e.ts))).toISOString() : null,
}

if (asJson) {
  console.log(JSON.stringify(summary, null, 2))
  process.exit(0)
}

console.log('本地调用统计（' + (days ? '最近 ' + days + ' 天' : '全部') + '）')
console.log('日志：' + file)
console.log('')
console.log('  调用次数   ' + summary.calls)
console.log('  安装次数   ' + summary.installs)
console.log('  成功率     ' + summary.success_rate + '%   （ok ' + ok + ' / gate ' + gate + ' / error ' + err + '）')
if (summary.duration_ms.avg !== null) {
  console.log('  耗时       avg ' + summary.duration_ms.avg + 'ms   p50 ' + summary.duration_ms.p50 + 'ms   p90 ' + summary.duration_ms.p90 + 'ms')
}
if (Object.keys(errKinds).length) {
  console.log('  失败归类   ' + Object.entries(errKinds).map(([k, v]) => k + '×' + v).join('，'))
}
const dayKeys = Object.keys(byDay).sort()
if (dayKeys.length) {
  console.log('')
  console.log('  按日调用：')
  for (const d of dayKeys) console.log('    ' + d + '  ' + '█'.repeat(Math.min(40, byDay[d])) + ' ' + byDay[d])
}
if (summary.first) {
  console.log('')
  console.log('  首次 ' + summary.first + '   最后 ' + summary.last)
}
