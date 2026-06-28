"""Render the daily packet markdown to a clean PDF (delivery layer).

This is a presentation-only adapter: it takes the markdown produced by
`brief_agent.daily.render_packet` and lays it out as a one-page-per-brief PDF using fpdf2 (pure
Python, no system dependencies). It invents nothing and reads no credentials — the only input is
the already-rendered packet text, so no secret can ever leak through here.

fpdf2 core fonts (Helvetica) encode latin-1 only, while the packet uses a little Unicode
punctuation (em/en dash, curly quotes, ellipsis). `_to_latin1` maps those to ASCII and replaces any
remaining non-latin-1 codepoint, so rendering never raises (latin-1 accents like the é in "Estée"
survive untouched).
"""

from fpdf import FPDF
from fpdf.enums import XPos, YPos

# Unicode punctuation the briefs use → latin-1-safe ASCII. Everything else is encode-replaced.
_SUBST = {
    "—": "-",    # em dash —
    "–": "-",    # en dash –
    "‒": "-",    # figure dash
    "‘": "'",    # left single quote
    "’": "'",    # right single quote / apostrophe
    "“": '"',    # left double quote
    "”": '"',    # right double quote
    "…": "...",  # ellipsis …
    "•": "-",    # bullet •
    " ": " ",    # non-breaking space
    "→": "->",   # right arrow →
}


def _to_latin1(s: str) -> str:
    """Map smart punctuation to ASCII, then drop anything still outside latin-1 (no exceptions)."""
    for src, dst in _SUBST.items():
        s = s.replace(src, dst)
    return s.encode("latin-1", "replace").decode("latin-1")


class _Packet(FPDF):
    """A4 page with a small running footer (page n of N) — nothing else fancy."""

    def footer(self) -> None:  # noqa: D401 - fpdf2 hook
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"{self.page_no()} / {{nb}}", align="C")


def _heading(pdf: _Packet, text: str, level: int) -> None:
    if level == 1:
        pdf.set_font("Helvetica", "B", 15)
        pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, 7.5, _to_latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1.5)
    else:
        pdf.ln(1.5)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(40, 70, 120)
        pdf.multi_cell(0, 6, _to_latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(0.5)


def _paragraph(pdf: _Packet, text: str) -> None:
    stub = len(text) >= 2 and text.startswith("_") and text.endswith("_")
    body = text[1:-1] if stub else text
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "I" if stub else "", 10)
    # markdown=True interprets the briefs' **bold** runs (e.g. the metadata line, who-you're-meeting).
    pdf.multi_cell(0, 5.2, _to_latin1(body), markdown=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _bullet(pdf: _Packet, text: str) -> None:
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "", 10)
    left = pdf.l_margin
    pdf.set_x(left + 3)
    pdf.cell(4, 5.2, "-")
    pdf.multi_cell(
        0, 5.2, _to_latin1(text), markdown=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT
    )


def _rule(pdf: _Packet) -> None:
    """A thin horizontal rule for a markdown `---` (NOT a page break — pages break on `# `)."""
    pdf.ln(1.5)
    pdf.set_draw_color(210, 210, 210)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(2.5)


def render_packet_pdf(markdown_text: str, *, title: str = "Meeting briefs") -> bytes:
    """Render packet markdown to PDF bytes. Each top-level `# ` heading starts a new page."""
    pdf = _Packet(format="A4", unit="mm")
    pdf.set_title(_to_latin1(title))
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(18, 16, 18)
    pdf.alias_nb_pages()

    for raw in markdown_text.split("\n"):
        line = raw.rstrip()
        if line.startswith("# "):
            pdf.add_page()  # day header and every brief begin on a fresh page
            _heading(pdf, line[2:], level=1)
            continue
        if pdf.page_no() == 0:
            pdf.add_page()  # defensive: content before any `# ` still needs a page
        if line.startswith("## "):
            _heading(pdf, line[3:], level=2)
        elif line.strip() == "---":
            _rule(pdf)
        elif line.startswith("- "):
            _bullet(pdf, line[2:])
        elif line.strip() == "":
            pdf.ln(2.5)
        else:
            _paragraph(pdf, line)

    return bytes(pdf.output())
