# -*- coding: utf-8 -*-
"""第 5 步：渲染四模块 + 输出前自检。

硬约束（来自 config/sections.yaml 与 skill.config.yaml）：
  模块数量/标题/顺序固定（R5）；**空模块**才保留标题 + 占位（R2/R3）；
  无计划只写"待确认"（R4）；一事不重复（R6）。

⚠️ 缺陷 D4 的定论（2026-10-13）：占位符**只允许出现在"模块完全为空"时**，
   真实条目**绝不追加任何占位符**——否则每条风险/阻塞后面都挂着占位符，
   汇报直接被污染。因此本模块没有任何"给条目补尾标"的代码路径；
   缺数据的诊断改走 `data_gap_notices()`，只进 warnings（stderr / 诊断折叠块），不进正文。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from . import extract
from .model import Item
from .textutil import section_route_table, split_inline_label

_LINE = re.compile(r"^\s*-\s+\S")

# 诊断用"有数据"判据：只有这些**数值型**指标才算数据支撑（可在 extract.yaml 覆盖）。
# deliverable（交付物名）等非数值指标不算——否则"提到一个文档名"就算"有数据"。
_DEFAULT_NUMERIC_TYPES = ("percent", "date", "count", "money", "duration", "version", "ticket")


def numeric_types(cfg) -> Set[str]:
    types = cfg.get("extraction.numeric_types")
    if isinstance(types, (list, tuple)) and types:
        return set(types)
    return set(_DEFAULT_NUMERIC_TYPES)


def has_numeric_data(item: Item, cfg) -> bool:
    """该事项是否带数值型量化数据（判据用抽取结果，不是展示结果）。"""
    numeric = numeric_types(cfg)
    return any(m.type in numeric for m in (item.metrics or []))


def section_items(items: List[Item], section: Dict[str, Any]) -> List[Item]:
    """按 accept_status 把事项分派到唯一模块（结构性保证 R6：一事不重复）。"""
    want = set(section.get("accept_status") or [])
    return [it for it in items if it.status in want]


def _brief(item: Item) -> str:
    text = item.text or ""
    return text if len(text) <= 30 else text[:30] + "…"


def data_gap_notices(items: List[Item], cfg) -> List[str]:
    """R2 诊断：模块要求数据支撑、而其中的条目没有数值型数据时给出的提示。

    ⚠️ **只进 warnings（stderr / 诊断折叠块），绝不写进正文** —— 见缺陷 D4 的定论。
    开关是 `sections.yaml → sections[].require_metrics`（按模块配置，不是全局动词清单）。
    """
    out: List[str] = []
    for sec in cfg.sections:
        if not sec.get("require_metrics"):
            continue
        gaps = [it for it in section_items(items, sec) if not has_numeric_data(it, cfg)]
        if gaps:
            out.append("R2 %s 有 %d 条缺数值支撑（仅提示，不写入正文）：%s"
                       % (sec.get("title", sec["id"]), len(gaps),
                          "；".join(_brief(it) for it in gaps[:3])))
    return out


def _keep_metric(m, text: str) -> bool:
    """数值噪声过滤（只影响"是否重复展示"，不影响"是否有数据"的判断）。
    1) 该数值已出现在正文里 → 不再在句尾重复一遍
    2) 不含数字的相对日期词（"下周""本周"）→ 不是量化数据
    """
    if m.value in text:
        return False
    if not any(ch.isdigit() for ch in m.value):
        return False
    return True


def format_item(item: Item, cfg,
                route_table: Optional[List[Tuple[str, str]]] = None,
                warn=None) -> str:
    """生成一行（必要时压缩）。绝不在文本里补任何素材没有的数据/占位符。"""
    prefix = cfg.out("line_prefix", "- ")
    sep = cfg.get("render.separator", "，")
    max_chars = int(cfg.get("compress.max_chars_per_item", 120))

    # 行内标签（`下周计划：…`）只在输出时剥离；分类/合并阶段仍按完整原文判定
    table = route_table if route_table is not None else section_route_table(cfg.sections)
    body = split_inline_label(item.text, table)[1]

    if item.conflict and item.conflict_note:
        if getattr(item, "conflict_kind", "") == "metric":
            guard = (cfg.get("classify.metric_conflict_guard")
                     or cfg.get("metric_conflict_guard") or {})
            conflict_marker = guard.get("marker") or "⚠️ 数值冲突："
        else:
            r7 = (cfg.rules.get("R7") or {})
            conflict_marker = ((r7.get("detail") or {}).get("conflict_marker")) or "⚠️ 状态冲突："
        body = "%s%s%s%s" % (conflict_marker, item.conflict_note, sep, body)

    # 缺陷 D7-2（**展示层**）：同一对象的多个取值先在呈现层消解（去重/区间化），
    # 必要时就地把正文里的旧取值换掉，避免"句中旧值 + 句尾新值"那种病句。
    # ⚠️ 只动呈现：抽取事实（item.metrics）与合并分组都不在这里改。
    body, metrics = extract.resolve_metric_conflicts(item.metrics, body, cfg, warn,
                                                     conflict=item.conflict)
    metrics = extract.limit_metrics([m for m in metrics if _keep_metric(m, body)], cfg)
    suffix = extract.render_suffix(metrics, cfg)

    # ⚠️ 这里刻意**不追加任何占位符**：有内容的模块里每条都必须是真实内容。
    # 缺数据只走 data_gap_notices() 的诊断通道。
    line = prefix + body + suffix
    if len(line) <= max_chars:
        return line

    # 压缩：绝不删数值 —— 从正文尾部截断，数值后缀保留
    keep = len(prefix) + len(suffix)
    room = max(0, max_chars - keep - 1)
    if room <= 0:
        return prefix + body[: max(1, max_chars - len(prefix) - 1)] + "…" + suffix
    return prefix + body[:room].rstrip() + "…" + suffix


def build(items: List[Item], cfg, warnings: Optional[List[str]] = None) -> str:
    """渲染完整周报（Markdown 字符串）。"""
    sections = cfg.sections
    blocks: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    table = section_route_table(sections)      # 一次算好，避免逐条重复构建

    for sec in sections:
        sid = sec["id"]
        titles[sid] = sec.get("title", sid)
        picked = section_items(items, sec)
        if not picked:
            blocks[sid] = sec.get("empty_item", "- 待确认")
            continue
        lines = [format_item(it, cfg, table,
                             warnings.append if warnings is not None else None)
                 for it in picked]
        blocks[sid] = "\n".join(lines)

    # 下周计划：无条目 → 只写"待确认"，绝不推导（铁律 R4）
    plan_sec = next((s for s in sections if s["id"] == "next_plan"), None)
    if plan_sec is not None and not section_items(items, plan_sec):
        plan = cfg.inp("missing_plan_placeholder", "待确认")
        blocks["next_plan"] = cfg.out("line_prefix", "- ") + plan

    tpl = cfg.template_text
    out = tpl
    for sec in sections:
        sid = sec["id"]
        out = out.replace("{{%s.title}}" % sid, titles[sid])
        out = out.replace("{{%s.items}}" % sid, blocks[sid])

    # 模板缺占位符时兜底：直接按 sections 顺序拼装（模块数量仍固定）
    if "{{" in out:
        parts = []
        for sec in sections:
            parts.append(sec.get("title", sec["id"]))
            parts.append(blocks[sec["id"]])
        out = "\n".join(parts)

    return out.rstrip() + "\n"


def _placeholder_bodies(cfg, sections) -> Set[str]:
    """占位文案集合（去行首标记），用于把"占位符重复"排除出 R6 判定。

    占位符来自配置：缺数据占位 / 缺计划占位 / 各模块的 empty_item。
    """
    texts = {str(cfg.inp("missing_data_placeholder", "（待补充）")),
             str(cfg.inp("missing_plan_placeholder", "待确认"))}
    for sec in (sections or []):
        item = str(sec.get("empty_item") or "")
        if item:
            texts.add(item)
    top = str(cfg.get("empty_item") or "")
    if top:
        texts.add(top)
    out: Set[str] = set()
    for t in texts:
        t = t.strip()
        if t.startswith("- "):
            t = t[2:].strip()
        if t:
            out.add(t)
    return out


def selfcheck(report: str, items: List[Item], cfg) -> List[str]:
    """输出前自检，返回违规列表（空 = 通过）。不展示给用户。"""
    bad: List[str] = []
    sections = cfg.sections
    lines = report.splitlines()
    max_lines = int(cfg.out("max_lines_per_item", 2))

    # 1. 模块数量/顺序/标题
    found = [ln.strip() for ln in lines if ln.strip().startswith("〖")]
    expect = [s.get("title") for s in sections]
    if found != expect:
        bad.append("R5 模块不匹配：期望 %s，实际 %s" % (expect, found))

    # 2. 每个模块下必须有 "- " 条目
    cur = None
    counts: Dict[str, int] = {}
    for ln in lines:
        s = ln.strip()
        if s.startswith("〖") and s.endswith("〗"):
            cur = s
            counts[cur] = 0
        elif cur and _LINE.match(ln):
            counts[cur] = counts.get(cur, 0) + 1
    for title in expect:
        if counts.get(title, 0) == 0:
            bad.append("R3 模块无条目：%s" % title)

    # 2b. **有内容的模块里不得出现占位符**（缺陷 D4）：占位符只允许用于空模块。
    #     "有内容" = 该模块至少有一条非占位符的条目；空模块本身只有占位符（合法）。
    placeholders = _placeholder_bodies(cfg, sections)
    module_bodies: Dict[str, List[str]] = {}
    cur = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("〖") and s.endswith("〗"):
            cur = s
            module_bodies.setdefault(cur, [])
            continue
        if cur and _LINE.match(ln):
            module_bodies[cur].append(re.sub(r"^-\s+", "", s))
    for title, bodies in module_bodies.items():
        real = [b for b in bodies if b not in placeholders]
        if real and len(real) != len(bodies):
            bad.append("R2 有内容的模块出现占位符：%s" % title)

    # 3. 单条不超过 max_lines_per_item（本实现一行一条，做冗余校验）
    if any("\n\n" in ln for ln in lines):
        bad.append("格式：出现空行分隔")
    if max_lines < 1:
        bad.append("配置异常：max_lines_per_item < 1")

    # 4. 一事不重复（R6）：正文互不重复
    #    注意：占位符（空模块统一写「- 待确认」，见缺陷 D6 方案 B）天然会在多个模块同时出现，
    #    那不是"一事重复"，必须排除——否则空模块一多就自检失败（缺陷：自然冲突）。
    seen: Dict[str, str] = {}
    cur = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("〖"):
            cur = s
            continue
        if not _LINE.match(ln):
            continue
        body = re.sub(r"^-\s+", "", s)
        body = re.sub(r"^(?:⚠️ 状态冲突：[^，]+，)", "", body)
        if body in placeholders:
            continue
        if body in seen and seen[body] != cur:
            bad.append("R6 跨模块重复：%s" % body[:30])
        seen[body] = cur or ""

    # 5. 计划模块不得凭空生成（R4）
    plan_title = next((s["title"] for s in sections if s["id"] == "next_plan"), None)
    if plan_title:
        idx = found.index(plan_title) if plan_title in found else -1
        if idx >= 0:
            body_lines = []
            for ln in lines[lines.index(plan_title) + 1:]:
                if ln.strip().startswith("〖"):
                    break
                if _LINE.match(ln):
                    body_lines.append(ln.strip())
            if not body_lines:
                bad.append("R4 计划模块为空")
    return bad
