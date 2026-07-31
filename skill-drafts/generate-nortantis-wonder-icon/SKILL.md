---
name: generate-nortantis-wonder-icon
description: Generate unregistered semantic fantasy world-wonder icons for FU-GM and Nortantis from structured landmark briefs. Use when producing or validating custom raster map symbols with Image 2, converting chroma-key monochrome line art into Nortantis semi-transparent alpha masks, or preparing disabled candidate icon catalogs. Never use this skill to place or enable icons, calculate map coordinates, or infer icon choice from place-name keywords.
---

# Generate Nortantis Wonder Icon

Create one semantic map symbol from a structured landmark brief, then convert it into a transparent black-ink mask compatible with Nortantis.

## Safety Boundary

- Keep generated assets unregistered by default.
- Write `enabled: false` and `runtime_registration: "none"` in candidate catalogs.
- Do not edit the Nortantis installed art pack, exporter, WorldState, or live campaign data.
- Do not let the image model choose coordinates, measure distance, or inspect map geometry.
- Do not select icons from place-name keywords. Require a semantic brief or an explicit `icon_id`.

## Workflow

1. Normalize the landmark into the structure in [references/brief-schema.md](references/brief-schema.md).
2. Separate required silhouette features from decorative details. Prefer one main subject, three to five mid-scale distinctive motifs, and a controlled layer of texture/detail marks.
3. Read [references/prompt-template.md](references/prompt-template.md), then call the configured Image 2 provider once per icon.
4. Use [assets/style-reference.png](assets/style-reference.png) only as a line-quality reference.
5. Generate on a flat chroma-key background with no text, shadow, frame, or map context. Use `#00ff00` by default, but switch to `#ff00ff` when the subject may contain plants, green terrain, or green magic.
6. Run:

   ```powershell
   python scripts/prepare_nortantis_icon.py `
     --input <generated.png> `
     --output <candidate.png> `
     --key-color "#00ff00"
   ```

7. Inspect the icon at approximately 96 px and 160 px high on a parchment background. Reject it when its main concept disappears at 96 px, or when it reads as a generic/simple pictogram instead of a handmade Nortantis-style wonder.
8. Add a catalog entry with semantic description, terrain constraints, scale class, and `enabled: false`.

## Visual Contract

- Use rough monochrome medieval cartography line art.
- Keep only black ink with alpha; make white interiors and the green screen transparent.
- Limit maximum ink alpha to `205/255` so the map texture remains visible.
- Preserve transparent padding around the cropped subject.
- Aim for a moderately intricate map symbol, not a minimalist pictogram. Each icon should have readable primary massing, secondary structures, and small surface details.
- Use a deliberate detail budget: one dominant silhouette, three to five mid-scale motifs, and roughly eight to eighteen short texture strokes, hatching marks, cracks, windows, banners, roots, runes, ripples, masonry seams, or contour accents as appropriate.
- Vary line weight and scale: use stronger outlines for the silhouette, medium lines for structural divisions, and lighter short strokes for texture. Avoid uniformly thick, blocky geometry.
- Keep complexity clustered inside the subject silhouette. Do not simplify by replacing architecture or terrain with plain rectangles, generic towers, or flat geometric blocks.
- Do not bake political-region color, terrain color, island color, or parchment fill into the asset. Enabled FU-GM land icons keep transparent interiors so Nortantis can merge them with the final rendered map background under the icon; only the ink line is darkened from the dominant map region under the icon's bottom footprint. Semantic island/ocean icons may still receive special fill handling.
- Use an isolated compact silhouette; avoid full scenes and tiny ornamental clutter.
- Treat the icon as a map overlay, not as its own terrain tile. Do not include raised ground slabs, cliff-side bases, thick platform edges, drop shadows, or baked-in land coloring.
- If an existing icon has an unwanted base, regenerate or redraw the icon. Do not "fix" it by masking, cropping, painting over, or hiding the bottom portion of the old asset; that leaves broken silhouettes and should be rejected.
- For bottom-grounded buildings, keep the footprint narrow and self-contained so it can sit on plain land without appearing to float over water. Do not draw a large opaque patch of ground under the building.
- Keep natural wonders free of town icons unless the brief explicitly includes a settlement.
- Depict lakes as water shapes and islands as light line-art islands surrounded by water rather than floating labels. Avoid heavy island fill that clashes with Nortantis political colors.
- Preview icons on parchment, forest, mountain, and coast backgrounds; reject any asset whose transparent edges make it look pasted on, whose base looks like a raised cake layer, or whose details collapse into a blob at 96 px.

## Nortantis Runtime Color Contract

- Treat prepared wonders such as `excavator_seven` and `oneria` as the reference path: source/catalog metadata is materialized into a black-ink alpha mask, then rendered through Nortantis as a `decorations` custom icon.
- Do not fix color mismatches by adding Java exporter special cases for `default_country_anchor`, country icons, or one-off icon names. Default country icons must use the same materialization and normal `customIconFilterColor` path as prepared wonders; only metadata such as `place_kind`, unique-per-map assignment, scale, and label offset may differ.
- Enabled FU-GM custom icon catalogs that rely on runtime color safety should declare `style: "nortantis_black_ink_alpha_mask"` and `alpha_max: 205`. The registry/materializer must honor these fields before copying files into `assets/nortantis_custom/decorations/fu_gm_world_wonders`.
- A valid materialized land icon has RGB set to pure black for every non-transparent pixel and alpha capped at `205`. Validate both the source icon and the generated Nortantis custom-pack copy when colors look wrong.
- If a previously correct prepared icon and a newly added country icon render differently, compare the materialized PNG statistics first (`rgb_bad == 0`, `max_alpha <= 205`) before touching placement or Java rendering code.
- Avoid repeatedly transforming already-materialized images on every render. Use a fast path such as alpha/RGB extrema checks or equivalent cache-safe detection.

## Output Contract

Return or persist:

- RGBA PNG candidate asset.
- Stable semantic `icon_id` using lowercase snake case.
- Chinese display name.
- Semantic description.
- Preferred terrain/domain.
- Suggested scale class.
- Explicit disabled status.

Do not register or activate the result unless a later task explicitly requests integration.

## Resources

- `scripts/prepare_nortantis_icon.py`: deterministic chroma extraction for configurable key colors, ink-mask conversion, crop, padding, and validation.
- [references/brief-schema.md](references/brief-schema.md): model-independent semantic input and catalog output.
- [references/prompt-template.md](references/prompt-template.md): Image 2 prompt construction.
- [assets/style-reference.png](assets/style-reference.png): Nortantis line-art reference.
- [assets/expected-mask.png](assets/expected-mask.png): example processed output.

## Integration Notes

- Register FU-GM wonder icons as Nortantis `decorations`, not `cities`. The exporter owns placement and labels; the icon asset should not rely on Nortantis city bottom-water deletion rules.
- Use catalog `place_kind`, `preferred_terrain`, `placement`, `anchor_mode`, and `default_scale` as structured metadata. Do not infer icon choice or placement from display-name keywords.
- Treat catalog `default_scale` as the semantic size class before runtime tuning. FU-GM multiplies it by `FU_GM_NORTANTIS_WONDER_ICON_SCALE_MULTIPLIER` (default `0.8`) when writing render briefs, so enabled icons should not be drawn larger to compensate.
- Use `place_kind` values such as `prepared_sky_island` or `world_wonder_sky_island` for floating-island/sky-city assets. The exporter treats these as land icons that may prefer mountains or hills while still avoiding water edges.
- If an enabled icon is edited after materialization, regenerate the Nortantis custom image pack before rendering smoke maps.
- After modifying Nortantis exporter logic, rebuild the jar before testing. If the machine has no system JDK, use a local JDK under `.runtime/jdk` and run Gradle with `JAVA_HOME` pointing at it.
- Smoke-test icon color changes with at least one prepared wonder and one default country icon on the same map. They should share the same black-ink alpha-mask behavior; differences should come from map-region tinting and placement, not baked asset color.
