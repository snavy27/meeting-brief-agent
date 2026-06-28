"""Offline tests for the packet -> PDF renderer (no network, no credentials).

Verifies the renderer produces a valid PDF and never raises on the Unicode punctuation the real
briefs contain (em/en dash, curly quotes, ellipsis, accented latin-1 like the é in "Estée").
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief_agent.pdf import _to_cp1252, render_packet_pdf  # noqa: E402

# A packet-shaped sample with every shape the real renderer must handle, plus tricky characters.
_SAMPLE = """# Daily briefing — Monday 29 June 2026

2 meetings · 1 briefed / 0 unresolved / 1 skipped (internal)

## Skipped (internal)
- Mon 29 Jun 2026, 11:00–11:30 (Europe/Paris) — Internal: standup

---

# Meeting Brief — Renewal sync with Sarah Chen (CEO, Estée Lauder)

**Meeting:** Mon 29 Jun 2026, 09:00–09:30 (Europe/Paris) · **Who:** Sarah Chen — CEO

---

## Bottom line
Pricing is the “sticking point” … keep it strategic, not transactional.

## Who you're meeting
- **Sarah Chen** — CEO and economic buyer; prefers big-picture partnership talk.

# Meeting Brief — Intro call — Quantum Robotics

**When:** Mon 29 Jun 2026, 13:00–13:30 (Europe/Paris) · **Who:** Jane Doe
**Sources:** No CRM match — calendar details only.

_No CRM match — calendar details only. Confirm the record in Notion before the meeting._
"""


def test_to_cp1252_normalises_dashes_and_keeps_typography():
    out = _to_cp1252("em—dash en–dash “curly” ‘quotes’ ellipsis… arrow→")
    # Em dash collapses to ONE consistent en dash; en dash, curly quotes and ellipsis survive
    # as real glyphs (cp1252 / WinAnsi); the arrow has no cp1252 glyph so it degrades to "->".
    assert "—" not in out and "–" in out
    assert "“curly”" in out and "‘quotes’" in out and "…" in out
    assert "->" in out
    # The whole result must be cp1252-encodable (so fpdf2 core fonts never raise on it).
    out.encode("cp1252")
    # An accented char survives; a non-cp1252 char is replaced, not raised.
    assert _to_cp1252("Estée") == "Estée"
    assert isinstance(_to_cp1252("emoji 🚀 here"), str)


def test_render_returns_valid_pdf_bytes():
    pdf = render_packet_pdf(_SAMPLE, title="Meeting briefs — test")
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 1500  # a non-trivial multi-page document


def test_render_handles_unicode_without_exception():
    # Pure-Unicode content must not raise even though core fonts are latin-1.
    tricky = "# Título — café ☕\n\n## Señor “quote”\n\n- bullet — café…\n"
    pdf = render_packet_pdf(tricky, title="unicode")
    assert pdf.startswith(b"%PDF")


def test_render_real_packet_if_present():
    # If a generated packet exists, render it too (belt-and-braces on real content).
    sample = Path(__file__).resolve().parents[1] / "day-2026-06-29.md"
    if sample.exists():
        pdf = render_packet_pdf(sample.read_text(encoding="utf-8"), title="real")
        assert pdf.startswith(b"%PDF") and len(pdf) > 3000


if __name__ == "__main__":
    test_to_cp1252_normalises_dashes_and_keeps_typography()
    test_render_returns_valid_pdf_bytes()
    test_render_handles_unicode_without_exception()
    test_render_real_packet_if_present()
    print("test_pdf_unit: all passed")
