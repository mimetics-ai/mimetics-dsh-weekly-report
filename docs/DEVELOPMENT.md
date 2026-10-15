# 开发说明（DEVELOPMENT）

本仓库是 **`mimetics-dsh-weekly-report`** 的独立公开仓库——一个 DeepSeek Harness（dsh）工具插件，
把「素材 → 四模块周报」的生成流水线封装成模型可调用的工具 `generate_weekly_report`。

> 面向使用者的内容（安装、用法、兼容性、风险声明）见根目录 [`README.md`](../README.md)；
> 版本变更见 [`CHANGELOG.md`](../CHANGELOG.md)；许可见 [`LICENSE`](../LICENSE) 与 [`NOTICE`](../NOTICE)。

---

## 一、目录结构

```
<repo>/
├── package.json            # 声明 dsh.bundle.patch；含 name/version/license/engines
├── package-lock.json       # 依赖锁定（版本号与 package.json 三处同步）
├── tsconfig.json           # TypeScript 配置（编译输出到 lib/）
├── cordis.patch.yml        # bundle 层：把插件行插入 profile 插件树
├── src/index.ts            # 插件源码（唯一源文件）
├── lib/                    # 编译产物（npm run build 生成；npm 包主体）
├── test/link-test.mjs      # 外部链路自测（不启动 Harness 也能跑）
├── pipeline/               # **随包分发的流水线**（装上即用，无需本机另有流水线）
│   ├── src/                #   Python 流水线源码
│   ├── config/             #   规则配置（YAML 源 + 派生件 config.json）
│   └── VERSION             #   流水线版本戳（与上游同步生成）
└── docs/DEVELOPMENT.md     # 本文件
```

`node_modules/`、`dist/`（内部发布输出）**不进仓库**，见 `.gitignore`。

---

## 二、工作原理

```
Harness 对话框
   └─ 模型调用 generate_weekly_report({ materials: [...] })
        └─ 插件 execute()
             ├─ 逐项判定：文件路径？还是素材原文？
             │    · 路径类 → 直接进 --input
             │    · 原文类 → 落盘为临时 .md 再进 --input
             ├─ 解析流水线根目录（见下）
             ├─ execFile(python, ['-m','src.pipeline','--input',...,'--out',...], { cwd: projectRoot })
             └─ 抓取 stdout 的 Markdown → 作为工具结果返回
```

### 流水线根目录解析（四级，v1.3.0 起）

| 顺序 | 来源 | 说明 |
|---|---|---|
| ① | 插件配置 `projectRoot` | **显式配置**：给了就以此为准；不合法**直接报错**，不静默回落 |
| ② | 环境变量 `WEEKLY_REPORT_PROJECT_ROOT` | 同上，属显式配置 |
| ③ | **包内 `pipeline/`** | 随包分发；社区用户装上即用（默认走这一级） |
| ④ | 插件目录上两级 | 兼容早期"插件放在流水线项目里"的布局 |

判定"是不是流水线根"的标准：该目录下存在 `src/pipeline.py`。四级全落空时报错并**列出所有已探测路径**。

### 关键实现决策

| 决策 | 原因 |
|---|---|
| 用 `execFile` 而非 `exec` | 素材路径常含空格与中文；`exec` 走 shell 会引入引号转义与命令注入风险 |
| 注入 `PYTHONIOENCODING=utf-8` | Windows 下 Python 默认 `cp936`，不强制 UTF-8 会让中文输出乱码 |
| 路径类入参**按形态判定**，不存在即报错 | 若把不存在的路径悄悄当文本，用户会拿到一份"把路径字符串当素材"的假周报——静默失败比报错危险 |
| 错误一律 `throw`，不吞 | 失败时 dsh 把 `Error: <message>` 原样交给模型，满足"错误原文返回，不许编造" |
| 具名导出、**无 default export** | dsh 官方约定：`export default` 会让 Loader 的 `unwrapExports` 丢掉 `inject` |
| Python 解释器**不硬编码路径** | 包内不得携带任何机器相关路径；改为 配置 → 环境变量 → PATH 命令名 三级解析 |
| 版本**运行时读取**，不写死常量 | `package.json`（插件）与 `pipeline/VERSION`（流水线）是唯一真源，避免两处漂移 |

---

## 三、本地开发与自测

```bash
npm install
npm run build          # tsc → lib/
node test/link-test.mjs
```

`test/link-test.mjs` 模拟一个最小 `ctx`（只提供 `tools.register`），加载**真实产物** `lib/index.js`，
取出 `generate_weekly_report` 直接调用其 `execute`，因此验证的就是 Harness 里跑的那份代码。覆盖：

