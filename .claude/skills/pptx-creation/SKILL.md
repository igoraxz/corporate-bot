---
description: "Professional corporate PowerPoint presentation creation via pptxgenjs (Node.js). TRIGGER when: user asks to create a presentation, make slides, build a deck, generate PowerPoint, make a pitch deck, prepare meeting slides, create a corporate slideshow."
---

# Corporate PowerPoint Presentation Creation

Create professional, consultant-grade corporate PowerPoint presentations using pptxgenjs (Node.js).
The package is globally installed — use `node script.js` via Bash to generate .pptx files.

## Workflow

1. **Understand the brief**: What's the purpose, audience, key message?
2. **Plan structure**: Outline slides using assertion-evidence framework
3. **Choose design system**: Pick color palette, fonts, visual motif
4. **Write generation script**: Node.js script using pptxgenjs
5. **Run and deliver**: Execute via Bash, send file to chat

## Output Path

- Admin/team chats: write to `/app/data/tmp/presentation.pptx`
- Sandbox/coding chats: write to `./presentation.pptx` (current directory)
- Relay chats (Mac): write to `/tmp/presentation.pptx`, use `[RELAY_FILE: /tmp/presentation.pptx]`

## Design Principles — MANDATORY

### Slide Structure
- **Action titles**: Every slide title is a CONCLUSION, not a topic. "Revenue grew 40% YoY" not "Revenue Overview"
- **One idea per slide**: Never mix multiple arguments on one slide
- **Assertion-evidence**: Title states the takeaway; body provides visual EVIDENCE (chart, diagram, image)
- **Ghost Deck Test**: Read ONLY the titles in sequence — they must tell the complete story without the body
- **No text-only slides**: Every slide MUST have a visual element (chart, image, shape, diagram, icon)

### Typography
- Title: 28-36pt, bold, sans-serif (Arial Black, Helvetica Neue Bold, or Calibri Bold)
- Subtitle/headers: 20-24pt, semibold
- Body text: 16-18pt, regular weight
- Captions/footnotes: 12-14pt, lighter color
- NEVER go below 14pt for any text on a slide
- Max 40 words of body text per slide

### Color Palettes (pick ONE per deck, match to topic)

**Midnight Executive** (corporate/finance):
- Primary: #1B2838 (dark navy), Secondary: #4A90A4 (steel blue), Accent: #E8B84B (gold)

**Forest & Moss** (sustainability/nature):
- Primary: #2D4A3E (deep green), Secondary: #7BA68C (sage), Accent: #D4A96A (warm sand)

**Coral Energy** (startup/creative):
- Primary: #2C3E50 (charcoal), Secondary: #E74C3C (coral red), Accent: #F39C12 (amber)

**Arctic Professional** (tech/consulting):
- Primary: #1A1A2E (midnight), Secondary: #4ECDC4 (teal), Accent: #FF6B6B (salmon)

**Warm Slate** (education/healthcare):
- Primary: #3D3D3D (warm gray), Secondary: #6B8E7E (muted green), Accent: #C17B4A (terracotta)

### Layout Rules
- Margins: minimum 0.5 inches from all edges
- Content blocks: 0.3-0.5 inch gaps between elements
- Alignment: left-align body text (never center body text)
- Consistency: same margins, same title position on EVERY slide
- Widescreen: always 16:9 (13.33 x 7.5 inches)

### Visual Hierarchy
- Size communicates importance (title > headers > body > caption)
- Color dominance: 60-70% primary color, 20-30% secondary, 5-10% accent
- Accent color for: key numbers, call-to-action, important data points ONLY
- Dark backgrounds for title/conclusion slides, light for content slides

### Anti-Patterns (NEVER do these)
- No walls of bullet points (max 4-5 bullets, prefer visual evidence)
- No center-aligned body text
- No default blue theme (always use a custom palette)
- No accent lines under titles (hallmark of AI-generated slides)
- No inconsistent spacing between slides
- No clip art or low-quality images
- No gradient backgrounds (solid colors or subtle textures only)
- No more than 2 fonts in one deck
- No orphan slides with different styling

## pptxgenjs API Reference

### Script Template

