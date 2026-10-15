/**
 * @module harness-plugin-weekly-report
 *
 * Harness（dsh）工具插件：`generate_weekly_report`
 *
 * 把**随包自带**的 Python 周报生成流水线（`pipeline/src/pipeline.py`）封装为
 * 一个模型可见的工具。模型在 Harness 对话里说「帮我生成周报」时，本插件会：
 *
 *   1. 把入参里的每一项区分成「已存在的文件路径」或「素材原文」；
 *   2. 原文项落盘为临时文件（`pipeline.py --input` 只接受路径）；
 *   3. 调用 Python 执行 `-m src.pipeline`，抓取 stdout 的 Markdown；
 *   4. 原样返回 Markdown；失败时把 Python 的报错原文抛给模型，绝不编造。
 *
 * 导出形状遵循 dsh 官方约定：具名导出 `name` / `inject` / `Config` / `apply`，
 * **不得有 default export**——Loader 的 `unwrapExports` 会因 default 导出而
 * 折叠模块并丢掉 `inject`（官方 postmortem 0001）。
 */
import { execFile, execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';
/** 稳定 Cordis 插件名（出现在配置与日志中）。 */
export const name = 'weekly-report';
/** 本插件消费的服务：只需要工具注册表。 */
export const inject = ['tools'];
/** 包内自带的流水线目录（v1.3.0 起随包分发）：装上即用，不需要本机另有流水线目录。 */
const BUNDLED_PIPELINE_ROOT = resolve(import.meta.dirname, '..', 'pipeline');
/** 旧行为（v1.2.x 及更早）：本插件放在该项目下的 harness-plugin/，故上两级即项目根。 */
const LEGACY_PROJECT_ROOT = resolve(import.meta.dirname, '..', '..');
/** 判定一个候选是否"像"流水线根：必须含 src/pipeline.py。 */
function looksLikePipelineRoot(dir) {
    return Boolean(dir) && existsSync(join(dir, 'src', 'pipeline.py'));
}
/**
 * 解析流水线根目录。顺序（v1.3.0 起，四级）：
 *   ① 插件配置 projectRoot
 *   ② 环境变量 WEEKLY_REPORT_PROJECT_ROOT
 *   ③ **包内自带 pipeline/**（随包分发，社区用户装上即用）
 *   ④ 插件上两级（保留 v1.2.x 旧行为）
 *
 * ① ② 属**显式配置**：给了就以此为准，不合法**直接报错**（不静默回落），避免掩盖配置错误；
 * ③ ④ 属自动探测：命中即用，全落空时报错并列出所有已探测路径。
 */
export function resolveProjectRoot(config = {}, env = process.env) {
    const explicit = config.projectRoot?.trim() || env.WEEKLY_REPORT_PROJECT_ROOT?.trim();
    if (explicit) {
        if (!looksLikePipelineRoot(explicit)) {
            throw new Error(`projectRoot 配置无效：${explicit}\n` +
                `该目录须包含 src/pipeline.py 与 config/config.json。` +
                `若希望改用包内自带流水线，请移除这一配置项。`);
        }
        return explicit;
    }
    const probed = [BUNDLED_PIPELINE_ROOT, LEGACY_PROJECT_ROOT];
    const hit = probed.find(looksLikePipelineRoot);
    if (hit)
        return hit;
    throw new Error(`未找到周报流水线，已探测：\n` +
        probed.map((dir) => `  - ${dir}`).join('\n') +
        `\n请确认插件包含 pipeline/ 目录，或在插件配置里显式指定 projectRoot。`);
}
/** 读取插件自身版本：**运行时从 package.json 读，不硬编码**（避免两处漂移）。 */
export function readPluginVersion() {
    try {
        const raw = readFileSync(resolve(import.meta.dirname, '..', 'package.json'), 'utf8');
        const parsed = JSON.parse(raw);
        return String(parsed.version ?? 'unknown');
    }
    catch {
        return 'unknown';
    }
}
/** 读取随包流水线版本：优先 `<projectRoot>/VERSION`，回落包内 pipeline/VERSION。 */
export function readPipelineVersion(projectRoot) {
    const candidates = [join(projectRoot, 'VERSION'), join(BUNDLED_PIPELINE_ROOT, 'VERSION')];
    for (const file of candidates) {
        try {
            const version = readFileSync(file, 'utf8').trim();
            if (version)
                return version;
        }
        catch {
            /* 试下一个 */
        }
    }
    return 'unknown';
}
/** 单次调用默认超时。周报流水线是纯 CPU 正则/文本处理，2 分钟足够宽裕。 */
const DEFAULT_TIMEOUT_MS = 120_000;
/**
 * 兜底探测用的**命令名**（走 PATH 解析，**不硬编码任何本机绝对路径**）。
 * 交付包纪律：包内不得出现用户目录或盘符形式的绝对路径。
 * 只做可用性检查，不执行解释器业务逻辑——避免在加载期产生副作用。
 */
const PYTHON_COMMANDS = ['python', 'python3', 'py'];
/** 命令是否可执行（交给 PATH 解析，不做任何绝对路径假设）。 */
function commandExists(cmd) {
    try {
        execFileSync(cmd, ['-V'], { stdio: 'ignore', timeout: 5000, windowsHide: true });
        return true;
    }
    catch {
        return false;
    }
}
/**
 * 解析实际使用的 Python 解释器；全部落空时抛出可读错误。
 *
 * 解析顺序：插件配置 pythonPath → 环境变量 WEEKLY_REPORT_PYTHON → PATH 上的
 * `python`/`python3`/`py`。**包内不写死任何本机绝对路径**；
 * pythonPath 既可以给绝对路径，也可以给 PATH 上的命令名（如 `python`）。
 */
function resolvePython(config) {
    const explicit = config.pythonPath?.trim() || process.env.WEEKLY_REPORT_PYTHON?.trim();
    if (explicit) {
        const looksLikePath = /[\\/]/.test(explicit);
        const ok = looksLikePath ? existsSync(explicit) : commandExists(explicit);
        if (!ok) {
            throw new Error(`Python 解释器不可用：${explicit}\n` +
                `请修正插件配置 pythonPath、环境变量 WEEKLY_REPORT_PYTHON，或改用有效的解释器路径/命令名。`);
        }
        return explicit;
    }
    for (const cmd of PYTHON_COMMANDS) {
        if (commandExists(cmd))
            return cmd;
    }
    throw new Error(`未找到可用的 Python 解释器。已尝试 PATH 上的命令：${PYTHON_COMMANDS.join('、')}\n` +
        `请在插件配置里显式指定 pythonPath，或设置环境变量 WEEKLY_REPORT_PYTHON。`);
}
/** Windows 盘符 / UNC / 类 Unix 绝对路径 / ./ 与 ../ 相对路径。 */
const EXPLICIT_PATH_RE = /^(?:[A-Za-z]:[\\/]|\\\\|\/|\.{1,2}[\\/])/;
/** 形如纯文件名：带扩展名、不含路径分隔符与空白。 */
const BARE_FILENAME_RE = /^[^\\/\s]+\.[A-Za-z0-9]{1,8}$/;
/** 存在且是普通文件。 */
function isFile(p) {
    try {
        return existsSync(p) && statSync(p).isFile();
    }
    catch {
        return false;
    }
}
/**
 * 区分一项入参是「文件路径」还是「素材原文」。
 *
 * 判定优先看**形态**而非存在性：凡带路径特征的字符串一律按路径对待，
 * 不存在就直接报错——绝不能悄悄降级成"文本素材"，否则用户写错路径时
 * 会拿到一份把路径字符串当素材的假周报。静默失败比报错危险得多。
 */
function classifyMaterial(raw, index) {
    // 含换行的必然是素材原文（路径不会跨行）。
    if (/[\r\n]/.test(raw))
        return 'text';
    const value = raw.trim();
    if (value.length === 0) {
        throw new Error(`materials[${index}] 是空字符串。请传入素材文件路径或素材原文。`);
    }
    if (EXPLICIT_PATH_RE.test(value)) {
        if (isFile(value))
            return 'path';
        throw new Error(`素材文件不存在：${value}\n` +
            `materials[${index}] 看起来是一个文件路径，但该位置没有这个文件。请检查路径是否写错。`);
    }
    if (BARE_FILENAME_RE.test(value)) {
        const absolute = resolve(process.cwd(), value);
        if (isFile(absolute))
            return 'path';
        throw new Error(`素材文件不存在：${value}\n` +
            `materials[${index}] 看起来是一个文件名，按相对路径解析为 ${absolute}，但该文件不存在。` +
            `若要把它当作素材原文，请粘贴正文内容而不是文件名。`);
    }
    // 其余一律视为素材原文。
    return 'text';
}
/** 用 Promise 包一层 `execFile`，并把「超时」「信号终止」「启动失败」区分开。 */
function runProcess(python, args, cwd, timeoutMs, signal) {
    return new Promise((settle) => {
        execFile(python, args, {
            cwd,
            timeout: timeoutMs,
            // 周报 Markdown 可能较大，默认 1 MiB 不够。
            maxBuffer: 64 * 1024 * 1024,
            windowsHide: true,
            encoding: 'utf8',
            // 关键：强制 Python 以 UTF-8 输出，否则 Windows 默认 cp936 会让中文变乱码。
            env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
            ...(signal ? { signal } : {}),
        }, (error, stdout, stderr) => {
            const err = error;
            settle({
                code: err && typeof err.code === 'number' ? err.code : err ? null : 0,
                stdout: typeof stdout === 'string' ? stdout : String(stdout ?? ''),
                stderr: typeof stderr === 'string' ? stderr : String(stderr ?? ''),
                spawnError: err,
                timedOut: Boolean(err && err.killed && err.signal === 'SIGTERM'),
            });
        });
    });
}
/**
 * 执行一次周报生成。
 *
 * 退出码语义沿用 `pipeline.py` 的约定：
 *   0 = 正常产出；2 = R0 闸门（无可用素材，stdout 是一句提示）；1 = 参数或内部错误。
 * 退出码 1 视为失败并原文抛出；0/2 都算「业务上成功」，返回 stdout。
 */
async function generate(materials, outPath, config, signal) {
    const projectRoot = resolveProjectRoot(config);
    const python = resolvePython(config);
    const timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    // ── 前置校验：项目结构 ───────────────────────────────────────────────
    const pipelineEntry = join(projectRoot, 'src', 'pipeline.py');
    if (!existsSync(pipelineEntry)) {
        throw new Error(`未找到周报流水线入口：${pipelineEntry}\n` +
            `请确认 projectRoot 指向该项目根（应包含 src/pipeline.py 与 config/config.json）。`);
    }
    const configFile = join(projectRoot, 'config', 'config.json');
    if (!existsSync(configFile)) {
        throw new Error(`未找到流水线配置：${configFile}\n` +
            `该文件由 tools/build_config_json.py 从 config/*.yaml 派生，缺失则流水线无法启动。`);
    }
    // ── 素材分流：已存在路径直接用；否则视为原文落盘为临时文件 ─────────────
    const materialPaths = [];
    const spilled = [];
    let scratchDir = null;
    materials.forEach((raw, index) => {
        const value = raw ?? '';
        if (classifyMaterial(value, index) === 'path') {
            materialPaths.push(value.trim());
            return;
        }
        // 视为素材原文：写盘，交给 pipeline 的文本解析分支处理。
        if (scratchDir === null)
            scratchDir = mkdtempSync(join(tmpdir(), 'weekly-report-'));
        const target = join(scratchDir, `material-${String(index + 1).padStart(2, '0')}.md`);
        writeFileSync(target, value, { encoding: 'utf8' });
        materialPaths.push(target);
        spilled.push(target);
    });
    // ── 组装并执行 ───────────────────────────────────────────────────────
    const args = ['-m', 'src.pipeline', '--input', ...materialPaths];
    if (outPath && outPath.trim().length > 0)
        args.push('--out', outPath);
    const outcome = await runProcess(python, args, projectRoot, timeoutMs, signal);
    // 解析类错误：把 Python 侧的原文错误完整带出来。
    if (outcome.code === 1) {
        const detail = outcome.stderr.trim() || outcome.stdout.trim() || '（Python 未输出任何诊断信息）';
        throw new Error(`周报生成失败（Python 退出码 1）。\n` +
            `命令：${python} ${args.map((a) => (/\s/.test(a) ? JSON.stringify(a) : a)).join(' ')}\n` +
            `工作目录：${projectRoot}\n` +
            `原始错误：\n${detail}`);
    }
    // 无法归类的失败：进程根本没起来，或超时。
    if (outcome.spawnError && outcome.code === null) {
        if (outcome.timedOut) {
            throw new Error(`周报生成超时（超过 ${timeoutMs} ms）。\n` +
                `工作目录：${projectRoot}\n` +
                (outcome.stderr.trim() ? `已产生输出：\n${outcome.stderr.trim()}` : ''));
        }
        const err = outcome.spawnError;
        throw new Error(`无法启动 Python 进程：${err.message}\n` +
            `解释器：${python}\n` +
            `退出码/错误码：${String(err.code ?? 'n/a')}`);
    }
    // 退出码 2 = R0 闸门，属正常业务分支，stdout 就是给用户的那句提示。
    const markdown = outcome.stdout;
    if (markdown.trim().length === 0) {
        throw new Error(`Python 执行成功（退出码 ${String(outcome.code)}）但没有任何 Markdown 输出。\n` +
            `工作目录：${projectRoot}\n` +
            (outcome.stderr.trim() ? `stderr：\n${outcome.stderr.trim()}` : '（stderr 为空）'));
    }
    // 把 stats / warnings / 临时文件信息作为附注附在末尾，便于排查。
    const notes = [];
    if (outcome.stderr.trim())
        notes.push(outcome.stderr.trim());
    if (spilled.length > 0)
        notes.push(`[plugin] 已将 ${spilled.length} 项素材原文落盘为临时文件后交给流水线。`);
    if (notes.length === 0)
        return markdown;
    return `${markdown}\n\n---\n\n<details>\n<summary>流水线诊断信息（stderr）</summary>\n\n\`\`\`\n${notes.join('\n')}\n\`\`\`\n\n</details>\n`;
}
/**
 * 挂载插件：向 `ctx.tools` 注册 `generate_weekly_report`。
 * @param ctx - 携带工具注册表的 Cordis 上下文。
 * @param config - 部署侧的显式配置（可缺省）。
 */
export function apply(ctx, config = {}) {
    ctx.tools.register(defineTool({
        name: 'generate_weekly_report',
        description: '根据本周工作素材生成结构化周报（Markdown）。' +
            '入参 materials 是一个数组，每一项可以是素材文件的绝对路径，也可以直接是素材原文文本（插件会自动落盘）。' +
            '产出四模块结构：【本周完成】【进行中】【风险与阻塞】【下周计划】，并附带状态冲突提示与合并审计。' +
            '当 Python 侧报错时，本工具会把原始错误信息原样返回，不做任何修饰。',
        parameters: {
            materials: {
                type: 'array',
                items: { type: 'string' },
                required: true,
                description: '素材列表。每项为文件绝对路径（.md/.txt/.csv/.xlsx 等）或素材原文文本；两种可混用。',
            },
            outPath: {
                type: 'string',
                description: '可选。把生成的 Markdown 另外写入该绝对路径（目录会被自动创建）。',
            },
        },
        output: {
            schema: { type: 'string' },
            render: (_args, value) => [{ type: 'text', text: value }],
        },
        // 超时预算：流水线是 CPU 密集但不阻塞的纯计算，可与兄弟调用并行。
        timeoutMs: DEFAULT_TIMEOUT_MS,
        isConcurrencySafe: () => true,
        async execute(args, exec) {
            return generate(args.materials, args.outPath, config, exec.signal);
        },
    }));
    // 挂载确认：仅在显式开启调试时输出，避免污染正常运行日志。
    // D8：版本可观测——常驻一行，用户不必开 DEBUG 也能确认装的是哪个版本。
    let resolvedRoot = '';
    try {
        resolvedRoot = resolveProjectRoot(config);
    }
    catch {
        resolvedRoot = '(未解析)';
    }
    console.error(`[weekly-report] generate_weekly_report 已注册 | ` +
        `plugin v${readPluginVersion()} | pipeline v${readPipelineVersion(resolvedRoot)}`);
    if (process.env.WEEKLY_REPORT_DEBUG === '1') {
        console.error(`[weekly-report] generate_weekly_report 已注册 | ` +
            `projectRoot=${resolvedRoot} | ` +
            `python=${config.pythonPath?.trim() || process.env.WEEKLY_REPORT_PYTHON?.trim() || '(自动探测)'}`);
    }
}
