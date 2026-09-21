"""HTML -> απλό κείμενο (stdlib μόνο)."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._article_depth = 0
        self.all_parts: list[str] = []
        self.article_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if tag in ("article", "main") and not self._skip_depth:
            self._article_depth += 1
        if tag in _BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("article", "main") and self._article_depth:
            self._article_depth -= 1
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._emit(data)

    def _emit(self, text: str) -> None:
        self.all_parts.append(text)
        if self._article_depth:
            self.article_parts.append(text)


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(markup: str, max_chars: int = 0) -> str:
    """Κείμενο σελίδας. Προτιμά το περιεχόμενο <article>/<main> αν υπάρχει και έχει ουσία."""
    p = _Extractor()
    try:
        p.feed(markup)
        p.close()
    except Exception:  # ελαττωματικό HTML: κρατάμε ό,τι μαζεύτηκε
        pass
    article = _tidy("".join(p.article_parts))
    text = article if len(article) >= 200 else _tidy("".join(p.all_parts))
    return text[:max_chars] if max_chars else text


def strip_accents(s: str) -> str:
    """Αφαίρεση τόνων/διαλυτικών: το `'ί'.upper()` στην Python κρατά τον τόνο ('Ί'), οπότε η σύγκριση
    ελληνικών κεφαλαίων χρειάζεται ρητή κανονικοποίηση."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def strip_tags(fragment: str) -> str:
    """Για summaries RSS: αφαίρεση tags + αποκωδικοποίηση entities."""
    no_tags = re.sub(r"<[^>]+>", " ", fragment or "")
    return _tidy(html.unescape(no_tags))
