/** 上报开关与端点的显式覆盖（来自插件配置）。 */
export interface TelemetryOptions {
    /** 显式开关。缺省时由环境变量 `WEEKLY_REPORT_TELEMETRY` 决定。 */
    enabled?: boolean;
    /** 收集端地址。缺省时依次取环境变量 `WEEKLY_REPORT_TELEMETRY_URL`、内置常量。 */
    endpoint?: string;
}
/** 单次调用的业务结局。 */
export type CallOutcome = 'ok' | 'gate' | 'error';
/** 上报字段白名单（结构即白名单：多余字段不会被带出去）。 */
export interface TelemetryEvent {
    /** 事件类型：install = 本机首次加载；call = 一次工具调用。 */
    event: 'install' | 'call';
    /** 插件版本（来自 package.json，运行时读取）。 */
    version: string;
    /** 仅 call 事件：业务结局。 */
    outcome?: CallOutcome;
    /** 仅 call 事件：端到端耗时（毫秒）。 */
    durationMs?: number;
    /** 仅 call 事件：素材条数（不给内容）。 */
    materials?: number;
    /** 仅 call 事件：失败归类（不含错误原文）。 */
    errorKind?: string;
}
/**
 * 判定是否启用上报：显式配置 > 环境变量 > **默认开启**。
 *
 * 「默认开启」是产品决定：插件跑在用户本机，不主动上报就永远没有调用数据。
 * 代价是必须把这件事写进 README 并保证可关闭——`WEEKLY_REPORT_TELEMETRY=0`
 * 或配置 `telemetryEnabled: false` 即为完全静默。
 *
 * 注意：开关为真但收集端地址为空时，仍然一个请求都不发（见 report）。
 */
export declare function isTelemetryEnabled(options?: TelemetryOptions): boolean;
/** 解析收集端地址：显式配置 > 环境变量 > 内置常量。空串表示不发送。 */
export declare function resolveEndpoint(options?: TelemetryOptions): string;
/**
 * 取本机匿名实例 ID，首次调用时生成并落盘。
 * 只读环境（如容器内 home 不可写）下降级为进程内 ID：统计会重复计数，但绝不报错。
 */
export declare function getInstanceId(): string;
/**
 * 上报一条事件。**同步返回**，不返回 Promise——调用方无法（也不该）等待它。
 */
export declare function report(event: TelemetryEvent, options?: TelemetryOptions): void;
/**
 * 上报「本机首次安装」。同一台机器只发一次（以标记文件为准），
 * 因此 install 事件数 ≈ 独立安装数，而不是「每次启动都 +1」。
 */
export declare function reportInstall(version: string, options?: TelemetryOptions): void;
