# -*- coding: utf-8 -*-
"""第 2 步：合并同类项。

合并信号（权重来自 extract.yaml → dedup.match_by）：
  ticket 完全一致（强）> 共享交付物名（中）> 文本相似度（弱）
合并策略 richest：保留信息量最全的表述（数值最多 > 带工单号 > 带状态）。
合并后不同素材状态不一致 → 交给 classify.finalize 触发 R7。

状态护栏（dedup.status_guard，缺陷 D2 的修复）：
  · 强标识（工单号一致）= 同一实体 → 照常合并，状态矛盾按 R7 暴露；
  · 弱信号（交付物/相似度）= 可能只是"同话题" → 两条状态明确不同时**不合并**，
    避免造出"伪状态冲突"把条目从〖进行中〗〖本周完成〗〖下周计划〗里抽走。

合并后的收尾（2026-10-13，缺陷 D7-1 的修复）：
  · 代表条目之外的**文本**会被丢弃 → 补一条**诊断**说明"未采用文本"，只进 warnings/notices，
    **绝不写进正文**（正文取最全表述，这是 `richest` 的既定语义）。
  · ⚠️ 本模块是**事实合并层**：`_union_metrics` 如实保留各成员的数值，不做取舍/区间化。
    "同一对象多个取值怎么呈现"属展示层（`extract.resolve_metric_conflicts`，缺陷 D7-2）。
  · ⚠️ `dedup.merge_policy` / `dedup.keep_identifiers` 目前**是本模块未读取的死配置**：
    实现恒用 `_richness` 选代表。改 YAML 不会改变行为（Harness 侧核查结论，2026-10-13 已复核）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import classify
from .model import Item, Metric
from .textutil import containment, jaccard, ngrams



def _match_cfg(cfg) -> Dict[str, Any]:
    spec = cfg.get("dedup.match_by") or []
    out: Dict[str, Any] = {}
    for entry in spec:
        out[entry.get("id")] = entry
    return out


def _deliverable_tokens(item: Item) -> Set[str]:
    toks = set()
    for m in item.metrics:
        if m.type == "deliverable":
            toks.add(m.value)
    return toks


def _similarity(a: Item, b: Item, n: int) -> Tuple[float, float]:
    ga, gb = ngrams(a.text, n), ngrams(b.text, n)
    return jaccard(ga, gb), containment(ga, gb)


def _identity_of(a: Item, b: Item) -> Optional[str]:
    """两条事项共有的**强标识**（目前只有工单号完全一致）→ 返回信号 id。

    强标识意味着"这就是同一个实体"：此时两条状态不同 = **真实**冲突（R7），
    必须合并后暴露，而不是当成误合拦下。弱信号（相似度/交付物）则不然（见 _status_guard）。
    """
    if a.ticket and b.ticket and a.ticket == b.ticket:
        return "ticket"
    return None


def _should_merge(a: Item, b: Item, sim_cfg: Dict[str, Any]) -> Optional[str]:
    """返回合并依据（字符串）；不合并返回 None。

    铁律 R8：宁可漏合，绝不误合 —— 每条代价都有可复述的依据（工单号/交付物/相似度），
    不存在"没有信号也合一下"的兜底路径。
    """
    # 1) 工单号完全一致 —— 最强信号，无条件合并
    if _identity_of(a, b):
        return "工单号一致(%s)" % a.ticket

    n = int(sim_cfg.get("ngram", 2))
    jac, cont = _similarity(a, b, n)
    threshold = float(sim_cfg.get("threshold", 0.72))
    cont_threshold = float(sim_cfg.get("containment_threshold", 0.55))

    # 2) 共享交付物名（仍需相似度背书，避免"看板"这类泛词把不相干的事并起来）
    shared = _deliverable_tokens(a) & _deliverable_tokens(b)
    if shared and (cont >= cont_threshold or jac >= threshold):
        return "共享交付物(%s)+相似度(jac=%.2f,cont=%.2f)" % (",".join(sorted(shared)), jac, cont)

    # 3) Jaccard
    if jac >= threshold:
        return "文本相似度(jac=%.2f≥%.2f)" % (jac, threshold)
    # 4) 重叠系数（短事项被长事项覆盖）
    if cont >= cont_threshold:
        return "重叠系数(cont=%.2f≥%.2f)" % (cont, cont_threshold)
    return None


def _status_guard(signal: Optional[str], status_a: Optional[str], status_b: Optional[str],
                  guard_cfg: Dict[str, Any]) -> Optional[str]:
    """状态护栏（缺陷 D2）：返回"应拦下的原因"；允许合并返回 None。

    背景：两条素材都含同一件事的名词（如「风控规则配置」），但状态一个 in_progress、
    一个 completed —— 这多半是**两件不同的事**（一件在做、一件已完成），而不是同一件的冲突。
    弱信号（相似度/重叠/共享交付物）下强行合并，会造出**伪状态冲突**：
    条目被从〖进行中〗/〖本周完成〗里抽走、塞进〖风险与阻塞〗，还会连带把
    〖下周计划〗挤成"待确认"。按 R8「宁可漏合，绝不误合」，此时**不合并、保留两条**。

    例外：强标识（工单号一致）说明确实是同一实体，状态矛盾是 R7 的真实冲突，
    必须合并后暴露 → 由 guard_cfg.exempt_signals 放行。
    """
    if not guard_cfg or not guard_cfg.get("enabled", True):
        return None
    if not status_a or not status_b or status_a == status_b:
        return None            # 状态未知 → 无证据表明是两件事，照常合并
    if signal and signal in (guard_cfg.get("exempt_signals") or []):
        return None
    return "状态不同(%s vs %s)" % (status_a, status_b)


def _brief(item: Item, limit: int = 24) -> str:
    t = item.text
    return t if len(t) <= limit else t[:limit] + "…"


def _richness(item: Item) -> float:
    score = len(item.metrics) * 5.0
    score += 3.0 if item.ticket else 0.0
    score += 2.0 * len(set(item.hints or []))
    score += min(len(item.text), 80) / 80.0
    return score


def _notice(warn, msg: str) -> None:
    """把判定依据写进常规诊断（R8：每一次判定都要能复述依据）。

    只进 warnings / notices（stderr 诊断折叠块）——**绝不写进正文**。
    """
    if warn is not None:
        warn("[info] " + msg)


def _union_metrics(items: Sequence[Item], cfg) -> List[Metric]:
    """合并各成员的数值（**事实层**）：按 (type, value) 去重后如实并集。

    ⚠️ 这里刻意**不做任何取舍/区间化**——抽取结果是事实，应如实保留；
    "同一对象多个取值怎么呈现"属于**展示层**的事（见 `extract.resolve_metric_conflicts`，缺陷 D7-2）。
    """
    seen = set()
    out: List[Metric] = []
    for it in items:
        for m in it.metrics:
            key = (m.type, m.value)
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
    return out


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def run(items: List[Item], cfg, warn, audit: Optional[List[Dict[str, Any]]] = None) -> List[Item]:
    """合并后返回新的事项列表（保持原有顺序）。

    audit（可选）：每次合并写入一条记录（依据 + 参与原文），供 --audit 复核；
    这既是排障手段，也是 R8"宁可漏合"的监督通道。
    """
    if not items:
        return []
    if not cfg.get("dedup.enabled", True):
        return items

    mc = _match_cfg(cfg)
    sim_cfg = mc.get("similarity") or {}
    guard_cfg = cfg.get("dedup.status_guard") or {}

    # 每条事项先算一次状态：合并前的状态是"两条是不是同一件事"的关键证据
    # （分类链与最终判定完全一致：同一 route_hint / hints 输入）。
    pre_status = [classify.detect(it.text, it.hints, cfg, getattr(it, "route_hint", None)).status
                  for it in items]

    # 先把所有"该合并"的对找出来，并记下依据
    pairs: List[Tuple[int, int, str]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            reason = _should_merge(items[i], items[j], sim_cfg)
            if reason:
                pairs.append((i, j, reason))

    uf = _UF(len(items))
    for i, j, reason in pairs:
        blocked = _status_guard(_identity_of(items[i], items[j]),
                                pre_status[i], pre_status[j], guard_cfg)
        if blocked:
            # R8 的对称面：拒绝合并同样要留依据（默认进诊断信息，可用 --audit 复核上下文）
            if warn is not None:
                warn("状态护栏：不合并「%s」与「%s」（%s）" % (_brief(items[i]), _brief(items[j]), blocked))
            continue
        uf.union(i, j)

    groups: Dict[int, List[int]] = {}
    for idx in range(len(items)):
        groups.setdefault(uf.find(idx), []).append(idx)

    merged: List[Item] = []
    for root in sorted(groups.keys(), key=lambda r: min(groups[r])):
        idxs = groups[root]
        members = [items[k] for k in idxs]
        if len(members) == 1:
            merged.append(members[0])
            continue

        reasons = [r for (i, j, r) in pairs if uf.find(i) == root]
        reason_text = "; ".join(dict.fromkeys(reasons))
        base = max(members, key=_richness)
        members_sorted = members  # idxs 已按升序收集
        statuses = [pre_status[k] for k in idxs if pre_status[k]]

        # D7-1：代表条目之外的**文本**会被丢弃（数值会并入并集、状态进 member_statuses）。
        #       按修复指引**只补诊断通道**——把被丢掉的文本说出来，
        #       绝不改写正文（正文不得新增任何提示字样，否则 D4 的 R2 自检会报违规）。
        dropped = [m for m in members_sorted if m is not base]
        if dropped:
            _notice(warn, "合并取最全表述：保留「%s」；未采用文本：%s（依据：%s）"
                    % (_brief(base, 60),
                       "；".join(_brief(m, 60) for m in dropped),
                       reason_text))

        item = Item(
            text=base.text,
            source=base.source,
            source_file=base.source_file,
            ts=base.ts,
            ticket=base.ticket or next((m.ticket for m in members if m.ticket), None),
            hints=[h for m in members_sorted for h in (m.hints or [])],
            hint_origin=[h for m in members_sorted for h in (m.hint_origin or [])],
            # 合并后仍沿用"最全那条"的结构性路由（弱信号跨状态已被护栏拦下，
            # 走到这里的成员状态一致或互不冲突）
            route_hint=base.route_hint,
            # 事实层：按 (type, value) 去重后**如实并集**，不在这里做任何取舍/区间化
            #（D7-2 的呈现策略在 `extract.resolve_metric_conflicts`）
            metrics=_union_metrics(members_sorted, cfg),
            member_statuses=list(dict.fromkeys(statuses)),
            order=min(m.order for m in members),
            merged_from=["%s:%s" % (m.source, _brief(m)) for m in members_sorted],
            merge_reason=reason_text,
        )
        merged.append(item)

        if audit is not None:
            audit.append({
                "group": len(merged),
                "reason": item.merge_reason,
                "picked": "%s:%s" % (base.source, _brief(base)),
                "members": item.merged_from,
            })
    return merged
