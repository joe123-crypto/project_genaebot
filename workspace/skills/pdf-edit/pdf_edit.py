#!/home/joseph/.openclaw/workspace/skills/pdf-edit/.venv/bin/python3
"""
pdf-edit — Local-first PDF CLI powered by PyMuPDF.
No API calls needed for core operations.

Requirements: pymupdf (pip install pymupdf)
"""

import argparse
import json
import os
import sys
from pathlib import Path

import pymupdf


# ─── helpers ────────────────────────────────────────────────────────────────

def ensure_output(input_path, output):
    if output is None:
        stem = Path(input_path).stem
        output = f"{stem}_edited.pdf"
    if os.path.abspath(output) == os.path.abspath(input_path):
        print("error: output cannot be the same file as input")
        sys.exit(1)
    return output


def resolve_pages(doc, pages_str):
    """Resolve '1,3,5-7,N' to a list of 0-based page indices."""
    if not pages_str:
        return list(range(len(doc)))
    indices = set()
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            a = 1 if a.strip().upper() == "N" else int(a)
            b = len(doc) if b.strip().upper() == "N" else int(b)
            if a <= b:
                indices.update(range(a - 1, b))
            else:
                indices.update(range(b - 1, a))
                indices = set(sorted(indices, reverse=True))
        else:
            n = len(doc) if part.strip().upper() == "N" else int(part)
            indices.add(n - 1)
    return sorted(indices)


def insert_text_box(page, rect, text, fontsize=11, color=(0, 0, 0)):
    """Insert word-wrapped text inside a rectangle. Returns overflow char count (negative = overflow)."""
    return page.insert_textbox(
        rect, text,
        fontsize=fontsize,
        fontname="helv",
        color=color,
        align=pymupdf.TEXT_ALIGN_LEFT,
    )


def insert_text_styled(page, rect, text, fontsize=11, color=(0, 0, 0)):
    """Insert text trying to fill the rect; if textbox won't fit, use plain insert_text."""
    rc = page.insert_textbox(
        rect, text,
        fontsize=fontsize,
        fontname="helv",
        color=color,
        align=pymupdf.TEXT_ALIGN_LEFT,
    )
    if rc < 0:
        # Fallback: plain insert at top-left of rect
        page.insert_text(
            rect.tl + (0, fontsize),
            text,
            fontsize=fontsize,
            fontname="helv",
            color=color,
        )


# ─── commands ───────────────────────────────────────────────────────────────

def cmd_replace(args):
    input_path, find, replace = args.input, args.find, args.replace
    output = ensure_output(input_path, args.output)
    doc = pymupdf.open(input_path)
    pages = resolve_pages(doc, args.pages)

    found = 0
    for pi in pages:
        page = doc[pi]
        hits = page.search_for(find)
        if not hits:
            continue
        found += len(hits)
        for rect in hits:
            page.add_redact_annot(rect)
        page.apply_redactions()
        for rect in hits:
            insert_text_styled(page, rect, replace)

    doc.save(output)
    doc.close()
    print(f"✓ Replaced {found} occurrence(s) of '{find}' → '{replace}' on {len(pages)} page(s)")
    print(f"  Output: {output}")


def cmd_redact(args):
    input_path, find = args.input, args.find
    output = ensure_output(input_path, args.output)
    doc = pymupdf.open(input_path)
    pages = resolve_pages(doc, args.pages)

    found = 0
    for pi in pages:
        page = doc[pi]
        hits = page.search_for(find)
        if not hits:
            continue
        found += len(hits)
        for rect in hits:
            page.add_redact_annot(rect)
        page.apply_redactions()
        if args.replace_with:
            for rect in hits:
                insert_text_styled(page, rect, args.replace_with)

    doc.save(output)
    doc.close()
    msg = f"✓ Redacted {found} occurrence(s) of '{find}' on {len(pages)} page(s)"
    if args.replace_with:
        msg += f" → '{args.replace_with}'"
    print(msg)
    print(f"  Output: {output}")


def cmd_create(args):
    output = args.output
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    elif os.path.isfile(text):
        text = Path(text).read_text()

    doc = pymupdf.open()
    page = doc.new_page()
    margin = 72
    rect = pymupdf.Rect(margin, margin, page.rect.width - margin, page.rect.height - margin)
    overflow = insert_text_box(page, rect, text, fontsize=12)
    if overflow < 0:
        print(f"⚠  Warning: {-overflow} chars didn't fit on one page")

    doc.save(output)
    doc.close()
    print(f"✓ Created PDF: {output}")


def cmd_merge(args):
    output = args.output
    merger = pymupdf.open()
    for f in args.files:
        with pymupdf.open(f) as src:
            merger.insert_pdf(src)
    merger.save(output)
    merger.close()
    print(f"✓ Merged {len(args.files)} file(s) → {output}")


def cmd_extract(args):
    output = ensure_output(args.input, args.output)
    doc = pymupdf.open(args.input)
    pages = resolve_pages(doc, args.pages)
    out = pymupdf.open()
    for pi in pages:
        out.insert_pdf(doc, from_page=pi, to_page=pi)
    out.save(output)
    out.close()
    doc.close()
    print(f"✓ Extracted pages {args.pages or 'all'} → {output}")


def cmd_extract_images(args):
    input_path = args.input
    output_dir = args.output or "."
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(input_path)
    pages = resolve_pages(doc, args.pages)
    count = 0
    for pi in pages:
        for img in doc[pi].get_images():
            xref = img[0]
            pix = pymupdf.Pixmap(doc, xref)
            if pix.n > 4:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            fname = f"{output_dir}/page{pi+1}_img{count}.png"
            pix.save(fname)
            count += 1
    doc.close()
    print(f"✓ Extracted {count} image(s) → {output_dir}")


