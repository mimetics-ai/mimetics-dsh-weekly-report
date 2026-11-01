-- 匿名调用统计的事件表。
-- 纪律：不存 IP、不存 User-Agent 原文、不存任何可定位到个人的字段。
-- 唯一可用于归并的标识是客户端本机随机生成的 instance（SHA-256 前 16 位）。
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at INTEGER NOT NULL,          -- 服务端接收时间（毫秒）
  schema      INTEGER NOT NULL,          -- 协议版本
  instance    TEXT    NOT NULL,          -- 匿名实例 ID（16 位 hex）
  event       TEXT    NOT NULL,          -- install | call
  version     TEXT,                      -- 插件版本
  outcome     TEXT,                      -- ok | gate | error（仅 call）
  duration_ms INTEGER,                   -- 端到端耗时（仅 call）
  materials   INTEGER,                   -- 素材条数（仅 call）
  error_kind  TEXT,                      -- 失败归类（仅 call）
  node        TEXT,                      -- Node 主版本号
  os          TEXT                       -- 平台（win32 / darwin / linux）
);

CREATE INDEX IF NOT EXISTS idx_events_received ON events (received_at);
CREATE INDEX IF NOT EXISTS idx_events_event    ON events (event);
CREATE INDEX IF NOT EXISTS idx_events_instance ON events (instance);
