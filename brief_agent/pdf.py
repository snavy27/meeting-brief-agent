"""Render the daily packet markdown to a clean PDF (delivery layer).

This is a presentation-only adapter: it takes the markdown produced by
`brief_agent.daily.render_packet` and lays it out as a one-page-per-brief PDF using fpdf2 (pure
Python, no system dependencies). It invents nothing and reads no credentials — the only input is
the already-rendered packet text, so no secret can ever leak through here.

Layout (presentation only — the packet markdown is unchanged):
  * Page 1 is a day-at-a-glance agenda: every meeting in start order with time, person, company
    and status (briefed / no CRM match / skipped-internal / could not complete).
  * Each brief then gets its own page: a compact one-line header (time, who, purpose), the body
    left-aligned, and a small grey footer carrying Sources + clickable Web citations.

fpdf2 core fonts are the 14 standard PDF fonts, which use WinAnsi (cp1252) encoding. Setting
`core_fonts_encoding="cp1252"` lets the briefs' typography — en/em dash, curly quotes, ellipsis,
bullet, and latin-1 accents (the é in "Estée") — render as real glyphs. `_to_cp1252` normalises
em dashes to a single consistent en dash, maps the few codepoints outside cp1252 (e.g. arrow),
and replaces anything still unencodable so rendering never raises (a CJK/Cyrillic name becomes
'?' — embedding a Unicode TTF is the known follow-up).
"""

import re
from urllib.parse import urlparse

from fpdf import FPDF
from fpdf.enums import XPos, YPos

# Punctuation the briefs use → cp1252-safe. Em/figure dashes collapse to ONE en dash so the whole
# packet uses a single, consistent dash; the arrow has no cp1252 glyph so it degrades to "->".
_SUBST = {
    "—": "–",    # em dash  → en dash (one consistent dash throughout)
    "‒": "–",    # figure dash → en dash
    "→": "->",   # right arrow (outside cp1252)
    "•": "-",    # inline bullet (we draw our own bullet glyphs)
}

# Precomputed translation table (ordinal -> replacement) so substitution is one pass, not N scans.
_TRANSLATE = {ord(src): dst for src, dst in _SUBST.items()}


def _to_cp1252(s: str) -> str:
    """Normalise to a cp1252 (WinAnsi) string — the encoding fpdf2 core fonts render.

    Keeps en dash, curly quotes, ellipsis, bullet and latin-1 accents as real glyphs; replaces any
    remaining out-of-cp1252 codepoint (e.g. a CJK name) with '?' so rendering never raises.
    """
    return s.translate(_TRANSLATE).encode("cp1252", "replace").decode("cp1252")


# --------------------------------------------------------------------------- #
# Colours + status labels (presentation only)
# --------------------------------------------------------------------------- #
_LINK = (40, 90, 160)
_GREY = (120, 120, 120)
_STATUS = {
    "briefed": ("briefed", (30, 120, 60)),
    "stub": ("no CRM match", (170, 110, 20)),
    "skipped": ("skipped-internal", (130, 130, 130)),
    "failed": ("could not complete", (185, 55, 55)),
}

_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[–-]\s*\d{1,2}:\d{2}")
_FIELD_MARK = re.compile(r"\*\*(Meeting|When|Who|Purpose|Sources):\*\*")


