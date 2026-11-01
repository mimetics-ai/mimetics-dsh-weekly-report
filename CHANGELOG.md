# Changelog

本文件记录 `mimetics-dsh-weekly-report` 的对外版本变更。
版本号规则：**流水线（pipeline/）逻辑变更 → 本包补丁版至少 +1**；新增能力 → minor；破坏性 → major。

## 未发布（Unreleased）· 建议版本 1.4.0

- **新增：匿名调用统计（默认关闭，需显式开启）**。新增 `src/telemetry.ts` 与 `telemetry/`（Cloudflare Worker + D1 + 看板）。
  每次 `generate_weekly_report` 执行后上报一条极简事件：版本、结局（ok/gate/error）、耗时、素材条数、
  失败归类、Node 主版本、操作系统、匿名实例 ID。**不含素材内容、文件路径、错误原文、IP**。
  ⚠️ **生效需同时满足**：① `telemetryEnabled: true`（或 `WEEKLY_REPORT_TELEMETRY=1`）；
  ② 收集端地址非空（`telemetryUrl` / `WEEKLY_REPORT_TELEMETRY_URL` / `DEFAULT_ENDPOINT`）。
  默认状态下**零网络请求**。
- **内部**：`generate()` 返回值由 `string` 改为 `{ markdown, exitCode }`（未导出，对外接口不变），
  用于区分「正常产出」与「R0 闸门」——统计口径依赖这个区分，闸门不计为失败。
- **README 修订**：「权限与风险 → 网络」保留「无任何网络请求」，补充「显式开启且配置收集端后才发一条匿名事件」
  这一例外；新增「匿名调用统计（默认关闭，需显式开启）」章节。**默认行为未变，仍是零请求**。
- **自测**：新增 `test/telemetry-test.mjs`（插件端）与 `telemetry/worker/test.mjs`（收集端），均不出公网。

## 1.3.1

纯文档级发布（**无代码变更**，`lib/` 与 `pipeline/` 字节不变）：
- README 增加 **DSH Plugin Hub 官方收录徽章**（此前 1.3.0 的 tarball 发布于加徽章之前，npm 页面看不到）；
- 仓库补齐 `v1.3.0` / `v1.3.1` 两个 release tag。

## 1.3.0

首发公开版（社区上架）。

- **新增：随包分发的流水线**（`pipeline/`）——装上即可运行，不再需要本机另有流水线目录；
  `projectRoot` 解析顺序：显式配置 → 环境变量 → **包内 `pipeline/`** → 插件上两级（兼容旧行为）。
- **新增：版本可观测**（D8）——注册日志与工具诊断块会输出 `plugin vX.Y.Z | pipeline vA.B.C`，
  版本运行时从 `package.json` 与 `pipeline/VERSION` 读取，不硬编码。
- **元数据**：更名 `mimetics-dsh-weekly-report`；许可证 MIT；补 `engines`（Node ≥ 20.11）、
  `repository` / `keywords` / `author`；`files` 增加 `pipeline`。

## 1.2.1

- 配套流水线侧 D7-1 / D7-2 修复：
  - 合并时把「未采用文本」写进**诊断**（只进 warnings，绝不进正文）；
  - 同一对象多取值的呈现修正（不再拼出「覆盖 3 条规则，5 条规则」这类病句）。
- 插件自身行为未变（桥接层字节不变），仅随流水线修复发布同版本。

## 1.2.0

- 配套流水线侧 D1–D5 修复：分节标题强路由、合并状态护栏、行内标签输出层剥离、
  占位符只用于空模块、裸模块名不进内容池。
- 修正：移除包内所有本机绝对路径（Python 解释器改为 PATH 探测 / 显式配置）。

## 1.1.x 及更早

内部开发版本，未对外发布。
