# Nortantis in FU-GM

This directory vendors and modifies Nortantis as FU-GM's visual map generator.

- Upstream: https://github.com/jeheydorn/nortantis
- Imported commit: `16d81ce92b474b9c61b86877b57b0a255832fdc1`
- License: GNU Affero General Public License version 3; see `LICENSE` in this
  directory.
- Java requirement: JDK 21 or newer.

## Modification notice

FU-GM contributors modified this upstream source between 2026-06-23 and
2026-08-25. Relative to the imported commit, the material changes are:

- `src/nortantis/tools/FuGmHeadlessExporter.java`, a headless JSON-to-map
  command-line exporter;
- a small custom-icon rendering adjustment in `src/nortantis/IconDrawer.java`;
- longer Gradle download timeouts and use of the official downloads.gradle.org
  distribution host;
- `fu_gm_examples/` and this integration document.

The corresponding modified source is this directory. FU-GM invokes Nortantis as
a separate Java process and exchanges JSON briefs and exported files with it.
The Python world graph remains the authoritative game-rules state.

FU-GM should treat Nortantis as a visual-map provider only. Travel days,
threat levels, route choices, and other hard rules remain in FU-GM's Python
graph map system.

Planned integration shape:

1. Generate or load a `.nort` map settings file.
2. Translate Session 0 world facts into Nortantis settings and edits:
   land shape, region colors, labels, roads, icons, and custom art packs.
3. Export a PNG for player-facing map visuals.
4. Keep `WorldState.map_routes` as the authoritative machine-readable map.

Useful upstream classes:

- `nortantis.SettingsGenerator`
- `nortantis.MapSettings`
- `nortantis.MapCreator`
- `nortantis.swing.MapEdits`
- `nortantis.MapText`
- `nortantis.editor.FreeIcon`
- `nortantis.editor.Road`

The upstream application entry point is `nortantis.swing.MainWindow`, which
opens a Swing GUI. FU-GM adds a headless Java wrapper around the map generator
and image export pipeline.

## Headless exporter

FU-GM-specific command-line entry point:

```powershell
.\gradlew.bat --no-daemon jar
java --enable-native-access=ALL-UNNAMED -cp build\libs\Nortantis.jar nortantis.tools.FuGmHeadlessExporter --brief fu_gm_examples\gear_vine_brief.json
```

The brief format currently supports:

- base generation fields such as `seed`, `landShape`, `worldSize`, `regionCount`, `generatedWidth`, `generatedHeight`, and `resolution`;
- visual toggles and colors;
- `fontFamily` for CJK or other non-Latin labels;
- explicit `labels`;
- explicit `roads`;
- `outputPath` for PNG export and optional `settingsPath` for `.nort` export.

Coordinates may be normalized (`0.0` to `1.0`) or Nortantis source-pixel coordinates. Normalized coordinates are the expected FU-GM path, because FU-GM can convert logical graph nodes into rough canvas positions without asking a vision model to measure the rendered map.
