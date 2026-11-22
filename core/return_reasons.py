"""Извлечение шаблонных причин возврата из старого HTML-справочника."""

from __future__ import annotations

import threading
from html.parser import HTMLParser
from pathlib import Path


class _ReasonsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.category = "Общие"
        self._capture: str | None = None
        self._parts: list[str] = []
        self.items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"h1", "h2", "label"}:
            self._capture = tag
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._capture != tag:
            return
        text = " ".join("".join(self._parts).split())
        if tag in {"h1", "h2"} and text:
            self.category = text
        elif tag == "label" and text:
            self.items.append({"category": self.category, "text": text})
        self._capture = None
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)


#: Разобранный справочник по пути: ``path -> ((mtime_ns, size), items)``. Справочник читается
#: на каждый DTO заказа (подсказки причин возврата), поэтому разбор HTML кэшируется и
#: повторяется только после изменения файла.
_CACHE: dict[Path, tuple[tuple[int, int], tuple[tuple[str, str], ...]]] = {}
_CACHE_LOCK = threading.Lock()


def _default_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "Цифра - Загальне.html"


def _parse_reasons(template_path: Path) -> tuple[tuple[str, str], ...]:
    parser = _ReasonsParser()
    parser.feed(template_path.read_text(encoding="utf-8-sig"))

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in parser.items:
        normalized = item["text"].strip().rstrip(".;")
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append((item["category"], normalized))
    return tuple(result)


def load_return_reasons(template_path: Path | None = None) -> list[dict[str, str]]:
    """Возвращает уникальные причины возврата с категорией из HTML-шаблона.

    Разбор кэшируется по пути и ``(mtime, размер)`` файла; каждый вызов возвращает
    новые словари, так что изменение результата не портит кэш.
    """
    path = template_path if template_path is not None else _default_template_path()
    try:
        stat = path.stat()
    except OSError:
        return []
    if not path.is_file():
        return []
    signature = (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _CACHE.get(path)
    if cached is None or cached[0] != signature:
        items = _parse_reasons(path)
        with _CACHE_LOCK:
            _CACHE[path] = (signature, items)
    else:
        items = cached[1]
    return [{"category": category, "text": text} for category, text in items]
