# -*- coding: utf-8 -*-
"""文本工具：子句切分、相似度、清洗。"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

CLAUSE_DELIM = set("，,。；;：:！!？?\n\r、（）()【】[]|")

_TICKET_PREFIX = re.compile(r"^\s*(?:[A-Z][A-Z0-9]{1,9}-\d{1,6})\s*[:：\-—]?\s*")
_LEAD_MARKER = re.compile(r"^\s*(?:[-*•·\u2022>]+|\d+[.)、])\s*")
_LEAD_TS = re.compile(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\s*")


def clean_line(text: str) -> str:
    """去掉行首标记/时间戳/多余空白，保留原始语义与标点。"""
    t = (text or "").replace("\u3000", " ")
    t = _LEAD_MARKER.sub("", t)
    t = _LEAD_TS.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def strip_leading_ticket(text: str) -> str:
    return _TICKET_PREFIX.sub("", text or "").strip()


def clause_bounds(text: str, pos: int) -> Tuple[int, int]:
    """返回 pos 所在子句的 [start, end)。"""
    start = 0
    for i in range(pos - 1, -1, -1):
        if text[i] in CLAUSE_DELIM:
            start = i + 1
            break
    end = len(text)
    for i in range(pos, len(text)):
        if text[i] in CLAUSE_DELIM:
            end = i
            break
    return start, end


def normalize_for_similarity(text: str) -> str:
    """相似度用归一化：去空白标点与数字，仅留中英文。"""
    kept = [ch for ch in (text or "") if ch.isalnum() and not ch.isdigit()]
    return "".join(kept).lower()


def ngrams(text: str, n: int = 3) -> Set[str]:
    s = normalize_for_similarity(text)
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def containment(a: Iterable[str], b: Iterable[str]) -> float:
    """重叠系数：短串被长串包含的程度（短 vs 长重复场景比 Jaccard 更准）。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(min(len(sa), len(sb)))


def is_garbled(text: str) -> bool:
    """乱码率过高（替换字符/不可打印占比大）视为无法解析。"""
    if not text:
        return False
    bad = sum(1 for ch in text if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\n\r\t"))
    return bad / float(len(text)) > 0.5


def first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------------
# 分节标题 / 行内标签的**强路由**（缺陷 D1 / D3）
#
# 素材里常见两种"结构性信号"：
#   · 分节标题：`## 进行中`        → 其下所有事项都属于该模块
#   · 行内标签：`- 下周计划：完成风控规则配置` → 该条属于该模块，且标签不该出现在输出里
#
# 两者共用同一份标签表（由 sections.yaml 的 title + heading_aliases 构建），
# 因此这里不认识任何业务名词——别名全部来自配置。
# --------------------------------------------------------------------------
# 标题装饰/包裹符号（在两端反复剥离）
_HEAD_WRAP = "〖〗【】[]（）()《》<>「」『』“”\"'*#：: 　\t"
# 标题前缀序号："一、" / "(2)" / "3." / "第四部分"
_HEAD_NUM = re.compile(
    r"^(?:第[一二三四五六七八九十百\d]+[章节部分条项]?"
    r"|[（(]?[一二三四五六七八九十\d]+[)）]?[、.．:：])\s*")
# 行内标签："下周计划：正文" —— 冒号前最多 16 字
_INLINE_LABEL = re.compile(r"^(.{1,16}?)[：:]\s*(\S[\s\S]*)$")


def normalize_label(text: str) -> str:
    """把标题/标签归一化：去装饰符号、去序号、去空白，便于与别名精确比对。"""
    t = (text or "").strip()
    t = t.strip(_HEAD_WRAP)
    t = _HEAD_NUM.sub("", t).strip()
    t = t.strip(_HEAD_WRAP)
    t = re.sub(r"[\s\u3000]+", "", t)
    return t


def section_route_table(sections: Optional[Sequence[Dict]]) -> List[Tuple[str, str]]:
    """从 sections 配置构建 `(归一化别名, 状态)` 表，长别名优先（避免短别名抢先命中）。

    status 取 `route_status`，缺省回退到 `accept_status` 的第一个。
    别名来源：title（自动去掉 〖〗 等装饰）+ heading_aliases。
    """
    pairs: Dict[str, str] = {}
    for sec in (sections or []):
        if not isinstance(sec, dict):
            continue
        accept = sec.get("accept_status") or []
        status = sec.get("route_status") or (accept[0] if accept else None)
        if not status:
            continue
        names = [sec.get("title") or ""] + list(sec.get("heading_aliases") or [])
        for name in names:
            key = normalize_label(name)
            if len(key) >= 2:
                pairs.setdefault(key, status)
    return sorted(pairs.items(), key=lambda kv: -len(kv[0]))


def match_route(text: str, table: Optional[Sequence[Tuple[str, str]]]) -> Optional[str]:
    """整行/整标题是否恰好是一个已知分节标签 → 返回对应状态；否则 None。"""
    key = normalize_label(text)
    if not key:
        return None
    for alias, status in (table or []):
        if key == alias:
            return status
    return None


def split_inline_label(text: str,
                       table: Optional[Sequence[Tuple[str, str]]]
                       ) -> Tuple[Optional[str], str]:
    """行首行内标签 `下周计划：正文` → `(状态, 正文)`；无标签则 `(None, 原文)`。

    只剥离**已知标签**（配置里的别名），未知的 `备注：…` 原样保留。
    """
    raw = text or ""
    m = _INLINE_LABEL.match(raw.strip())
    if not m:
        return None, raw
    status = match_route(m.group(1), table)
    if not status:
        return None, raw
    return status, m.group(2).strip()
