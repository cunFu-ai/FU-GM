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
2. Separate required silhouette features from decorative details. Prefer one main subject and two or three distinctive motifs.
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

7. Inspect the icon at approximately 96 px and 160 px high on a parchment background. Reject it when its main concept disappears at 96 px.
8. Add a catalog entry with semantic description, terrain constraints, scale class, and `enabled: false`.

## Visual Contract

- Use rough monochrome medieval cartography line art.
- Keep only black ink with alpha; make white interiors and the green screen transparent.
- Limit maximum ink alpha to `205/255` so the map texture remains visible.
- Preserve transparent padding around the cropped subject.
- Do not bake political-region color, terrain color, island color, or parchment fill into the asset. Enabled FU-GM land icons keep transparent interiors so Nortantis can merge them with the final rendered map background under the icon; only the ink line is darkened from the dominant map region under the icon's bottom footprint. Semantic island/ocean icons may still receive special fill handling.
- Use an isolated compact silhouette; avoid full scenes and tiny ornamental clutter.
- Treat the icon as a map overlay, not as its own terrain tile. Do not include raised ground slabs, cliff-side bases, thick platform edges, drop shadows, or baked-in land coloring.
- If an existing icon has an unwanted base, regenerate or redraw the icon. Do not "fix" it by masking, cropping, painting over, or hiding the bottom portion of the old asset; that leaves broken silhouettes and should be rejected.
- For bottom-grounded buildings, keep the footprint narrow and self-contained so it can sit on plain land without appearing to float over water. Do not draw a large opaque patch of ground under the building.
- Keep natural wonders free of town icons unless the brief explicitly includes a settlement.
- Depict lakes as water shapes and islands as light line-art islands surrounded by water rather than floating labels. Avoid heavy island fill that clashes with Nortantis political colors.
- Preview icons on parchment, forest, mountain, and coast backgrounds; reject any asset whose transparent edges make it look pasted on or whose base looks like a raised cake layer.

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
