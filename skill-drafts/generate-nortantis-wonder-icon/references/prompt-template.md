# Image 2 Prompt Template

Fill the semantic subject block from the approved brief. Do not add landmarks that are absent from the brief.

```text
Use case: stylized-concept
Asset type: isolated fantasy world-wonder cartography icon for Nortantis
Input image: style reference only. Match its loose handmade black-ink line quality, imperfect medieval map-symbol construction, sparse solid-black accents, and simple readable silhouette.
Primary request: <one subject plus two or three mandatory silhouette motifs>
Style/medium: monochrome black ink line art, slightly rough hand-drawn strokes, no grayscale shading, no color inside the subject.
Composition: one centered isolated landmark, three-quarter elevated view unless the brief requires top-down or cross-section, compact silhouette readable at 96 pixels, generous padding.
Scene/backdrop: perfectly flat solid <key color> chroma-key background. Use #00ff00 by default, or #ff00ff when the subject may include green plants, green terrain, or green magic.
Constraints: uniform chroma-key background; crisp edges; no cast shadow; no frame; no label; no letters; no watermark; no map context; do not use the chroma-key color in the subject; avoid tiny ornamental clutter; no raised terrain slab; no cliff-side base; no thick platform edge; no baked-in land color; no baked-in political or terrain fill color.
Avoid: <forbidden motifs from the semantic brief>, terrain cake, floating base, opaque ground patch, baked-in region tint, cropped old asset, masked-over base
```

## Prompt Checks

- Describe geometry and visible motifs, not lore alone.
- State exact counts when identity depends on count, such as exactly three crystal towers.
- State exclusions explicitly when a natural feature must not receive a town icon.
- Keep waterfalls, rivers, roads, reflections, and gravity direction unambiguous.
- For settlements and towers, draw only the building silhouette and immediate footprint; do not draw a large island of ground underneath it.
- For wastelands, deserts, wounds, borders, and similar terrain concepts, draw symbolic surface marks from top-down/low relief rather than an extruded tile.
- For sky islands and floating cities, draw the floating structure silhouette only; do not add a colored underside patch or surrounding terrain plate.
- If revising a bad icon, redraw from scratch or use image editing that reconstructs the missing form. Cropping, masking, or painting over the bottom of an existing icon is not acceptable.
- Generate one image per distinct asset.