```javascript
const pptxgen = require("pptxgenjs");
const pres = new pptxgen();

pres.author = "Generated";
pres.layout = "LAYOUT_WIDE"; // 13.33 x 7.5 inches (16:9)

// Define master slides for consistency
pres.defineSlideMaster({
  title: "TITLE_SLIDE",
  background: { color: "1B2838" },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.8, y: 2.5, w: 11.5, h: 1.5, color: "FFFFFF", fontSize: 36, bold: true, fontFace: "Arial" } } },
    { placeholder: { options: { name: "subtitle", type: "body", x: 0.8, y: 4.2, w: 11.5, h: 1.0, color: "CCCCCC", fontSize: 20, fontFace: "Arial" } } },
  ],
});

pres.defineSlideMaster({
  title: "CONTENT_SLIDE",
  background: { color: "FFFFFF" },
  objects: [
    { rect: { x: 0, y: 0, w: 13.33, h: 1.2, fill: { color: "1B2838" } } },
    { placeholder: { options: { name: "title", type: "title", x: 0.8, y: 0.2, w: 11.5, h: 0.9, color: "FFFFFF", fontSize: 24, bold: true, fontFace: "Arial" } } },
  ],
});

// === SLIDE 1: Title ===
let slide = pres.addSlide({ masterName: "TITLE_SLIDE" });
slide.addText("Main Title Here", { placeholder: "title" });
slide.addText("Subtitle or date", { placeholder: "subtitle" });

// === SLIDE 2: Content with chart ===
slide = pres.addSlide({ masterName: "CONTENT_SLIDE" });
slide.addText("Revenue grew 40% year-over-year", { placeholder: "title" });
slide.addChart(pres.charts.BAR, [
  { name: "Revenue ($M)", labels: ["2022", "2023", "2024"], values: [12, 15, 21] }
], { x: 1.0, y: 1.8, w: 7.0, h: 4.5, showValue: true, chartColors: ["4A90A4"] });

// === SLIDE 3: KPI metrics ===
slide = pres.addSlide({ masterName: "CONTENT_SLIDE" });
slide.addText("Three pillars drive our growth", { placeholder: "title" });
const kpis = [
  { label: "Revenue", value: "$21M", delta: "+40%" },
  { label: "Customers", value: "1,250", delta: "+28%" },
  { label: "NPS", value: "72", delta: "+12pts" },
];
kpis.forEach((kpi, i) => {
  const x = 1.0 + i * 4.0;
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 2.0, w: 3.5, h: 3.5, fill: { color: "F5F5F5" }, rectRadius: 0.1 });
  slide.addText(kpi.value, { x, y: 2.5, w: 3.5, h: 1.2, fontSize: 36, bold: true, color: "1B2838", align: "center" });
  slide.addText(kpi.label, { x, y: 3.7, w: 3.5, h: 0.6, fontSize: 16, color: "666666", align: "center" });
  slide.addText(kpi.delta, { x, y: 4.3, w: 3.5, h: 0.6, fontSize: 14, color: "27AE60", align: "center" });
});

// Save
pres.writeFile({ fileName: "/app/data/tmp/presentation.pptx" })
  .then(() => console.log("Created: presentation.pptx"))
  .catch(err => { console.error(err); process.exit(1); });
```

### Chart Types

| Type | Use When |
|------|----------|
| `pres.charts.BAR` | Comparing categories |
| `pres.charts.LINE` | Trends over time |
| `pres.charts.PIE` | Parts of a whole (max 5-6 slices) |
| `pres.charts.DOUGHNUT` | Modern pie alternative |
| `pres.charts.AREA` | Volume/cumulative trends |
| `pres.charts.SCATTER` | Correlation between variables |

### Common Elements

```javascript
// Text
slide.addText("Bold statement", {
  x: 1, y: 2, w: 10, h: 1,
  fontSize: 24, bold: true, color: "1B2838", fontFace: "Arial", align: "left",
});

// Image
slide.addImage({ path: "/path/to/image.png", x: 1, y: 2, w: 5, h: 3 });

// Table (array of arrays, first row = header)
const rows = [
  [{ text: "Metric", options: { bold: true } }, { text: "Value", options: { bold: true } }],
  ["Revenue", "$21M"],
  ["Growth", "40%"],
];
slide.addTable(rows, {
  x: 1, y: 2, w: 11, h: 3,
  border: { type: "solid", pt: 0.5, color: "CCCCCC" },
  colW: [5, 6], fontSize: 14, color: "333333",
});

// Shape
slide.addShape(pres.shapes.RECTANGLE, {
  x: 0, y: 6.5, w: 13.33, h: 1.0, fill: { color: "1B2838" },
});

// Speaker notes
slide.addNotes("Key talking points for this slide...");
```

### Slide Types

1. **Title slide**: Full-bleed dark background, large title, subtitle
2. **Agenda/TOC**: Numbered items with brief descriptions
3. **Section divider**: Dark background, section number + title
4. **Content + chart**: Action title + single chart filling most of the space
5. **KPI/metrics**: 3-4 metric cards with value + label + delta
6. **Comparison**: Two columns (before/after, option A/B)
7. **Timeline**: Horizontal flow with milestones
8. **Quote**: Large quote text + attribution
9. **Image + caption**: Full-width image with overlaid caption
10. **Conclusion/CTA**: Dark background, key takeaway, next steps

## Critical Pitfalls (pptxgenjs)

1. NEVER use # with hex colors (causes file corruption):
   CORRECT: color: "FF0000"
   WRONG:   color: "#FF0000"

2. NEVER encode opacity in hex color (8-char hex corrupts file):
   CORRECT: shadow: { color: "000000", opacity: 0.15 }
   WRONG:   shadow: { color: "00000020" }

3. NEVER reuse option objects across calls (pptxgenjs mutates them):
   CORRECT: Use factory functions: const makeShadow = () => ({...})
   WRONG:   const shadow = {...}; used in multiple addShape calls

4. Use bullet: true for lists, NEVER unicode bullet characters

5. Use breakLine: true between text array items

6. NEVER use lineSpacing with bullets (use paraSpaceAfter instead)

7. ROUNDED_RECTANGLE + rectangular accent bars dont align (use RECTANGLE)

8. For upward shadows use angle: 270 with positive offset (never negative offset)

## Quality Checklist

- [ ] Every title is an assertion (conclusion), not a topic label
- [ ] Ghost Deck Test passes (titles alone tell the story)
- [ ] No text-only slides (every slide has a visual)
- [ ] Consistent margins and positioning across all slides
- [ ] Font sizes follow hierarchy (title > header > body > caption)
- [ ] Max 2 fonts used in entire deck
- [ ] Color palette is consistent (no random colors)
- [ ] No text below 14pt
- [ ] Charts have proper labels and are readable
- [ ] Script runs without errors before sending file
