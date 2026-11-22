"""Printable tag16h5 race bib generator (IDs 0-29) and OpenCV marker helpers."""
from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Iterable, List

import cv2
import numpy as np

TAG16H5_COUNT = 30
VALID_PER_PAGE = (1, 2, 6, 15, 30)


def get_tag16h5_matrix(tag_id: int) -> np.ndarray:
    """Return the 6x6 uint8 (0 or 255) marker grid for tag16h5 ID 0..29."""
    if not (0 <= tag_id < TAG16H5_COUNT):
        raise ValueError(f"tag16h5 supports IDs 0..{TAG16H5_COUNT - 1}, got {tag_id}")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_16h5)
    return cv2.aruco.generateImageMarker(dictionary, tag_id, 6)


def render_tag16h5_image(tag_id: int, tag_px: int = 240, border_cells: int = 1) -> np.ndarray:
    """Render a crisp grayscale uint8 image of tag16h5 with a white quiet-zone border."""
    grid = get_tag16h5_matrix(tag_id)
    total_cells = 6 + 2 * border_cells
    padded = np.full((total_cells, total_cells), 255, dtype=np.uint8)
    padded[border_cells : border_cells + 6, border_cells : border_cells + 6] = grid
    total_px = int(round(tag_px * (total_cells / 6.0)))
    return cv2.resize(padded, (total_px, total_px), interpolation=cv2.INTER_NEAREST)


def render_tag16h5_svg(tag_id: int) -> str:
    """Render a crisp, resolution-independent SVG with a 1-module white quiet zone (8x8 viewBox)."""
    grid = get_tag16h5_matrix(tag_id)
    rects = ['<rect x="0" y="0" width="8" height="8" fill="#ffffff"/>']
    for r in range(6):
        for c in range(6):
            if grid[r, c] == 0:
                rects.append(
                    f'<rect x="{c + 1}" y="{r + 1}" width="1" height="1" fill="#000000"/>'
                )
    inner = "".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8" '
        f'shape-rendering="crispEdges" role="img" aria-label="AprilTag 16h5 ID {tag_id}">'
        f"{inner}</svg>"
    )


