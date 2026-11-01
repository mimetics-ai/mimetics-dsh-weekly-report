/**
 * 收集端本地测试：用假的 D1 绑定跑通路由、字段校验与鉴权。
 *
 * 不依赖 wrangler、不联网。用法：node telemetry/worker/test.mjs
 */
import worker from './src/index.js'

let failures = 0
function check(label, ok, extra = '') {
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${extra ? ' — ' + extra : ''}`)
  if (!ok) failures += 1
}

const inserted = []
const FIXED = {
  totals: { calls: 42, ok: 38, gate: 2, errors: 2, installs: 9, callers: 6, avg_ms: 312, last_ts: Date.now() },
  last24: { calls: 3, instances: 2 },
  daily: [
    { day: '2026-10-30', calls: 5, instances: 2, errors: 0 },
    { day: '2026-10-31', calls: 8, instances: 3, errors: 1 },
  ],
  versions: [{ version: '1.4.0', calls: 40 }, { version: '1.3.1', calls: 2 }],
  errors: [{ error_kind: 'python_missing', count: 2 }],
  os: [{ os: 'win32', instances: 6 }],
};

/** 极简 D1 替身：按 SQL 特征返回对应的固定结果。 */
function makeDB() {
  return {
    prepare(sql) {
      const stmt = {
        args: [],
        bind(...args) {
          stmt.args = args
          return stmt
        },
        async first() {
          if (sql.includes('GROUP BY')) return null
          // 近 24 小时的那条查询带 WHERE，累计那条不带。
          if (sql.includes('FROM events WHERE received_at')) return FIXED.last24
          return FIXED.totals
        },
        async all() {
          if (sql.includes('GROUP BY day')) return { results: FIXED.daily }
          if (sql.includes('GROUP BY version')) return { results: FIXED.versions }
          if (sql.includes('GROUP BY error_kind')) return { results: FIXED.errors }
          if (sql.includes('GROUP BY os')) return { results: FIXED.os }
          return { results: [] }
        },
        async run() {
          inserted.push({ sql, args: stmt.args })
          return { success: true }
        },
      }
      return stmt
    },
  }
}

const env = { DB: makeDB(), STATS_KEY: 'test-key' }
const base = 'https://telemetry.test'
const post = (body, headers = { 'content-type': 'application/json' }) =>
  worker.fetch(
    new Request(`${base}/v1/events`, { method: 'POST', headers, body: typeof body === 'string' ? body : JSON.stringify(body) }),
    env,
  )

const valid = {
  schema: 1,
  instance: '0123456789abcdef',
  event: 'call',
  version: '1.3.1',
  outcome: 'ok',
  duration_ms: 1234,
  materials: 2,
  node: '22',
  os: 'win32',
  ts: Date.now(),
}

console.log('='.repeat(72))
console.log('telemetry worker —— 本地测试')
console.log('='.repeat(72))

console.log('\n[1] 合法事件写入')
let res = await post(valid)
check('返回 202', res.status === 202, `实得 ${res.status}`)
check('写入 1 行', inserted.length === 1, `实得 ${inserted.length}`)
check('落库字段与白名单一致', inserted[0]?.args.length === 11, `实得 ${inserted[0]?.args.length} 个绑定值`)
check('不落 IP（SQL 中无 ip 字段）', !/ip/i.test(inserted[0]?.sql ?? ''))

console.log('\n[2] 非法输入被拒')
check('instance 非 16 位 hex → 400', (await post({ ...valid, instance: 'zzz' })).status === 400)
check('缺 event → 400', (await post({ ...valid, event: undefined })).status === 400)
check('content-type 不对 → 415', (await post(valid, { 'content-type': 'text/plain' })).status === 415)
check('超大 body → 413', (await post({ ...valid, padding: 'x'.repeat(4000) })).status === 413)
check('非法 JSON → 400', (await post('{not json')).status === 400)

console.log('\n[3] 字段清洗')
inserted.length = 0
await post({ ...valid, version: 'x'.repeat(80), os: 'plan9', error_kind: 'Bad-Kind!' })
const args = inserted[0]?.args ?? []
check('version 截断到 32 字符', String(args[4]).length <= 32, `实得 ${String(args[4]).length}`)
check('未知 os 归为 unknown', args[10] === 'unknown', `实得 ${args[10]}`)
check('非法 error_kind 置空', args[8] === null, `实得 ${args[8]}`)

console.log('\n[4] 看板与聚合')
const noKey = await worker.fetch(new Request(`${base}/stats`), env)
check('/stats 无 key → 401', noKey.status === 401, `实得 ${noKey.status}`)
const badKey = await worker.fetch(new Request(`${base}/stats?key=wrong`), env)
check('/stats 错误 key → 401', badKey.status === 401)

const statsRes = await worker.fetch(new Request(`${base}/stats?key=test-key`), env)
const stats = await statsRes.json()
check('/stats 正确 key → 200', statsRes.status === 200, `实得 ${statsRes.status}`)
check('累计调用 = 42', stats.totals?.calls === 42, `实得 ${stats.totals?.calls}`)
check('成功率按 (ok+gate)/calls 计算', stats.totals?.success_rate === 95.2, `实得 ${stats.totals?.success_rate}`)
check('日序列返回 2 天', stats.daily?.length === 2, `实得 ${stats.daily?.length}`)
check('版本分布返回 2 条', stats.versions?.length === 2)
check('失败归类返回 1 条', stats.errors?.length === 1)

const dash = await worker.fetch(new Request(`${base}/`), env)
const html = await dash.text()
check('看板返回 HTML', dash.headers.get('content-type')?.includes('text/html') === true)
check('看板含调用统计标题', html.includes('调用统计'))
check('看板不会被当成 JSON', !html.startsWith('{'))

const cors = await worker.fetch(new Request(`${base}/v1/events`, { method: 'OPTIONS' }), env)
check('OPTIONS 预检 → 204', cors.status === 204, `实得 ${cors.status}`)
const notFound = await worker.fetch(new Request(`${base}/nope`), env)
check('未知路径 → 404', notFound.status === 404)

console.log('\n' + '='.repeat(72))
console.log(failures === 0 ? '全部通过' : `有 ${failures} 项失败`)
console.log('='.repeat(72))
process.exit(failures === 0 ? 0 : 1)
