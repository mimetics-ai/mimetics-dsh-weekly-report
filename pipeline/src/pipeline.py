# -*- coding: utf-8 -*-
"""周报生成流水线（五步）+ 命令行入口。

用法：
  python -m src.pipeline --input tests/fixtures/notes.md tests/fixtures/chat.txt \
                         tests/fixtures/jira.csv --out dist/report.md

退出码：0 正常 / 2 无素材（R0 闸门） / 1 参数或内部错误
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import classify, dedup, extract, render, sources
from .config_loader import Config
from .model import Item


@dataclass
class Report:
    markdown: str = ""
    items: List[Item] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    unknown: List[Item] = field(default_factory=list)
    merges: List[Dict[str, Any]] = field(default_factory=list)   # 合并审计（R8 可复核）
    notices: List[str] = field(default_factory=list)             # 正向提示（如"已读取表格文本数据"）
    stats: Dict[str, int] = field(default_factory=dict)
    gated: bool = False


def _expand(paths: Sequence[str]) -> List[Path]:
    out: List[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(q for q in path.iterdir() if q.is_file()))
        else:
            out.append(path)
    return out


def build(paths: Sequence[Path], cfg: Optional[Config] = None) -> Report:
    cfg = cfg or Config()
    rep = Report()
    warn = rep.warnings.append

    # ---- 第 1 步：读素材 ----
    items = sources.collect(list(paths), cfg, warn, notice=rep.notices.append)

    # ---- R0 闸门：无可用素材 ----
    if not items:
        rep.gated = True
        rep.markdown = cfg.inp("no_material_message", "请先上传本周工作素材（聊天记录/工单/文档均可）。")
        return rep

    # ---- 第 2 步（前置）：量化数据抽取，同时回填工单号供合并使用 ----
    for it in items:
        extract.attach_metrics(it, cfg)

    # ---- 第 3 步：合并同类项（R8：宁可漏合，绝不误合；每次合并留依据） ----
    merged = dedup.run(items, cfg, warn, audit=rep.merges)

    # ---- 第 4 步：状态分类 ----
    unknown: List[Item] = []
    for it in merged:
        verdict = classify.finalize(it, cfg)
        it.status = verdict.status
        it.conflict = verdict.conflict
        it.conflict_note = verdict.note
        # 数值冲突护栏（v1.1.2）走的是同一条 R7 链路，只换渲染前缀
        it.conflict_kind = "metric" if "metric_conflict" in (verdict.tags or []) else ""
        if verdict.tags:
            warn("分类标记[%s]%s" % (verdict.tags, it.text[:30]))
        if it.status is None:
            unknown.append(it)

    rep.unknown = unknown
    if unknown:
        policy = cfg.get("classification.unknown_policy", "ask_user")
        if policy == "ask_user":
            warn("以下 %d 条无明确状态，未写入四模块，需用户确认：" % len(unknown))
            for it in unknown:
                warn("  · [%s] %s" % (it.source, it.text))

    known = [it for it in merged if it.status]

    # ---- 第 5 步：渲染 + 自检 ----
    rep.markdown = render.build(known, cfg, rep.warnings)
    violations = render.selfcheck(rep.markdown, known, cfg)
    for v in violations:
        warn("自检未通过：%s" % v)

    # R2 缺数据诊断：**只进 warnings，绝不写进正文**（缺陷 D4 的定论）
    for notice in render.data_gap_notices(known, cfg):
        warn(notice)

    rep.items = known
    rep.stats = {
        "素材条数": len(items),
        "合并后": len(merged),
        "进入四模块": len(known),
        "待确认": len(unknown),
        "合并掉": len(items) - len(merged),
        "合并组数": len(rep.merges),
    }
    return rep


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="mimetics-dsh-weekly-report 周报生成")
    ap.add_argument("--input", "-i", nargs="+", required=True, help="素材文件或目录（可多个）")
    ap.add_argument("--out", "-o", default=None, help="输出 Markdown 路径（可省，仅打印）")
    ap.add_argument("--config-dir", default=None, help="配置目录（默认 ./config）")
    ap.add_argument("--trace", action="store_true", help="打印每条素材的中间判定")
    ap.add_argument("--audit", action="store_true", help="打印合并审计（每条合并的依据与原文）")
    args = ap.parse_args(argv)

    cfg = Config(Path(args.config_dir) if args.config_dir else None)
    paths = _expand(args.input)

    try:
        rep = build(paths, cfg)
    except Exception as exc:  # 配置/解析异常：直接报错退出，不产出半成品周报
        print("[ERROR] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1

    if rep.gated:
        # R0：只输出这一行，不加任何分析/追问
        print(rep.markdown)
        return 2

    print(rep.markdown, end="")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rep.markdown, encoding="utf-8")
        print("[written] %s (%d bytes)" % (out, out.stat().st_size), file=sys.stderr)

    if args.trace:
        print("\n---- trace ----", file=sys.stderr)
        for it in rep.items:
            print("[%s] status=%s metrics=%s" % (it.source, it.status, it.metrics_values), file=sys.stderr)
        for it in rep.unknown:
            print("[%s] UNKNOWN %s" % (it.source, it.text), file=sys.stderr)

    if args.audit:
        print("\n---- merge audit（R8：宁可漏合，绝不误合）----", file=sys.stderr)
        for rec in rep.merges:
            print("组%d 依据: %s" % (rec["group"], rec["reason"]), file=sys.stderr)
            print("     保留: %s" % rec["picked"], file=sys.stderr)
            for m in rec["members"]:
                print("     成员: %s" % m, file=sys.stderr)
        print("合并组数: %d" % len(rep.merges), file=sys.stderr)

    print("---- stats ----", file=sys.stderr)
    for k, v in rep.stats.items():
        print("%s: %s" % (k, v), file=sys.stderr)
    for n in rep.notices:
        print("[info] %s" % n, file=sys.stderr)
    for w in rep.warnings:
        print("[warn] %s" % w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
