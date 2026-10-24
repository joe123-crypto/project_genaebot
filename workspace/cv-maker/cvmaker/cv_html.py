"""Canonical CV HTML: build it, style it, convert a PDF into it, and vet it.

A CV built from interview facts and a CV converted from an uploaded PDF come out
as the same semantic, unstyled document; the stylesheet is injected on render.
`validate_cv_html_source` gates every document before it is rendered: no
scripts, no remote references, no browser print chrome.
"""

from __future__ import annotations

import html
import html as html_module
from pathlib import Path
import re
import subprocess
import tempfile


BROWSER_TIMESTAMP = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s+(?:AM|PM)$", re.IGNORECASE)
PAGE_COUNTER = re.compile(r"^\d+\s*/\s*\d+$")


def visible_lines_from_html(source: str) -> list[str]:
  without_styles = re.sub(r"<style\b[^>]*>.*?</style>", "\n", source, flags=re.IGNORECASE | re.DOTALL)
  visible = html.unescape(re.sub(r"<[^>]+>", "\n", without_styles))
  return [re.sub(r"\s+", " ", line).strip() for line in visible.splitlines() if line.strip()]


def validate_cv_html_source(source: str) -> str:
  text = str(source or "")
  if not text.strip():
    raise RuntimeError("CV HTML is empty")
  if not re.search(r"<html(?:\s|>)", text, flags=re.IGNORECASE) or not re.search(r"<body(?:\s|>)", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML must contain html and body elements")
  if re.search(r"<(?:script|iframe|object|embed|base)\b", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML contains an unsafe element")
  if re.search(r"\son[a-z]+\s*=|(?:href|src)\s*=\s*['\"]?\s*javascript:", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML contains executable content")
  if re.search(r"(?:href|src|srcset|poster|action)\s*=\s*['\"]?\s*(?:https?:)?//", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML must be self-contained")
  if re.search(r"(?:@import\s+(?:url\()?|url\()\s*['\"]?\s*(?:https?:)?//", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML must be self-contained")
  lines = visible_lines_from_html(text)
  if any(line.casefold().startswith(("file://", "about:blank")) for line in lines):
    raise RuntimeError("CV HTML contains browser print chrome")
  if any(BROWSER_TIMESTAMP.fullmatch(line) or PAGE_COUNTER.fullmatch(line) for line in lines):
    raise RuntimeError("CV HTML contains browser print chrome")
  return text


CV_HTML_STYLE = """\
  @page { size: A4; margin: 0; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #fff; color: #000; font-family: Arial, Helvetica, sans-serif; font-size: 10.5pt; line-height: 1.45; }
  main.cv { width: 210mm; min-height: 297mm; margin: 0 auto; padding: 14mm 18mm 13mm; }
  .cv-header { margin: 0 0 6mm; padding: 0 0 4mm; border-bottom: 1px solid #000; }
  h1 { margin: 0; font-size: 22pt; line-height: 1.1; font-weight: 700; }
  .headline { margin: 1.5mm 0 0; font-size: 12pt; text-transform: uppercase; letter-spacing: 0.4pt; }
  .cv-body { display: block; }
  section { break-inside: avoid; margin: 0 0 6mm; }
  h2 { margin: 0 0 3mm; padding: 0 0 1.5mm; font-size: 11pt; line-height: 1.2; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6pt; border-bottom: 1px solid #000; }
  p { margin: 0 0 3mm; }
  .entry { margin: 0 0 3.5mm; }
  .entry-head { display: flex; justify-content: space-between; align-items: baseline; gap: 4mm; }
  .entry-meta { flex: none; font-size: 9.5pt; }
  ul { margin: 0; padding: 0 0 0 5mm; list-style: disc; }
  li { margin: 0 0 1.5mm; }
  .skills { }
  .skill:not(:last-child)::after { content: ', '; }
  .contact-line { margin: 0 0 2mm; overflow-wrap: anywhere; }
  .language { display: flex; justify-content: space-between; gap: 3mm; margin: 0 0 1.5mm; max-width: 90mm; }
  .language .level { flex: none; }
  p, li { overflow-wrap: anywhere; }
  @media print { main.cv { margin: 0; } }
"""


SIDE_SECTION_TITLES = {"CONTACT", "SKILLS", "LANGUAGES", "REFERENCES"}
IMMUTABLE_SECTION_TITLES = {"CONTACT", "EDUCATION", "CERTIFICATIONS", "REFERENCES"}
KNOWN_SECTION_TITLES = SIDE_SECTION_TITLES | IMMUTABLE_SECTION_TITLES | {
  "ACHIEVEMENTS",
  "ADDITIONAL INFORMATION",
  "EMPLOYMENT",
  "EXPERIENCE",
  "INTERESTS",
  "PROFESSIONAL EXPERIENCE",
  "PROFESSIONAL PROFILE",
  "PROFESSIONAL SUMMARY",
  "PROFILE",
  "PROJECTS",
  "SUMMARY",
  "TRAINING",
  "WORK EXPERIENCE",
}
_BROWSER_TIMESTAMP = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s+(?:AM|PM)$", re.IGNORECASE)
_PAGE_COUNTER = re.compile(r"^\d+\s*/\s*\d+$")


def _clean_cv_line(value: str) -> str:
  line = " ".join(str(value or "").replace("\ufffd", "").split())
  if not line or line.casefold().startswith(("file://", "about:blank")):
    return ""
  if _BROWSER_TIMESTAMP.fullmatch(line) or _PAGE_COUNTER.fullmatch(line):
    return ""
  phone = re.search(r"\+\d[\d\s()\-]{6,}", line)
  if phone:
    return phone.group(0).strip()
  email = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]*", line)
  if email:
    return email.group(0).strip()
  line = re.sub(r"^[\u00a2\u00a9@]+\s*", "", line)
  return line


def _sanitized_cv_text(text: str) -> str:
  lines = [_clean_cv_line(line) for line in text.replace("\f", "\n").splitlines()]
  return "\n".join(lines).strip()


def _cv_text_blocks(text: str) -> list[list[str]]:
  """Blank-line-separated blocks of whitespace-normalized text lines."""
  blocks: list[list[str]] = []
  current: list[str] = []
  for raw_line in _sanitized_cv_text(text).splitlines():
    line = _clean_cv_line(raw_line)
    if line and line.rstrip(":").upper() in KNOWN_SECTION_TITLES:
      if current:
        blocks.append(current)
        current = []
      blocks.append([line])
      continue
    if line:
      current.append(line)
    elif current:
      blocks.append(current)
      current = []
  if current:
    blocks.append(current)
  return blocks


def _looks_like_heading(block: list[str]) -> bool:
  if len(block) != 1:
    return False
  line = block[0]
  if len(line) > 60:
    return False
  letters = [character for character in line if character.isalpha()]
  mostly_upper = bool(letters) and sum(character.isupper() for character in letters) / len(letters) > 0.8
  return mostly_upper or line.endswith(":")


def _section_title(block: list[str]) -> str:
  return block[0].rstrip(":").upper()


_BULLET_MARKER = re.compile(r"^[>\u00bb\u203a\u25b8\u2039\u2022\u2023\u2043\-]+\s*")


def _is_lone_marker(line: str) -> bool:
  """True when a line is nothing but a bullet marker (no following text)."""
  return bool(line) and not _BULLET_MARKER.sub("", line).strip()


def _join_orphaned_markers(block: list[str]) -> list[str]:
  """Fold a lone-marker line onto the following text line so no orphan survives."""
  joined: list[str] = []
  pending_marker = False
  for line in block:
    if _is_lone_marker(line):
      pending_marker = True
      continue
    if pending_marker and line:
      # Re-attach the dropped marker so the block still reads as a bullet list.
      line = f"\u25b8 {_BULLET_MARKER.sub('', line).strip()}"
    pending_marker = False
    if line:
      joined.append(line)
  return joined


def _render_cv_block(block: list[str]) -> str:
  block = _join_orphaned_markers(block)
  if not block:
    return ""
  cleaned = [_BULLET_MARKER.sub("", line).strip() for line in block]
  if all(re.match(r"^[>\u00bb\u203a\u25b8\u2039\u2022\u2023\u2043\-]", line) for line in block):
    items = "\n".join(f"          <li>{html_module.escape(item)}</li>" for item in cleaned if item)
    return f"        <ul>\n{items}\n        </ul>" if items else ""
  body = "<br>\n          ".join(html_module.escape(line) for line in cleaned if line)
  return f"        <p class=\"entry\">{body}</p>" if body else ""


def _repair_contact_blocks(blocks: list[list[str]]) -> list[list[str]]:
  repaired: list[list[str]] = []
  for block in blocks:
    lines: list[str] = []
    for line in block:
      if lines and "@" in lines[-1] and "@" not in line and re.fullmatch(r"[A-Za-z]{1,4}\.com", line):
        lines[-1] += line
      else:
        lines.append(line)
    lines = [re.sub(r"@gmai(?:i|l)+\.com$", "@gmail.com", line, flags=re.IGNORECASE) for line in lines]
    repaired.append(lines)
  return repaired


def _render_contact_line(line: str) -> str:
  """Tag a CONTACT line with location/phone/email so the stylesheet can add an icon."""
  text = _BULLET_MARKER.sub("", line).strip()
  if not text:
    return ""
  if "@" in text:
    kind = "email"
  elif re.search(r"\+?\d[\d\s()\-]{5,}", text):
    kind = "phone"
  else:
    kind = "location"
  return f'        <p class="contact-line {kind}">{html_module.escape(text)}</p>'


# A recognized fluency level trailing a LANGUAGES line, right-aligned in render.
_LANGUAGE_LEVEL = re.compile(
  r"[\s\-–—:]+(native|fluent|proficient|advanced|intermediate|conversational|basic|beginner|elementary|mother tongue)\.?$",
  re.IGNORECASE,
)


def _render_language_line(line: str) -> str:
  """Split a LANGUAGES entry into the name plus an optionally right-aligned level."""
  text = _BULLET_MARKER.sub("", line).strip()
  if not text:
    return ""
  match = _LANGUAGE_LEVEL.search(text)
  if not match:
    return f'        <p class="language"><span class="name">{html_module.escape(text)}</span></p>'
  name = text[: match.start()].strip(" \t-–—:") or text
  level = match.group(1)
  return (
    '        <p class="language">'
    f'<span class="name">{html_module.escape(name)}</span>'
    f'<span class="level">{html_module.escape(level.title())}</span>'
    "</p>"
  )


def _render_cv_section(title: str, blocks: list[list[str]]) -> str:
  title_attr = html_module.escape(title.title(), quote=True)
  immutable = f' data-cv-immutable="section-{title_attr}"' if title in IMMUTABLE_SECTION_TITLES else ""
  lines: list[str] = []
  if title == "CONTACT":
    blocks = _repair_contact_blocks(blocks)
  if title == "SKILLS":
    skills = [html_module.escape(line) for block in blocks for line in block if line]
    content = "\n".join(f'          <span class="skill">{skill}</span>' for skill in skills)
    lines.append(f'        <div class="skills">\n{content}\n        </div>')
  elif title == "CONTACT":
    for line in (line for block in blocks for line in block if line):
      lines.append(_render_contact_line(line))
  elif title == "LANGUAGES":
    for line in (line for block in blocks for line in block if line):
      rendered = _render_language_line(line)
      if rendered:
        lines.append(rendered)
  elif title in {"PROFESSIONAL PROFILE", "PROFILE", "SUMMARY", "PROFESSIONAL SUMMARY"}:
    paragraph = " ".join(line for block in blocks for line in block)
    lines.append(f"        <p>{html_module.escape(paragraph)}</p>")
  else:
    for block in blocks:
      rendered = _render_cv_block(block)
      if rendered:
        lines.append(rendered)
  if not lines:
    return ""
  return (
    f'      <section class="cv-section" data-cv-section="{title_attr}"{immutable}>\n'
    f"        <h2>{html_module.escape(title.title())}</h2>\n"
    + "\n".join(lines)
    + "\n      </section>"
  )


def cv_html_document(text: str, styled: bool = False) -> str:
  blocks = _cv_text_blocks(text)
  if not blocks:
    raise RuntimeError("no text could be extracted from the CV PDF")
  title = blocks[0][0]
  introductory = blocks[0][1:]
  remaining = blocks[1:]
  headline = ""
  if remaining and len(remaining[0]) == 1 and len(remaining[0][0]) <= 80:
    possible_headline = _section_title(remaining[0])
    if possible_headline not in KNOWN_SECTION_TITLES:
      headline = remaining.pop(0)[0]

  sections: list[tuple[str, list[list[str]]]] = []
  current_title = "CONTACT" if introductory else ""
  current_blocks: list[list[str]] = [introductory] if introductory else []
  for block in remaining:
    if _looks_like_heading(block):
      if current_title and current_blocks:
        sections.append((current_title, current_blocks))
      current_title = _section_title(block)
      current_blocks = []
    else:
      if not current_title:
        current_title = "PROFESSIONAL PROFILE"
      current_blocks.append(block)
  if current_title and current_blocks:
    sections.append((current_title, current_blocks))

  # A single-column CV: every section is stacked in one column in document
  # order (no sidebar/main divide), so the page parses cleanly for applicant
  # tracking systems and reads as one unified column.
  return _assemble_cv_html(title, headline, sections, styled=styled)


def _assemble_cv_html(
  title: str,
  headline: str,
  sections: list[tuple[str, list[list[str]]]],
  styled: bool = False,
) -> str:
  """Wrap rendered sections in the canonical professional-v2 document shell.

  Shared by the PDF-text path (cv_html_document) and the interview-facts path
  (cv_html_from_facts) so both emit byte-identical structure: the same
  data-cv-template header, data-cv-section/data-cv-immutable markers, and
  single-column body. The base CV is stored UNSTYLED (no <style> block); the
  modern stylesheet is injected at render time by style_cv_html(). A styled
  document is still available via styled=True for any caller that wants a
  standalone page.
  """
  rendered_sections = [_render_cv_section(name, values) for name, values in sections]
  headline_html = f'\n      <p class="headline">{html_module.escape(headline)}</p>' if headline else ""
  body_html = "\n".join(section for section in rendered_sections if section)
  style_html = f"<style>\n{CV_HTML_STYLE}</style>\n" if styled else ""
  return (
    "<!doctype html>\n"
    "<html lang=\"en\">\n"
    "<head>\n"
    "<meta charset=\"utf-8\">\n"
    f"<title>{html_module.escape(title)} - CV</title>\n"
    f"{style_html}"
    "</head>\n"
    "<body>\n"
    "  <main class=\"cv\" data-cv-template=\"professional-v2\">\n"
    "    <header class=\"cv-header\">\n"
    f"      <h1 data-cv-field=\"identity\">{html_module.escape(title)}</h1>{headline_html}\n"
    "    </header>\n"
    f"    <div class=\"cv-body\">\n{body_html}\n    </div>\n"
    "  </main>\n"
    "</body>\n"
    "</html>\n"
  )


def _fact_str(value: object) -> str:
  """A single normalized line from an interview fact value."""
  return " ".join(str(value if value is not None else "").split())


def _date_line(start: object, end: object) -> str:
  """Render an entry's date range the way the CV builder does elsewhere.

  A start with no end reads as ongoing; an end alone is shown as-is. Years are
  preserved verbatim.
  """
  start_text = _fact_str(start)
  end_text = _fact_str(end)
  if start_text and end_text:
    return f"{start_text} – {end_text}"
  if start_text:
    return f"{start_text} – Present"
  return end_text


def _entry_block(*lines: str) -> list[str]:
  """One CV entry as a block of non-empty text lines.

  _render_cv_block joins these with <br> inside a single <p class="entry">, so
  an entry's heading, dates, and description stay visually grouped — the same
  shape a PDF-extracted entry block takes.
  """
  return [line for line in (_fact_str(line) for line in lines) if line]


def _fact_list(value: object) -> list[str]:
  """Coerce a skills-style fact into a clean list.

  Accepts a list, or a string with comma/newline/semicolon separators, matching
  the InterviewData contract the web app collects.
  """
  if isinstance(value, (list, tuple)):
    items = [_fact_str(item) for item in value]
  else:
    items = [_fact_str(part) for part in re.split(r"[,\n;]", str(value or ""))]
  seen: set[str] = set()
  output: list[str] = []
  for item in items:
    key = item.casefold()
    if item and key not in seen:
      seen.add(key)
      output.append(item)
  return output


def _fact_rows(value: object) -> list[dict]:
  return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def cv_html_from_facts(facts: dict) -> str:
  """Build the canonical semantic CV HTML from structured interview facts.

  This is the WhatsApp analogue of the web app's buildCvHtml + create route:
  the agent collects the same InterviewData shape conversationally, and this
  turns it into the identical professional-v2 semantic document the PDF-convert
  path produces (so a WhatsApp-built CV and a PDF-uploaded CV are the same shape
  for the personalization agent). Presentation stays decoupled — the document is
  stored unstyled and styled on render.

  Only the full name is required; every other section is optional and omitted
  when empty.
  """
  source = facts if isinstance(facts, dict) else {}
  full_name = _fact_str(source.get("fullName"))
  if not full_name:
    raise SystemExit("fullName is required to build a CV")

  sections: list[tuple[str, list[list[str]]]] = []

  contact_lines = [
    line for line in (
      _fact_str(source.get("email")),
      _fact_str(source.get("phone")),
      _fact_str(source.get("location")),
    ) if line
  ]
  if contact_lines:
    sections.append(("CONTACT", [contact_lines]))

  summary = _fact_str(source.get("summary"))
  if summary:
    sections.append(("PROFESSIONAL SUMMARY", [[summary]]))

  skills = _fact_list(source.get("skills"))
  if skills:
    sections.append(("SKILLS", [skills]))

  experience_blocks = [
    block for row in _fact_rows(source.get("experience"))
    if (block := _entry_block(
      ", ".join(part for part in (_fact_str(row.get("title")), _fact_str(row.get("company"))) if part),
      _date_line(row.get("start"), row.get("end")),
      _fact_str(row.get("description")),
    ))
  ]
  if experience_blocks:
    sections.append(("WORK EXPERIENCE", experience_blocks))

  education_blocks = [
    block for row in _fact_rows(source.get("education"))
    if (block := _entry_block(
      ", ".join(part for part in (_fact_str(row.get("degree")), _fact_str(row.get("institution"))) if part),
      _date_line(row.get("start"), row.get("end")),
      _fact_str(row.get("description")),
    ))
  ]
  if education_blocks:
    sections.append(("EDUCATION", education_blocks))

  referee_blocks = [
    block for row in _fact_rows(source.get("referees"))
    if (block := _entry_block(
      _fact_str(row.get("name")),
      ", ".join(part for part in (_fact_str(row.get("position")), _fact_str(row.get("company"))) if part),
      _fact_str(row.get("email")),
      _fact_str(row.get("phone")),
    ))
  ]
  if referee_blocks:
    sections.append(("REFERENCES", referee_blocks))

  return _assemble_cv_html(full_name, "", sections)


def style_cv_html(html: str) -> str:
  """Inject the modern stylesheet into a semantic (unstyled) CV document.

  Idempotent-safe: if the document already carries a <style> block (a legacy or
  already-styled document), it is returned unchanged rather than double-styled.
  """
  text = str(html or "")
  if re.search(r"<style\b", text, flags=re.IGNORECASE):
    return text
  style_block = f"<style>\n{CV_HTML_STYLE}</style>\n"
  head_close = re.search(r"</head\s*>", text, flags=re.IGNORECASE)
  if head_close:
    return text[: head_close.start()] + style_block + text[head_close.start() :]
  head_open = re.search(r"<head\b[^>]*>", text, flags=re.IGNORECASE)
  if head_open:
    return text[: head_open.end()] + "\n" + style_block + text[head_open.end() :]
  # No <head> at all: wrap the style before the first content so it still applies.
  return style_block + text


def _ocr_cv_text(pdf_path: Path) -> str:
  with tempfile.TemporaryDirectory() as tmp:
    prefix = Path(tmp) / "page"
    subprocess.run(
      ["pdftoppm", "-png", "-r", "180", str(pdf_path), str(prefix)],
      check=True,
      capture_output=True,
      timeout=180,
    )
    def page_number(path: Path) -> int:
      match = re.search(r"(\d+)$", path.stem)
      return int(match.group(1)) if match else 0

    pages = sorted(Path(tmp).glob("page-*.png"), key=page_number) or sorted(Path(tmp).glob("page*.png"), key=page_number)
    if not pages:
      raise RuntimeError("CV OCR produced no pages")
    extracted: list[str] = []
    for page in pages:
      completed = subprocess.run(
        ["tesseract", str(page), "stdout"],
        check=True,
        capture_output=True,
        timeout=120,
      )
      extracted.append(completed.stdout.decode("utf-8", errors="replace"))
    return "\n\n".join(extracted)


def _pdftotext_cv_text(pdf_path: Path, layout: bool = False) -> str:
  command = ["pdftotext", "-enc", "UTF-8"]
  if layout:
    # -layout keeps text at its physical x-position so side-by-side columns stay
    # spatially separated instead of being read across the page row by row.
    command.append("-layout")
  command += [str(pdf_path), "-"]
  completed = subprocess.run(command, check=True, capture_output=True, timeout=120)
  return completed.stdout.decode("utf-8", errors="replace")


def _decolumn_layout_text(text: str) -> str | None:
  """De-interleave a two-column CV from layout-preserving pdftotext output.

  A two-column CV (a narrow sidebar beside a wide main column) is read across
  the page by plain extraction, interleaving unrelated lines (a contact address
  landing inside the profile paragraph). Layout-preserving output keeps each
  column at its physical x, so a vertical band of whitespace separates them. We
  detect that gutter and emit the left column top-to-bottom, then the right,
  restoring per-column reading order. Returns None when no confident two-column
  structure is found so single-column CVs fall through unchanged.
  """
  lines = [line.rstrip() for line in text.replace("\f", "\n").split("\n")]
  content = [line for line in lines if line.strip()]
  if len(content) < 8:
    return None
  width = max(len(line) for line in content)
  if width < 40:
    return None
  total = len(content)

  def space_fraction(column: int) -> float:
    return sum(1 for line in content if column >= len(line) or line[column] == " ") / total

  low, high = int(width * 0.15), int(width * 0.75)
  gutter_columns = [column for column in range(low, high + 1) if space_fraction(column) >= 0.95]
  if not gutter_columns:
    return None
  # Collapse the always-space columns into contiguous bands and take the widest;
  # a real gutter is several characters wide, not an incidental aligned space.
  bands: list[tuple[int, int]] = []
  start = previous = gutter_columns[0]
  for column in gutter_columns[1:]:
    if column == previous + 1:
      previous = column
    else:
      bands.append((start, previous))
      start = previous = column
  bands.append((start, previous))
  bands = [band for band in bands if band[1] - band[0] + 1 >= 2]
  if not bands:
    return None
  band = max(bands, key=lambda item: item[1] - item[0])
  split = (band[0] + band[1]) // 2

  left: list[str] = []
  right: list[str] = []

  def append(column: list[str], value: str) -> None:
    if value:
      column.append(value)
    elif column and column[-1] != "":
      column.append("")

  for line in lines:
    if not line.strip():
      append(left, "")
      append(right, "")
      continue
    append(left, line[:split].rstrip())
    append(right, line[split:].strip())

  left_rows = sum(1 for value in left if value)
  right_rows = sum(1 for value in right if value)
  # Both sides must be substantially populated; otherwise this is a
  # single-column document that merely has ragged right-hand whitespace.
  if left_rows < total * 0.25 or right_rows < total * 0.25:
    return None
  return "\n".join(left).strip() + "\n\n" + "\n".join(right).strip()


def _extract_cv_text(pdf_path: Path) -> str:
  # Prefer layout-preserving extraction so a two-column CV is de-interleaved
  # into per-column reading order before parsing; only genuinely two-column
  # documents take this path, everything else falls through unchanged.
  try:
    decolumned = _decolumn_layout_text(_pdftotext_cv_text(pdf_path, layout=True))
    if decolumned:
      return decolumned
  except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
    pass
  try:
    return _ocr_cv_text(pdf_path)
  except FileNotFoundError as err:
    try:
      return _pdftotext_cv_text(pdf_path)
    except FileNotFoundError:
      raise RuntimeError("tesseract or pdftotext is required to convert pending CVs") from err
  except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
    try:
      return _pdftotext_cv_text(pdf_path)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
      raise RuntimeError("could not extract readable text from the pending CV") from err


def convert_pdf_to_cv_html(pdf_path: Path, html_path: Path) -> Path:
  html_path.write_text(cv_html_document(_extract_cv_text(pdf_path)), encoding="utf-8")
  return html_path
