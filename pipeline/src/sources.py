# -*- coding: utf-8 -*-
"""第 1 步：读取素材 —— 按 config/sources.yaml 选适配器，切成"事项"。

硬约束：只读用户显式传入的路径（tests/ 内脱敏样本亦然），不做任何目录漫游。
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .model import Item, Metric
from .textutil import (clean_line, first_nonempty_line, is_garbled, match_route,
                       section_route_table, split_inline_label)

ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030")


def _route_table(cfg):
    """分节标签 → 状态 的路由表（来自 sections.yaml 的 title + heading_aliases）。"""
    return section_route_table(cfg.sections)


def _inline_route(body: str, table):
    """行首行内标签（`下周计划：…`）→ 状态；无标签 → None。仅用于路由，不改正文。"""
    status, _rest = split_inline_label(body, table)
    return status


def _bare_marker(line: str, table):
    """整行**只是**一个模块标记（裸行 `·进行中` / `- 风险` / `〖下周计划〗`）→ 返回其状态。

    缺陷 D5：模块标记必须**只作路由信号被消费**，绝不进入内容池——
    否则会输出一条 `- 进行中` / `- 风险` 的假条目，把汇报污染掉。
    注意：`- 已完成：数据看板优化` 这类"标签 + 正文"**不是**裸标记
    （归一化后不等于别名），仍按正常条目处理，只在输出时剥离标签。
    """
    text = (line or "").strip()
    if not text:
        return None
    return match_route(clean_line(text), table)


def read_text(path: Path) -> str:
    for enc in ENCODINGS:
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _header_hit(header: str, candidates) -> bool:
    """表头里是否出现该字段的任一候选列名（忽略大小写）。

    "$" 开头的占位符（notes 的 `$line`、log 的 `$ts`）不算候选名。
    正则串（meeting 的分节正则）也不会命中表头，天然被排除。
    """
    h = (header or "").lower()
    if not h:
        return False
    if isinstance(candidates, str):
        candidates = [candidates]
    for cand in (candidates or []):
        key = str(cand).strip().lower()
        if len(key) >= 2 and not key.startswith("$") and key in h:
            return True
    return False


def _detect(path: Path, cfg, header: str, sample: str = "") -> Dict[str, Any]:
    """按 ext / 文件名线索 / 表头线索 / **字段匹配度** / 内容特征 给适配器打分，取最高分。"""
    best: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)
    ext = path.suffix.lower()
    stem = path.stem.lower()
    for spec in cfg.sources:
        det = spec.get("detect") or {}
        score = 0.0
        if ext and ext in (det.get("ext") or []):
            score += 1.0
        for hint in (det.get("filename_hint") or []):
            if str(hint).lower() in stem:
                score += 3.0
        for hint in (det.get("header_hints") or []):
            if header and str(hint).lower() in header.lower():
                score += 4.0
        # 字段匹配度：命中该适配器 map 候选列名的字段数量。
        # 这是表头语义最强的信号，用于区分"Jira 导出"与"Excel 另存为的 CSV"：
        # 中文表头（工单号/事项/状态/负责人/截止日期）应归 xlsx 适配器，而不是 jira。
        if header:
            hit = sum(1 for cands in (spec.get("map") or {}).values()
                      if _header_hit(header, cands))
            score += 2.0 * hit
        # 内容特征：聊天的"发言人+时间戳"形态是强信号
        sp = det.get("speaker_line_regex")
        if sp and sample and re.search(sp, sample, re.MULTILINE):
            score += 4.0
        # 内容特征：会议纪要的分节标题
        for hint in (det.get("section_hints") or []):
            if sample and str(hint) in sample:
                score += 2.0
        if score > best[0]:
            best = (score, spec)
    if best[1] is None:
        # 兜底适配器由配置声明（input.default_source），代码不按 id 硬判
        fallback = cfg.inp("default_source", None)
        for spec in cfg.sources:
            if fallback and spec["id"] == fallback:
                return spec
        return cfg.sources[0]
    return best[1]


def _pick(row: Dict[str, Any], candidates) -> Optional[str]:
    for name in (candidates or []):
        if name in row and str(row[name]).strip():
            return str(row[name]).strip()
    return None


# --------------------------------------------------------------------------
def _split_notes(text: str, spec, name: str, start: int, table=None) -> List[Item]:
    """工作记录 / 待办：逐行成条；分节标题与裸模块标记作为**强路由**信号下传。"""
    items: List[Item] = []
    cur_route: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            # 标题行本身不是事项：只更新"当前分节的路由状态"。
            # 认不出的标题 → 清空路由（避免跨节串味）。
            cur_route = match_route(line, table)
            continue
        bare = _bare_marker(line, table)
        if bare:
            # 裸模块标记（`·进行中` / `- 风险` / `〖下周计划〗`）：只作路由，不进内容池（D5）
            cur_route = bare
            continue
        body = clean_line(line)
        if not body or body.startswith("#"):
            continue
        route = _inline_route(body, table) or cur_route
        items.append(Item(text=body, source=spec["id"], source_file=name,
                          route_hint=route, order=start + len(items)))
    return items


def _split_chat(text: str, spec, name: str, start: int, table=None) -> List[Item]:
    det = spec.get("detect") or {}
    sp_rx = re.compile(det["speaker_line_regex"]) if det.get("speaker_line_regex") else None
    in_rx = re.compile(det["inline_speaker_regex"]) if det.get("inline_speaker_regex") else None

    turns: List[Item] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if sp_rx:
            m = sp_rx.match(line)
            if m:
                ts = m.groupdict().get("ts")
                turns.append(Item(text="", source=spec["id"], source_file=name, ts=ts))
                continue
        if in_rx:
            m = in_rx.match(line)
            if m:
                turns.append(Item(text=(m.groupdict().get("text") or "").strip(),
                                  source=spec["id"], source_file=name))
                continue
        if turns:
            turns[-1].text = (turns[-1].text + " " + line).strip()
        else:
            turns.append(Item(text=line, source=spec["id"], source_file=name))

    noise = set(spec.get("noise_filters") or [])
    keep_rx = re.compile(r"[A-Z][A-Z0-9]{1,9}-\d{1,6}|\d")
    out: List[Item] = []
    cur_route: Optional[str] = None
    for t in turns:
        t.text = clean_line(t.text)
        if not t.text:
            continue
        bare = _bare_marker(t.text, table)
        if bare:
            # 聊天里单独成条的模块标记：只作路由（D5），不输出为条目
            cur_route = bare
            continue
        if t.text in noise and not keep_rx.search(t.text):
            continue
        t.route_hint = _inline_route(t.text, table) or cur_route
        out.append(t)
        t.order = start + len(out)
    return out


def _read_table_rows(path: Path) -> List[List[str]]:
    """读表格式素材（.csv/.tsv）为行列表，分隔符自动嗅探（与 xlsx_reader 一致）。"""
    from . import xlsx_reader
    text = read_text(path)
    delim = xlsx_reader._sniff_delimiter(text[:4096], path.suffix.lower())
    return [row for row in csv.reader(io.StringIO(text, newline=""), delimiter=delim)]


def _split_jira(path: Path, spec, name: str, start: int) -> List[Item]:
    """表格式素材（Jira 导出 / 通用表格 CSV）。字段名全部来自配置 map，不硬编码。"""
    mp = spec.get("map") or {}
    status_map = spec.get("status_map") or {}
    items: List[Item] = []
    rows = _read_table_rows(path)
    if not rows:
        return items
    header = [(c or "").strip() for c in rows[0]]
    for row in rows[1:]:
        row = list(row)
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        record = {header[i]: row[i] for i in range(len(header)) if header[i]}
        if not any(str(v).strip() for v in record.values()):
            continue
        title = _pick(record, mp.get("title")) or ""
        ticket = _pick(record, mp.get("ticket"))
        status = _pick(record, mp.get("status"))
        due = _pick(record, mp.get("duedate"))
        comment = _pick(record, mp.get("comment"))
        if not title and not ticket:
            continue
        # 截止日期作为"量化数据"挂到事项上，而不是拼进正文
        # —— 拼进去会污染相似度计算（"截止日期"四个字会拉低重复项的重叠度）
        metrics: List[Metric] = []
        if due:
            metrics.append(Metric(type="date", label="截止日期", value=due, start=0, end=0, priority=85))
        body = title
        if comment:
            body = "%s %s" % (body, comment)
        hint = status_map.get(status, None) if status else None
        items.append(Item(
            text=body,
            source=spec["id"],
            source_file=name,
            ticket=ticket,
            hints=[hint] if hint else [],
            hint_origin=["%s:status=%s" % (spec["id"], status)] if hint else [],
            metrics=metrics,
            order=start + len(items),
        ))
    return items


def _split_meeting(text: str, spec, name: str, start: int, table=None) -> List[Item]:
    mp = spec.get("map") or {}
    hints = spec.get("section_status_hint") or {}
    patterns = {kind: re.compile(rx) for kind, rx in mp.items() if isinstance(rx, str)}
    kinds = [k for k in ("decision", "action", "risk", "attendee") if k in patterns]

    items: List[Item] = []
    cur_kind = None
    cur_route: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            cur_kind = None
            for k in kinds:
                if patterns[k].search(heading):
                    cur_kind = k
                    break
            # 分节标题 = 强路由：先看纪要自己的分节语义表，认不出再回落到通用分节标签表
            cur_route = hints.get(cur_kind) if cur_kind else None
            if cur_route is None:
                cur_route = match_route(heading, table)
            continue
        bare = _bare_marker(line, table)
        if bare:
            # 裸模块标记（`·进行中` / `- 风险`）同样只作路由，不进内容池（D5）
            cur_route = bare
            continue
        body = clean_line(line)
        if not body or body.startswith("#"):
            continue
        if cur_kind == "attendee":
            continue
        route = _inline_route(body, table) or cur_route
        items.append(Item(
            text=body,
            source=spec["id"],
            source_file=name,
            route_hint=route,
            hint_origin=["meeting:%s" % cur_kind] if cur_kind else [],
            order=start + len(items),
        ))
    return items


def _split_log(text: str, spec, name: str, start: int, table=None) -> List[Item]:
    items: List[Item] = []
    cur_route: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            cur_route = match_route(line, table)
            continue
        bare = _bare_marker(line, table)
        if bare:
            cur_route = bare       # 裸模块标记只作路由（D5）
            continue
        body = clean_line(raw)
        if not body:
            continue
        route = _inline_route(body, table) or cur_route
        items.append(Item(text=body, source=spec["id"], source_file=name,
                          route_hint=route, order=start + len(items)))
    return items


_SPLITTERS: Dict[str, Callable] = {
    "notes": _split_notes,
    "chat": _split_chat,
    "meeting": _split_meeting,
    "log": _split_log,
}


def _split_xlsx(path: Path, spec, name: str, start: int, warn, notice) -> List[Item]:
    """Excel（.xlsx/.xlsm/.xls）：读所有 sheet 的**值**，按表头映射成事项。

    - 只取值：公式取缓存值，无缓存 → 该格为空并跳过（不计算、不猜）；
    - 图表/图片天然不在值域内；
    - 单 sheet / 单元格异常 → 跳过并提示，不中断整份周报；
    - 引擎缺失 → 明确提示缺哪个库，绝不静默返回空数据（避免"假装支持"）。
    """
    from . import xlsx_reader

    opts = spec.get("read") or {}
    mp = spec.get("map") or {}
    status_map = spec.get("status_map") or {}
    fallback = spec.get("fallback") or {}
    sep = fallback.get("separator", "，")

    try:
        sheets = xlsx_reader.read_sheets(path, opts, warn)
    except xlsx_reader.EngineMissing as exc:
        # 去依赖版：不再是"缺 pip 包"，而是"标准库读不了该格式" → 提示必须是可执行的动作
        warn("无法读取 %s：%s" % (name, exc))
        return []
    except Exception as exc:
        warn("表格读取失败，已跳过 %s：%s" % (name, exc))
        return []

    items: List[Item] = []
    read_any = False
    for sheet in sheets:
        col = {kind: _header_index(sheet.header, candidates) for kind, candidates in mp.items()}
        for row in sheet.rows:
            body, hint, ticket, metrics = _row_to_item(row, col, status_map, sep, fallback)
            if not body:
                continue
            read_any = True
            items.append(Item(
                text=body,
                source=spec["id"],
                source_file="%s#%s" % (name, sheet.sheet),
                ticket=ticket,
                hints=[hint] if hint else [],
                hint_origin=["xlsx:%s!status=%s" % (sheet.sheet, hint)] if hint else [],
                metrics=metrics,
                order=start + len(items),
            ))
        for skipped in sheet.skipped:
            warn("表格 %s#%s 跳过一项：%s" % (name, sheet.sheet, skipped))

    if read_any:
        notice(spec.get("notice") or "已读取表格文本数据")
    return items


def _header_index(header: List[str], candidates) -> Optional[Tuple[int, str]]:
    """表头匹配：先精确（忽略大小写/空格），再退一步"表头包含候选词"。
    返回 (列序号, 素材里的原始表头文字) —— 标签用素材自己的词，不自己造词。
    """
    cells = [(i, (c or "").strip()) for i, c in enumerate(header or [])]
    for cand in (candidates or []):
        key = str(cand).strip().lower()
        for i, cell in cells:
            if cell and cell.lower() == key:
                return i, cell
    for cand in (candidates or []):
        key = str(cand).strip().lower()
        if len(key) < 2:
            continue
        for i, cell in cells:
            if cell and key in cell.lower():
                return i, cell
    return None


def _cell(row: List[str], col: Optional[Tuple[int, str]]) -> str:
    if col is None or col[0] >= len(row):
        return ""
    return row[col[0]].strip()


def _label(col: Optional[Tuple[int, str]], default: str) -> str:
    return col[1] if col else default


def _row_to_item(row: List[str], col: Dict[str, Optional[Tuple[int, str]]],
                 status_map: Dict[str, str], sep: str, fallback: Dict[str, Any]):
    """一行 → (正文, 状态提示, 工单号, 附加量化数据)。

    正文只由素材单元格内容 + 素材自己的表头文字构成，不引入任何外部措辞。
    """
    ticket = _cell(row, col.get("ticket"))
    title = _cell(row, col.get("title"))
    status = _cell(row, col.get("status"))
    owner = _cell(row, col.get("owner"))
    due = _cell(row, col.get("duedate"))
    note = _cell(row, col.get("note"))

    parts = [p for p in (title, note) if p]
    if not parts:
        if not fallback.get("join_all", True):
            return "", None, None, []
        parts = [c for c in row if c]          # 表头不可识别 → 整行拼接（内容仍全部来自素材）
    if not parts:
        return "", None, None, []

    body = sep.join(parts)
    if owner and owner not in body:
        body = "%s%s%s %s" % (body, sep, _label(col.get("owner"), "负责人"), owner)
    hint = status_map.get(status) if status else None
    metrics = []
    if due:
        metrics.append(Metric(type="date", label=_label(col.get("duedate"), "截止日期"),
                              value=due, start=0, end=0, priority=85))
    return body, hint, ticket or None, metrics


def collect(paths: List[Path], cfg, warn, notice=None) -> List[Item]:
    """读取全部素材，返回事项列表（order 全局递增，保持素材内原顺序）。

    notice：正向提示回调（如"已读取表格文本数据"），只进 stderr，不进四模块。
    """
    notice = notice or (lambda _msg: None)
    table = _route_table(cfg)
    items: List[Item] = []
    for path in paths:
        name = path.name
        if not path.exists() or not path.is_file():
            warn("素材不存在，跳过：%s" % path)
            continue

        text = ""
        if path.suffix.lower() not in (".xlsx", ".xlsm", ".xls"):
            text = read_text(path)
            if is_garbled(text):
                warn("疑似乱码，跳过：%s" % name)
                continue

        # 二进制表格按扩展名直连 xlsx 适配器：不要走文本探测（否则会被 CSV 分支误吃）
        if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
            spec = next((s for s in cfg.sources if s.get("parser") == "xlsx"), None)
            if spec is None:
                warn("配置里没有声明 xlsx 适配器，已跳过 %s" % name)
                continue
            items.extend(_split_xlsx(path, spec, name, len(items), warn, notice))
            continue

        header = first_nonempty_line(text)
        spec = _detect(path, cfg, header, text[:800])

        if spec.get("parser") == "table":
            items.extend(_split_jira(path, spec, name, len(items)))
            continue
        if spec.get("parser") == "xlsx":
            items.extend(_split_xlsx(path, spec, name, len(items), warn, notice))
            continue

        splitter = _SPLITTERS.get(spec["id"], _split_notes)
        items.extend(splitter(text, spec, name, len(items), table))
    return items