def render_printable_bibs_html(
    tag_ids: Iterable[int] = range(TAG16H5_COUNT),
    per_page: int = 6,
    event_title: str = "Trackside AprilTag Race Bibs",
) -> str:
    """Generate a tight, zero-waste printable HTML sheet of tag16h5 race bibs (IDs 0..29)."""
    per_page = per_page if per_page in VALID_PER_PAGE else 6
    ids: List[int] = [tid for tid in tag_ids if 0 <= tid < TAG16H5_COUNT]
    safe_title = html.escape(event_title)

    # Chunk into explicit physical pages so CSS Grid never overflows onto half-blank extra sheets
    pages_html: List[str] = []
    for page_start in range(0, len(ids), per_page):
        chunk = ids[page_start : page_start + per_page]
        cards: List[str] = []
        for tid in chunk:
            svg = render_tag16h5_svg(tid)
            bib_num = f"#{tid:02d}"
            if per_page == 2:
                # Half-sheet landscape bib: maximize 4.8in tag on left + bold bib readout on right
                cards.append(
                    f"""
                    <article class="bib-cell cell-2">
                      <div class="tag-wrap">{svg}</div>
                      <div class="bib-side-meta">
                        <span class="bib-event">{safe_title}</span>
                        <span class="bib-number">{bib_num}</span>
                        <span class="bib-family">tag16h5 &middot; ID {tid}</span>
                        <span class="bib-hint">Keep white border uncovered</span>
                      </div>
                    </article>
                    """
                )
            elif per_page == 1:
                cards.append(
                    f"""
                    <article class="bib-cell cell-1">
                      <div class="bib-top-meta">
                        <span>{safe_title}</span>
                        <span>APRILTAG 16h5 &middot; ID {tid}</span>
                      </div>
                      <div class="tag-wrap">{svg}</div>
                      <div class="bib-bottom-meta">
                        <span class="bib-number">{bib_num}</span>
                        <span class="bib-hint">Keep white border uncovered &bull; Pin flat on chest</span>
                      </div>
                    </article>
                    """
                )
            else:
                # Tight zero-margin cut grid for 6/page (5 sheets), 15/page (2 sheets), 30/page (1 sheet)
                cards.append(
                    f"""
                    <article class="bib-cell cell-{per_page}">
                      <div class="tag-wrap">{svg}</div>
                      <div class="bib-compact-bar">
                        <span class="bib-number">{bib_num}</span>
                        <span class="bib-family">16h5 &middot; ID {tid}</span>
                      </div>
                    </article>
                    """
                )
        pages_html.append(
            f'<section class="print-page page-{per_page}">{"".join(cards)}</section>'
        )

    total_sheets = len(pages_html)
    nav_links = []
    labels = {
        1: "1 / page (30 sheets · XL)",
        2: "2 / page (15 sheets · Bibs)",
        6: "6 / page (5 sheets)",
        15: "15 / page (2 sheets)",
        30: "30 / page (1 sheet)",
    }
    for opt in VALID_PER_PAGE:
        cls = "toolbar-link active" if opt == per_page else "toolbar-link"
        nav_links.append(f'<a class="{cls}" href="?per_page={opt}">{labels[opt]}</a>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="only light">
  <title>{safe_title} ({per_page}/page &middot; {total_sheets} sheet{"s" if total_sheets != 1 else ""})</title>
  <style>
    :root {{
      color-scheme: only light;
      background-color: #e4e4e7;
      color: #09090b;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 0;
      background-color: #e4e4e7;
      color: #09090b;
    }}
    .print-toolbar {{
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.85rem;
      padding: 0.75rem 1.25rem;
      background: #18181b;
      color: #fafafa;
      border-bottom: 1px solid #3f3f46;
    }}
    .toolbar-left h1 {{
      margin: 0;
      font-size: 1rem;
      font-weight: 700;
    }}
    .toolbar-left p {{
      margin: 0.15rem 0 0;
      font-size: 0.78rem;
      color: #a1a1aa;
    }}
    .toolbar-actions {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      flex-wrap: wrap;
    }}
    .toolbar-link, .print-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 0.35rem 0.75rem;
      border-radius: 0.45rem;
      font-size: 0.8rem;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
      border: 1px solid #52525b;
      background: #27272a;
      color: #fafafa;
    }}
    .toolbar-link.active {{
      border-color: #22d3ee;
      background: #164e63;
      color: #ecfeff;
    }}
    .print-btn {{
      background: #10b981;
      border-color: #059669;
      color: #052e16;
      font-weight: 700;
    }}
    .pages-stack {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.25rem;
      padding: 1.25rem 1rem 2.5rem;
    }}

    /* Exact US Letter printable area (8.0in x 10.4in inside 0.25in margins) */
    .print-page {{
      width: 8.0in;
      height: 10.4in;
      background: #ffffff;
      color: #09090b;
      display: grid;
      border-top: 1px dashed #a1a1aa;
      border-left: 1px dashed #a1a1aa;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.14);
      overflow: hidden;
    }}
    .page-1 {{ grid-template-columns: 1fr; grid-template-rows: 1fr; }}
    .page-2 {{ grid-template-columns: 1fr; grid-template-rows: repeat(2, 1fr); }}
    .page-6 {{ grid-template-columns: repeat(2, 1fr); grid-template-rows: repeat(3, 1fr); }}
    .page-15 {{ grid-template-columns: repeat(3, 1fr); grid-template-rows: repeat(5, 1fr); }}
    .page-30 {{ grid-template-columns: repeat(5, 1fr); grid-template-rows: repeat(6, 1fr); }}

    /* Shared single-cut dashed borders with zero gutter waste */
    .bib-cell {{
      border-right: 1px dashed #a1a1aa;
      border-bottom: 1px dashed #a1a1aa;
      background: #ffffff;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: space-between;
      overflow: hidden;
    }}

    .tag-wrap {{
      display: flex;
      align-items: center;
      justify-content: center;
      background: #ffffff;
      flex: 1;
      width: 100%;
      min-height: 0;
    }}
    .tag-wrap svg {{
      display: block;
    }}

    /* 1 / page (8.0in x 10.4in) */
    .cell-1 {{ padding: 0.25in 0.3in; }}
    .cell-1 .bib-top-meta {{
      width: 100%;
      display: flex;
      justify-content: space-between;
      font-size: 0.9rem;
      font-weight: 800;
      text-transform: uppercase;
      border-bottom: 2px solid #09090b;
      padding-bottom: 0.08in;
    }}
    .cell-1 .tag-wrap svg {{ width: 7.4in; height: 7.4in; }}
    .cell-1 .bib-bottom-meta {{
      width: 100%;
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      border-top: 2px solid #09090b;
      padding-top: 0.08in;
    }}
    .cell-1 .bib-number {{ font-size: 3.6rem; font-weight: 900; line-height: 1; }}
    .cell-1 .bib-hint {{ font-size: 0.85rem; font-weight: 600; color: #3f3f46; }}

    /* 2 / page (8.0in x 5.2in side-by-side) */
    .cell-2 {{
      flex-direction: row;
      padding: 0.12in 0.2in;
      gap: 0.15in;
    }}
    .cell-2 .tag-wrap {{ flex: 0 0 auto; width: auto; }}
    .cell-2 .tag-wrap svg {{ width: 4.85in; height: 4.85in; }}
    .cell-2 .bib-side-meta {{
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: flex-start;
      gap: 0.08in;
      padding-left: 0.12in;
      border-left: 2px solid #09090b;
      height: 82%;
    }}
    .cell-2 .bib-event {{
      font-size: 0.72rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #3f3f46;
    }}
    .cell-2 .bib-number {{ font-size: 3.4rem; font-weight: 900; line-height: 1; letter-spacing: -0.03em; }}
    .cell-2 .bib-family {{ font-size: 0.85rem; font-weight: 700; }}
    .cell-2 .bib-hint {{ font-size: 0.7rem; color: #52525b; font-weight: 600; }}

    /* 6 / page (4.0in x 3.46in each -> 5 exact pages) */
    .cell-6 {{ padding: 0.04in 0.08in 0.06in; }}
    .cell-6 .tag-wrap svg {{ width: 3.04in; height: 3.04in; }}
    .bib-compact-bar {{
      width: 100%;
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      padding: 0 0.18in;
    }}
    .cell-6 .bib-number {{ font-size: 1.25rem; font-weight: 900; line-height: 1; }}
    .cell-6 .bib-family {{ font-size: 0.72rem; font-weight: 700; color: #3f3f46; }}

    /* 15 / page (2.66in x 2.08in each -> 2 exact pages) */
    .cell-15 {{ padding: 0.02in 0.05in 0.04in; }}
    .cell-15 .tag-wrap svg {{ width: 1.76in; height: 1.76in; }}
    .cell-15 .bib-compact-bar {{ padding: 0 0.12in; }}
    .cell-15 .bib-number {{ font-size: 0.92rem; font-weight: 900; line-height: 1; }}
    .cell-15 .bib-family {{ font-size: 0.62rem; font-weight: 700; color: #3f3f46; }}

    /* 30 / page (1.60in x 1.73in each -> 1 single sheet!) */
    .cell-30 {{ padding: 0.02in 0.03in 0.03in; }}
    .cell-30 .tag-wrap svg {{ width: 1.42in; height: 1.42in; }}
    .cell-30 .bib-compact-bar {{ padding: 0 0.06in; }}
    .cell-30 .bib-number {{ font-size: 0.78rem; font-weight: 900; line-height: 1; }}
    .cell-30 .bib-family {{ font-size: 0.54rem; font-weight: 700; color: #52525b; }}

    @page {{
      size: letter portrait;
      margin: 0.25in;
    }}
    @media print {{
      html, body {{
        background: #ffffff;
      }}
      .print-toolbar {{
        display: none !important;
      }}
      .pages-stack {{
        padding: 0;
        gap: 0;
        display: block;
      }}
      .print-page {{
        box-shadow: none;
        margin: 0 auto;
        break-inside: avoid;
        page-break-inside: avoid;
      }}
      .print-page:not(:last-child) {{
        break-after: page;
        page-break-after: always;
      }}
    }}
  </style>
</head>
<body>
  <header class="print-toolbar">
    <div class="toolbar-left">
      <h1>Printable AprilTag 16h5 Bibs &mdash; {total_sheets} Sheet{"s" if total_sheets != 1 else ""} ({per_page}/page)</h1>
      <p>Edge-to-edge shared cut lines with built-in white quiet zones. Print at 100% scale (Default/Minimum margins).</p>
    </div>
    <div class="toolbar-actions">
      {"".join(nav_links)}
      <a class="toolbar-link" href="/">Back to Tracker</a>
      <button type="button" class="print-btn" id="print-btn">Print / Save PDF</button>
    </div>
  </header>
  <main class="pages-stack">
    {"".join(pages_html)}
  </main>
  <script>
    document.getElementById("print-btn").addEventListener("click", () => window.print());
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate printable tag16h5 race bibs (IDs 0-29).")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent / "data" / "printable_tags.html"),
        help="Output HTML file path",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        choices=VALID_PER_PAGE,
        default=6,
        help="Number of bibs per printed page (choices: 1, 2, 6, 15, 30)",
    )
    args = parser.parse_args()
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_printable_bibs_html(per_page=args.per_page), encoding="utf-8")
    print(f"Wrote printable tag16h5 sheet ({args.per_page}/page) to {out_path}")


if __name__ == "__main__":
    main()
