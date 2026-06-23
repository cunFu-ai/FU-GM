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
  "forbidden_motifs": ["ordinary town", "label", "map frame"],
  "placement_domain": ["land", "ruins"],
  "scale_class": "large_landmark"
}
```

## Rules

- Derive the brief from meaning and narrative context, never from place-name keyword matching.
- Keep `icon_id` stable after the asset is persisted.
- Keep placement constraints separate from image composition.
- Let WorldState and the Nortantis exporter choose valid coordinates.
- Use `scale_class` values `normal_landmark`, `large_landmark`, or `major_wonder`.

## Candidate Catalog Entry

```json
{
  "icon_id": "broken_ascension_tower",
  "name_zh": "断裂登神塔",
  "file": "broken_ascension_tower.png",
  "place_kind": "world_wonder_ruin",
  "preferred_terrain": ["ruins", "plain", "mountain", "land"],
  "default_scale": 1.05,
  "semantic_description": "高塔从中央断裂，上半段与碎石悬浮在下半段之上。",
  "enabled": false
}
```
