---
description: "Professional corporate Word document creation via docx npm package (Node.js). TRIGGER when: user asks to create a document, write a business report, generate a Word file, make a proposal, format a corporate letter, create a formal document."
---

# Corporate Word Document Creation

Create professional corporate Word documents (reports, proposals, memos) using the `docx` npm package (Node.js).
Write a generation script, run via Bash, send the .docx file.

The `docx` npm package is globally installed. NODE_PATH is set so
require("docx") works from any directory.

## Workflow

1. Understand document purpose, audience, structure
2. Plan sections (heading hierarchy, content flow)
3. Write Node.js generation script using docx package
4. Run via Bash, send file to chat

## Output Path

- Admin/team chats: /app/data/tmp/document.docx
- Sandbox/coding chats: ./document.docx (current directory)
- Relay chats (Mac): /tmp/document.docx, use [RELAY_FILE: /tmp/document.docx]

## Design Principles

### Document Structure
- Clear heading hierarchy (H1 > H2 > H3, never skip levels)
- Executive summary or introduction first
- Logical section flow (situation > analysis > recommendations)
- Page breaks between major sections
- Table of contents for docs > 5 pages

### Typography
- Title: 28pt, bold
- Heading 1: 22pt, bold
- Heading 2: 16pt, bold
- Heading 3: 14pt, bold
- Body: 11pt, regular (Calibri or Arial)
- Line spacing: 1.15 or 1.5 for readability

### Page Layout
- Margins: 1 inch all sides (Normal) or 0.75 inch (Narrow for dense content)
- Page size: A4 (default) or US Letter (specify if needed)
- Headers: document title or section name
- Footers: page numbers (centered or right-aligned)
- Page numbers: Arabic numerals, starting from content page

### Tables
- Header row: bold, shaded background
- Alternating row colors for readability
- Consistent column widths
- No merged cells unless structurally necessary
- All measurements in DXA (1440 DXA = 1 inch)

### Anti-Patterns
- No inconsistent heading levels
- No walls of text without visual breaks
- No tables without headers
- No orphaned headings (heading at page bottom, content on next page)
- No multiple consecutive blank lines
- No mixing font families within body text

## Script Template

```javascript
const { Document, Packer, Paragraph, TextRun, HeadingLevel,
        Table, TableRow, TableCell, WidthType, AlignmentType,
        Header, Footer, PageNumber, NumberFormat,
        BorderStyle, ShadingType, PageBreak } = require("docx");
const fs = require("fs");

const doc = new Document({
    creator: "Generated",
    title: "Document Title",
    styles: {
        default: {
            document: {
                run: { font: "Calibri", size: 22 }, // 11pt = 22 half-points
                paragraph: { spacing: { line: 276 } }, // 1.15 spacing
            },
        },
    },
    sections: [{
        properties: {
            page: {
                size: { width: 11906, height: 16838 }, // A4 in DXA
                margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 },
            },
        },
        headers: {
            default: new Header({
                children: [new Paragraph({
                    children: [new TextRun({ text: "Document Title", italics: true, size: 18, color: "888888" })],
                    alignment: AlignmentType.RIGHT,
                })],
            }),
        },
        footers: {
            default: new Footer({
                children: [new Paragraph({
                    children: [new TextRun({ children: [PageNumber.CURRENT] })],
                    alignment: AlignmentType.CENTER,
                })],
            }),
        },
        children: [
            // Title
            new Paragraph({
                children: [new TextRun({ text: "Document Title", bold: true, size: 56 })],
                spacing: { after: 400 },
            }),
            // Subtitle
            new Paragraph({
                children: [new TextRun({ text: "Prepared for [Audience]", size: 28, color: "666666" })],
                spacing: { after: 800 },
            }),

            // Section heading
            new Paragraph({
                text: "1. Executive Summary",
                heading: HeadingLevel.HEADING_1,
                spacing: { before: 400, after: 200 },
            }),
            new Paragraph({
                children: [new TextRun("Summary content goes here...")],
                spacing: { after: 200 },
            }),

            // Page break before next section
            new Paragraph({ children: [new PageBreak()] }),

            // Table example
            new Paragraph({
                text: "2. Key Metrics",
                heading: HeadingLevel.HEADING_1,
                spacing: { before: 400, after: 200 },
            }),
            new Table({
                rows: [
                    new TableRow({
                        children: [
                            new TableCell({
                                children: [new Paragraph({ children: [new TextRun({ text: "Metric", bold: true })] })],
                                width: { size: 5000, type: WidthType.DXA },
                                shading: { type: ShadingType.CLEAR, fill: "E8E8E8" },
                            }),
                            new TableCell({
                                children: [new Paragraph({ children: [new TextRun({ text: "Value", bold: true })] })],
                                width: { size: 5000, type: WidthType.DXA },
                                shading: { type: ShadingType.CLEAR, fill: "E8E8E8" },
                            }),
                        ],
                    }),
                    new TableRow({
                        children: [
                            new TableCell({ children: [new Paragraph("Revenue")], width: { size: 5000, type: WidthType.DXA } }),
                            new TableCell({ children: [new Paragraph("$21M")], width: { size: 5000, type: WidthType.DXA } }),
                        ],
                    }),
                ],
                width: { size: 10000, type: WidthType.DXA },
            }),
        ],
    }],
});

// Generate and save
Packer.toBuffer(doc).then(buffer => {
    fs.writeFileSync("/app/data/tmp/document.docx", buffer);
    console.log("Created: document.docx");
}).catch(err => { console.error(err); process.exit(1); });
```

## Key API Notes

- Sizes are in HALF-POINTS (22 = 11pt, 28 = 14pt, 44 = 22pt)
- Page dimensions in DXA (1440 DXA = 1 inch, 11906 x 16838 = A4)
- Table widths: ALWAYS use WidthType.DXA (never PERCENTAGE)
- Shading: use ShadingType.CLEAR (not SOLID)
- Bullets: use numbering config with LevelFormat.BULLET (not unicode chars)
- PageBreak MUST be inside a Paragraph

## Document Types

1. **Report**: Title page + TOC + sections + appendix
2. **Letter/Memo**: Header + date + recipient + body + signature
3. **Proposal**: Cover + executive summary + approach + timeline + pricing
4. **Meeting notes**: Date + attendees + agenda + discussion + actions
5. **Policy/procedure**: Purpose + scope + definitions + procedure + revision history

## Quality Checklist

- Clear heading hierarchy (no skipped levels)
- Consistent font and spacing throughout
- Tables have header rows with shading
- Page numbers in footer
- Proper margins (1 inch standard)
- No orphaned headings
- Content logically structured
- File generates without errors
