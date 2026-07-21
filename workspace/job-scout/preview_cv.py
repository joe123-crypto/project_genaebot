#!/usr/bin/env python3
"""Preview the styled, ready CV template for a given CV source.

Runs a CV through the exact production pipeline used at dispatch time so you can
eyeball what an employer receives, without a live agent or R2 access:

  * a .pdf source is converted to unstyled semantic HTML (convert_pdf_to_cv_html)
  * an .html source is used as-is (base/canonical, or an agent-personalized file
    from a claim's personalized/ directory)
  * the modern stylesheet is injected on the fly (style_cv_html)
  * optionally rendered to PDF with local headless Chromium (no browser chrome,
    matching the production Playwright worker)

Usage:
  PYTHONPATH=.:../lib:../account-link python3 preview_cv.py <source.pdf|source.html> [out_basename]

Outputs <out_basename>.html (always) and <out_basename>.pdf (if chromium is found)
next to the source, defaulting to "<source-stem>-preview".
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from jobscout.commands_setup import convert_pdf_to_cv_html, style_cv_html


def _semantic_html(source: Path) -> str:
  if source.suffix.casefold() == ".pdf":
    with tempfile.TemporaryDirectory() as tmp:
      html_path = Path(tmp) / "cv.html"
      convert_pdf_to_cv_html(source, html_path)
      return html_path.read_text(encoding="utf-8")
  return source.read_text(encoding="utf-8")


def _render_pdf(html_path: Path, pdf_path: Path) -> bool:
  chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
  if not chromium:
    return False
  subprocess.run(
    [chromium, "--headless", "--disable-gpu", "--no-sandbox",
     "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", str(html_path)],
    check=False, capture_output=True, timeout=120,
  )
  return pdf_path.is_file()


def main(argv: list[str]) -> int:
  if not argv:
    print(__doc__)
    return 2
  source = Path(argv[0]).expanduser()
  if not source.is_file():
    print(f"source not found: {source}", file=sys.stderr)
    return 1
  base = argv[1] if len(argv) > 1 else f"{source.stem}-preview"
  out_html = source.with_name(f"{base}.html")
  out_pdf = source.with_name(f"{base}.pdf")

  styled = style_cv_html(_semantic_html(source))
  out_html.write_text(styled, encoding="utf-8")
  print(f"styled HTML: {out_html}")
  if _render_pdf(out_html, out_pdf):
    print(f"rendered PDF: {out_pdf}")
  else:
    print("chromium not found; open the HTML in a browser to preview.", file=sys.stderr)
  return 0


if __name__ == "__main__":
  raise SystemExit(main(sys.argv[1:]))
