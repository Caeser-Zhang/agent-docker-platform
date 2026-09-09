# Editing Existing Presentations

## Template-Based Workflow

The source deck is either one the user supplied, or one from the shared platform
library (`$PPTX_LIBRARY_DIR`, read-only). Both flows below are identical — the
only difference is where `template.pptx` is copied from, and that library decks
are already normalized. See [template-library.md](template-library.md) for
catalogue fields and how to choose.

1. **Copy and analyze**:
   ```bash
   # user-supplied deck
   cp /path/to/user-provided.pptx template.pptx

   # platform library deck (read-only mount — copy out, never edit in place)
   cp "$PPTX_LIBRARY_DIR/files/<id>.pptx" template.pptx

   pptx-markitdown template.pptx > template.md
   ```
   Review `template.md` to see placeholder text and slide structure.

   For a library deck, also read its `index.json` record: `page_types` is your
   slide map (`index` already matches the normalized deck), `content_layout` is
   the layout to use for new slides, and `must_replace` is the list of
   placeholder strings you must clear.

2. **Plan slide mapping**: For each content section, choose a template slide.

   **USE VARIED LAYOUTS** — monotonous presentations are a common failure mode. Don't default to basic title + bullet slides. Actively seek out:
   - Multi-column layouts (2-column, 3-column)
   - Image + text combinations
   - Full-bleed images with text overlay
   - Quote or callout slides
   - Section dividers
   - Stat/number callouts
   - Icon grids or icon + text rows

   **Avoid:** Repeating the same text-heavy layout for every slide.

   Match content type to layout style (e.g., key points -> bullet slide, team info -> multi-column, testimonials -> quote slide).

3. **Unpack**: Extract the PPTX into an editable XML tree using Python's `zipfile` module. Pretty-print the XML for readability.

4. **Build presentation** (do this yourself, not with subagents):
   - Delete unwanted slides (remove from `<p:sldIdLst>`)
   - Duplicate slides you want to reuse (copy slide XML, relationships, and update `Content_Types.xml` and `presentation.xml`)
   - Reorder slides in `<p:sldIdLst>`
   - **Complete all structural changes before step 5**

5. **Edit content**: Update text in each `slide{N}.xml`.
   **Use subagents here if available** — slides are separate XML files, so subagents can edit in parallel.

   For a library deck, clear every `must_replace` entry from the catalogue record
   (`residual_texts` tells you which part each one lives in). Then verify:

   ```bash
   grep -rnE "单击此处|点击此处|在此处键入|请输入|Click to (add|edit)|Type to add|Lorem ipsum|20\s*XX|\bXXX+\b" unpacked/ppt/
   ```

   Empty output is the pass condition.

6. **Clean**: Remove orphaned files — slides not in `<p:sldIdLst>`, unreferenced media, orphaned rels.

   **Skip this step for library decks.** Ingest already stripped promo slides,
   orphaned media/rels/tag parts and vendor branding, and slimmed the images.
   Re-running a cleanup pass only risks breaking references that are already
   consistent. Do not rename files under `ppt/media/` (a `.jpg` may have been a
   PNG originally — its rels and content types were updated together), and do not
   re-compress images. You still must remove orphans created by *your own* slide
   deletions in step 4.

7. **Pack**: Repack the XML tree into a PPTX file. Validate, repair, condense XML, re-encode smart quotes.

   Always write to `/tmp/` first, then copy to the final path. Python's `zipfile` module uses `seek` internally, which fails on some volume mounts (e.g. Docker bind mounts). Writing to a local temp path avoids this.

## Output Structure

Copy the source deck to `template.pptx` in cwd — whether it came from the user or
from `$PPTX_LIBRARY_DIR/files/<id>.pptx`. This preserves the original and gives a
predictable name for all downstream operations. The library mount is read-only and
shared by every user: nothing is ever written, unpacked or repacked inside it.

```bash
cp /path/to/user-provided.pptx template.pptx
# or
cp "$PPTX_LIBRARY_DIR/files/<id>.pptx" template.pptx
```

