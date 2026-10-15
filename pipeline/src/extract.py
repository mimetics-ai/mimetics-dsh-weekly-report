# -*- coding: utf-8 -*-
"""第 4 步：量化数据抽取（含边界校验）。

铁律 R1：只搬运素材原文里出现的片段，绝不换算/估算。
重难点：8 类模式互相误报 —— 本模块用两层防护：
  L1 边界校验（boundary）：左右邻字符 / 上下文 / 数字头部长度等
  L2 重叠消解（priority_win）：同一段文字被多模式命中时，priority 高者胜

误报样例（都已覆盖并有单测）：
  '10:30:15'      → 不是工单号（左侧是冒号/数字、前后是时间形态）
  '3.5万元'       → 不是版本号（右侧紧邻货币单位）
  '2026.10'       → 不是版本号（数字头部 4 位且无 v 前缀）
  '通过率 98%'    → '通过' 不是完成态（follow_stop_chars 命中 '率'）
  '耗时 10 天'    → 不是普通数量（duration priority 更高，覆盖 count）
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from .model import Item, Metric

# 数值片段拆解：'3 条规则' → ('3', ' ', '条规则')；'2026-10-20' → ('2026', '', '-10-20')
# 单独捕获"数字与单位之间的空白"，否则合成区间会写成 '3~5条规则'（与原文风格不一致）
_NUM_SPLIT = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)(\s*)(.*)$", re.S)
_DEFAULT_RANGE_TYPES = ("count", "percent", "money", "duration")


def _compile(cfg) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for spec in cfg.patterns:
        rx = spec.get("regex")
        if not rx:
            continue
        out.append({
            "id": spec["id"],
            "label": spec.get("label", spec["id"]),
            "priority": int(spec.get("priority", 0)),
            "regex": re.compile(rx),
            "suffixes": spec.get("suffixes") or [],
        })
    return out


# --------------------------------------------------------------------------
# L1 边界校验
# --------------------------------------------------------------------------
def _adj_ok(text: str, s: int, e: int) -> bool:
    """通用：命中片段左右紧邻数字 → 判定为从更大数字里切出来的。"""
    if s > 0 and text[s - 1].isdigit():
        return False
    if e < len(text) and text[e].isdigit():
        return False
    return True


def _valid_ticket(text: str, s: int, e: int, val: str, bc: Dict[str, Any]) -> bool:
    prefix = val.split("-")[0]
    if len(prefix) < int(bc.get("min_prefix_len", 2)):
        return False
    if prefix in (bc.get("deny_prefixes") or []):
        return False
    if s > 0 and text[s - 1] in (bc.get("deny_left_chars") or ""):
        return False
    right_time = bc.get("deny_right_time_regex")
    if right_time and re.match(right_time, text[e:e + 3]):
        return False
    ctx = text[max(0, s - 24):s]
    for token in (bc.get("deny_context") or []):
        if token in ctx:
            return False
    return True


def _valid_version(text: str, s: int, e: int, val: str, bc: Dict[str, Any]) -> bool:
    if s > 0:
        prev = text[s - 1]
        if prev.isdigit() or prev in (bc.get("deny_left_chars") or []):
            return False
    if e < len(text):
        nxt = text[e]
        if nxt.isdigit() or nxt in (bc.get("deny_right_chars") or []):
            return False
    head = val.lstrip("vV").split(".")[0]
    need = bc.get("require_v_prefix_when") or {}
    gate = int(need.get("head_digits_ge", 99))
    if len(head) >= gate and val[:1] not in "vV":
        return False
    return True


def _valid_money(text: str, s: int, e: int, val: str, bc: Dict[str, Any]) -> bool:
    if bc.get("deny_right_digit") and e < len(text) and text[e].isdigit():
        return False
    return True


def _valid_count(text: str, s: int, e: int, val: str, bc: Dict[str, Any]) -> bool:
    if bc.get("deny_left_digit") and s > 0 and text[s - 1].isdigit():
        return False
    if s > 0 and text[s - 1] in (bc.get("deny_left_chars") or []):
        return False
    if e < len(text) and text[e] in (bc.get("deny_right_chars") or []):
        return False
    return True


def _valid_percent(text: str, s: int, e: int, val: str, bc: Dict[str, Any]) -> bool:
    return _valid_count(text, s, e, val, bc)


def _valid_date(text: str, s: int, e: int, val: str, bc: Dict[str, Any]) -> bool:
    if bc.get("deny_left_digit") and s > 0 and text[s - 1].isdigit():
        return False
    if e < len(text) and text[e].isdigit():
        return False
    if val[:1].isdigit() and bc.get("numeric_require_full"):
        if not re.fullmatch(r"(?:\d{4}[-/年])?\d{1,2}[-/月]\d{1,2}日?", val):
            return False
    return True


def _valid_deliverable(text: str, s: int, e: int, val: str, bc: Dict[str, Any],
                       suffixes: List[str]) -> bool:
    if s > 0 and text[s - 1].isdigit():
        return False
    tail = re.sub(r"\s?(?:v?\d+\.\d+(?:\.\d+)?)$", "", val).strip()
    name = tail
    for suf in suffixes:
        if tail.endswith(suf):
            name = tail[: -len(suf)]
            break
    if bc.get("deny_exact_suffix") and not name:
        return False
    return len(name) >= int(bc.get("min_name_len", 2))


_VALIDATORS = {
    "ticket": _valid_ticket,
    "version": _valid_version,
    "money": _valid_money,
    "count": _valid_count,
    "percent": _valid_percent,
    "date": _valid_date,
}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def find_metrics(text: str, cfg) -> List[Metric]:
    """返回消解后的量化数据列表（按出现顺序）。"""
    if not text or not cfg.get("extraction.enabled", True):
        return []
    boundary_on = bool(cfg.get("extraction.boundary_check", True))
    bc_all: Dict[str, Any] = cfg.get("boundary", {}) or {}

    candidates: List[Metric] = []
    generic_adj = bool(cfg.get("boundary.reject_if_adjacent_digit", True))
    for spec in _compile(cfg):
        pid, label, priority = spec["id"], spec["label"], spec["priority"]
        bc = (bc_all.get(pid) or {})
        # 自带 deny_*_digit 的模式已做更细的邻接判断，不重复套用通用规则
        own_digit_rule = ("deny_left_digit" in bc) or ("deny_right_digit" in bc)
        for m in spec["regex"].finditer(text):
            s, e = m.start(), m.end()
            val = m.group(0)
            if boundary_on:
                if generic_adj and not own_digit_rule and not _adj_ok(text, s, e):
                    continue
                validator = _VALIDATORS.get(pid)
                if validator and not validator(text, s, e, val, bc):
                    continue
                if pid == "deliverable" and not _valid_deliverable(text, s, e, val, bc, spec["suffixes"]):
                    continue
            candidates.append(Metric(type=pid, label=label, value=val, start=s, end=e, priority=priority))

    return _resolve_overlaps(candidates, cfg)


def _resolve_overlaps(cands: List[Metric], cfg) -> List[Metric]:
    """按 priority 降序贪心保留互不重叠的命中（L2 防护）。"""
    if not cands:
        return []
    policy = cfg.get("extraction.overlap_policy", "priority_win")
    if policy != "priority_win":
        return sorted(cands, key=lambda m: m.start)

    ordered = sorted(cands, key=lambda m: (-m.priority, -(m.end - m.start), m.start))
    accepted: List[Metric] = []
    for cand in ordered:
        if any(not (cand.end <= a.start or cand.start >= a.end) for a in accepted):
            continue
        accepted.append(cand)
    return sorted(accepted, key=lambda m: m.start)


def limit_metrics(metrics: List[Metric], cfg) -> List[Metric]:
    """按 priority 截断到 max_metrics_per_item。"""
    cap = int(cfg.get("extraction.max_metrics_per_item",
                      cfg.get("render.max_metrics_per_item", 3)))
    if len(metrics) <= cap:
        return metrics
    ordered = sorted(metrics, key=lambda m: (-m.priority, m.start))[:cap]
    return sorted(ordered, key=lambda m: m.start)


def render_suffix(metrics: List[Metric], cfg) -> str:
    """把数值拼成"-"后的后缀串。value 原样输出。"""
    if not metrics or cfg.get("render.placement", "suffix") != "suffix":
        return ""
    sep = cfg.get("render.separator", "，")
    dedupe = bool(cfg.get("render.dedupe", True))
    values: List[str] = []
    for m in metrics:
        if dedupe and m.value in values:
            continue
        values.append(m.value)
    return sep + sep.join(values) if values else ""


# ---------------------------------------------------------------------------
# 展示层：同一"对象"出现多个取值时的消解（缺陷 D7-2）
#
# 机制（修复前）：合并时数值取并集 → 渲染时把"正文里没有的那个"追加到句尾 →
#   `完成风控规则配置，覆盖 3 条规则` + `，5 条规则` = 「覆盖 3 条规则，5 条规则」（病句）。
# 修法（本函数，**只在展示层**）：同一对象的多个取值按配置去重/区间化；
#   ⚠️ 抽取结果（`dedup._union_metrics`）**保持原样并存两条事实**——这里是"怎么呈现"，
#      不是"事实取舍"，所以不违反 R1，也不触碰合并分组（R8 的语义资产）。
# ---------------------------------------------------------------------------
def _num_parts(value: str) -> Optional[Tuple[float, str]]:
    """把量化片段拆成 (数值, 尾部单位/对象名)。非数值返回 None。"""
    m = _NUM_SPLIT.match(value or "")
    if not m:
        return None
    return float(m.group(1)), m.group(3).strip()


def _raw_tail(value: str) -> str:
    """尾部原文（**保留数字与单位之间的空白**）：'3 条规则' → ' 条规则'。"""
    m = _NUM_SPLIT.match(value or "")
    return (m.group(2) + m.group(3)) if m else ""


def _metric_group_key(m: Metric) -> Tuple[str, str]:
    """判定"是不是同一个对象的取值"的键：**同类型 + 同数值尾部（单位/对象名）**。

    为什么不用 `Metric.label`：label 只是模式类目（"数量"/"日期/节点"/"金额"…），
    不是语义槽位；拿它做键会把 `3 个核心指标` 与 `2 个边界用例` 当成同一对象的两个取值，
    凭空造出 `2~3 个…` —— 那是编造数据（违反 R1）。
    `3 条规则` 与 `5 条规则` 的尾部都是 `条规则` → 同一对象的两个取值。
    """
    parts = _num_parts(m.value)
    return (m.type, parts[1] if parts else m.value)


def metric_conflicts(metrics: List[Metric]) -> List[Tuple[str, str, List[str]]]:
    """列出"同一对象出现多个取值"的冲突组：`[(type, 尾部, [取值…]), …]`。

    供**数值冲突护栏**（`classify.finalize`，v1.1.2）与展示层消解共用同一判定口径，
    避免两处各写一套。
    """
    order: List[Tuple[str, str]] = []
    groups: Dict[Tuple[str, str], List[str]] = {}
    for m in metrics or []:
        key = _metric_group_key(m)
        if key not in groups:
            groups[key] = []
            order.append(key)
        if m.value not in groups[key]:
            groups[key].append(m.value)
    return [(k[0], k[1], groups[k]) for k in order if len(groups[k]) > 1]


def _resolve_value_conflict(mtype: str, values: List[str], cfg, warn,
                            tail: str, prefer: Optional[str]) -> str:
    """同一对象多个取值 → 按 `render.metric_conflict_policy` 消解（展示层）。

    range —— 合成区间（需类型在 `render.range_types` 内、且各取值尾部一致）；
    max   —— 取数值最大者；
    first —— 取"正文里已有的那个"（即代表条目的取值），不引入任何推导（最保守）。
    """
    policy = str(cfg.get("render.metric_conflict_policy", "range") or "range").lower()
    joined = " / ".join(values)
    fallback = prefer or values[0]

    if policy == "range":
        types = [str(t) for t in (cfg.get("render.range_types") or list(_DEFAULT_RANGE_TYPES))]
        parts = [_num_parts(v) for v in values]
        if mtype in types and all(parts) and len({p[1] for p in parts if p}) == 1:
            nums = [p[0] for p in parts if p]
            if min(nums) != max(nums):
                out = "%s%s%s%s" % ("%g" % min(nums), cfg.get("render.range_separator", "~"),
                                    "%g" % max(nums), tail)
                if warn is not None:
                    warn("[render] 同一项数值不一致（%s）→ 展示层按 range 合成区间「%s」" % (joined, out))
                return out
            if warn is not None:
                warn("[render] 同一项数值一致但写法不同（%s）→ 取「%s」" % (joined, fallback))
            return fallback
        if warn is not None:
            warn("[render] 同一项数值不一致（%s）→ 类型 %s 不在 range_types 或单位不一致，"
                 "退化为取「%s」" % (joined, mtype, fallback))
        return fallback

    if policy == "max":
        got = [v for v in values if _num_parts(v)]
        best = max(got, key=lambda v: _num_parts(v)[0]) if got else fallback   # type: ignore[index]
        if warn is not None:
            warn("[render] 同一项数值不一致（%s）→ 展示层按 max 取「%s」" % (joined, best))
        return best

    if warn is not None:
        warn("[render] 同一项数值不一致（%s）→ 展示层按 first 取「%s」" % (joined, fallback))
    return fallback


def _keep_in_text_only(metrics: List[Metric], text: str) -> Tuple[str, List[Metric]]:
    """数值冲突条目（已判 R7、要归入风险）的呈现：**不合成任何形态**。

    对"同一对象多个取值"的组，只保留正文里已有的那个取值（代表条目的），丢弃其余——
    取值差异由护栏写在 `⚠️ …（3 条规则 / 5 条规则）` 告警里。
    **唯一取值的其它指标照常保留**（不能因为一处冲突就把其它数据也丢了）。
    """
    groups: Dict[Tuple[str, str], List[str]] = {}
    for m in metrics or []:
        groups.setdefault(_metric_group_key(m), [])
        if m.value not in groups[_metric_group_key(m)]:
            groups[_metric_group_key(m)].append(m.value)
    out: List[Metric] = []
    for m in metrics or []:
        vals = groups[_metric_group_key(m)]
        if len(vals) == 1 or m.value in text:
            out.append(m)
    return text, out


def resolve_metric_conflicts(metrics: List[Metric], text: str, cfg,
                             warn=None, conflict: bool = False) -> Tuple[str, List[Metric]]:
    """展示层消解：返回 `(可能被就地改写的正文, 仍需作为句尾后缀的指标)`。

    对每个"同一对象"的取值组：
      · 只有一个取值 → 原样保留；
      · 多个取值 → 按策略消解成一个。若该形态已在正文里 → 不再补后缀；
                   否则若正文里是**旧的**那个取值 → 就地把正文里的它换掉（避免"句中旧值 + 句尾新值"）；
                   正文里完全没有这个对象 → 作为后缀呈现消解后的那一个。

    `conflict=True`（该条目已被**数值冲突护栏**判为 R7 冲突、要归入风险模块）：
    此时**不做任何合成**，只保留正文里已有的那个取值、丢弃其余——
    取值差异由护栏写在 `⚠️ …（3 条规则 / 5 条规则）` 告警里，不再由展示层拼成区间。
    """
    if conflict:
        return _keep_in_text_only(metrics, text)
    order: List[Tuple[str, str]] = []
    groups: Dict[Tuple[str, str], List[Metric]] = {}
    for m in metrics:
        key = _metric_group_key(m)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    out: List[Metric] = []
    for key in order:
        entries = groups[key]
        values: List[str] = []
        for m in entries:
            if m.value not in values:
                values.append(m.value)
        if len(values) == 1:
            out.append(entries[0])
            continue

        mtype, tail = key
        in_text = [v for v in values if v in text]
        raw_tail = _raw_tail(entries[0].value) if _num_parts(entries[0].value) else tail
        resolved = _resolve_value_conflict(mtype, values, cfg, warn, raw_tail,
                                          in_text[0] if in_text else None)
        if resolved in text:
            continue                                  # 正文已是该形态，无需后缀
        if in_text:
            text = text.replace(in_text[0], resolved, 1)   # 就地替换旧取值
            continue
        pick = next((m for m in entries if m.value == resolved), None)
        out.append(pick if pick is not None else replace(entries[0], value=resolved))
    return text, out


def attach_metrics(item: Item, cfg) -> Item:
    """抽取并挂到事项上（同时回填 ticket 作为合并标识）。

    注意：保留适配器已挂上的结构化数据（如 Jira 截止日期），只做追加去重。
    """
    found = limit_metrics(find_metrics(item.text, cfg), cfg)
    merged: List[Metric] = []
    seen = set()
    for m in list(item.metrics) + list(found):
        key = (m.type, m.value)
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)
    item.metrics = limit_metrics(merged, cfg)
    for m in item.metrics:
        if m.type == "ticket":
            item.ticket = m.value
            break
    return item