- 注册形状与 schema（工具名、必填项、`output.render` / `output.schema`）；
- 端到端链路：文件路径素材、纯文本素材（落盘）、不存在的路径（错误透传）；
- 流水线根解析四级顺序、显式配置非法即报错、插件与流水线版本可读。

解释器可用 `WEEKLY_REPORT_PYTHON=<python>` 指定；未设置时按 PATH 探测 `python` / `python3` / `py`。

---

## 四、加载到 Harness

### 方式 A（推荐）：profile 用户补丁层

编辑 `%USERPROFILE%\.dsh\profiles\web\cordis.patch.yml`，追加：

```yaml
- insert:
    - id: weekly-report
      name: 'file:///<repo>/lib/index.js'
      disabled: false
      config:
        pythonPath: 'python'   # 绝对路径或 PATH 上的命令名；可省略走自动探测
        # projectRoot 可省略：默认使用包内 pipeline/
```

> `name` 用 `file:///` + 正斜杠形式的绝对路径。
> 校验配置解析：`dsh web --dump-config | Select-String -Context 2,6 "weekly-report"`。

### 方式 B：作为 bundle 安装进 profile

```bash
dsh plugin --profile web add mimetics-dsh-weekly-report   # 或 github:mimetics-ai/mimetics-dsh-weekly-report
```

随后在 `cordis.patch.yml` 里以包名引用：

```yaml
- insert:
    - id: weekly-report
      name: 'mimetics-dsh-weekly-report'
```

### 启动与验证

```bash
dsh web --port 3080        # 终端打印 http://127.0.0.1:3080/?token=... 即启动成功
```

启动时会打印一行版本确认（无需任何环境变量）：

```
[weekly-report] generate_weekly_report 已注册 | plugin v1.3.0 | pipeline v1.1.2
```

设置 `WEEKLY_REPORT_DEBUG=1` 可额外打印流水线根目录与解释器的解析结果。

---

## 五、工具入参

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `materials` | `string[]` | ✅ | 素材列表。每项可以是**文件绝对路径**（`.md`/`.txt`/`.csv`/`.xlsx`），也可以是**素材原文文本**，两者可混用 |
| `outPath` | `string` | ✖ | 额外输出路径，把 Markdown 另存一份（目录自动创建） |

**路径 / 原文的判定规则**：含换行 → 原文；以盘符、UNC、`/`、`./`、`../` 开头 → 按路径处理（**不存在则报错**）；
形如 `note.md` 的纯文件名 → 相对当前工作目录解析；其余 → 原文，落盘为临时 `.md`。

---

## 六、配置项

| 配置键 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `pythonPath` | `WEEKLY_REPORT_PYTHON` | 依次探测 PATH 上的 `python` / `python3` / `py` | Python 解释器：绝对路径**或**命令名 |
| `projectRoot` | `WEEKLY_REPORT_PROJECT_ROOT` | **包内 `pipeline/`** | 流水线根目录（须含 `src/pipeline.py`）；显式给出时必须合法 |
| `timeoutMs` | — | `120000` | 单次 Python 调用超时 |
| — | `WEEKLY_REPORT_DEBUG` | 未设置 | 设为 `1` 打印路径解析详情 |

---

## 七、排障

| 现象 | 原因与处理 |
|---|---|
| `projectRoot 配置无效：…` | 显式配置指向的目录不含 `src/pipeline.py`；改用正确路径，或**删掉该配置**回落到包内 `pipeline/` |
| `未找到周报流水线，已探测：…` | 包内 `pipeline/` 缺失（安装包不完整）；按报错列出的路径逐个确认 |
| `未找到可用的 Python 解释器` | 显式配置 `pythonPath` 或设置 `WEEKLY_REPORT_PYTHON`；插件只按 PATH 探测命令名 |
| `Python 解释器不可用：…` | `pythonPath` 给了绝对路径但文件不在，或该命令名不在 PATH 上 |
| 报 `素材文件不存在` | **预期行为**：路径类入参若不存在直接失败，而不是当成文本产出假周报 |
| 中文输出乱码 | 不应出现（已强制 `PYTHONIOENCODING=utf-8`）；若出现请检查是否被替换成了 `exec` |
| 启动日志没有 `[weekly-report]` | 插件未加载：用 `dsh web --dump-config` 确认 profile 里有对应条目 |

---

## 八、维护者说明

- **`pipeline/` 是上游流水线的随包快照**：上游开发仓库变更后，由内部同步脚本重新生成
  `pipeline/{src,config}` 与 `pipeline/VERSION`（逐文件哈希校验，防止"改了上游忘了同步"）。
- **版本联动规则**：`pipeline/` 逻辑变更 → 本包**补丁版至少 +1**（见 `CHANGELOG.md` 顶部规则）。
- **发布前自查**：`npm pack --dry-run` 复核包内容；确认仓库内无机器相关路径与内部台账类文档。