# --------------------------------------------------------------------------- #
# Markdown → a small structured model (so the renderer can restructure the header,
# move sources to a footer, and build the agenda — without touching the packet markdown).
# --------------------------------------------------------------------------- #
class _Brief:
    """One parsed brief: its title, parsed metadata fields, body lines, status, and web URLs."""

    def __init__(self, title: str):
        self.title = title
        self.time = ""
        self.who = ""
        self.purpose = ""
        self.sources = ""        # CRM sources text (web stripped out)
        self.web: list[str] = []
        self.body: list[str] = []
        self.status = "briefed"  # "briefed" | "stub"

    # -- derived, for the agenda row ------------------------------------- #
    @property
    def person(self) -> str:
        if not self.who:
            return ""
        return re.split(r"[,(]|\s[—–]\s", self.who, maxsplit=1)[0].strip()

    @property
    def company(self) -> str:
        # Briefed: the CRM "<Company> (account)" is the most reliable source of the company name.
        m = re.search(r"([^;,(]+?)\s*\(account\)", self.sources)
        if m:
            return m.group(1).strip()
        # Stub: the deterministic stub body says "<person> / <company> was not found ...".
        for ln in self.body:
            m = re.search(r"/\s*(.+?)\s+was not found", ln)
            if m:
                return m.group(1).strip()
        # Fallback: a company after an em/en dash in the title ("Intro call — Quantum Robotics").
        m = re.search(r"\s[—–]\s*(.+)$", self.title)
        return m.group(1).strip() if m else ""

    @property
    def start_min(self) -> int:
        return _start_min(self.time)


class _Packet:
    def __init__(self):
        self.day_title = ""
        self.summary = ""
        self.briefs: list[_Brief] = []
        self.skipped: list[tuple[str, str]] = []       # (time, title)
        self.failed: list[tuple[str, str]] = []         # (time, title)


def _start_min(timestr: str) -> int:
    m = re.search(r"(\d{1,2}):(\d{2})", timestr or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 9999


def _time_range(when: str) -> str:
    """Pull the compact 'HH:MM–HH:MM' out of a full when-string; fall back to the raw text."""
    m = _TIME_RE.search(when or "")
    return m.group(0).replace("-", "–").replace(" ", "") if m else (when or "").strip()


def _parse_fields(meta: str) -> dict:
    """Split a '**Field:** value · **Field:** value …' metadata blob into a {field: value} dict.

    Splitting on markers (not ' · ') keeps values that themselves contain ' · ' intact — notably
    the Sources field, whose trailing '· Web: <url> · <url>' must stay attached for the footer.
    """
    out: dict[str, str] = {}
    marks = list(_FIELD_MARK.finditer(meta))
    for i, m in enumerate(marks):
        name = m.group(1).lower()
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(meta)
        out[name] = meta[start:end].strip().strip("·").strip()
    return out


def _split_when_title(line: str) -> tuple[str, str]:
    """A '- <when> — <title>' agenda/skipped line → (time, title)."""
    body = line[2:] if line.startswith("- ") else line
    parts = re.split(r"\s[—–]\s", body, maxsplit=1)
    when = parts[0].strip()
    title = parts[1].strip() if len(parts) > 1 else ""
    return _time_range(when), title


def _parse_packet(md: str) -> _Packet:
    """Parse the packet markdown into briefs + skipped + failed. Defensive: arbitrary markdown
    (e.g. a stray '# Heading') simply becomes a brief so no content is ever dropped."""
    pkt = _Packet()
    lines = md.split("\n")

    # Split into top-level blocks on '# ' headings.
    blocks: list[tuple[str, list[str]]] = []
    cur_head: str | None = None
    cur: list[str] = []
    for raw in lines:
        ln = raw.rstrip()
        if ln.startswith("# "):
            if cur_head is not None or cur:
                blocks.append((cur_head, cur))
            cur_head, cur = ln[2:].strip(), []
        else:
            cur.append(ln)
    if cur_head is not None or cur:
        blocks.append((cur_head, cur))

    for head, body in blocks:
        if head and head.startswith("Daily briefing"):
            pkt.day_title = head
            _parse_day_header(pkt, body)
        elif head is not None:
            pkt.briefs.append(_parse_brief(head, body))
    return pkt


def _parse_day_header(pkt: _Packet, body: list[str]) -> None:
    section = None
    for ln in body:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("## Skipped"):
            section = "skipped"
        elif s.startswith("## Could not complete"):
            section = "failed"
        elif s.startswith("## "):
            section = None
        elif s.startswith("- "):
            when, title = _split_when_title(s)
            if section == "skipped":
                pkt.skipped.append((when, title))
            elif section == "failed":
                # '- <when> — <title> — <reason>': drop the reason for the one-line agenda.
                title = re.split(r"\s[—–]\s", title)[0].strip() if " — " in s else title
                pkt.failed.append((when, title))
        elif s == "_None._" or s == "---":
            continue
        elif not pkt.summary and "meeting" in s.lower():
            pkt.summary = s


def _parse_brief(title: str, body: list[str]) -> _Brief:
    b = _Brief(title)
    i, n = 0, len(body)
    while i < n and not body[i].strip():
        i += 1
    meta: list[str] = []
    while i < n:
        s = body[i].strip()
        if not s:
            i += 1
            continue
        if _FIELD_MARK.search(s):
            meta.append(s)
            i += 1
            continue
        break
    b.body = body[i:]

    fields = _parse_fields(" ".join(meta))
    b.time = _time_range(fields.get("meeting") or fields.get("when", ""))
    b.who = _trim_who(fields.get("who", ""))
    b.purpose = fields.get("purpose", "")
    sources_val = fields.get("sources", "")
    b.web = re.findall(r"https?://[^\s·,]+", sources_val)
    b.sources = re.split(r"\s*·?\s*Web:\s*", sources_val)[0].strip().strip("·").strip()
    if b.sources.lower().startswith("no crm match") or any(
        "no crm match" in ln.lower() for ln in b.body
    ):
        b.status = "stub"
    return b


def _trim_who(who: str) -> str:
    """'Name, Role, Company — extra commentary' → 'Name, Role, Company' (drop the commentary).

    Only trims when the part before the dash is itself comma-structured (the real-data shape), so a
    'Name — Role' who is left whole.
    """
    head = re.split(r"\s[—–]\s", who, maxsplit=1)[0]
    return head.strip() if "," in head else who.strip()


# --------------------------------------------------------------------------- #
# PDF document
# --------------------------------------------------------------------------- #
class _Doc(FPDF):
    """A4 page with a small running footer (page n of N) — nothing else fancy."""

    def footer(self) -> None:  # noqa: D401 - fpdf2 hook
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"{self.page_no()} / {{nb}}", align="C")


