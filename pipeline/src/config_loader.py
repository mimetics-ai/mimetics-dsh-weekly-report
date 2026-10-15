# -*- coding: utf-8 -*-
"""配置装载（去依赖版）：**只依赖 Python 标准库**（json / copy / pathlib）。

适配 Coze 代码节点（Python 3.9、无 PyYAML、无第三方包）而改写：
  · 旧版：读 config/ 下 6 份 YAML，按 skill.config.yaml 的 config_modules 顺序深合并；
  · 新版：只读单份 config/config.json（由 tools/build_config_json.py 从 YAML 派生，
          合并语义与旧版逐节点等价，生成时已断言）。

公开 API 与旧版**完全一致**（raw / get / sections / rules / status_spec / patterns /
sources / template_text / version / out / inp），因此 src/ 其余模块一行都不用改。

三处与旧版不同的必要改动：
  1. 不再 import yaml；改用 json。
  2. 模板内联：config.json 的 "templates" 里带 template.md 正文，运行期不做文件 IO
     （Coze 里没有用户目录可读）。
  3. 三种装载入口：目录 / .json 文件 / JSON 字符串（Coze 用第三种或直接传 dict）。

设计原则不变：代码不认识任何业务名词（模块名/关键词/正则全部来自配置）。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    _HERE = Path(__file__).resolve().parent.parent
except NameError:
    # Coze 代码节点没有 __file__（也没有文件系统）：以下路径常量仅作占位，
    # 节点内一律用 Config(raw=CONFIG) 装载。此处不做任何 IO，不会失败。
    _HERE = Path(".")

PROJECT_ROOT = _HERE
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = "config.json"

# 允许用环境变量把配置体（JSON 字符串）直接注入，便于无文件系统环境（Coze/沙箱）
ENV_CONFIG_JSON = "WEEKLY_REPORT_CONFIG_JSON"


class ConfigError(RuntimeError):
    """配置缺失或结构不合法。绝不静默降级为"空配置"（那等于假装支持）。"""


# --------------------------------------------------------------------------- #
def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """dict 递归合并（后者覆盖前者）；list 整体替换。与 YAML 版语义一致。"""
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(out.get(key), dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load_json(source: Union[str, Path, os.PathLike]) -> Dict[str, Any]:
    """读 JSON 文件（UTF-8，容忍 BOM）。"""
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(
            "找不到配置 %s。请先在项目根目录运行：\n"
            "    python tools/build_config_json.py\n"
            "（该脚本从 config/*.yaml 生成 config/config.json；YAML 为唯一真源。）" % path
        )
    return parse_json(text, origin=str(path))


def parse_json(text: str, origin: str = "<string>") -> Dict[str, Any]:
    """解析 JSON 文本。出错时报明文位置，不给半成品配置。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("配置 JSON 解析失败（%s）：第 %d 行第 %d 列 %s"
                          % (origin, exc.lineno, exc.colno, exc.msg))
    if not isinstance(data, dict):
        raise ConfigError("配置 JSON 顶层必须是对象（dict），实际是 %s（%s）"
                          % (type(data).__name__, origin))
    return data