def cmd_meta(args):
    doc = pymupdf.open(args.input)
    if args.title or args.author or args.subject or args.keywords:
        meta = doc.metadata
        if args.title:
            meta["title"] = args.title
        if args.author:
            meta["author"] = args.author
        if args.subject:
            meta["subject"] = args.subject
        if args.keywords:
            meta["keywords"] = args.keywords
        doc.set_metadata(meta)
        output = ensure_output(args.input, args.output)
        doc.save(output)
        print(f"✓ Metadata updated → {output}")
    else:
        meta = doc.metadata
        for k, v in meta.items():
            if v:
                print(f"  {k}: {v}")
    doc.close()


def cmd_info(args):
    doc = pymupdf.open(args.input)
    meta = doc.metadata
    print(f"  File:       {args.input}")
    print(f"  Pages:      {len(doc)}")
    print(f"  Format:     {meta.get('format', 'unknown')}")
    print(f"  Title:      {meta.get('title', '-')}")
    print(f"  Author:     {meta.get('author', '-')}")
    print(f"  Encryption: {meta.get('encryption', 'none')}")
    print(f"  File size:  {os.path.getsize(args.input):,} bytes")
    doc.close()


def cmd_ai(args):
    """AI-powered edit using Gemini text API (optional)."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("error: GEMINI_API_KEY not set in environment")
        sys.exit(1)

    input_path, instruction = args.input, args.instruction
    output = ensure_output(input_path, args.output)

    doc = pymupdf.open(input_path)
    all_text = []
    for pi in range(len(doc)):
        page_text = doc[pi].get_text()
        all_text.append(f"--- PAGE {pi+1} ---\n{page_text}")
    full_text = "\n\n".join(all_text)
    doc.close()

    import urllib.request

    prompt = f"""You are a PDF editor. Given the following PDF text content and a natural-language instruction,
return a JSON array of edit operations. Each operation must be: {{"page": <int>, "find": "<text>", "replace": "<text>"}}.

Only include edits for pages where changes are needed. Keep find strings precise enough to match a unique location.
Do NOT include markdown code fences in your response — output pure JSON only.

PDF CONTENT:
{full_text[:30000]}

INSTRUCTION: {instruction}
"""
    req_data = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }).encode()

    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}",
        data=req_data,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    try:
        edits = json.loads(raw)
    except json.JSONDecodeError:
        print("error: Gemini returned invalid JSON. Response:")
        print(raw)
        sys.exit(1)

    doc = pymupdf.open(input_path)
    for edit in edits:
        pi = edit["page"] - 1
        find = edit["find"]
        replace = edit["replace"]
        if 0 <= pi < len(doc):
            page = doc[pi]
            hits = page.search_for(find)
            for rect in hits:
                page.add_redact_annot(rect)
            page.apply_redactions()
            for rect in hits:
                insert_text_styled(page, rect, replace)

    doc.save(output)
    doc.close()
    print(f"✓ AI edit applied ({len(edits)} change(s)) → {output}")


# ─── cli ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="pdf-edit",
        description="Local-first PDF editing CLI — no API calls needed",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("replace", help="Search and replace text")
    p.add_argument("input", help="Input PDF file")
    p.add_argument("find", help="Text to find")
    p.add_argument("replace", help="Replacement text")
    p.add_argument("-o", "--output", help="Output file (default: *_edited.pdf)")
    p.add_argument("--pages", help="Limit to pages, e.g. 1,3,5-7,N")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("redact", help="Permanently remove text")
    p.add_argument("input", help="Input PDF file")
    p.add_argument("find", help="Text to redact")
    p.add_argument("-o", "--output", help="Output file")
    p.add_argument("--pages", help="Limit to pages")
    p.add_argument("--replace-with", help="Optional replacement text")
    p.set_defaults(func=cmd_redact)

    p = sub.add_parser("create", help="Create a new PDF from text")
    p.add_argument("output", help="Output PDF file")
    p.add_argument("text", help="Text content, file path, or '-' for stdin")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("merge", help="Combine multiple PDFs")
    p.add_argument("files", nargs="+", help="Input PDF files")
    p.add_argument("-o", "--output", required=True, help="Output file")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("extract", help="Extract pages to a new PDF")
    p.add_argument("input", help="Input PDF file")
    p.add_argument("--pages", required=True, help="Pages to extract, e.g. 1,3,5-7,N")
    p.add_argument("-o", "--output", help="Output file")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("extract-images", help="Extract embedded images")
    p.add_argument("input", help="Input PDF file")
    p.add_argument("-o", "--output", help="Output directory")
    p.add_argument("--pages", help="Limit to pages")
    p.set_defaults(func=cmd_extract_images)

    p = sub.add_parser("meta", help="Show or set PDF metadata")
    p.add_argument("input", help="Input PDF file")
    p.add_argument("--title", help="Set title")
    p.add_argument("--author", help="Set author")
    p.add_argument("--subject", help="Set subject")
    p.add_argument("--keywords", help="Set keywords")
    p.add_argument("-o", "--output", help="Output file (if modifying)")
    p.set_defaults(func=cmd_meta)

    p = sub.add_parser("info", help="Show basic PDF info")
    p.add_argument("input", help="Input PDF file")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("ai", help="AI-powered edit (requires GEMINI_API_KEY)")
    p.add_argument("input", help="Input PDF file")
    p.add_argument("instruction", help="Natural language instruction")
    p.add_argument("-o", "--output", help="Output file")
    p.set_defaults(func=cmd_ai)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
