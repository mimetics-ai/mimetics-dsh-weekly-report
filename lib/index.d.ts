/** 稳定 Cordis 插件名（出现在配置与日志中）。 */
export declare const name = "weekly-report";
/** 本插件消费的服务：只需要工具注册表。 */
export declare const inject: string[];
/** 插件配置形态（全部可选，缺省走内置默认）。 */
export interface WeeklyReportConfig {
    /** Python 解释器：绝对路径，或 PATH 上的命令名（如 python）。缺省时按 env → PATH 探测。 */
    pythonPath?: string;
    /** 流水线项目根目录（含 src/ 与 config/）。 */
    projectRoot?: string;
    /** 单次 Python 调用的超时（毫秒）。 */
    timeoutMs?: number;
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
export declare function resolveProjectRoot(config?: WeeklyReportConfig, env?: NodeJS.ProcessEnv): string;
/** 读取插件自身版本：**运行时从 package.json 读，不硬编码**（避免两处漂移）。 */
export declare function readPluginVersion(): string;
/** 读取随包流水线版本：优先 `<projectRoot>/VERSION`，回落包内 pipeline/VERSION。 */
export declare function readPipelineVersion(projectRoot: string): string;
/**
 * 挂载插件：向 `ctx.tools` 注册 `generate_weekly_report`。
 * @param ctx - 携带工具注册表的 Cordis 上下文。
 * @param config - 部署侧的显式配置（可缺省）。
 */
export declare function apply(ctx: any, config?: WeeklyReportConfig): void;
