/**
 * 外部链路对拍测试：在 Harness 之外验证「Node → Python → Markdown」这条链路。
 *
 * 思路：不启动 Harness，而是模拟一个最小 ctx（只提供 tools.register），
 * 加载真实的插件产物 lib/index.js，取出 generate_weekly_report 的定义，
 * 直接调用它的 execute。
 *
 * 这样验证的是**同一份代码**——插件在 Harness 里跑的就是这个 execute。
 *
 * 用法：node test/link-test.mjs
 */
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { existsSync, readFileSync } from 'node:fs'
import { apply, resolveProjectRoot, readPluginVersion, readPipelineVersion } from '../lib/index.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = join(HERE, '..', '..')
const FIXTURES = join(PROJECT_ROOT, 'tests', 'fixtures')

// 交付包纪律：测试脚本同样不硬编码本机绝对路径。
// 设置 WEEKLY_REPORT_PYTHON 可指定解释器；未设置时由插件按 PATH（python/python3/py）自动探测。
const PYTHON = process.env.WEEKLY_REPORT_PYTHON || 'python'

let failures = 0
function check(label, ok, extra = '') {
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${extra ? ' — ' + extra : ''}`)
  if (!ok) failures += 1
}

console.log('='.repeat(72))
console.log('harness-plugin-weekly-report —— 外部链路对拍测试')
console.log('='.repeat(72))
console.log(`项目根  : ${PROJECT_ROOT}`)
console.log(`Python  : ${PYTHON}   (来源: ${process.env.WEEKLY_REPORT_PYTHON ? 'WEEKLY_REPORT_PYTHON' : 'PATH 自动探测'})`)
console.log('')

// ── 1. 加载插件，模拟 ctx 捕获注册结果 ─────────────────────────────────
console.log('[1] 加载插件并捕获注册的工具')
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
  { pythonPath: PYTHON, projectRoot: PROJECT_ROOT },
)

check('插件向 ctx.tools 注册了恰好 1 个工具', captured.length === 1, `实得 ${captured.length}`)
const tool = captured[0]
if (!tool) {
  console.log('\n未注册任何工具，后续测试无法进行。')
  process.exit(1)
}
check('工具名为 generate_weekly_report', tool.name === 'generate_weekly_report', `实得 ${tool.name}`)
check('工具带有 output.render', typeof tool.output?.render === 'function')
check('工具带有可执行的 execute', typeof tool.execute === 'function')

// ── 2. schema 编译结果（defineTool 已把 author spec 编译为 JSON Schema）──
console.log('\n[2] 校验 schema 编译结果')
const params = tool.parameters
check('parameters 是 object 根', params?.type === 'object', `实得 ${params?.type}`)
check('materials 为 array<string>', params?.properties?.materials?.type === 'array' && params.properties.materials.items?.type === 'string')
check('materials 被标记为 required', Array.isArray(params?.required) && params.required.includes('materials'))
check('outPath 为可选 string', params?.properties?.outPath?.type === 'string' && !params.required.includes('outPath'))
check('output.schema 为 string', tool.output?.schema?.type === 'string')

const signal = new AbortController().signal

// ── 3. 用例 A：素材为文件路径 ───────────────────────────────────────────
console.log('\n[3] 用例 A —— materials 传入文件路径')
const fileMaterials = [
  join(FIXTURES, 'notes.md'),
  join(FIXTURES, 'chat_export.txt'),
  join(FIXTURES, 'jira.csv'),
]
console.log('  素材:', fileMaterials.map((p) => p.replace(PROJECT_ROOT + '\\', '')).join(' | '))

let outA = ''
try {
  outA = await tool.execute({ materials: fileMaterials }, { signal })
  check('execute 返回非空 Markdown', outA.trim().length > 0, `${outA.length} 字符`)
  check('含【本周完成】模块', outA.includes('本周完成'))
  check('含【下周计划】模块', outA.includes('下周计划'))
} catch (error) {
  check('execute 正常返回', false, String(error?.message ?? error).split('\n')[0])
}

// ── 4. 用例 B：素材为纯文本原文（验证落盘分支）────────────────────────
console.log('\n[4] 用例 B —— materials 传入纯文本原文（验证落盘分支）')
const textMaterials = [
  '本周完成了登录页崩溃修复，回归用例全部通过（PROJ-8801）。',
  '下周计划：推进风控规则配置，预计 10月25日 上线。',
  '风险：财务审批卡住了那笔 3.5万元的合同。',
]

let outB = ''
try {
  outB = await tool.execute({ materials: textMaterials }, { signal })
  check('execute 返回非空 Markdown', outB.trim().length > 0, `${outB.length} 字符`)
  check('文本素材被落盘并成功解析', outB.includes('本周完成') || outB.includes('下周计划'))
} catch (error) {
  check('execute 正常返回', false, String(error?.message ?? error).split('\n')[0])
}

// ── 5. 用例 C：错误必须原文透传，不许编造 ───────────────────────────────
console.log('\n[5] 用例 C —— 传入不存在的路径，验证错误原文透传')
let errC = null
try {
  await tool.execute({ materials: [join(FIXTURES, '__这个文件不存在__.md')] }, { signal })
} catch (error) {
  errC = error
}
check('不存在的素材触发了失败（未编造成功结果）', errC !== null)
if (errC) {
  const msg = String(errC.message ?? errC)
  console.log(`  原始错误首行: ${msg.split('\n')[0]}`)
  check('错误信息来自真实执行路径', /Python|素材|退出码|不存在/.test(msg))
}

// ── 7. v1.3.0：流水线根解析（四级）与版本可观测（D8）─────────────────────
console.log('\n[7] 流水线根解析与版本可观测（v1.3.0）')
const pluginDir = join(HERE, '..')
const pkg = JSON.parse(readFileSync(join(pluginDir, 'package.json'), 'utf8'))
const bundledRoot = join(pluginDir, 'pipeline')

check('① 显式 projectRoot 优先（不被包内副本抢走）',
  resolveProjectRoot({ projectRoot: PROJECT_ROOT }) === PROJECT_ROOT)
let strictError = null
try {
  resolveProjectRoot({ projectRoot: join(HERE, '不存在的流水线目录') })
} catch (error) {
  strictError = error
}
check('① 显式 projectRoot 不合法 → 直接报错，不静默回落',
  strictError !== null && /无效/.test(String(strictError.message)),
  strictError ? String(strictError.message).split('\n')[0] : '(未抛错)')
check('② 环境变量 WEEKLY_REPORT_PROJECT_ROOT 生效',
  resolveProjectRoot({}, { WEEKLY_REPORT_PROJECT_ROOT: PROJECT_ROOT }) === PROJECT_ROOT)
const autoRoot = resolveProjectRoot({}, {})
check('③ 无显式配置时命中**包内自带流水线**', autoRoot === bundledRoot, `实得 ${autoRoot}`)
check('③ 包内流水线入口存在（src/pipeline.py）', existsSync(join(autoRoot, 'src', 'pipeline.py')))
check('D8 插件版本运行时取自 package.json（非硬编码）',
  readPluginVersion() === pkg.version, `plugin=${readPluginVersion()} / package.json=${pkg.version}`)
check('D8 流水线版本取自 pipeline/VERSION',
  readPipelineVersion(autoRoot) === readFileSync(join(autoRoot, 'VERSION'), 'utf8').trim(),
  readPipelineVersion(autoRoot))

// ── 6. 结果输出 ────────────────────────────────────────────────────────
console.log('\n' + '='.repeat(72))
console.log('用例 A 产出（前 900 字符）')
console.log('='.repeat(72))
console.log(outA.slice(0, 900) || '(空)')

if (outB) {
  console.log('\n' + '='.repeat(72))
  console.log('用例 B 产出（前 600 字符）')
  console.log('='.repeat(72))
  console.log(outB.slice(0, 600))
}

console.log('\n' + '='.repeat(72))
console.log(failures === 0 ? '全部断言通过（0 失败）' : `存在 ${failures} 项失败`)
console.log('='.repeat(72))
process.exit(failures === 0 ? 0 : 1)
