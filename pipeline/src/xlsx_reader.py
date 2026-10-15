# -*- coding: utf-8 -*-
"""表格读取器（**去依赖版**）——只用 Python 标准库。

适配 Coze 代码节点（无 openpyxl、无 xlrd）而改写。三条通道：

  1. `.xlsx` / `.xlsm` → `zipfile` + `xml.etree.ElementTree` **直读 OOXML**
     （.xlsx 本质是 zip 包，里面是 XML；所以"去依赖"不等于"降能力"）
  2. `.csv` / `.tsv`     → 标准库 `csv`（分隔符嗅探 + 编码回退）—— 即"降级版"通道
  3. `.xls`（旧版 BIFF 二进制）→ 标准库**读不了** → 抛 `EngineMissing`，
     明确提示并给出可执行建议（另存为 .xlsx / .csv），**不假装支持**

能力边界（如实声明，不做超出范围的承诺）：
  · **只取单元格的值**：公式只取 Excel 缓存值 `<v>`；文件从未被 Excel 打开过（无缓存）
    → 该格为空并跳过，**不推算、不猜测**（与 openpyxl `data_only=True` 行为一致）。
  · 数字格式为日期/时间的单元格 → 按单元格数字格式转成 `YYYY-MM-DD` / `HH:MM`；
    **格式识别不出就原样输出数字，不猜**。
  · 图表/图片/批注/条件格式不在单元格值域内 → 天然跳过。
  · 合并单元格只取左上角的值（与 openpyxl 一致）。
  · 错误值（`#DIV/0!` 等）跳过，不当作文本搬运。
  · 单元格文本一律**原样搬运**，不做措辞加工。
  · 只读，**不写回、不修改**用户文件。
  · 单个 sheet / 单元格异常 → 跳过并回传提示，不中断整份报告。
  · 能力不可用（见 `_CAPABILITY`）→ 抛 `EngineMissing`，由调用方转为用户可读提示，
    **不允许静默返回空数据**（否则等于假装支持）。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:                                    # xml.etree 为标准库；缺失时降级为"不能读 xlsx"
    from xml.etree import ElementTree as ET
except Exception:                       # pragma: no cover
    ET = None                           # type: ignore

MAX_ROWS_DEFAULT = 2000
MAX_COLS_DEFAULT = 50
_MAX_MEMBER_BYTES = 64 * 1024 * 1024    # 单个 zip 成员解压上限（防 zip 炸弹）
_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030")


class EngineMissing(RuntimeError):
    """当前运行时缺少读取该格式所需的能力（旧版 .xls、或能力开关被关闭）。

    名称保留 `EngineMissing` 以兼容既有调用方（`src/sources.py`）。
    """

    _HINT = {
        "xlsx": "当前运行时未启用 xlsx 读取能力（需要标准库 zipfile + xml.etree）",
        "csv": "当前运行时未启用 CSV 读取能力（需要标准库 csv）",
        "xls": "标准库无法读取旧版 .xls（BIFF 二进制格式）；"
               "请在 Excel/WPS 中另存为 .xlsx 或 .csv 后重试",
    }

    def __init__(self, name: str):
        self.name = name
        self.hint = self._HINT.get(name, "当前运行时无法读取该格式：%s" % name)
        super().__init__(self.hint)

    def __str__(self) -> str:            # 日志里直接读到人话，而不是一个模块名
        return self.hint


@dataclass
class SheetData:
    sheet: str
    header: List[str] = field(default_factory=list)   # 首个非空行
    rows: List[List[str]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)  # 该 sheet 内被跳过的项说明


# --------------------------------------------------------------------------- #
# 能力表：标准库能读什么。测试可注入 `_CAPABILITY` 模拟"能力不可用"。
# --------------------------------------------------------------------------- #
_CAPABILITY: Dict[str, bool] = {
    "xlsx": ET is not None,     # .xlsx/.xlsm：zipfile + xml.etree
    "csv": True,                # .csv/.tsv：csv
    "xls": False,               # .xls：标准库读不了（BIFF 二进制）
}


def available_engines() -> Dict[str, bool]:
    """当前运行时可用的表格读取通道（实时反映 `_CAPABILITY`，便于测试注入）。"""
    cap = dict(_CAPABILITY)
    if ET is None:
        cap["xlsx"] = False
    return cap


def engines_ok() -> Dict[str, bool]:
    """对外可声明的能力表（供上架物料如实声明用）。"""
    return available_engines()


def missing_hint(name: str) -> str:
    return EngineMissing._HINT.get(name, "当前运行时无法读取该格式：%s" % name)


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _read_text(path: Path) -> str:
    for enc in _ENCODINGS:
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _fmt_number(raw: Any) -> str:
    """数字 → 文本：整数不带 .0；无缓存值的公式（以 = 开头）一律丢弃。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text or text.startswith("="):
        return ""
    try:
        f = float(text)
    except ValueError:
        return text
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return "%g" % f


