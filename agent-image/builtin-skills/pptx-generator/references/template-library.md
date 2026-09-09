# Platform Template Library

The platform ships a curated PPTX template library plus machine-readable style
presets on a **read-only** mount inside every container. Prefer it over building
a deck from scratch whenever the user did not supply their own presentation.

| Env var | Default | Contents |
|---------|---------|----------|
| `PPTX_LIBRARY_DIR` | `/library/pptx` | catalogue + template decks + previews |
| `PPTX_STYLES_DIR` | `/library/pptx/styles` | palettes / recipes / typography JSON |

```text
/library/pptx/
├── index.json            # the catalogue — read this FIRST
├── files/{id}.pptx       # normalized decks (id = 12-hex content hash)
├── thumbs/{id}.png       # first-slide previews
└── styles/
    ├── styles.json       # everything below in one file
    ├── palettes.json     # curated color palettes
    ├── recipes.json      # style recipes + selection guide
    └── typography.json   # fonts, type scale, spacing, mandatory rules
```

**The mount is read-only and shared by every user.** Never write, unpack or
repack inside `$PPTX_LIBRARY_DIR` — the write fails with `EROFS`. Always copy the
deck into the workspace first, and never plan a workflow that depends on
something you stored in the library.

---

## Step 1 — Pick a template

1. **If the prompt names one** (a `/library/pptx/files/<id>.pptx` path, or a
   template id / name), use exactly that one. Do not substitute a "better fit".
2. **Otherwise read the catalogue** and choose from it:
   ```bash
   cat "$PPTX_LIBRARY_DIR/index.json"
   ```
   Match on `tags`, `name_zh` / `description`, `palette` or `palette_hint`,
   `aspect`, and `page_type_summary` (does the deck have the page kinds the
   content needs — cover, toc, section, content, summary?).
