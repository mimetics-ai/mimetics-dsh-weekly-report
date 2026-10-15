# -*- coding: utf-8 -*-
"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Metric:
    """一条量化数据。value 是素材原文片段，不做任何换算。"""
    type: str
    label: str
    value: str
    start: int
    end: int
    priority: int = 0


@dataclass
class Item:
    """一件事项。所有字段都来自素材原文或配置语义，不含推断。"""
    text: str
    source: str
    source_file: str = ""
    ts: Optional[str] = None
    ticket: Optional[str] = None
    hints: List[str] = field(default_factory=list)        # 素材自带状态（Jira Status / 纪要分节）
    hint_origin: List[str] = field(default_factory=list)
    # 结构性**强路由**信号（分节标题 `## 进行中` / 行内标签 `下周计划：…`）：
    # 命中即代表"用户已把这条放进该模块"，优先于关键词判定（blocked 除外）。
    route_hint: Optional[str] = None
    metrics: List[Metric] = field(default_factory=list)
    member_statuses: List[str] = field(default_factory=list)  # 合并前各成员各自的判定结果
    status: Optional[str] = None
    conflict: bool = False
    conflict_note: str = ""
    # 冲突类型："" = 状态冲突（R7 默认）；"metric" = **数值冲突**（v1.1.2 护栏）。
    # 只用于选渲染前缀（⚠️ 状态冲突： / ⚠️ 数值冲突：），不参与任何判定。
    conflict_kind: str = ""
    merged_from: List[str] = field(default_factory=list)
    merge_reason: str = ""            # 合并依据（R8：每次合并都必须有可复述的理由）
    order: int = 0

    @property
    def metrics_values(self) -> List[str]:
        return [m.value for m in self.metrics]

    def has_ticket(self) -> bool:
        return bool(self.ticket)
