"""Local repository search adapter used by VeriForge retrieval.

The legacy provider hook is intentionally rejected. VeriForge owns repository
boundaries and result validation, so retrieval stays inside local source roots.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from code_search import search_repository

class RAGResult(list):
    def __init__(self, results, status):
        super().__init__(results); self.status = status


class RAGProvider(Protocol):
    def search(self, *, root: str, query: str, max_results: int) -> list[dict[str, Any]]: ...


def search_with_fallback(
    root: str,
    query: str,
    *,
    provider: RAGProvider | Callable[..., list[dict[str, Any]]] | None = None,
    allowed_source_roots: list[str] | None = None,
    max_results: int = 50,
    context_lines: int = 2,
    **filters: str | None,
) -> list[dict]:
    """Use bounded local search and reject the retired provider integration."""
    if not allowed_source_roots:
        raise ValueError("必须显式配置至少一个允许源码目录")
    if provider is not None:
        raise ValueError("RAG provider integration is disabled; use local repository search")
    local = search_repository(
        root,
        query,
        allowed_source_roots=allowed_source_roots,
        max_results=max_results,
        context_lines=context_lines,
        **filters,
    )
    return RAGResult(local, "hit" if local else "empty")
