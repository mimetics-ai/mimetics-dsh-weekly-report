/**
 * @module telemetry
 *
 * 匿名调用统计：回答「装了多少、调了多少次、失败在哪一步」。
 *
 * 四条不可让步的纪律：
 *
 *   1. **绝不阻塞主流程**：调用即返回，不等回包；单次请求 800ms 超时；
 *      任何异常（DNS、TLS、超时、5xx）一律静默吞掉，绝不影响周报生成。
 *   2. **默认关闭（网络）**：必须**同时**满足「开关打开」与「地址非空」才会发一个请求；
 *      默认状态下零网络请求，与 README 的对外承诺一致。
 *      开启：`WEEKLY_REPORT_TELEMETRY=1`（或配置 `telemetryEnabled: true`）+ 配置 `telemetryUrl`。
 *   3. **只上报白名单字段**：不含素材内容、文件路径、用户名、机器名、IP、错误原文。
 *   4. **本地留痕默认开启（纯本地）**：每次调用往
 *      `~/.mimetics/weekly-report/events.jsonl` 追加一行同样的白名单字段，
 *      **不联网、不上传**，使用者随时可看、可删；设 `WEEKLY_REPORT_LOCAL_LOG=0` 关闭。
 *      这一条是给「只想知道自己用了多少次」的人准备的——不需要收集端也能有数。
 *
 * 匿名实例 ID：本机首次运行时随机生成 UUID v4，取其 SHA-256 前 16 位，
 * 存于 `~/.mimetics/weekly-report/instance-id`。与账号、硬件指纹、IP 均无关，
 * 唯一用途是把同一台机器的多次调用归并成「一个使用者」，从而算出留存与人均调用。
 */
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs'
import { homedir, platform } from 'node:os'
import { dirname, join } from 'node:path'

/**
 * 内置收集端地址。默认空串 = 不上报。
 *
 * 与 `isTelemetryEnabled` 是**「与」的关系**：开关打开 **且** 地址非空，才会发出请求。
 * 部署好收集端后把域名填进来（形如 `https://xxx.workers.dev/v1/events`），
 * 也可由用户用 `WEEKLY_REPORT_TELEMETRY_URL` 覆盖。
 */
const DEFAULT_ENDPOINT = ''

/** 单次上报的超时上限。宁可丢一条统计，也不能让用户的等待时间变长。 */
const REQUEST_TIMEOUT_MS = 800

/** 事件协议版本：收集端据此兼容旧客户端。 */
const SCHEMA_VERSION = 1

/**
 * 存放实例 ID 的目录。
 *
 * 默认 `~/.mimetics/weekly-report/`；可用 `WEEKLY_REPORT_TELEMETRY_HOME` 重定向
 * （容器只读 home、共享账号、以及自测时避免污染真实目录都需要这个口子）。
 */
function storeDir(): string {
  const override = process.env.WEEKLY_REPORT_TELEMETRY_HOME?.trim()
  if (override) return join(override, 'weekly-report')
  return join(homedir(), '.mimetics', 'weekly-report')
}

/** 匿名实例 ID 的落盘位置。 */
function idFilePath(): string {
  return join(storeDir(), 'instance-id')
}

/** 「已上报过 install」的标记文件，用于把 install 事件收敛为「每台机器一次」。 */
function installMarkerPath(): string {
  return join(storeDir(), 'installed-at')
}

/** 本地事件日志（JSONL，一行一条）。**纯本地文件，不联网**。 */
function localLogPath(): string {
  return join(storeDir(), 'events.jsonl')
}

/** 本地日志单文件上限：超过就轮转一份（events.jsonl.1），避免无限增长。 */
const LOCAL_LOG_MAX_BYTES = 2 * 1024 * 1024

/**
 * 本地留痕开关：**默认开启**（只写本机文件，不产生任何网络请求）。
 * 设 `WEEKLY_REPORT_LOCAL_LOG=0/false/off/no` 可关闭。
 */
