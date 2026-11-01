/**
 * 匿名统计链路测试：验证「开了会发、关了不发、发了不拖慢、报喜不报忧」。
 *
 * 自包含：不依赖 tests/fixtures，也不出公网——收集端是本地起的假服务器。
 *
 * 用法：node test/telemetry-test.mjs
 *
 * 五个断言点：
 *   1. 默认关闭时零请求（守住 README「无网络请求」的默认承诺）；
 *   2. 开启后 install 事件每台机器只发一次（靠标记文件收敛，不是每次启动都算一次安装）；
 *   3. 失败路径照旧抛错，同时上报 outcome=error 与错误归类；
 *   4. 成功路径上报 outcome 为 ok 或 gate（R0 闸门不算失败）；
 *   5. 收集端慢到 1 秒时，execute 的耗时不受影响（证明没有 await 上报）。
 */
import { execFileSync } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = join(HERE, '..')
/** 流水线根：包内自带的那份（含 src/pipeline.py 与 config/config.json）。 */
const PIPELINE_ROOT = join(PROJECT_ROOT, 'pipeline')

// 必须在 import 插件之前设置：把实例 ID 落到临时目录，避免污染真实 ~/.mimetics。
const TELEMETRY_HOME = mkdtempSync(join(tmpdir(), 'wr-telemetry-'))
process.env.WEEKLY_REPORT_TELEMETRY_HOME = TELEMETRY_HOME

const received = []
let responseDelayMs = 0

const collector = createServer((req, res) => {
  let body = ''
  req.on('data', (chunk) => {
    body += chunk
  })
  req.on('end', () => {
    try {
      received.push(JSON.parse(body))
    } catch {
      received.push({ raw: body })
    }
    setTimeout(() => {
      res.writeHead(202, { 'content-type': 'application/json' })
      res.end('{"ok":true}')
    }, responseDelayMs)
  })
})

await new Promise((resolve) => collector.listen(0, '127.0.0.1', resolve))
const { port } = collector.address()
const ENDPOINT = `http://127.0.0.1:${port}/v1/events`

const { apply, readPluginVersion } = await import('../lib/index.js')
const VERSION = readPluginVersion()

