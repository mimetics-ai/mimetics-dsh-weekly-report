# mimetics-dsh-weekly-report

> **装上后，在 Harness 对话框说一句「帮我生成本周周报」，就能拿到固定四模块的 Markdown 周报**
> （〖本周完成〗〖进行中〗〖风险与阻塞〗〖下周计划〗）——由 `generate_weekly_report` 工具驱动内置流水线生成。

[![Listed on dsh-plugin.org](https://dsh-plugin.org/badges/listed.svg)](https://dsh-plugin.org/plugins/mimetics-ai/mimetics-dsh-weekly-report)

[![npm](https://img.shields.io/npm/v/mimetics-dsh-weekly-report.svg)](https://www.npmjs.com/package/mimetics-dsh-weekly-report)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)

---

## 安装

```bash
dsh plugin --profile web add mimetics-dsh-weekly-report
```

从 GitHub 直装（等价写法）：

```bash
dsh plugin --profile web add github:mimetics-ai/mimetics-dsh-weekly-report
```

安装后**重启 `dsh web`**，然后在对话框里直接说：

> 帮我生成本周周报，素材是：本周把登录页崩溃修完了，数据看板还在联调……

**验证已挂载**（二选一）：

1. **设置 → 插件**：应能看到 `mimetics-dsh-weekly-report`；
2. 打开调试日志（安装后设置环境变量 `WEEKLY_REPORT_DEBUG=1` 再启动 `dsh web`），启动日志会打印：

```
[weekly-report] generate_weekly_report 已注册 | plugin v1.3.0 | pipeline v1.1.2 | projectRoot=… | python=…
```

---

## 它做什么

- **把散素材收成一份周报**：工作记录 / 聊天摘录 / Jira 工单 / Excel 表 / 会议纪要 / 日志，可混着给；
- **合并同类项**：同一件事在不同来源出现多次时合并为一条，并在诊断里说明合并依据；
- **区分状态**：自动分到〖本周完成〗〖进行中〗〖风险与阻塞〗〖下周计划〗四个固定模块；
- **冲突不擅自取舍**：跨来源状态矛盾、或同一对象报了不同数值时，**归入〖风险与阻塞〗并写明冲突点**；
- **绝不编造**：数值/日期一律取自素材原文；缺数据的条目不做估算，只在诊断里提示。

## 工具入参

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `materials` | `string[]` | ✅ | 素材列表。每项可以是**文件绝对路径**（`.md`/`.txt`/`.csv`/`.xlsx`），也可以是**素材原文文本**，两者可混用 |
| `outPath` | `string` | ✖ | 额外输出路径，把 Markdown 另存一份（目录自动创建） |

## 输出示例

```markdown
〖本周完成〗
- 修复登录页在 iOS 上的崩溃问题，回归通过
- 数据看板优化，覆盖 3 个核心指标
〖进行中〗
- 对账模块重构，已提测
〖风险与阻塞〗
- ⚠️ 数值冲突：跨素材数值不一致（3 条规则 / 5 条规则），完成风控规则配置，覆盖 3 条规则
〖下周计划〗
- 待确认
```

---

## 权限与风险（Clear risk）

| 项 | 说明 |
|---|---|
| **运行依赖** | 本机需有 **Python 3.9+**（实测 3.13 / 3.15）。插件通过 `execFile` 调用 Python 运行内置流水线；未找到解释器时会给出明确的配置指引 |
| **文件读写** | 只读写**包内流水线目录**与插件自己的临时目录；若你在配置里显式给出 `projectRoot`，则读写该目录下的 `src/`、`config/` |
| **网络** | **默认零网络请求**。只有**显式开启**（`telemetryEnabled: true` 或 `WEEKLY_REPORT_TELEMETRY=1`）**且**填了收集端地址时，每次调用才会发出**一条**匿名事件；**素材内容、文件路径、错误原文永不外传**。详见 [`telemetry/README.md`](./telemetry/README.md) |
| **数据** | 素材只在本地进程间传递，产出物由你自行决定保存位置 |
| **权限** | 仅注册一个工具 `generate_weekly_report`，不申请额外系统权限、不执行生命周期脚本 |

## 配置

在 profile 的插件配置里可覆盖（一般无需配置，装上即用）：

| 配置键 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `pythonPath` | `WEEKLY_REPORT_PYTHON` | 依次探测 PATH 上的 `python` / `python3` / `py` | Python 解释器：绝对路径或命令名 |
| `projectRoot` | `WEEKLY_REPORT_PROJECT_ROOT` | **包内 `pipeline/` 目录** | 流水线根目录（含 `src/pipeline.py` 与 `pipeline/config/config.json`） |
| `timeoutMs` | — | `120000` | 单次调用超时 |
| `telemetryEnabled` | `WEEKLY_REPORT_TELEMETRY` | `false`（关闭） | 是否上报匿名调用统计；设为 `true` / `1` 开启 |
| `telemetryUrl` | `WEEKLY_REPORT_TELEMETRY_URL` | 空（不发请求） | 匿名统计收集端地址；**开关打开且地址非空才生效** |
| — | `WEEKLY_REPORT_DEBUG` | 未设置 | 设为 `1` 打印挂载与版本日志 |

## 匿名调用统计（默认关闭，需显式开启）

插件跑在你的电脑上，任何第三方（包括 GitHub 与 npm）都看不到「调用」这件事——它们只能看到下载和 clone。如果你想观察「用了多少次、失败在哪一步」，可以打开插件自带的匿名统计。

**默认关闭**：未显式开启时不发任何请求，代码路径等同于空实现。

**上报的字段**：插件版本、结局（ok / gate / error）、耗时毫秒、素材条数、失败归类、Node 主版本、操作系统、匿名实例 ID。
**绝不上报**：素材内容、文件路径、用户名、机器名、IP、错误原文。

匿名实例 ID 是本机首次运行随机生成的 UUID 之 SHA-256 前 16 位，与账号、硬件、IP 无关，只用于把同一台机器的多次调用归并成「一个使用者」。删除 `~/.mimetics/weekly-report/instance-id` 即换新 ID。

**发请求需同时满足两个条件**：① 统计开关打开（默认关闭）；② 收集端地址非空（默认为空）。任一不满足即零请求。

**开启方式**：配置 `telemetryEnabled: true` + `telemetryUrl`，或环境变量 `WEEKLY_REPORT_TELEMETRY=1` + `WEEKLY_REPORT_TELEMETRY_URL`。

自建收集端与看板（Cloudflare Worker + D1，免费额度足够）：

```bash
cd telemetry/worker
wrangler d1 create mimetics-telemetry          # 把返回的 database_id 填进 wrangler.toml
wrangler d1 execute mimetics-telemetry --remote --file=./schema.sql
wrangler secret put STATS_KEY                  # 看板访问口令，随便生成一串长随机字符
wrangler deploy                                # 记下输出的 https://<name>.<subdomain>.workers.dev
```

然后把插件指向它：`WEEKLY_REPORT_TELEMETRY_URL=https://<name>.<subdomain>.workers.dev/v1/events`，并同时设 `WEEKLY_REPORT_TELEMETRY=1`（或配置 `telemetryUrl` + `telemetryEnabled: true`）。打开该域名根路径即可看看板。

> 若将来要改成**对所有用户默认开启**（不需要用户配置）：需要同时修改 `src/telemetry.ts` 的 `DEFAULT_ENDPOINT` 与 `isTelemetryEnabled` 的默认返回值，并同步修改本节与「权限与风险 → 网络」的对外表述——这是承诺变更，不能只改代码。


## 兼容性

- **DSH**：`0.2.0-rc.2` 实测通过。peer 依赖：`@deepseek-ai/cordis ~4.0.4`、`@deepseek-ai/dsh-tools 0.2.0-rc.2`。
  ⚠️ DSH 处于开发者预览，官方明确会有破坏性变更；**升级 dsh 后请核对本段与包的版本**。
- **Node**：`>= 20.11`（实测 22.x）。
- **平台**：Windows 实测；macOS / Linux 未验证（代码不依赖平台专有 API，理论上可用）。
- **Python**：3.9+；仅用标准库，不需要 `pip install` 任何东西。

## 排障

| 现象 | 处理 |
|---|---|
| 启动日志没有 `[weekly-report]` | 确认已安装且 `WEEKLY_REPORT_DEBUG=1`；用 `dsh plugin --profile web list` 核对 |
| `未找到可用的 Python 解释器` | 显式配置 `pythonPath`，或设置 `WEEKLY_REPORT_PYTHON` |
| `未找到周报流水线入口` | 你的 `projectRoot` 配置指向了不含 `src/pipeline.py` 的目录；清掉该配置即回落到包内流水线 |
| 报 `素材文件不存在` | 这是**预期行为**：路径类入参若不存在会直接失败，而不是把路径当文本产出一份假周报 |
| 中文输出乱码 | 不应出现（插件强制 `PYTHONIOENCODING=utf-8`） |

---

## 开发

见 [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md)（本地加载方式、源码结构、构建与自测）。

## 许可与声明

- **许可证**：[MIT](./LICENSE)
- **主体**：广州永摹纪智能科技有限公司
- **声明**：本项目是**社区插件**，非 DeepSeek 官方项目；遵循 DeepSeek Harness 插件规范
  （导出 `apply(ctx)`，通过 `dsh.bundle.patch` 声明 bundle）。问题请在本仓库提 issue。