function localLogEnabled(): boolean {
  const flag = process.env.WEEKLY_REPORT_LOCAL_LOG?.trim().toLowerCase()
  if (flag === '0' || flag === 'false' || flag === 'off' || flag === 'no') return false
  return true
}

/**
 * 往本地 JSONL 追加一行。**永不抛错**：只读环境、磁盘满、权限不足都静默跳过，
 * 统计永远不该让用户看到报错，更不该影响周报生成。
 */
function appendLocal(payload: Record<string, unknown>): void {
  if (!localLogEnabled()) return
  try {
    const dir = storeDir()
    mkdirSync(dir, { recursive: true })
    const file = localLogPath()
    try {
      if (statSync(file).size > LOCAL_LOG_MAX_BYTES) {
        renameSync(file, file + '.1')
      }
    } catch {
      /* 文件还不存在：正常情况，无需处理 */
    }
    const line = JSON.stringify({ ...payload, iso: new Date().toISOString() })
    writeFileSync(file, line + '\n', { encoding: 'utf8', flag: 'a' })
  } catch {
    /* 只读 home / 磁盘满 / 权限不足：静默放弃本地留痕 */
  }
}

/** 上报开关与端点的显式覆盖（来自插件配置）。 */
export interface TelemetryOptions {
  /** 显式开关。缺省时由环境变量 `WEEKLY_REPORT_TELEMETRY` 决定。 */
  enabled?: boolean
  /** 收集端地址。缺省时依次取环境变量 `WEEKLY_REPORT_TELEMETRY_URL`、内置常量。 */
  endpoint?: string
}

/** 单次调用的业务结局。 */
export type CallOutcome = 'ok' | 'gate' | 'error'

/** 上报字段白名单（结构即白名单：多余字段不会被带出去）。 */
export interface TelemetryEvent {
  /** 事件类型：install = 本机首次加载；call = 一次工具调用。 */
  event: 'install' | 'call'
  /** 插件版本（来自 package.json，运行时读取）。 */
  version: string
  /** 仅 call 事件：业务结局。 */
  outcome?: CallOutcome
  /** 仅 call 事件：端到端耗时（毫秒）。 */
  durationMs?: number
  /** 仅 call 事件：素材条数（不给内容）。 */
  materials?: number
  /** 仅 call 事件：失败归类（不含错误原文）。 */
  errorKind?: string
}

/** 缓存实例 ID，避免每次调用都读盘。 */
let cachedInstanceId: string | null = null

/** 「开关开着却没配地址」的提示只打一次，避免每次调用都刷日志。 */
let warnedMissingEndpoint = false

/**
 * 判定是否启用上报：显式配置 > 环境变量 > **默认关闭**。
 *
 * 默认关闭是对外承诺：插件不主动联网，除非使用者自己打开。
 * 只有 `enabled: true`，或 `WEEKLY_REPORT_TELEMETRY=1/true/on/yes`，才算打开；
 * 其余情况（含未设置）一律视为关闭。
 *
 * 注意：开关为真但收集端地址为空时，仍然一个请求都不发（见 report）。
 */
export function isTelemetryEnabled(options: TelemetryOptions = {}): boolean {
  if (options.enabled === false) return false
  if (options.enabled === true) return true
  const flag = process.env.WEEKLY_REPORT_TELEMETRY?.trim().toLowerCase()
  if (flag === '1' || flag === 'true' || flag === 'on' || flag === 'yes') return true
  return false
}

/** 解析收集端地址：显式配置 > 环境变量 > 内置常量。空串表示不发送。 */
export function resolveEndpoint(options: TelemetryOptions = {}): string {
  const explicit = options.endpoint?.trim()
  if (explicit) return explicit
  const fromEnv = process.env.WEEKLY_REPORT_TELEMETRY_URL?.trim()
  if (fromEnv) return fromEnv
  return DEFAULT_ENDPOINT
}

/**
 * 取本机匿名实例 ID，首次调用时生成并落盘。
 * 只读环境（如容器内 home 不可写）下降级为进程内 ID：统计会重复计数，但绝不报错。
 */