3. **If nothing fits**, or `index.json` is missing or its `templates` array is
   empty, fall back to the
   [from-scratch workflow](../SKILL.md#creating-from-scratch--workflow).
   An empty library is a normal state, not an error — never fail the task over it.

### Catalogue fields worth reading

| Field | Meaning | How to use it |
|-------|---------|---------------|
| `id` | 12-hex content hash | filename is `files/{id}.pptx` |
| `slides` | page count after normalization | plan your outline against it |
| `aspect` | `16:9` / `4:3` | see [Aspect ratio](#aspect-ratio-mismatch) |
| `slide_size_inches` | `[w, h]` | must match `pres.layout` when mixing |
| `page_types` | per-page `{index, part, layout, type, title, images}` | your slide map — `index` matches the normalized deck |
| `page_type_summary` | counts per page type | variety check before you start |
| `content_layout` | layout most content pages use | the layout for **new** slides |
| `palette` / `recipe` | admin-assigned preset ids | use these when set |
| `palette_hint` | auto-detected dominant colors | fallback when `palette` is null |
| `fonts` | `{theme: {...}, used: [...]}` | keep the deck's own typefaces |
| `must_replace` | placeholder strings still present | your mandatory TODO list |
| `residual_texts` | same hits **with part names** | locates them for editing |
| `has_chart_part` | deck contains real charts | charts need `ppt/charts/` surgery, not text edits |
| `animated_slides` | pages carrying `<p:timing>` | must be stripped, see below |
| `warnings` | residual structural notes | read before trusting the deck |

---

## Step 2 — Copy, then edit

```bash
cp "$PPTX_LIBRARY_DIR/files/<id>.pptx" template.pptx
pptx-markitdown template.pptx > template.md
```

Then continue with [editing.md](editing.md) from step 2 (plan slide mapping).
`template.pptx` stays untouched; your deliverable is `edited.pptx` in the
workspace. Writing to `/tmp/` before the final path is still required — Python's
`zipfile` uses `seek`, which fails on some volume mounts.

---

## What normalization already did — do not redo

Every deck was processed at ingest, so a library template is cleaner than a
random download:

- promo / "more templates" marketing slides removed
- orphaned media, orphaned relationships and dangling tag parts removed
- images slimmed: no-alpha PNG → JPEG q80, alpha PNG → 256-color quantized,
  long edge downsampled to ≤1600 px
- vendor branding scrubbed from theme, `docProps`, layouts, masters and notes
- structural validation passed — no dangling relationship, complete
  `[Content_Types].xml`, so PowerPoint opens it without a "repair" prompt

Consequences for you:

- **Skip the cleanup pass.** There is nothing to clean; re-running one only risks
  breaking consistent references.
- **Don't touch media filenames.** A `ppt/media/image7.jpg` may have been a PNG
  originally — its `.rels` targets and content types were updated together and
  are correct as they stand.
- **Don't re-compress images.** They are already at the quality/size floor.
- `page_types[].index` refers to the **normalized** deck, so it lines up with
  what you unpack — no offset bookkeeping needed.

---

## Step 3 — Clear `must_replace`

Normalization *reports* leftover placeholder text; it never rewrites slide
content. `must_replace` is therefore your mandatory TODO list. Typical entries:
`单击此处添加标题`, `在此处键入`, `Click to add`, `20XX年`, `XXX`, `Lorem ipsum`.

For each entry:

1. Locate it — `residual_texts` already tells you the part
   (`ppt/slides/slide3.xml`, `ppt/slideLayouts/slideLayout5.xml`, …).
2. Replace it with real content, **or delete the whole shape group** if you have
   nothing to put there (see editing.md §Template Adaptation — clearing the text
   and leaving an empty box is the failure mode).
3. Layout/master hits you cannot fill are usually decorative prompts; deleting
   the placeholder shape there is safe and stops it showing through on new slides.

Verify nothing survived:

```bash
grep -rnE "单击此处|点击此处|在此处键入|请输入|Click to (add|edit)|Type to add|Lorem ipsum|20\s*XX|\bXXX+\b" unpacked/ppt/
```

Empty output is the pass condition.

---

## Step 4 — Adding slides

New slides must use the layout named in `content_layout` — the layout the deck's
own content pages dominate on. Attaching body content to the *title* layout is
the most common way a template deck starts looking inconsistent.

`page_types` maps every existing page to its layout, so the reliable move is:
find the page whose `type` and `images` count are closest to what you need, and
duplicate that page (XML + `.rels` + `[Content_Types].xml` + `<p:sldIdLst>`, per
editing.md §Slide Operations).

---

## Style presets

`$PPTX_STYLES_DIR` is the JSON mirror of
[design-system.md](design-system.md) — same values, machine-readable. Read the
JSON when you need exact numbers instead of transcribing Markdown tables.

| File | Payload |
|------|---------|
| `palettes.json` | `palettes[]`: `id`, `name`, `name_zh`, `colors` (5 hex, ordered), `style`, `use_cases[]`, `tips`, `dark_mode_required` |
| `recipes.json` | `recipes[]`: `id` (`sharp`/`soft`/`rounded`/`pill`), `corner_radius`, `spacing`, `components` (rectRadius per component), `radius_by_height`; plus `selection_guide` |
| `typography.json` | `fonts` + `pairings`, `type_scale_pt`, `spacing_scale_inches`, `density_zones`, and `rules` |
| `styles.json` | all of the above in one document |

How to apply them:

- **Template-based work:** if the record has `palette` / `recipe` set, use those
  — they were chosen to match the deck. If only `palette_hint` is present, keep
  the template's own colors and use the hint to pick a matching palette for the
  *new* elements you add.
- **From scratch:** pick a `palette` and a `recipe` from the presets rather than
  inventing colors. `rules.palette` forbids modifying or mixing them; the only
  allowed adjustment is `transparency` (0–100).
- **Always:** `rules.forbidden` — no gradients, no animations, no slide
  transitions. Solid colors and static slides only.
- **Units:** recipes and typography are in **inches** (PptxGenJS native); the
  slide is 10 × 5.625 in.
- **Hex format:** `palettes[].colors` carry a leading `#`; PptxGenJS wants 6-char
  hex **without** `#`. Strip it.
- `dark_mode_required: true` palettes need a dark background — pair them with a
  light text color from the same palette.

---

## Aspect ratio mismatch

`aspect` is normally `16:9`. If a template says `4:3` and the user wants 16:9,
do **not** stretch it: pick another template, or generate from scratch with
`pres.layout = 'LAYOUT_16x9'`. Editing `<p:sldSz>` in `ppt/presentation.xml`
rescales the canvas but leaves every absolutely-positioned shape where it was,
which is worse than starting over.

---

## Hard rules

- Never write into `$PPTX_LIBRARY_DIR` — read-only mount, shared by all users.
- Never delete library pages to "make room". Promo pages are already gone; what
  remains is content.
- Never assume a template exists. Read `index.json` first and degrade gracefully.
- Never reference the library path in the delivered deck's text or notes — the
  recipient does not have that mount.
- If `animated_slides > 0`, strip `<p:timing>` from those slides: the deliverable
  must be static.
- Deliverables go to the workspace (`edited.pptx`, or `slides/output/` for
  from-scratch runs), never to the library.
