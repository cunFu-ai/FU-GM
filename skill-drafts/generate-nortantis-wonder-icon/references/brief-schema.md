# Semantic Brief

Require the caller to provide or approve this structure before image generation:

```json
{
  "icon_id": "broken_ascension_tower",
  "display_name": "断裂登神塔",
  "semantic_description": "通天高塔从中央断裂，上半段悬浮在断口之上。",
  "subject_class": "world_wonder_ruin",
  "silhouette": "one tall broken tower with a floating upper section",
  "mandatory_motifs": ["central break", "floating masonry fragments"],
  "detail_motifs": ["cracked stone courses", "small arched windows", "loose falling blocks"],
  "forbidden_motifs": ["ordinary town", "label", "map frame"],
  "placement_domain": ["land", "ruins"],
  "scale_class": "large_landmark",
  "detail_density": "moderate"
}
```

## Rules

- Derive the brief from meaning and narrative context, never from place-name keyword matching.
- Keep `icon_id` stable after the asset is persisted.
- Keep placement constraints separate from image composition.
- Let WorldState and the Nortantis exporter choose valid coordinates.
- Use `scale_class` values `normal_landmark`, `large_landmark`, or `major_wonder`.
- Use `detail_density` values `moderate` or `rich`; avoid `minimal` unless the user explicitly asks for a simple marker.
- Fill `detail_motifs` with medium-scale readable marks, not tiny ornament dust. Good examples: masonry seams, cracks, arched windows, hatching bands, banners, roots, contour lines, runes, ripples, floating fragments.
- Treat `mandatory_motifs` as identity-defining silhouette/features and `detail_motifs` as richness/texture requirements.

## Candidate Catalog Entry

For enabled/custom FU-GM icon catalogs, include top-level materialization settings unless the caller explicitly wants raw passthrough assets:

```json
{
  "style": "nortantis_black_ink_alpha_mask",
  "alpha_max": 205,
  "icons": []
}
```

These fields are part of the runtime color contract. They make default country icons and prepared wonders follow the same black-ink alpha-mask path before Nortantis applies its normal custom icon filtering.

```json
{
  "icon_id": "broken_ascension_tower",
  "name_zh": "断裂登神塔",
  "file": "broken_ascension_tower.png",
  "place_kind": "world_wonder_ruin",
  "preferred_terrain": ["ruins", "plain", "mountain", "land"],
  "default_scale": 1.05,
  "semantic_description": "高塔从中央断裂，上半段与碎石悬浮在下半段之上。",
  "detail_density": "moderate",
  "enabled": false
}
```

When diagnosing color mismatches, inspect the materialized PNG under the Nortantis custom decorations pack as well as the source candidate. A correct land icon has pure black RGB for non-transparent pixels and alpha no higher than `205`.
