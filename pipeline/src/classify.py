# -*- coding: utf-8 -*-
"""第 3 步：状态分类。

判定链（顺序即权威顺序）：
  0. route_hint    —— 分节标题 / 行内标签这类**结构性强路由**信号：用户已经把这条
                      归进某个模块了，直接按它路由（唯一例外：blocked 永不被压制）
  1. future_gate   —— 出现「下周/待办/打算/拟」等显式计划线索 → 直接 planned
  2. blocked 关键词 —— 永远最高优先（用户要求：冲突一律归"受阻/待确认"）
  3. verb_state_guard —— 过滤"完成/上线/通过"这类动词误判（工单标题重灾区）
  4. precedence    —— 同一事项多状态命中时按 classify.yaml 的 precedence 取一条
  5. 跨来源冲突     —— 合并后不同素材判定不一致 → R7 归入〖风险与阻塞〗
无任何命中 → unknown，按 unknown_policy 处理（不进四模块，避免撑出第五模块）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .extract import metric_conflicts
from .model import Item
from .textutil import clause_bounds, strip_leading_ticket


@dataclass
class Verdict:
    status: Optional[str] = None
    conflict: bool = False
    note: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class Hit:
    status: str
    keyword: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


# --------------------------------------------------------------------------
def _status_map(cfg) -> Dict[str, Dict[str, Any]]:
    return cfg.status_spec or {}


def _precedence(cfg) -> List[str]:
    return cfg.get("classification.precedence", []) or []


def _hits(text: str, cfg) -> List[Hit]:
    out: List[Hit] = []
    for status_id, spec in _status_map(cfg).items():
        for kw in (spec.get("keywords") or []):
            for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
                out.append(Hit(status=status_id, keyword=kw, start=m.start(), end=m.end()))
    return out


def _uncertain_near(text: str, hit: Hit, ignores: Sequence[str], window: int = 6) -> bool:
    """语气词在命中前 window 字内 → 该命中不算状态（防"应该完成了"）。"""
    ctx = text[max(0, hit.start - window):hit.start]
    return any(w in ctx for w in ignores)


def _guard_bare_verb(text: str, hit: Hit, guard: Dict[str, Any]) -> bool:
    """返回 True = 该动词命中有效（确认是状态描述）。"""
    if not guard or not guard.get("enabled", True):
        return True
    verbs = guard.get("verbs") or []
    if hit.keyword not in verbs:
        return True  # 只约束"光杆动词"（完成/上线/…），"已完成"这类不受约束

    # 右边紧跟这些字 → 是名词化的动词（"通过率 98%"）
    follow = guard.get("follow_stop_chars") or []
    if hit.end < len(text) and text[hit.end] in follow:
        return False

    # 左边紧邻否定/进行态 → 不是完成态
    left_neg = guard.get("left_negators") or []
    left_ctx = text[max(0, hit.start - 3):hit.start]
    if any(tok in left_ctx for tok in left_neg):
        return False

    c_start, c_end = clause_bounds(text, hit.start)
    clause = text[c_start:c_end]
    clause_pre = text[c_start:hit.start]

    # a) 同一子句内、动词前有完成态标记
    if any(mark in clause_pre for mark in (guard.get("markers") or [])):
        return True

    # b) 动词正好结束该子句，且子句前段没有未来/期限线索
    if guard.get("clause_end_is_state", True):
        if hit.end >= c_end or (hit.end < len(text) and text[hit.end] in "，,。；;：:！!？?"):
            window = int(guard.get("future_marker_window", 8))
            pre = text[max(0, hit.start - window):hit.start]
            futures = guard.get("future_markers") or []
            if not any(tok in pre for tok in futures):
                return True

    # c) 动词位于子句起始（工作日志里的"修复登录崩溃问题"），且整条无未来线索
    stripped_start = len(text) - len(text[c_start:].lstrip())
    if hit.start <= c_start + stripped_start:
        futures = guard.get("future_markers") or []
        if not any(tok in text for tok in futures):
            return True

    return False


def _resolve_hits(hits: List[Hit], cfg) -> List[Hit]:
    """命中重叠消解：longest_win —— '部分完成' 命中后内部的 '完成' 不再计数。"""
    if not hits:
        return []
    policy = cfg.get("classification.hit_resolution", "longest_win")
    if policy != "longest_win":
        return sorted(hits, key=lambda h: h.start)
    ordered = sorted(hits, key=lambda h: (-h.length, h.start))
    accepted: List[Hit] = []
    for h in ordered:
        if any(not (h.end <= a.start or h.start >= a.end) for a in accepted):
            continue
        accepted.append(h)
    return sorted(accepted, key=lambda h: h.start)


def _pick(statuses: Sequence[str], cfg) -> Optional[str]:
    order = _precedence(cfg)
    for sid in order:
        if sid in statuses:
            return sid
    return statuses[0] if statuses else None


# --------------------------------------------------------------------------
def detect(text: str, hints: Sequence[str], cfg,
           route: Optional[str] = None) -> Verdict:
    """单来源判定：只吃一条文本 + 该来源自带状态（+ 可选的结构性强路由状态）。

    route：来自分节标题 / 行内标签（`## 进行中`、`下周计划：…`）。
           它是**结构性**信号——用户主动把事项放进了这个模块，因此优先于关键词判定；
           唯一例外是 blocked：受阻信息永不被压制（与 future_gate 同一原则）。
    """
    body = strip_leading_ticket((text or "").strip())
    verdict = Verdict()

    gate = cfg.get("classify.future_gate", None) or cfg.get("future_gate") or {}
    gate_on = gate.get("enabled", True)
    strong = [c for c in (gate.get("strong_cues") or []) if c in body]
    suppress = set(gate.get("suppress") or [])

    ignores = cfg.get("classification.ignore_patterns") or []
    guard = cfg.get("verb_state_guard") or {}

    valid: List[Hit] = []
    for h in _hits(body, cfg):
        if _uncertain_near(body, h, ignores):
            continue
        if not _guard_bare_verb(body, h, guard):
            continue
        valid.append(h)

    resolved = _resolve_hits(valid, cfg)
    statuses = [h.status for h in resolved]

    if gate_on and strong:
        # 显式计划线索：压制 completed / in_progress，blocked 永不被压制
        kept = [s for s in statuses if s not in suppress]
        if kept != statuses:
            verdict.tags.append("future_gate")
        if not kept:
            return _merge_with_hints(Verdict(status="planned", tags=verdict.tags), hints)
        statuses = kept

    if len(set(statuses)) > 1:
        # 同一事项多状态命中：按 precedence 取一条（不判冲突，冲突只发生在跨来源）
        picked = _pick(statuses, cfg)
        verdict.tags.append("multi_match:" + "/".join(sorted(set(statuses))))
        verdict.status = picked
    elif statuses:
        verdict.status = statuses[0]

    # 强路由：分节标题 / 行内标签决定模块归属，优先于关键词判定；
    # 但 blocked 关键词是例外（受阻信息必须暴露，不被任何信号压制）。
    if route and (route == "blocked" or verdict.status != "blocked"):
        verdict.status = route
        verdict.tags.append("route:%s" % route)

    return _merge_with_hints(verdict, hints)


def _merge_with_hints(verdict: Verdict, hints: Sequence[str]) -> Verdict:
    """素材自带状态（Jira Status / 纪要分节）与关键词判定合并。"""
    uniq = [h for h in dict.fromkeys(hints or []) if h]
    if len(uniq) > 1:
        verdict.conflict = True
        verdict.note = "素材自带状态不一致（%s）" % "/".join(uniq)
        verdict.status = "blocked"
        return verdict
    if uniq:
        hint = uniq[0]
        if verdict.status and verdict.status != hint:
            verdict.conflict = True
            verdict.note = "关键词判定=%s，素材状态=%s" % (verdict.status, hint)
            verdict.status = "blocked"
            return verdict
        verdict.status = hint
    return verdict


def finalize(item: Item, cfg) -> Verdict:
    """合并后事项的最终判定：并入各成员各自的判定，出现不一致即 R7 冲突。"""
    member = [s for s in dict.fromkeys(item.member_statuses or []) if s]
    own = detect(item.text, item.hints, cfg, getattr(item, "route_hint", None))

    if own.conflict:
        # 同一来源内部（关键词 vs 素材状态）已判定冲突
        own.tags = own.tags + ["R7"]
        return own

    candidates = [s for s in dict.fromkeys(member + ([own.status] if own.status else [])) if s]
    if len(candidates) > 1:
        note = "跨素材判定不一致（%s）" % "/".join(candidates)
        return Verdict(status="blocked", conflict=True, note=note, tags=own.tags + ["R7"])
    # 数值冲突护栏（v1.1.2，用户 2026-10-13 拍板）：
    #   同一事项合并后，各来源对**同一对象**报了不同数值（"覆盖 3 条规则" / "覆盖 5 条规则"）→
    #   复用 R7 链路：写明两个取值 + 归入〖风险与阻塞〗。**不改合并分组、不改状态判定逻辑**。
    # 配置是**扁平合并命名空间**：classify.yaml 的顶层键即顶层键，与 verb_state_guard / future_gate 同约定
    guard = cfg.get("classify.metric_conflict_guard") or cfg.get("metric_conflict_guard") or {}
    if guard.get("enabled", True):
        clashes = metric_conflicts(item.metrics)
        if clashes:
            detail = "、".join(" / ".join(vals) for _t, _tail, vals in clashes)
            tpl = guard.get("note_template") or "跨素材数值不一致（%s）"
            return Verdict(status="blocked", conflict=True, note=tpl % detail,
                           tags=own.tags + ["R7", "metric_conflict"])

    if candidates:
        return Verdict(status=candidates[0], tags=own.tags)
    return Verdict(status=None, tags=own.tags)
