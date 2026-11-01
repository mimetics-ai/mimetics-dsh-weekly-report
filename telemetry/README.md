# 匿名调用统计（可选，默认关闭）

回答一个问题：**这个插件装了多少次、被调用多少次、失败在哪一步、哪个版本在跑。**

插件本身在你本机执行，任何第三方（包括 GitHub 与 npm）都看不到「调用」这件事——它们只能看到 clone 和下载。所以要做产品判断，只能由插件自己上报一条极简事件。

## 一、采集边界

| 上报 | 不上报 |
|---|---|
| 插件版本号 | 素材内容（一个字符都不传） |
| 结局：`ok` / `gate`（无素材时的正常提示）/ `error` | 文件路径、文件名 |
| 端到端耗时（毫秒，取整） | 用户名、机器名、主机名 |
| 素材条数（只有数量） | IP 地址（收集端也不记录） |
| 失败归类（如 `python_missing`、`timeout`） | 错误原文（可能含路径） |
| Node 主版本、操作系统 | 任何可用于跨站追踪的标识 |

**匿名实例 ID**：本机首次运行随机生成 UUID v4，取 SHA-256 前 16 位，存于 `~/.mimetics/weekly-report/instance-id`。它与账号、硬件指纹、IP 均无关联，唯一用途是把同一台机器的多次调用归并成「一个使用者」，从而算出活跃实例数与人均调用量。删除该文件即换新 ID。

**默认关闭**：未显式开启时不发任何请求，代码路径等同于空操作。开启后仍需收集端地址非空——两者是**「与」的关系**，只有开关没地址、或只有地址没开关，都不会发出一个请求。

## 二、部署收集端（约 10 分钟）

前提：Cloudflare 账号（免费）、本机 `npm i -g wrangler` 并 `wrangler login`。

```bash
cd telemetry/worker

# 1. 建库，把输出的 database_id 填进 wrangler.toml
wrangler d1 create mimetics-telemetry

# 2. 建表
wrangler d1 execute mimetics-telemetry --remote --file=./schema.sql

# 3. 设看板口令（长随机字符串，别用弱口令）
wrangler secret put STATS_KEY

# 4. 部署，记下输出的域名
wrangler deploy
```

部署后自检（把 `$BASE` 换成你的域名，`$KEY` 换成刚设的口令）：

```bash
curl -s -X POST "$BASE/v1/events" -H 'content-type: application/json' \
  -d '{"schema":1,"instance":"0123456789abcdef","event":"call","version":"test","outcome":"ok","duration_ms":100,"materials":1,"node":"22","os":"win32"}'
# 期望：{"ok":true}

curl -s "$BASE/stats?key=$KEY"     # 期望：totals.calls ≥ 1
open "$BASE/?key=$KEY"             # 看板
```

## 三、开启上报

默认关闭，需要**同时**给出「开关」与「地址」。三种写法，优先级从高到低：

1. 插件配置：`telemetryEnabled: true` + `telemetryUrl: "https://<name>.<subdomain>.workers.dev/v1/events"`
2. 环境变量：`WEEKLY_REPORT_TELEMETRY=1` + `WEEKLY_REPORT_TELEMETRY_URL=https://...`
3. 改源码常量 `src/telemetry.ts` 的 `DEFAULT_ENDPOINT` —— **维护者用这条**。注意仅填地址不会生效，
   还需把 `isTelemetryEnabled` 的默认返回值改为 `true`，并同步修改 README 的对外表述。

关闭上报：配置 `telemetryEnabled: false`，或环境变量 `WEEKLY_REPORT_TELEMETRY=0`。

调试：`WEEKLY_REPORT_DEBUG=1` 会把准备上报的内容打到 stderr（含完整 payload，便于核对没有夹带隐私字段）；若开关开着但地址为空，也会提示一行。

## 四、看板指标口径

| 指标 | 口径 | 别误读成 |
|---|---|---|
| 累计调用 | `event=call` 的行数 | 不等于用户数：一个人可能调 50 次 |
| 安装数 | `event=install` 的行数（每台机器只发一次） | 不等于活跃：装了不用的人也在里面 |
| 活跃实例（30 天） | 近 30 天有 `call` 的不同 instance 数 | 同一人多机算多个 |
| 成功率 | `(ok + gate) / calls` | `gate` 是「无素材」的正常业务提示，不算故障 |
| 平均耗时 | `AVG(duration_ms)` | 含 Python 进程启动开销，不是纯计算时间 |
| 失败归类 | `error_kind` 分布 | 单项 > 20% 就说明有一类环境问题在批量发生 |

看板的日序列按 **UTC 日**聚合（D1 内部即 UTC），与北京时间差 8 小时。

## 五、成本与容量

Cloudflare 免费额度：Worker 10 万请求/天，D1 500 万行读/天、10 万行写/天。

按当前量级（每日数十次调用），占用不到 0.1%。即使 1 万个活跃用户、每人每天调 1 次，也只到免费额度的 10%。

一行事件约 120 字节；10 万次调用约 12 MB，D1 免费 5 GB，够用很多年。真到瓶颈时按 `received_at` 归档即可。

## 六、关闭与清理

- 用户侧关闭：`WEEKLY_REPORT_TELEMETRY=0` 或配置 `telemetryEnabled: false`。
- 停止收集：`wrangler delete`（删 Worker）、`wrangler d1 delete mimetics-telemetry`（删库）。
- 单条数据删除：`wrangler d1 execute mimetics-telemetry --remote --command "DELETE FROM events WHERE instance='<id>'"`。

## 七、自测

```bash
npm run build
node test/telemetry-test.mjs            # 插件端：关闭零请求、install 收敛、错误不吞、慢收集端不阻塞
node telemetry/worker/test.mjs          # 收集端：字段校验、鉴权、聚合、看板
```

两个脚本都不出公网（收集端是本地假服务器 / 假 D1 绑定），可在 CI 里直接跑。
