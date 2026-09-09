---
name: pptx-generator
description: "Generate, edit, and read PowerPoint presentations. Prefer a ready-made template from the shared platform library ($PPTX_LIBRARY_DIR), create from scratch with PptxGenJS (cover, TOC, content, section divider, summary slides), edit existing PPTX via XML workflows, or extract text with markitdown. Triggers: PPT, PPTX, PowerPoint, presentation, slide, deck, slides."
license: MIT
metadata:
  version: "1.0"
  category: productivity
  sources:
    - https://gitbrent.github.io/PptxGenJS/
    - https://github.com/microsoft/markitdown
---

# PPTX Generator & Editor

## Overview

This skill handles all PowerPoint tasks: reading/analyzing existing presentations, editing template-based decks via XML manipulation, and creating presentations from scratch using PptxGenJS. It includes a complete design system (color palettes, fonts, style recipes) and detailed guidance for every slide type.

**Source selection order** — use the first one that applies:

1. A presentation the user supplied (path, upload, or attachment).
2. A template from the **shared platform library** on the read-only mount
   `$PPTX_LIBRARY_DIR` (default `/library/pptx`) — see
   [Template Library](references/template-library.md).
3. From scratch with PptxGenJS — only when neither of the above fits.

## Quick Reference

| Task | Approach |
|------|----------|
| Read/analyze content | `pptx-markitdown presentation.pptx` |
| Use a platform template | Read `$PPTX_LIBRARY_DIR/index.json`, copy the deck, then edit — see [Template Library](references/template-library.md) |
| Edit or create from template | See [Editing Presentations](references/editing.md) |
| Create from scratch | See [Creating from Scratch](#creating-from-scratch-workflow) below |
| Color palette / style recipe presets | `$PPTX_STYLES_DIR/*.json` — machine-readable mirror of [design-system.md](references/design-system.md) |

| Item | Value |
|------|-------|
| **Dimensions** | 10" x 5.625" (LAYOUT_16x9) |
| **Colors** | 6-char hex without # (e.g., `"FF0000"`) |
| **English font** | Arial (default), or approved alternatives |
| **Chinese font** | Microsoft YaHei |
| **Page badge position** | x: 9.3", y: 5.1" |
| **Theme keys** | `primary`, `secondary`, `accent`, `light`, `bg` |
| **Shapes** | RECTANGLE, OVAL, LINE, ROUNDED_RECTANGLE |
| **Charts** | BAR, LINE, PIE, DOUGHNUT, SCATTER, BUBBLE, RADAR |

## Reference Files

| File | Contents |
|------|----------|
| [slide-types.md](references/slide-types.md) | 5 slide page types (Cover, TOC, Section Divider, Content, Summary) + additional layout patterns |
| [design-system.md](references/design-system.md) | Color palettes, font reference, style recipes (Sharp/Soft/Rounded/Pill), typography & spacing |
| [template-library.md](references/template-library.md) | Shared read-only template library (`$PPTX_LIBRARY_DIR`): catalogue fields, copy-then-edit flow, what normalization already did, `must_replace`, style preset JSON |
| [editing.md](references/editing.md) | Template-based editing workflow, XML manipulation, formatting rules, common pitfalls |
| [pitfalls.md](references/pitfalls.md) | QA process, common mistakes, critical PptxGenJS pitfalls |
| [pptxgenjs.md](references/pptxgenjs.md) | Complete PptxGenJS API reference |

---

## Reading Content

```bash
# Text extraction (markitdown[pptx] from the skill's dedicated venv)
pptx-markitdown presentation.pptx
```

---

## Template Library

A curated, admin-managed template library is mounted **read-only** into every
container. One physical copy serves all users — never copy it into the workspace
except for the single deck you are editing, and never write back to it.

| Env var | Default | Contents |
|---------|---------|----------|
| `PPTX_LIBRARY_DIR` | `/library/pptx` | `index.json` catalogue, `files/{id}.pptx`, `thumbs/{id}.png` |
| `PPTX_STYLES_DIR` | `/library/pptx/styles` | `styles.json`, `palettes.json`, `recipes.json`, `typography.json` |

```bash
cat "$PPTX_LIBRARY_DIR/index.json"          # pick a template
cp "$PPTX_LIBRARY_DIR/files/<id>.pptx" template.pptx
pptx-markitdown template.pptx > template.md
```

Then follow [editing.md](references/editing.md). Library decks are **already
normalized** (promo slides removed, orphaned media cleaned, images slimmed,
vendor branding scrubbed, structure validated), so skip the cleanup pass and go
straight to slide mapping and content edits. `index.json` also gives you
`must_replace` — the placeholder strings you are required to clear.

If `index.json` is missing or empty, degrade gracefully to the from-scratch
workflow. Full details in [template-library.md](references/template-library.md).

---

## Creating from Scratch — Workflow

**Use when the user supplied no presentation AND the platform library has no
template that fits (see [Template Library](#template-library)).**

### Step 1: Research & Requirements

Search to understand user requirements — topic, audience, purpose, tone, content depth.

### Step 2: Select Color Palette & Fonts

Use the [Color Palette Reference](references/design-system.md#color-palette-reference) to select a palette matching the topic and audience. Use the [Font Reference](references/design-system.md#font-reference) to choose a font pairing. When the mount is available, `$PPTX_STYLES_DIR/palettes.json` and `typography.json` carry the same values as JSON — read them instead of transcribing tables (strip the leading `#` from hex colors).

### Step 3: Select Design Style

Use the [Style Recipes](references/design-system.md#style-recipes) to choose a visual style (Sharp, Soft, Rounded, or Pill) matching the presentation tone.

### Step 4: Plan Slide Outline

Classify **every slide** as exactly one of the [5 page types](references/slide-types.md). Plan the content and layout for each slide. Ensure visual variety — do NOT repeat the same layout across slides.

### Step 5: Generate Slide JS Files

Create one JS file per slide in `slides/` directory. Each file must export a synchronous `createSlide(pres, theme)` function. Follow the [Slide Output Format](#slide-output-format) and the type-specific guidance in [slide-types.md](references/slide-types.md). Generate up to 5 slides concurrently using subagents if available.

**Tell each subagent:**
1. File naming: `slides/slide-01.js`, `slides/slide-02.js`, etc.
2. Images go in: `slides/imgs/`
3. Final PPTX goes in: `slides/output/`
4. Dimensions: 10" x 5.625" (LAYOUT_16x9)
5. Fonts: Chinese = Microsoft YaHei, English = Arial (or approved alternative)
6. Colors: 6-char hex without # (e.g. `"FF0000"`)
7. Must use the theme object contract (see [Theme Object Contract](#theme-object-contract))
8. Must follow the [PptxGenJS API reference](references/pptxgenjs.md)

### Step 6: Compile into Final PPTX

Create `slides/compile.js` to combine all slide modules:

```javascript
// slides/compile.js
const pptxgen = require('pptxgenjs');
const pres = new pptxgen();
pres.layout = 'LAYOUT_16x9';

const theme = {
  primary: "22223b",    // dark color for backgrounds/text
  secondary: "4a4e69",  // secondary accent
  accent: "9a8c98",     // highlight color
  light: "c9ada7",      // light accent
  bg: "f2e9e4"          // background color
};

for (let i = 1; i <= 12; i++) {  // adjust count as needed
  const num = String(i).padStart(2, '0');
  const slideModule = require(`./slide-${num}.js`);
  slideModule.createSlide(pres, theme);
}

pres.writeFile({ fileName: './output/presentation.pptx' });
```

Run with: `cd slides && pptx-node compile.js`

### Step 7: QA (Required)

See [QA Process](references/pitfalls.md#qa-process).

### Output Structure

```
slides/
├── slide-01.js          # Slide modules
├── slide-02.js
├── ...
├── imgs/                # Images used in slides
└── output/              # Final artifacts
    └── presentation.pptx
```

---

## Slide Output Format

Each slide is a **complete, runnable JS file**:

```javascript
// slide-01.js
const pptxgen = require("pptxgenjs");

const slideConfig = {
  type: 'cover',
  index: 1,
  title: 'Presentation Title'
};

// MUST be synchronous (not async)
function createSlide(pres, theme) {
  const slide = pres.addSlide();
  slide.background = { color: theme.bg };

  slide.addText(slideConfig.title, {
    x: 0.5, y: 2, w: 9, h: 1.2,
    fontSize: 48, fontFace: "Arial",
    color: theme.primary, bold: true, align: "center"
  });

  return slide;
}

// Standalone preview - use slide-specific filename
if (require.main === module) {
  const pres = new pptxgen();
  pres.layout = 'LAYOUT_16x9';
  const theme = {
    primary: "22223b",
    secondary: "4a4e69",
    accent: "9a8c98",
    light: "c9ada7",
    bg: "f2e9e4"
  };
  createSlide(pres, theme);
  pres.writeFile({ fileName: "slide-01-preview.pptx" });
}

module.exports = { createSlide, slideConfig };
```

---

## Theme Object Contract (MANDATORY)

The compile script passes a theme object with these **exact keys**:

| Key | Purpose | Example |
|-----|---------|---------|
| `theme.primary` | Darkest color, titles | `"22223b"` |
| `theme.secondary` | Dark accent, body text | `"4a4e69"` |
| `theme.accent` | Mid-tone accent | `"9a8c98"` |
| `theme.light` | Light accent | `"c9ada7"` |
| `theme.bg` | Background color | `"f2e9e4"` |

**NEVER use other key names** like `background`, `text`, `muted`, `darkest`, `lightest`.

---

## Page Number Badge (REQUIRED)

All slides **except Cover Page** MUST include a page number badge in the bottom-right corner.

- **Position**: x: 9.3", y: 5.1"
- Show current number only (e.g. `3` or `03`), NOT "3/12"
- Use palette colors, keep subtle

### Circle Badge (Default)

```javascript
slide.addShape(pres.shapes.OVAL, {
  x: 9.3, y: 5.1, w: 0.4, h: 0.4,
  fill: { color: theme.accent }
});
slide.addText("3", {
  x: 9.3, y: 5.1, w: 0.4, h: 0.4,
  fontSize: 12, fontFace: "Arial",
  color: "FFFFFF", bold: true,
  align: "center", valign: "middle"
});
```

### Pill Badge

```javascript
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x: 9.1, y: 5.15, w: 0.6, h: 0.35,
  fill: { color: theme.accent },
  rectRadius: 0.15
});
slide.addText("03", {
  x: 9.1, y: 5.15, w: 0.6, h: 0.35,
  fontSize: 11, fontFace: "Arial",
  color: "FFFFFF", bold: true,
  align: "center", valign: "middle"
});
```

---

## Dependencies (pre-installed in this container)

All dependencies are pre-baked into the agent image. The container rootfs is READ-ONLY — NEVER run `pip install` or `npm install` (they fail or pollute the tmpfs HOME).

- **Text extraction**: `pptx-markitdown <file>.pptx` — markitdown[pptx] from the skill's dedicated venv
- **Create from scratch**: `pptx-node compile.js` — isolated Node runtime with pptxgenjs@4.0.1 pre-installed (`NODE_PATH` is pre-set, so bare `require("pptxgenjs")` resolves from any working directory)
- **Template library**: `$PPTX_LIBRARY_DIR` / `$PPTX_STYLES_DIR` — read-only shared mount, may be absent or empty. Always guard with a graceful fallback; never write to it.
- **NOT installed**: react-icons / react / react-dom / sharp — do not plan icon pipelines that depend on them; use PptxGenJS shapes, text glyphs, or SVG instead
