---
name: pdf-edit
description: Local-first PDF CLI — create, merge, split, search/replace, redact, extract. No API calls needed.
metadata:
  openclaw:
    emoji: 📄
    requires:
      bins: [pdf-edit]
---

# pdf-edit

Local PDF editing CLI powered by PyMuPDF. No API calls, no rate limits, no image generation — all work happens on your machine.

## Commands

### Replace text
```bash
pdf-edit replace input.pdf "old text" "new text" -o output.pdf
pdf-edit replace input.pdf "old" "new" --pages 1-3 -o output.pdf  # limit pages
```

### Redact (permanently remove) text
```bash
pdf-edit redact input.pdf "sensitive info" -o output.pdf
pdf-edit redact input.pdf "John Doe" --replace-with "[REDACTED]" -o output.pdf
```

### Create a new PDF from scratch
```bash
pdf-edit create output.pdf "Title: My Document\n\nHello world, this is a new PDF."
```

### Merge multiple PDFs
```bash
pdf-edit merge file1.pdf file2.pdf file3.pdf -o combined.pdf
```

### Extract pages
```bash
pdf-edit extract input.pdf --pages 1,3,5-7 -o excerpt.pdf
```

### Extract images
```bash
pdf-edit extract-images input.pdf -o ./images/
```

### Metadata
```bash
pdf-edit meta input.pdf                    # show metadata
pdf-edit meta input.pdf --title "New Title" -o output.pdf  # set metadata
```

### Info (page count, size, format)
```bash
pdf-edit info input.pdf
```

### AI-powered edit (optional, needs GEMINI_API_KEY)
```bash
pdf-edit ai input.pdf "Make the title larger and change the date to December 2026" -o output.pdf
```

## Notes
- Always writes to a new file (never overwrites source unless you use same filename with `-o`)
- Page numbers are 1-based
- The AI command uses your free-tier Gemini key sparingly — only when you explicitly request it
