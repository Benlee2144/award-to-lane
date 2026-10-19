"""pdfplumber words -> lines with column lookups for the SAP 'PCA' purchase award report.

The report has no ruled lines, so pdfplumber's table finder sees nothing. The columns are fixed x
positions instead (measured across every sample; one older report version drifts by 1-2 points).
"""
from __future__ import annotations

from dataclasses import dataclass

X_TOLERANCE = 1.5
Y_TOLERANCE = 2
LINE_GAP = 3.0

# column name -> [x0 lower bound, x0 upper bound) of the first character of a word in that column
COLUMNS: dict[str, tuple[float, float]] = {
    "contractor": (0, 127),
    "material": (127, 186),
    "plant": (186, 243),
    "bid_item": (243, 287),
    "dest": (287, 340),
    "qty": (340, 415),   # 14-digit totals start as far left as x=345
    "uom": (415, 443),
    "price": (443, 700),
}


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float


@dataclass
class Line:
    page: int
    top: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def first(self) -> Word:
        return self.words[0]

    def in_col(self, name: str) -> list[Word]:
        lo, hi = COLUMNS[name]
        return [w for w in self.words if lo <= w.x0 < hi]

    def col_text(self, name: str) -> str:
        return " ".join(w.text for w in self.in_col(name))

    def min_x0(self) -> float:
        return min(w.x0 for w in self.words)


def page_lines(page, page_no: int) -> list[Line]:
    words = page.extract_words(x_tolerance=X_TOLERANCE, y_tolerance=Y_TOLERANCE, keep_blank_chars=False)
    words.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
    lines: list[Line] = []
    for w in words:
        word = Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"]))
        if lines and abs(word.top - lines[-1].top) <= LINE_GAP:
            lines[-1].words.append(word)
        else:
            lines.append(Line(page_no, word.top, [word]))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    return lines


def page_flow_text(page) -> str:
    """Narrative text. use_text_flow repairs most of pdfplumber's shifted spaces ("fromN ovember")."""
    return page.extract_text(x_tolerance=3, use_text_flow=True) or ""