# --------------------------------------------------------------------------- #
class Config:
    """运行时配置。

    Config()                     —— 读 ./config/config.json
    Config(Path("a/b.json"))     —— 读指定 JSON 文件
    Config(Path("cfgdir"))       —— 读该目录下的 config.json
    Config(raw=<dict>)           —— 直接用内存中的配置（Coze 里传 CONFIG 常量）
    Config(json_text="...")      —— 直接解析 JSON 字符串
    """

    def __init__(self,
                 config: Optional[Union[str, Path, os.PathLike]] = None,
                 raw: Optional[Dict[str, Any]] = None,
                 json_text: Optional[str] = None):
        self.path: Optional[Path] = None
        self.dir: Path = Path(config) if isinstance(config, (str, Path, os.PathLike)) else CONFIG_DIR
        self.raw: Dict[str, Any] = {}
        self.modules: List[str] = []
        self._raw_inline = raw
        self._json_inline = json_text
        self.reload()

    # ---- 装载 ----
    def _resolve_path(self) -> Optional[Path]:
        """把构造参数解析成配置文件路径；返回 None 表示配置来自内存。"""
        if self._raw_inline is not None or self._json_inline is not None:
            return None
        target = self.dir
        if target.suffix.lower() == ".json":
            return target
        return target / CONFIG_FILE

    def reload(self) -> "Config":
        if self._raw_inline is not None:
            base = copy.deepcopy(self._raw_inline)
        elif self._json_inline is not None:
            base = parse_json(self._json_inline, origin="<inline json>")
        else:
            env = os.environ.get(ENV_CONFIG_JSON)
            if env and not Path(self.dir).exists():
                base = parse_json(env, origin="env:%s" % ENV_CONFIG_JSON)
            else:
                path = self._resolve_path()
                assert path is not None
                self.path = path
                base = load_json(path)

        # 运行期可选分层：$layers 里列出的 .json 依次覆盖（默认空 = 单文件即完整配置）
        layers = base.get("$layers") or []
        if not isinstance(layers, list):
            raise ConfigError("$layers 必须是数组，实际是 %s" % type(layers).__name__)
        merged = copy.deepcopy(base)
        for name in layers:
            if not str(name).endswith(".json"):
                # 不静默忽略：YAML 分层已随"去依赖化"取消，写错名字必须立刻暴露
                raise ConfigError(
                    "$layers 只接受 .json 分层文件，收到 %r。"
                    "YAML 分层已停用，请改用 tools/build_config_json.py 生成完整 config.json。"
                    % (name,))
            lp = (self.path.parent if self.path else Path(self.dir)) / str(name)
            if not lp.exists():
                raise ConfigError("$layers 声明的文件不存在：%s" % lp)
            merged = _deep_merge(merged, load_json(lp))

        if "sections" not in merged:
            raise ConfigError("配置缺少必需键 sections（来源：%s）" % (self.path or "<inline>"))
        self.modules = list(layers)
        self.raw = merged
        return self

    # ---- 取值 ----
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    # ---- 便捷视图（与旧版逐项一致） ----
    @property
    def sections(self) -> List[Dict[str, Any]]:
        return sorted(self.get("sections", []) or [], key=lambda s: s.get("order", 0))

    @property
    def rules(self) -> Dict[str, Dict[str, Any]]:
        return {r["id"]: r for r in (self.get("rules", []) or [])}

    @property
    def status_spec(self) -> Dict[str, Any]:
        return self.get("status", {}) or {}

    @property
    def patterns(self) -> List[Dict[str, Any]]:
        return self.get("patterns", []) or []

    @property
    def sources(self) -> List[Dict[str, Any]]:
        return self.get("sources", []) or []

    @property
    def template_text(self) -> str:
        """模板内联在 "templates" 里；若有同名磁盘文件则优先读盘（开发期改模板即时生效）。"""
        name = self.get("output.template", "template.md")
        inline = (self.get("templates", {}) or {}).get(name)
        disk = self.dir / name
        if isinstance(self.dir, Path) and self.path is not None and disk.exists():
            return disk.read_text(encoding="utf-8")
        if inline is None:
            raise ConfigError(
                "配置里找不到模板 %r（templates 键缺失）。"
                "请运行 python tools/build_config_json.py 重新生成 config.json。" % name)
        return inline

    @property
    def version(self) -> str:
        return self.get("skill.version", "0.0.0")

    def out(self, key: str, default: Any = None) -> Any:
        return self.get("output.%s" % key, default)

    def inp(self, key: str, default: Any = None) -> Any:
        return self.get("input.%s" % key, default)

    # ---- 交付用：导出成可直接粘进 Coze 代码节点的字面量 ----
    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.raw, ensure_ascii=False, indent=indent)