# ---- OOXML（xlsx）解析 ----------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _iter_local(el, name: str) -> Iterator[Any]:
    for child in el.iter():
        if _local(child.tag) == name:
            yield child


def _first_local(el, name: str):
    for found in _iter_local(el, name):
        return found
    return None


def _attr_local(el, name: str) -> Optional[str]:
    for key, val in el.attrib.items():
        if _local(key) == name:
            return val
    return None


def _read_member(zf: zipfile.ZipFile, name: str) -> Optional[bytes]:
    """按名读 zip 成员（大小写不敏感回退）；找不到或过大返回 None。"""
    try:
        info = zf.getinfo(name)
    except KeyError:
        lower = {n.lower(): n for n in zf.namelist()}
        actual = lower.get(name.lower())
        if actual is None:
            return None
        info = zf.getinfo(actual)
    if info.file_size > _MAX_MEMBER_BYTES:
        return None
    try:
        return zf.read(info.filename)
    except Exception:
        return None


# Excel 内置数字格式（只需日期/时间相关的那几个；其余按普通数字处理）
_BUILTIN_NUMFMT: Dict[int, str] = {
    14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss",
    22: "m/d/yy h:mm", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0",
}

_STRIP_LITERAL = re.compile(r"\[[^\]]*\]|\"[^\"]*\"|\\.")


def _classify_format(code: str) -> str:
    """把数字格式串归类为 date / time / datetime / number。识别不出就是 number。"""
    if not code:
        return "number"
    c = _STRIP_LITERAL.sub("", code)
    has_date = bool(re.search(r"[ydYD]", c))
    has_time = bool(re.search(r"[hsHS]", c))
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    if ":" in c:
        return "time"
    return "number"


_EPOCH = _dt.datetime(1899, 12, 30)     # Excel 1900 历（对 serial>=61 精确）


def _serial_to_text(raw: Any, kind: str) -> str:
    try:
        f = float(str(raw).strip())
    except (TypeError, ValueError):
        return str(raw).strip()
    if kind == "date" and f < 1:        # 只有小数部分 → 其实是时间
        kind = "time"
    try:
        dt = _EPOCH + _dt.timedelta(days=f)
    except (OverflowError, ValueError):
        return _fmt_number(raw)
    if kind == "time":
        return dt.strftime("%H:%M:%S") if (dt.second or dt.microsecond) else dt.strftime("%H:%M")
    if kind == "datetime":
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def _parse_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    raw = _read_member(zf, "xl/sharedStrings.xml")
    if not raw:
        return []
    out: List[str] = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    for si in _iter_local(root, "si"):
        out.append("".join((t.text or "") for t in _iter_local(si, "t")))
    return out


def _parse_styles(zf: zipfile.ZipFile) -> List[str]:
    """返回 cellXfs 中每个 xf 的 formatCode（无则空串）。"""
    raw = _read_member(zf, "xl/styles.xml")
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    custom: Dict[int, str] = {}
    for nf in _iter_local(root, "numFmt"):
        fid, code = _attr_local(nf, "numFmtId"), _attr_local(nf, "formatCode")
        if fid is not None and code is not None:
            try:
                custom[int(fid)] = code
            except ValueError:
                pass
    xfs = _first_local(root, "cellXfs")
    out: List[str] = []
    if xfs is not None:
        for xf in _iter_local(xfs, "xf"):
            try:
                fid = int(_attr_local(xf, "numFmtId") or 0)
            except ValueError:
                fid = 0
            out.append(custom.get(fid) or _BUILTIN_NUMFMT.get(fid, ""))
    return out