```text
./
├── template.pptx               # Copy of the source deck (never modified)
├── template.md                 # markitdown extraction
├── unpacked/                   # Editable XML tree
└── edited.pptx                 # Final repacked deck
```

Minimum expected deliverable: `edited.pptx`.

## Slide Operations

Slide order is in `ppt/presentation.xml` -> `<p:sldIdLst>`.

**Reorder**: Rearrange `<p:sldId>` elements.

**Delete**: Remove `<p:sldId>`, then clean orphaned files.

**Add**: Copy the source slide's XML file, its `.rels` file, and update `Content_Types.xml` and `presentation.xml`. Never manually copy slide files without updating all references — this causes broken notes references and missing relationship IDs.

## Editing Content

**Subagents:** If available, use them here (after completing step 4). Each slide is a separate XML file, so subagents can edit in parallel. In your prompt to subagents, include:
- The slide file path(s) to edit
- **"Use the Edit tool for all changes"**
- The formatting rules and common pitfalls below

For each slide:
1. Read the slide's XML
2. Identify ALL placeholder content — text, images, charts, icons, captions
3. Replace each placeholder with final content

**Use the Edit tool, not sed or Python scripts.** The Edit tool forces specificity about what to replace and where, yielding better reliability.

## Formatting Rules

- **Bold all headers, subheadings, and inline labels**: Use `b="1"` on `<a:rPr>`. This includes:
  - Slide titles
  - Section headers within a slide
  - Inline labels like (e.g.: "Status:", "Description:") at the start of a line
- **Never use unicode bullets**: Use proper list formatting with `<a:buChar>` or `<a:buAutoNum>`
- **Bullet consistency**: Let bullets inherit from the layout. Only specify `<a:buChar>` or `<a:buNone>`.

## Common Pitfalls — Template Editing

### Template Adaptation

When source content has fewer items than the template:
- **Remove excess elements entirely** (images, shapes, text boxes), don't just clear text
- Check for orphaned visuals after clearing text content
- Run content QA with `markitdown` to catch mismatched counts

When replacing text with different length content:
- **Shorter replacements**: Usually safe
- **Longer replacements**: May overflow or wrap unexpectedly
- Verify with `markitdown` after text changes
- Consider truncating or splitting content to fit the template's design constraints

**Template slots != Source items**: If template has 4 team members but source has 3 users, delete the 4th member's entire group (image + text boxes), not just the text.

### Multi-Item Content

If source has multiple items (numbered lists, multiple sections), create separate `<a:p>` elements for each — **never concatenate into one string**.

**WRONG** — all items in one paragraph:
```xml
<a:p>
  <a:r><a:rPr .../><a:t>Step 1: Do the first thing. Step 2: Do the second thing.</a:t></a:r>
</a:p>
```

**CORRECT** — separate paragraphs with bold headers:
```xml
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" b="1" .../><a:t>Step 1</a:t></a:r>
</a:p>
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" .../><a:t>Do the first thing.</a:t></a:r>
</a:p>
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" b="1" .../><a:t>Step 2</a:t></a:r>
</a:p>
<!-- continue pattern -->
```

Copy `<a:pPr>` from the original paragraph to preserve line spacing. Use `b="1"` on headers.

### Smart Quotes

The Edit tool converts smart quotes to ASCII. **When adding new text with quotes, use XML entities:**

```xml
<a:t>the &#x201C;Agreement&#x201D;</a:t>
```

| Character | Name | Unicode | XML Entity |
|-----------|------|---------|------------|
| \u201c | Left double quote | U+201C | `&#x201C;` |
| \u201d | Right double quote | U+201D | `&#x201D;` |
| \u2018 | Left single quote | U+2018 | `&#x2018;` |
| \u2019 | Right single quote | U+2019 | `&#x2019;` |

### Other

- **Whitespace**: Use `xml:space="preserve"` on `<a:t>` with leading/trailing spaces
- **XML parsing**: Use `defusedxml.minidom`, not `xml.etree.ElementTree` (corrupts namespaces)
