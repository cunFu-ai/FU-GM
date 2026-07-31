# Image 2 Prompt Template

Fill the semantic subject block from the approved brief. Do not add landmarks that are absent from the brief.

```text
Use case: stylized-concept
Asset type: isolated fantasy world-wonder cartography icon for Nortantis
Input image: style reference only. Match its loose handmade black-ink line quality, imperfect medieval map-symbol construction, layered landmark detail, sparse solid-black accents, and readable silhouette.
Primary request: <one subject plus three to five mandatory silhouette/detail motifs>
Style/medium: monochrome black ink line art, slightly rough hand-drawn strokes, no grayscale shading, no color inside the subject, varied line weight.
Detail budget: moderately intricate, not minimalist. Include one dominant silhouette, three to five mid-scale features, and roughly eight to eighteen short detail marks such as cracks, windows, hatching, masonry seams, roots, banners, runes, ripples, contour lines, or broken fragments as appropriate.
Composition: one centered isolated landmark, three-quarter elevated view unless the brief requires top-down or cross-section, compact silhouette readable at 96 pixels, generous padding. Complexity should stay inside the subject silhouette rather than becoming a full scene.
Scene/backdrop: perfectly flat solid <key color> chroma-key background. Use #00ff00 by default, or #ff00ff when the subject may include green plants, green terrain, or green magic.
Constraints: uniform chroma-key background; crisp edges; no cast shadow; no frame; no label; no letters; no watermark; no map context; do not use the chroma-key color in the subject; no raised terrain slab; no cliff-side base; no thick platform edge; no baked-in land color; no baked-in political or terrain fill color.
Avoid: <forbidden motifs from the semantic brief>, overly simple pictogram, plain rectangles, generic tower cluster, tiny unreadable ornament dust, terrain cake, floating base, opaque ground patch, baked-in region tint, cropped old asset, masked-over base
```

## Prompt Checks

- Describe geometry and visible motifs, not lore alone.
- State exact counts when identity depends on count, such as exactly three crystal towers.
- State exclusions explicitly when a natural feature must not receive a town icon.
- Include a detail budget in the prompt. If the prompt can be satisfied by two or three plain shapes, it is underspecified.
- Prefer medium-detail marks that survive downscaling: windows, cracks, contour strokes, hatching bands, masonry seams, banners, roots, runes, ripples, broken fragments. Avoid confetti-like micro-ornament.
- For architecture, require believable massing: roofs, arches, windows, buttresses, spires, bridges, walls, or terraces should align and connect. Do not accept isolated rectangles or mismatched tower blocks.
- Keep waterfalls, rivers, roads, reflections, and gravity direction unambiguous.
- For settlements and towers, draw only the building silhouette and immediate footprint; do not draw a large island of ground underneath it.
- For wastelands, deserts, wounds, borders, and similar terrain concepts, draw symbolic surface marks from top-down/low relief rather than an extruded tile.
- For sky islands and floating cities, draw the floating structure silhouette only; do not add a colored underside patch or surrounding terrain plate.
- If revising a bad icon, redraw from scratch or use image editing that reconstructs the missing form. Cropping, masking, or painting over the bottom of an existing icon is not acceptable.
- Generate one image per distinct asset.
- Do not ask the image model to imitate final map-region tinting. The generated asset should stay monochrome; runtime materialization and Nortantis filtering handle black-ink alpha masks consistently for prepared wonders and default country icons.
