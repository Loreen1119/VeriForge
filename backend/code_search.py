"""Bounded, dependency-free source search for VeriForge repositories."""
from __future__ import annotations

import re
from pathlib import Path

EXCLUDED_DIRS = {".git", ".codeforge", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".pytest_cache"}
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
SYMBOL_RE = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:def|class|function|interface|type|const|let|var)\s+([A-Za-z_$][\w$]*)")

# 关键词命中权重：正文命中 200，文件名/目录名也命中则加权到 280。
# 依据（2026-10-17 评测实测）：原实现把全部正文命中一律记 200，排序退化成「按路径字典序」，
# 于是同名文件冲突里 `fastapi/telemetry.py` 永远压过真正该改的 `fastapi/telemetry/_asgi.py`，
# `mapping` 这类词的 top-4 也被字典序更靠前的无关文件占满 —— 18 条里 8 条属这种「文件里有词、
# 但没进 top-8」的排序压制。加权只动 keyword 档（200 → 280），不动 symbol 400 / filename 100 的优先级。
KEYWORD_SCORE = 200
KEYWORD_NAME_HIT_SCORE = 280


def _in_allowed(path: Path, roots: list[Path]) -> bool:
    return bool(roots) and any(path == root or root in path.parents for root in roots)


def _candidate(*, path: str, lines: list[str], index: int, symbol: str | None, match_type: str, score: int, context_lines: int, depth: int = 0) -> dict:
    start = max(0, index - context_lines)
    end = min(len(lines), index + context_lines + 1)
    return {"path": path, "line_start": start + 1, "line_end": end, "matched_line": index + 1, "symbol": symbol, "snippet": "\n".join(lines[start:end]), "match_type": match_type, "_score": score, "_depth": depth}


def _name_hit(relative: str, needle: str) -> bool:
    """关键词是否也是这个文件「名字」的一部分（文件名 stem 或某个目录段正好等于它）。

    这是同名文件冲突的判据：`telemetry` 对 `fastapi/telemetry.py`（stem 命中）和
    `fastapi/telemetry/_asgi.py`（目录段命中）都成立，于是靠路径深度分出主次。
    """
    if not needle:
        return False
    segments = relative.casefold().split("/")
    return needle in Path(segments[-1]).stem.casefold() or any(seg == needle for seg in segments[:-1])


def search_repository(
    repository_root: str,
    query: str,
    *,
    filename: str | None = None,
    symbol: str | None = None,
    allowed_source_roots: list[str] | None = None,
    max_results: int = 50,
    context_lines: int = 2,
) -> list[dict]:
    """Return deterministic, bounded matches without reading outside the repository."""
    root = Path(repository_root).resolve()
    if not root.is_dir():
        raise ValueError("仓库根目录不存在")
    if not 1 <= max_results <= 100 or not 0 <= context_lines <= 10:
        raise ValueError("检索参数超出允许范围")
    allowed = [(root / item).resolve() for item in (allowed_source_roots or [])]
    if not allowed:
        raise ValueError("必须显式配置至少一个允许源码目录")
    for item in allowed:
        if item == root or not _in_allowed(item, [root]) or not item.is_dir():
            raise ValueError("允许源码目录超出仓库范围")

    needle = query.casefold().strip()
    requested_filename = filename.casefold().strip() if filename else ""
    requested_symbol = symbol.casefold().strip() if symbol else ""
    if not needle and not requested_filename and not requested_symbol:
        raise ValueError("至少提供 query、filename 或 symbol")
    results: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in SOURCE_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        if not _in_allowed(path, allowed):
            continue
        relative = path.relative_to(root).as_posix()
        depth = relative.count("/")
        filename_match = bool(requested_filename and (requested_filename in path.name.casefold() or requested_filename in relative.casefold()))
        if requested_filename and not filename_match:
            continue
        name_hit = _name_hit(relative, needle)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        enclosing_symbol: str | None = None
        file_results: list[dict] = []
        for index, line in enumerate(lines):
            found_symbol = SYMBOL_RE.match(line)
            if found_symbol:
                enclosing_symbol = found_symbol.group(1)
            symbol_match = bool(requested_symbol and found_symbol and found_symbol.group(1).casefold() == requested_symbol)
            if needle:
                if needle.isascii():
                    text_match = bool(re.search(
                        rf"(?<![A-Za-z0-9_])(?i:{re.escape(needle)})(?![a-z0-9])", line
                    ))
                else:
                    text_match = needle in line.casefold()
            else:
                text_match = False
            if symbol_match:
                file_results.append(_candidate(path=relative, lines=lines, index=index, symbol=found_symbol.group(1), match_type="symbol", score=400, context_lines=context_lines, depth=depth))
            elif text_match:
                file_results.append(_candidate(path=relative, lines=lines, index=index, symbol=enclosing_symbol, match_type="keyword", score=KEYWORD_NAME_HIT_SCORE if name_hit else KEYWORD_SCORE, context_lines=context_lines, depth=depth))
        if filename_match and not file_results:
            file_results.append(_candidate(path=relative, lines=lines, index=0, symbol=None, match_type="filename", score=100, context_lines=context_lines, depth=depth))
        results.extend(file_results)
    results.sort(key=lambda item: (-item["_score"], -item["_depth"], item["path"], item["matched_line"]))
    for item in results:
        item.pop("_score")
        item.pop("_depth")
    return results[:max_results]


def read_code(
    repository_root: str,
    relative_path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    symbol: str | None = None,
    allowed_source_roots: list[str] | None = None,
) -> dict:
    """Read a bounded source fragment by path, line range, or declaration symbol."""
    root = Path(repository_root).resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("文件路径超出仓库范围") from exc
    allowed = [(root / item).resolve() for item in (allowed_source_roots or [])]
    if not allowed:
        raise ValueError("必须显式配置至少一个允许源码目录")
    if any(item == root or not _in_allowed(item, [root]) or not item.is_dir() for item in allowed):
        raise ValueError("允许源码目录无效")
    if not _in_allowed(path, allowed):
        raise ValueError("文件不在允许的源码目录内")
    if not path.is_file() or path.suffix.casefold() not in SOURCE_SUFFIXES or any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
        raise ValueError("文件不存在或不是受支持的源码文件")
    if start_line < 1 or (end_line is not None and end_line < start_line):
        raise ValueError("代码行范围无效")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("无法读取源码文件") from exc

    if symbol:
        wanted = symbol.casefold()
        matches = [index for index, line in enumerate(lines) if (declaration := SYMBOL_RE.match(line)) and declaration.group(1).casefold() == wanted]
        if not matches:
            raise ValueError("未找到指定符号")
        start_line = matches[0] + 1
        end_line = len(lines)
        base_indent = len(lines[matches[0]]) - len(lines[matches[0]].lstrip())
        for index in range(matches[0] + 1, len(lines)):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= base_indent and SYMBOL_RE.match(line):
                end_line = index
                break
    end_line = min(end_line or len(lines), len(lines))
    if start_line > len(lines):
        raise ValueError("起始行超出文件范围")
    return {"path": path.relative_to(root).as_posix(), "line_start": start_line, "line_end": end_line, "content": "\n".join(lines[start_line - 1:end_line])}