let failures = 0
function check(label, ok, extra = '') {
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${extra ? ' — ' + extra : ''}`)
  if (!ok) failures += 1
}
const settle = (ms = 150) => new Promise((resolve) => setTimeout(resolve, ms))

/** 模拟 Harness 的最小 ctx，捕获注册的工具定义。 */
function mount(config = {}) {
  const captured = []
  apply(
    {
      tools: {
        register(definition) {
          captured.push(definition)
          return definition
        },
      },
    },
    { projectRoot: PIPELINE_ROOT, ...config },
  )
  return captured[0]
}

console.log('='.repeat(72))
console.log('weekly-report —— 匿名统计链路测试')
console.log('='.repeat(72))
console.log(`收集端(假) : ${ENDPOINT}`)
console.log(`实例目录   : ${TELEMETRY_HOME}`)
console.log('')

// ── 1. 未配置收集端：开关默认开着，但一个请求都不许发 ─────────────────
console.log('[1] 未配置收集端时不应有任何网络请求（开关默认开启）')
delete process.env.WEEKLY_REPORT_TELEMETRY
delete process.env.WEEKLY_REPORT_TELEMETRY_URL
received.length = 0
const offTool = mount()
await offTool.execute({ materials: ['C:\\__not_here__\\a.md'] }, {}).catch(() => {})
await settle()
check('无地址时收到 0 条事件', received.length === 0, `实得 ${received.length}`)

// ── 2. 开启后 install 每台机器只发一次 ─────────────────────────────────
console.log('\n[2] 开启后 install 事件收敛为「每台机器一次」')
process.env.WEEKLY_REPORT_TELEMETRY = '1'
process.env.WEEKLY_REPORT_TELEMETRY_URL = ENDPOINT
received.length = 0
mount()
await settle()
const installs = received.filter((e) => e.event === 'install')
check('首次加载上报 1 条 install', installs.length === 1, `实得 ${installs.length}`)
check('install 事件带版本号', installs[0]?.version === VERSION, `实得 ${installs[0]?.version}`)
check('instance 为 16 位 hex', /^[0-9a-f]{16}$/.test(String(installs[0]?.instance)))

mount()
await settle()
check('再次加载不重复上报 install', received.filter((e) => e.event === 'install').length === 1, `实得 ${received.filter((e) => e.event === 'install').length}`)

received.length = 0
const optedOut = mount({ telemetryEnabled: false })
await optedOut.execute({ materials: ['C:\\__not_here__\\b.md'] }, {}).catch(() => {})
await settle()
check('显式关闭后即使配了地址也不发', received.length === 0, `实得 ${received.length}`)

// ── 3. 失败路径：照旧抛错 + 上报失败归类 ───────────────────────────────
console.log('\n[3] 失败路径：错误不被吞，同时上报可归类的失败')
received.length = 0
const tool = mount()
let thrown = null
try {
  await tool.execute({ materials: ['C:\\__definitely_not_here__\\b.md'] }, {})
} catch (error) {
  thrown = error
}
await settle()
check('错误仍然抛给调用方', thrown instanceof Error, thrown ? '已抛出' : '未抛出')
const errEvent = received.find((e) => e.event === 'call' && e.outcome === 'error')
check('上报了 1 条 outcome=error', Boolean(errEvent))
check('错误归类为 material_invalid', errEvent?.error_kind === 'material_invalid', `实得 ${errEvent?.error_kind}`)
check('未泄露素材路径（事件里无路径字段）', JSON.stringify(errEvent ?? {}).indexOf('__definitely_not_here__') === -1)

// ── 4. 成功路径：需要可用的 Python ─────────────────────────────────────
console.log('\n[4] 成功路径：需要本机 Python（不可用则跳过）')
const PYTHON = process.env.WEEKLY_REPORT_PYTHON || 'python'
let pythonOk = false
try {
  execFileSync(PYTHON, ['-V'], { stdio: 'ignore', timeout: 5000, windowsHide: true })
  pythonOk = true
} catch {
  pythonOk = false
}

if (!pythonOk) {
  console.log(`  [SKIP] 未探测到 Python（${PYTHON}），跳过真实生成链路`)
} else {
  received.length = 0
  const before = Date.now()
  let markdown = ''
  let execError = null
  try {
    markdown = await tool.execute(
      {
        materials: [
          '本周完成了匿名统计埋点：收集端、看板、自测三部分。风险：Cloudflare 账号未开通，部署待确认。下周计划：部署收集端并观察一周数据。',
        ],
      },
      {},
    )
  } catch (error) {
    execError = error
  }
  const elapsed = Date.now() - before
  await settle(300)
  check(
    '返回的是 Markdown 字符串',
    typeof markdown === 'string' && markdown.length > 0,
    execError ? `抛错：${String(execError.message).split('\n')[0]}` : `${markdown.length} 字符`,
  )
  const callEvent = received.find((e) => e.event === 'call')
  check('上报了 1 条 call 事件', Boolean(callEvent))
  check('outcome 为 ok 或 gate', callEvent?.outcome === 'ok' || callEvent?.outcome === 'gate', `实得 ${callEvent?.outcome}`)
  check('耗时字段为正整数', Number.isInteger(callEvent?.duration_ms) && callEvent.duration_ms > 0, `${callEvent?.duration_ms} ms`)
  check('素材条数正确', callEvent?.materials === 1, `实得 ${callEvent?.materials}`)
  check('插件真实耗时 < 60s', elapsed < 60000, `${elapsed} ms`)

  // ── 5. 非阻塞：收集端慢 1 秒，不应拖慢调用 ─────────────────────────────
  console.log('\n[5] 收集端响应慢 1 秒时，execute 不应被拖慢')
  responseDelayMs = 1000
  received.length = 0
  const t0 = Date.now()
  await tool.execute({ materials: ['第二轮：补充看板，验证不阻塞主流程。'] }, {})
  const blocked = Date.now() - t0
  check(`execute 耗时未受 1000ms 响应延迟影响`, blocked < 1000, `实得 ${blocked} ms`)
  responseDelayMs = 0
}

console.log('\n' + '='.repeat(72))
console.log(failures === 0 ? '全部通过' : `有 ${failures} 项失败`)
console.log('='.repeat(72))
collector.close()
process.exit(failures === 0 ? 0 : 1)