def _fit(pdf: _Doc, text: str, width: float) -> str:
    """Trim `text` (adding an ellipsis) until it fits in `width` mm — keeps agenda columns tidy.

    Sanitises to cp1252 first so the width measurement never trips on an out-of-font codepoint.
    """
    text = _to_cp1252(text)
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


# -- agenda (page 1) ---------------------------------------------------------- #
def _render_agenda(pdf: _Doc, pkt: _Packet) -> None:
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(20, 20, 20)
    pdf.multi_cell(0, 8, _to_cp1252(pkt.day_title or "Daily briefing"),
                   align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    if pkt.summary:
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*_GREY)
        pdf.multi_cell(0, 5, _to_cp1252(pkt.summary), align="L",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2.5)

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(0, 5, "AGENDA", align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(210, 210, 210)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(2)

    for time, mid, status in _agenda_rows(pkt):
        _agenda_row(pdf, time, mid, status)


def _agenda_rows(pkt: _Packet):
    """Every meeting as (time, 'Person · Company' / title, status_key), in start order."""
    rows = []
    for b in pkt.briefs:
        mid = " · ".join(p for p in (b.person, b.company) if p) or b.title
        rows.append((b.start_min, b.time, mid, b.status))
    for time, title in pkt.skipped:
        rows.append((_start_min(time), time, title, "skipped"))
    for time, title in pkt.failed:
        rows.append((_start_min(time), time, title, "failed"))
    rows.sort(key=lambda r: r[0])
    return [(t, m, s) for _, t, m, s in rows]


def _agenda_row(pdf: _Doc, time: str, mid: str, status_key: str) -> None:
    label, colour = _STATUS.get(status_key, (status_key, (90, 90, 90)))
    full = pdf.w - pdf.l_margin - pdf.r_margin
    w_time, w_status = 26.0, 36.0
    w_mid = full - w_time - w_status

    pdf.set_font("Helvetica", "B", 9.5)
    pdf.set_text_color(40, 40, 40)
    pdf.cell(w_time, 6, _to_cp1252(time), new_x=XPos.RIGHT, new_y=YPos.TOP)

    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(w_mid, 6, _fit(pdf, mid, w_mid - 2), new_x=XPos.RIGHT, new_y=YPos.TOP)

    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*colour)
    pdf.cell(w_status, 6, _to_cp1252(label), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


# -- one brief per page ------------------------------------------------------- #
def _render_brief(pdf: _Doc, b: _Brief) -> None:
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(20, 20, 20)
    pdf.multi_cell(0, 7, _to_cp1252(b.title), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    head = "  ·  ".join(p for p in (b.time, b.who, b.purpose) if p)
    if head:
        pdf.ln(0.5)
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(0, 5, _to_cp1252(head), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1.5)

    for raw in b.body:
        line = raw.rstrip()
        if line.strip() in ("", "---"):
            if line.strip() == "":
                pdf.ln(1.6)
            continue
        if line.startswith("## "):
            _section(pdf, line[3:])
        elif line.startswith("- "):
            _bullet(pdf, line[2:])
        else:
            _paragraph(pdf, line)

    _sources_footer(pdf, b)


def _section(pdf: _Doc, text: str) -> None:
    pdf.ln(1.2)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.set_text_color(40, 70, 120)
    pdf.multi_cell(0, 5.2, _to_cp1252(text), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(0.3)


def _paragraph(pdf: _Doc, text: str) -> None:
    stub = len(text) >= 2 and text.startswith("_") and text.endswith("_")
    body = text[1:-1] if stub else text
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "I" if stub else "", 9.5)
    # markdown=True interprets the briefs' **bold** runs; align left to avoid stretched-gap rivers.
    pdf.multi_cell(0, 4.7, _to_cp1252(body), markdown=True, align="L",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _bullet(pdf: _Doc, text: str) -> None:
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "", 9.5)
    left = pdf.l_margin
    pdf.set_x(left + 3)
    pdf.cell(4, 4.7, "–")
    pdf.multi_cell(0, 4.7, _to_cp1252(text), markdown=True, align="L",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _sources_footer(pdf: _Doc, b: _Brief) -> None:
    """A small grey footer at the end of the brief: Sources, then clickable [n] web citations."""
    if not (b.sources or b.web):
        return
    pdf.ln(2.5)
    pdf.set_draw_color(220, 220, 220)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(1.8)

    if b.sources:
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_GREY)
        pdf.multi_cell(0, 3.8, _to_cp1252("Sources  " + b.sources), align="L",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    for n, url in enumerate(b.web, 1):
        domain = urlparse(url).netloc.removeprefix("www.") or url
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*_GREY)
        pdf.cell(pdf.get_string_width(f"[{n}] ") + 0.5, 3.8, f"[{n}] ",
                 new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.set_text_color(*_LINK)
        pdf.cell(0, 3.8, _to_cp1252(domain), link=url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def render_packet_pdf(markdown_text: str, *, title: str = "Meeting briefs") -> bytes:
    """Render packet markdown to PDF bytes: a page-1 agenda, then one page per brief."""
    pdf = _Doc(format="A4", unit="mm")
    pdf.core_fonts_encoding = "cp1252"  # WinAnsi: en dash, curly quotes, ellipsis as real glyphs
    pdf.set_title(_to_cp1252(title))
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(16, 14, 16)
    pdf.alias_nb_pages()

    pkt = _parse_packet(markdown_text)
    _render_agenda(pdf, pkt)
    for b in pkt.briefs:
        _render_brief(pdf, b)

    return bytes(pdf.output())