export function getInstanceId(): string {
  if (cachedInstanceId !== null) return cachedInstanceId
  const file = idFilePath()
  try {
    const raw = readFileSync(file, 'utf8').trim()
    if (/^[0-9a-f]{16}$/.test(raw)) {
      cachedInstanceId = raw
      return raw
    }
  } catch {
    /* 首次运行：文件不存在，走生成分支 */
  }
  const fresh = createHash('sha256').update(randomUUID()).digest('hex').slice(0, 16)
  try {
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(file, `${fresh}\n`, { encoding: 'utf8' })
  } catch {
    /* 只读环境：退化为内存 ID */
  }
  cachedInstanceId = fresh
  return fresh
}

/** 真正发包。永不 reject：所有异常都在这里终结。 */
async function send(endpoint: string, payload: Record<string, unknown>): Promise<void> {
  try {
    await fetch(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
  } catch {
    /* 网络不可达、超时、DNS 失败：静默。统计永远不该让用户看到报错。 */
  }
}

/** 组装白名单事件体（网络与本地共用同一份结构）。 */
function buildPayload(event: TelemetryEvent): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    schema: SCHEMA_VERSION,
    instance: getInstanceId(),
    ts: Date.now(),
    event: event.event,
    version: event.version,
    // 只给主版本号：足够判断运行时兼容性，又不构成指纹。
    node: process.versions.node.split('.')[0] ?? 'unknown',
    os: platform(),
  }
  if (event.outcome !== undefined) payload.outcome = event.outcome
  if (event.durationMs !== undefined) payload.duration_ms = Math.round(event.durationMs)
  if (event.materials !== undefined) payload.materials = event.materials
  if (event.errorKind !== undefined) payload.error_kind = event.errorKind
  return payload
}

/**
 * 记录一条事件。**同步返回**，不返回 Promise——调用方无法（也不该）等待它。
 *
 * 顺序：先写本地 JSONL（默认开启，纯本地），再判断是否联网上报（默认关闭）。
 * 因此「默认零网络请求」的承诺不变，但使用者在本地始终有数可查。
 */
export function report(event: TelemetryEvent, options: TelemetryOptions = {}): void {
  const payload = buildPayload(event)

  // ① 本地留痕：不发网络请求，与联网开关无关
  appendLocal(payload)

  // ② 联网上报：开关 + 地址，缺一不发
  if (!isTelemetryEnabled(options)) return
  const endpoint = resolveEndpoint(options)
  if (!endpoint) {
    // 开关开着但没配地址：不发，但要让维护者知道「开关没白开」。
    if (process.env.WEEKLY_REPORT_DEBUG === '1' && !warnedMissingEndpoint) {
      warnedMissingEndpoint = true
      console.error('[weekly-report] 统计已开启但未配置收集端地址（telemetryUrl / WEEKLY_REPORT_TELEMETRY_URL），本次不上报')
    }
    return
  }

  void send(endpoint, payload)

  if (process.env.WEEKLY_REPORT_DEBUG === '1') {
    console.error(`[weekly-report] telemetry → ${endpoint} ${JSON.stringify(payload)}`)
  }
}

/**
 * 上报「本机首次安装」。同一台机器只发一次（以标记文件为准），
 * 因此 install 事件数 ≈ 独立安装数，而不是「每次启动都 +1」。
 */
export function reportInstall(version: string, options: TelemetryOptions = {}): void {
  if (!isTelemetryEnabled(options)) return
  // 没有地址就不落标记：否则「先无地址跑过一次，事后才配地址」会让 install 事件
  // 被标记永久吞掉——用户装了很久，后台却看到 0 安装。
  if (!resolveEndpoint(options)) return
  const marker = installMarkerPath()
  if (existsSync(marker)) return
  try {
    mkdirSync(dirname(marker), { recursive: true })
    writeFileSync(marker, new Date().toISOString(), { encoding: 'utf8' })
  } catch {
    /* 写不进去就每次启动都发：宁可多记，不可漏记 */
  }
  report({ event: 'install', version }, options)
}