def _parse_workbook(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """返回 [(sheet 名, rId)]，保持 Excel 里的 sheet 顺序。"""
    raw = _read_member(zf, "xl/workbook.xml")
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    out: List[Tuple[str, str]] = []
    for sh in _iter_local(root, "sheet"):
        out.append((_attr_local(sh, "name") or "", _attr_local(sh, "id") or ""))
    return out


def _parse_rels(zf: zipfile.ZipFile) -> Dict[str, str]:
    raw = _read_member(zf, "xl/_rels/workbook.xml.rels")
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for rel in _iter_local(root, "Relationship"):
        rid, target = _attr_local(rel, "Id"), _attr_local(rel, "Target")
        if not rid or not target:
            continue
        target = target.replace("\\", "/")
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = "xl/" + target
        out[rid] = target
    return out


def _col_index(ref: Optional[str]) -> Optional[int]:
    """单元格引用 'BC12' → 列序号（0 基）。"""
    m = re.match(r"\s*([A-Za-z]+)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _cell_text(c_el, shared: List[str], fmts: List[str]) -> str:
    """单个 `<c>` → 文本。取不到值就返回空串（绝不拼出素材里没有的东西）。"""
    ctype = _attr_local(c_el, "t") or ""

    if ctype == "inlineStr":
        is_el = _first_local(c_el, "is")
        if is_el is None:
            return ""
        return "".join((t.text or "") for t in _iter_local(is_el, "t")).strip()

    v_el = _first_local(c_el, "v")
    if v_el is None or v_el.text is None:
        return ""                       # 公式无缓存值 / 空单元格 → 跳过，不推算
    raw = v_el.text

    if ctype == "s":                    # 共享字符串
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if ctype == "str":                  # 公式的字符串结果（有缓存）
        return _fmt_number(raw) or raw.strip()
    if ctype == "b":                    # 布尔
        return "是" if raw.strip() not in ("0", "") else "否"
    if ctype == "e":                    # 错误值（#DIV/0! 等）→ 丢弃
        return ""
    if ctype == "d":                    # ISO 日期字符串
        return raw.strip()

    sidx = _attr_local(c_el, "s")
    fmt = ""
    if sidx is not None:
        try:
            fmt = fmts[int(sidx)]
        except (ValueError, IndexError):
            fmt = ""
    kind = _classify_format(fmt)
    if kind != "number":
        return _serial_to_text(raw, kind)
    return _fmt_number(raw)


def _parse_sheet_xml(raw: bytes, data: SheetData, shared: List[str], fmts: List[str],
                     opts: Dict[str, Any], warn: Callable[[str], None]) -> None:
    max_rows = int(opts.get("max_rows_per_sheet", MAX_ROWS_DEFAULT) or MAX_ROWS_DEFAULT)
    max_cols = int(opts.get("max_cols", MAX_COLS_DEFAULT) or MAX_COLS_DEFAULT)
    root = ET.fromstring(raw)
    idx = 0
    for row in _iter_local(root, "row"):
        if idx >= max_rows:
            data.skipped.append("超过行数上限 %d，本 sheet 余下内容未读" % max_rows)
            warn("表格 %s 超过行数上限 %d，已截断" % (data.sheet, max_rows))
            break
        idx += 1
        cells: Dict[int, str] = {}
        for c_el in _iter_local(row, "c"):
            ci = _col_index(_attr_local(c_el, "r"))
            if ci is None:
                ci = len(cells)
            if ci >= max_cols:
                continue
            cells[ci] = _cell_text(c_el, shared, fmts)
        if not cells:
            continue                    # 整行空（含图表占位行）
        width = min(max(cells.keys()) + 1, max_cols)
        vals = [cells.get(i, "") for i in range(width)]
        if not any(vals):
            continue
        if not data.header:
            data.header = vals
            continue
        data.rows.append(vals)


def _read_xlsx(path: Path, opts: Dict[str, Any], warn: Callable[[str], None]) -> List[SheetData]:
    if ET is None:
        raise EngineMissing("xlsx")
    try:
        zf = zipfile.ZipFile(str(path))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("不是有效的 xlsx（zip 解析失败）：%s" % exc)
    except Exception as exc:
        raise RuntimeError("无法打开 %s：%s" % (path.name, exc))
    with zf:
        return _read_xlsx_zip(zf, path.name, opts, warn)


def _read_xlsx_zip(zf: "zipfile.ZipFile", name: str, opts: Dict[str, Any],
                   warn: Callable[[str], None]) -> List[SheetData]:
    """已打开的 zip → SheetData 列表。

    与 `_read_xlsx` 分离，是为了让"内存中的 bytes"（Coze 里从 URL 拉下来的文件）
    能复用同一套解析核心：`zipfile.ZipFile(io.BytesIO(data))` 即可。
    """
    if ET is None:
        raise EngineMissing("xlsx")
    all_sheets = bool(opts.get("all_sheets", True))
    shared = _parse_shared_strings(zf)
    fmts = _parse_styles(zf)
    declared = _parse_workbook(zf)
    rels = _parse_rels(zf)

    if not declared:                    # workbook.xml 异常：退回按文件名枚举
        declared = [("Sheet1", "")]
    if not all_sheets:
        declared = declared[:1]

    sheets: List[SheetData] = []
    for idx_sheet, (sheet_name, rid) in enumerate(declared):
        data = SheetData(sheet=sheet_name or "Sheet1")
        target = rels.get(rid) if rid else None
        if target is None:
            # 无 rels 时按常见命名兜底枚举（sheet1.xml / sheetN.xml …）
            cands = sorted(n for n in zf.namelist()
                           if n.startswith("xl/worksheets/") and n.endswith(".xml"))
            target = cands[idx_sheet] if len(cands) > idx_sheet else None
        raw = _read_member(zf, target) if target else None
        if not raw:
            data.skipped.append("未找到该 sheet 的 XML（%s）" % target)
            warn("表格 %s#%s 未找到内容，已跳过" % (name, data.sheet))
            sheets.append(data)
            continue
        try:
            _parse_sheet_xml(raw, data, shared, fmts, opts, warn)
        except Exception as exc:
            data.skipped.append("%s：%s" % (type(exc).__name__, exc))
            warn("表格 %s#%s 读取异常，跳过该 sheet：%s" % (name, data.sheet, exc))
        sheets.append(data)
    return sheets


# ---- CSV / TSV 通道（"降级版"） -------------------------------------------- #
def _sniff_delimiter(sample: str, suffix: str) -> str:
    if suffix == ".tsv":
        return "\t"
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except Exception:
        counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=lambda k: counts[k])
        return best if counts[best] else ","


def _read_delimited(path: Path, opts: Dict[str, Any], warn: Callable[[str], None]) -> List[SheetData]:
    """以标准库 csv 读取 .csv / .tsv，返回与 xlsx 相同的 SheetData 结构。"""
    return _read_delimited_text(_read_text(path), path.name, opts, warn,
                                suffix=path.suffix.lower())


def _read_delimited_text(text: str, name: str, opts: Dict[str, Any],
                         warn: Callable[[str], None], suffix: str = "") -> List[SheetData]:
    """文本 → SheetData（供"内存中的 CSV 正文"复用；Coze 上传文件走同一路径）。"""
    max_rows = int(opts.get("max_rows_per_sheet", MAX_ROWS_DEFAULT) or MAX_ROWS_DEFAULT)
    max_cols = int(opts.get("max_cols", MAX_COLS_DEFAULT) or MAX_COLS_DEFAULT)
    stem = name.rsplit(".", 1)[0] if "." in name else (name or "sheet")
    delim = _sniff_delimiter(text[:4096], suffix or (("." + name.rsplit(".", 1)[-1].lower())
                                                     if "." in name else ""))
    data = SheetData(sheet=stem)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
        idx = 0
        for row in reader:
            if idx >= max_rows:
                data.skipped.append("超过行数上限 %d，余下内容未读" % max_rows)
                warn("表格 %s 超过行数上限 %d，已截断" % (name, max_rows))
                break
            idx += 1
            cells = [(c or "").strip() for c in row][:max_cols]
            if not any(cells):
                continue
            if not data.header:
                data.header = cells
                continue
            data.rows.append(cells)
    except Exception as exc:
        data.skipped.append("%s：%s" % (type(exc).__name__, exc))
        warn("表格 %s 读取异常，已跳过：%s" % (name, exc))
    return [data]


# --------------------------------------------------------------------------- #
def read_sheets(path: Path, opts: Dict[str, Any], warn: Callable[[str], None]) -> List[SheetData]:
    """按扩展名选通道。返回各 sheet 的表头与数据行（全部为字符串）。"""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in (".xlsx", ".xlsm"):
        if not available_engines().get("xlsx"):
            raise EngineMissing("xlsx")
        return _read_xlsx(path, opts, warn)

    if suffix in (".csv", ".tsv"):
        if not available_engines().get("csv"):
            raise EngineMissing("csv")
        return _read_delimited(path, opts, warn)

    if suffix == ".xls":
        raise EngineMissing("xls")       # 标准库读不了 BIFF；如实报错，不假装支持

    raise RuntimeError("不支持的表格格式：%s" % suffix)
