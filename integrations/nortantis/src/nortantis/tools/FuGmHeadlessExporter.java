package nortantis.tools;

import nortantis.BorderColorOption;
import nortantis.BorderPosition;
import nortantis.IconType;
import nortantis.IconDrawer;
import nortantis.HSBColor;
import nortantis.LandShape;
import nortantis.LineBreak;
import nortantis.LoggerWarningLogger;
import nortantis.MapCreator;
import nortantis.MapSettings;
import nortantis.MapText;
import nortantis.NamedResource;
import nortantis.Region;
import nortantis.RoadDrawer;
import nortantis.SettingsGenerator;
import nortantis.Stroke;
import nortantis.StrokeType;
import nortantis.TextType;
import nortantis.TextureSource;
import nortantis.WorldGraph;
import nortantis.editor.CenterIconType;
import nortantis.editor.CenterEdit;
import nortantis.editor.FreeIcon;
import nortantis.editor.MapParts;
import nortantis.editor.RegionEdit;
import nortantis.editor.Road;
import nortantis.geom.Point;
import nortantis.graph.voronoi.Center;
import nortantis.graph.voronoi.Edge;
import nortantis.platform.Color;
import nortantis.platform.Font;
import nortantis.platform.Image;
import nortantis.platform.ImageHelper;
import nortantis.platform.PlatformFactory;
import nortantis.platform.awt.AwtFactory;
import nortantis.swing.MapEdits;
import nortantis.swing.translation.Translation;
import nortantis.util.Assets;
import org.json.simple.JSONArray;
import org.json.simple.JSONObject;
import org.json.simple.JSONValue;
import org.json.simple.parser.ParseException;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

/**
 * Thin headless bridge for FU-GM. It lets Python pass a structured map brief to Nortantis without opening the Swing editor.
 */
public class FuGmHeadlessExporter
{
	private static final int FU_GM_SEMANTIC_ISLAND_REGION_ID = 870001;
	private static final int CUSTOM_ICON_MIN_HOP_DISTANCE = 4;
	private static final int CUSTOM_ICON_SPACING_HOPS = 8;
	private static final double CUSTOM_ICON_OVERLAP_GAP = 24.0;

	public static void main(String[] args) throws Exception
	{
		System.setProperty("java.awt.headless", "true");
		PlatformFactory.setInstance(new AwtFactory());
		Translation.initialize();

		Path briefPath = parseBriefPath(args);
		JSONObject brief = readBrief(briefPath);
		Path briefDir = briefPath.toAbsolutePath().getParent();
		if (briefDir == null)
		{
			briefDir = Paths.get(".").toAbsolutePath();
		}

		new FuGmHeadlessExporter().export(brief, briefDir);

		// AWT may keep non-daemon helper threads alive after a headless export.
		// The command-line exporter has no reusable process state, so terminate
		// explicitly once every requested artifact has been written.
		System.exit(0);
	}

	private void export(JSONObject brief, Path briefDir) throws IOException
	{
		String artPack = stringValue(brief, "artPack", Assets.installedArtPack);
		String customImagesPath = optionalPathString(brief, briefDir, "customImagesPath");
		long seed = longValue(brief, "seed", System.currentTimeMillis());

		MapSettings settings = SettingsGenerator.generate(new Random(seed), artPack, customImagesPath);
		settings.artPack = artPack;
		settings.customImagesPath = customImagesPath;
		applySettings(brief, settings);

		int terrainSeedAttempts = intValue(brief, "terrainSeedAttempts", 1);
		if (brief.containsKey("terrainSeed"))
		{
			settings.randomSeed = longValue(brief, "terrainSeed", settings.randomSeed);
		}
		else
		{
			settings.randomSeed = chooseTerrainSeed(brief, settings, terrainSeedAttempts);
		}

		MapCreator creator = new MapCreator();
		MapParts mapParts = new MapParts();

		// The Swing editor normally does this after the first draw. The command-line bridge must do it explicitly.
		settings.edits.bakeGeneratedTextAsEdits = true;
		Image warmup = creator.createMap(settings, null, mapParts);
		warmup.close();
		if (!settings.edits.isInitialized() && mapParts.graph != null)
		{
			settings.edits.initializeCenterEdits(mapParts.graph.centers);
			settings.edits.initializeRegionEdits(mapParts.graph.regions.values());
			settings.edits.initializeEdgeEdits(mapParts.graph.edges);
		}
		ensureTerrainOnMajorLandmasses(settings, mapParts.graph);
		thinRuggedTerrainIcons(settings, mapParts.graph);
		applyLocationGeography(brief, settings, mapParts.graph);

		settings.edits.text.clear();
		Map<String, LocationAnchor> locationAnchorsByName = applyLabels(brief, settings, mapParts.graph);
		applyLocationTerrainEdits(brief, settings, mapParts.graph, locationAnchorsByName);
		applyPoliticalRegions(brief, settings, mapParts.graph, locationAnchorsByName);
		applyLocationIcons(brief, settings, mapParts.graph, locationAnchorsByName);
		relocateGeneratedCitiesToPlains(brief, settings, mapParts.graph);
		normalizeCityFlagsToVisibleIcons(settings, mapParts.graph);
		applyGeneratedCityNames(brief, settings, mapParts.graph, locationAnchorsByName);
		settings.edits.roads.clear();
		if (boolValue(brief, "generateRandomCityRoads", false))
		{
			applyGeneratedCityRoads(settings, mapParts.graph);
		}
		applyRoads(brief, settings, mapParts.graph, locationAnchorsByName);

		Path outputPath = requiredOutputPath(brief, briefDir, "outputPath");
		Files.createDirectories(outputPath.getParent());

		MapParts finalMapParts = new MapParts();
		finalMapParts.graph = mapParts.graph;
		Image map = creator.createMap(settings, null, finalMapParts);
		ImageHelper.getInstance().write(map, outputPath.toString());
		map.close();

		String settingsPathRaw = stringValue(brief, "settingsPath", null);
		Path settingsPath = null;
		if (settingsPathRaw != null && !settingsPathRaw.isBlank())
		{
			settingsPath = resolvePath(briefDir, settingsPathRaw);
			Files.createDirectories(settingsPath.getParent());
			settings.writeToFile(settingsPath.toString());
		}

		String manifestPathRaw = stringValue(brief, "manifestPath", null);
		Path manifestPath = null;
		if (manifestPathRaw != null && !manifestPathRaw.isBlank())
		{
			manifestPath = resolvePath(briefDir, manifestPathRaw);
			writeSemanticManifest(
					brief,
					settings,
					mapParts.graph,
					locationAnchorsByName,
					manifestPath);
		}

		System.out.println("{\"ok\":true,\"outputPath\":\"" + escapeJson(outputPath.toString()) + "\",\"settingsPath\":"
				+ (settingsPath == null ? "null" : "\"" + escapeJson(settingsPath.toString()) + "\"") + ",\"manifestPath\":"
				+ (manifestPath == null ? "null" : "\"" + escapeJson(manifestPath.toString()) + "\"") + "}");
	}

	@SuppressWarnings("unchecked")
	private static void writeSemanticManifest(
			JSONObject brief,
			MapSettings settings,
			WorldGraph graph,
			Map<String, LocationAnchor> locationAnchorsByName,
			Path manifestPath) throws IOException
	{
		int width = Math.max(1, intValue(brief, "semanticGridWidth", 20));
		int height = Math.max(1, intValue(brief, "semanticGridHeight", 12));
		int[][] water = new int[height][width];
		int[][] lakes = new int[height][width];
		int[][] land = new int[height][width];
		int[][] coast = new int[height][width];
		int[][] forests = new int[height][width];
		int[][] mountains = new int[height][width];
		int[][] hills = new int[height][width];
		double graphWidth = graphWidth(graph, settings);
		double graphHeight = graphHeight(graph, settings);

		if (graph != null && graph.centers != null)
		{
			for (Center center : graph.centers)
			{
				if (center == null || center.loc == null)
				{
					continue;
				}
				int column = semanticColumn(center.loc.x, graphWidth, width);
				int row = semanticRow(center.loc.y, graphHeight, height);
				if (center.isLake)
				{
					lakes[row][column]++;
				}
				else if (center.isWater)
				{
					water[row][column]++;
				}
				else
				{
					land[row][column]++;
				}
				if (center.isCoast)
				{
					coast[row][column]++;
				}
			}
		}

		if (settings != null && settings.edits != null && settings.edits.freeIcons != null)
		{
			Map<Integer, Center> centersByIndex = centersByIndex(graph);
			for (FreeIcon icon : settings.edits.freeIcons)
			{
				if (icon == null || icon.centerIndex == null)
				{
					continue;
				}
				Center center = centersByIndex.get(icon.centerIndex);
				if (center == null || center.loc == null)
				{
					continue;
				}
				int column = semanticColumn(center.loc.x, graphWidth, width);
				int row = semanticRow(center.loc.y, graphHeight, height);
				if (icon.type == IconType.trees)
				{
					forests[row][column]++;
				}
				else if (icon.type == IconType.mountains)
				{
					mountains[row][column]++;
				}
				else if (icon.type == IconType.hills)
				{
					hills[row][column]++;
				}
			}
		}

		char[][] symbols = new char[height][width];
		boolean[][] openWater = new boolean[height][width];
		for (int row = 0; row < height; row++)
		{
			for (int column = 0; column < width; column++)
			{
				int waterCount = water[row][column];
				int lakeCount = lakes[row][column];
				int landCount = land[row][column];
				openWater[row][column] = waterCount > 0 && lakeCount == 0;
				if (lakeCount > Math.max(waterCount, landCount))
				{
					symbols[row][column] = 'L';
				}
				else if (coast[row][column] > 0 && waterCount > 0 && landCount > 0)
				{
					symbols[row][column] = 'C';
				}
				else if (waterCount > landCount)
				{
					symbols[row][column] = '~';
				}
				else if (mountains[row][column] > 0)
				{
					symbols[row][column] = 'M';
				}
				else if (forests[row][column] > 0)
				{
					symbols[row][column] = 'F';
				}
				else if (hills[row][column] > 0)
				{
					symbols[row][column] = 'H';
				}
				else
				{
					symbols[row][column] = '.';
				}
			}
		}
		markInlandWater(symbols, openWater, width, height);

		JSONArray terrainRows = new JSONArray();
		for (int row = 0; row < height; row++)
		{
			terrainRows.add(new String(symbols[row]));
		}

		JSONObject locations = new JSONObject();
		for (Map.Entry<String, LocationAnchor> entry : locationAnchorsByName.entrySet())
		{
			String name = entry.getKey();
			LocationAnchor anchor = entry.getValue();
			if (name == null || name.isBlank() || anchor == null)
			{
				continue;
			}
			Center center = anchor.customIconCenter != null
					? anchor.customIconCenter
					: anchor.iconCenter != null
							? anchor.iconCenter
							: anchor.labelCenter;
			Point graphPoint = center != null && center.loc != null
					? center.loc
					: anchor.labelPoint == null
							? null
							: toGraphPoint(anchor.labelPoint, settings);
			if (graphPoint == null)
			{
				continue;
			}
			int column = semanticColumn(graphPoint.x, graphWidth, width);
			int row = semanticRow(graphPoint.y, graphHeight, height);
			JSONObject location = new JSONObject();
			location.put("cell", semanticCell(column, row));
			location.put("normalized_x", graphPoint.x / Math.max(1.0, graphWidth));
			location.put("normalized_y", graphPoint.y / Math.max(1.0, graphHeight));
			location.put("terrain", String.valueOf(symbols[row][column]));
			location.put(
					"anchor_kind",
					anchor.customIconCenter != null
							? "custom_icon"
							: anchor.iconCenter != null
									? "icon"
									: anchor.labelCenter != null ? "label_center" : "label_point");
			locations.put(name, location);
		}

		JSONObject manifest = new JSONObject();
		manifest.put("version", 1);
		manifest.put("grid_width", width);
		manifest.put("grid_height", height);
		manifest.put("terrain_rows", terrainRows);
		manifest.put("locations", locations);
		Path parent = manifestPath.getParent();
		if (parent != null)
		{
			Files.createDirectories(parent);
		}
		Files.writeString(
				manifestPath,
				manifest.toJSONString(),
				StandardCharsets.UTF_8);
	}

	private static void markInlandWater(char[][] symbols, boolean[][] water, int width, int height)
	{
		boolean[][] exterior = new boolean[height][width];
		ArrayDeque<int[]> queue = new ArrayDeque<>();
		for (int column = 0; column < width; column++)
		{
			enqueueWater(column, 0, water, exterior, queue);
			enqueueWater(column, height - 1, water, exterior, queue);
		}
		for (int row = 0; row < height; row++)
		{
			enqueueWater(0, row, water, exterior, queue);
			enqueueWater(width - 1, row, water, exterior, queue);
		}
		int[][] directions = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
		while (!queue.isEmpty())
		{
			int[] point = queue.removeFirst();
			for (int[] direction : directions)
			{
				int column = point[0] + direction[0];
				int row = point[1] + direction[1];
				if (column < 0 || column >= width || row < 0 || row >= height)
				{
					continue;
				}
				enqueueWater(column, row, water, exterior, queue);
			}
		}
		for (int row = 0; row < height; row++)
		{
			for (int column = 0; column < width; column++)
			{
				if (water[row][column] && !exterior[row][column] && symbols[row][column] == '~')
				{
					symbols[row][column] = 'I';
				}
			}
		}
	}

	private static void enqueueWater(
			int column,
			int row,
			boolean[][] water,
			boolean[][] exterior,
			ArrayDeque<int[]> queue)
	{
		if (water[row][column] && !exterior[row][column])
		{
			exterior[row][column] = true;
			queue.addLast(new int[] {column, row});
		}
	}

	private static int semanticColumn(double x, double graphWidth, int width)
	{
		return Math.max(
				0,
				Math.min(width - 1, (int) Math.round(x / Math.max(1.0, graphWidth) * (width - 1))));
	}

	private static int semanticRow(double y, double graphHeight, int height)
	{
		return Math.max(
				0,
				Math.min(height - 1, (int) Math.round(y / Math.max(1.0, graphHeight) * (height - 1))));
	}

	private static String semanticCell(int column, int row)
	{
		return String.valueOf((char) ('A' + column)) + String.format("%02d", row + 1);
	}

	private long chooseTerrainSeed(JSONObject brief, MapSettings settings, int attempts)
	{
		int maxAttempts = Math.max(1, attempts);
		long originalSeed = settings.randomSeed;
		long bestSeed = originalSeed;
		TerrainCoverageReport bestReport = null;

		for (int attempt = 0; attempt < maxAttempts; attempt++)
		{
			long candidateSeed = attempt == 0 ? originalSeed : nextTerrainSeed(originalSeed, attempt);
			settings.randomSeed = candidateSeed;
			settings.edits = new MapEdits();

			WorldGraph graph = MapCreator.createGraph(settings, true);
			bakeGeneratedTerrainIcons(settings, graph, candidateSeed);
			TerrainCoverageReport report = analyzeTerrainCoverage(brief, settings, graph);
			if (bestReport == null || report.score > bestReport.score)
			{
				bestReport = report;
				bestSeed = candidateSeed;
			}
		}

		if (bestReport != null)
		{
			System.err.println("Selected Nortantis terrain seed " + bestSeed + " after " + maxAttempts + " attempt(s): " + bestReport.summary());
		}
		settings.randomSeed = bestSeed;
		settings.edits = new MapEdits();
		return bestSeed;
	}

	private static long nextTerrainSeed(long baseSeed, int attempt)
	{
		long mixed = baseSeed + 0x9E3779B97F4A7C15L * attempt;
		mixed ^= (mixed >>> 30);
		mixed *= 0xBF58476D1CE4E5B9L;
		mixed ^= (mixed >>> 27);
		mixed *= 0x94D049BB133111EBL;
		mixed ^= (mixed >>> 31);
		return Math.floorMod(mixed, 2_147_483_647L);
	}

	private static void bakeGeneratedTerrainIcons(MapSettings settings, WorldGraph graph, long seed)
	{
		if (settings == null || graph == null)
		{
			return;
		}
		Random mapRandom = new Random(seed);
		mapRandom.nextLong(); // Consumed by MapCreator.createGraph for the graph random seed.
		IconDrawer iconDrawer = new IconDrawer(graph, new Random(mapRandom.nextLong()), settings);
		iconDrawer.markMountains();
		iconDrawer.markHills();
		iconDrawer.markCities(settings.cityProbability);
		List<Set<Center>> mountainAndHillGroups = iconDrawer.findMountainAndHillGroups();
		iconDrawer.addIcons(mountainAndHillGroups, new LoggerWarningLogger());
	}

	private static void ensureTerrainOnMajorLandmasses(MapSettings settings, WorldGraph graph)
	{
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}

		int totalLand = 0;
		for (Center center : graph.centers)
		{
			if (isLandCenter(center))
			{
				totalLand++;
			}
		}
		if (totalLand == 0)
		{
			return;
		}

		Set<Center> visited = new HashSet<>();
		int majorComponentThreshold = Math.max(80, (int) Math.round(totalLand * 0.06));
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || visited.contains(center))
			{
				continue;
			}
			LandComponent component = collectLandComponent(center, visited);
			if (component.size < majorComponentThreshold)
			{
				continue;
			}

			int requiredTerrain = Math.max(125, (int) Math.ceil(component.size * 0.20));
			int requiredRugged = Math.max(22, (int) Math.ceil(component.size * 0.038));
			enrichTerrainGroup(settings, component.centers, requiredTerrain, requiredRugged, true);
		}

		ensureTerrainOnGeneratedRegions(settings, graph, totalLand);
		ensureTerrainInLandGridCells(settings, graph, totalLand);
		ensureTerrainInLandmassLocalCells(settings, graph, totalLand);
	}

	private static void ensureTerrainOnGeneratedRegions(MapSettings settings, WorldGraph graph, int totalLand)
	{
		if (graph == null || graph.regions == null || graph.regions.isEmpty())
		{
			return;
		}

		int minimumSize = Math.max(45, (int) Math.round(totalLand * 0.025));
		for (Region region : graph.regions.values())
		{
			List<Center> centers = new ArrayList<>();
			for (Center center : region.getCenters())
			{
				if (isLandCenter(center))
				{
					centers.add(center);
				}
			}
			if (centers.size() < minimumSize)
			{
				continue;
			}
			int requiredTerrain = Math.max(60, (int) Math.ceil(centers.size() * 0.16));
			int requiredRugged = Math.max(12, (int) Math.ceil(centers.size() * 0.030));
			enrichTerrainGroup(settings, centers, requiredTerrain, requiredRugged, true);
		}
	}

	private static void ensureTerrainInLandGridCells(MapSettings settings, WorldGraph graph, int totalLand)
	{
		if (settings == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}

		final int gridSize = 8;
		List<List<Center>> cells = new ArrayList<>();
		for (int i = 0; i < gridSize * gridSize; i++)
		{
			cells.add(new ArrayList<>());
		}

		double mapWidth = graphWidth(graph, settings);
		double mapHeight = graphHeight(graph, settings);
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || center.loc == null)
			{
				continue;
			}
			int x = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(center.loc.x / mapWidth * gridSize)));
			int y = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(center.loc.y / mapHeight * gridSize)));
			cells.get(y * gridSize + x).add(center);
		}

		int minimumSize = Math.max(24, (int) Math.round(totalLand * 0.012));
		for (List<Center> centers : cells)
		{
			if (centers.size() < minimumSize)
			{
				continue;
			}
			int requiredTerrain = Math.max(24, (int) Math.ceil(centers.size() * 0.17));
			int requiredRugged = Math.max(6, (int) Math.ceil(centers.size() * 0.030));
			enrichTerrainGroup(settings, centers, requiredTerrain, requiredRugged, true);
		}
	}

	private static void ensureTerrainInLandmassLocalCells(MapSettings settings, WorldGraph graph, int totalLand)
	{
		if (settings == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}

		Set<Center> visited = new HashSet<>();
		int majorComponentThreshold = Math.max(80, (int) Math.round(totalLand * 0.06));
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || visited.contains(center))
			{
				continue;
			}
			LandComponent component = collectLandComponent(center, visited);
			if (component.size < majorComponentThreshold)
			{
				continue;
			}
			ensureTerrainInLocalCells(settings, component.centers, component.size >= 900 ? 4 : 3);
		}
	}

	private static void ensureTerrainInLocalCells(MapSettings settings, List<Center> componentCenters, int gridSize)
	{
		if (settings == null || componentCenters == null || componentCenters.isEmpty())
		{
			return;
		}

		double minX = Double.POSITIVE_INFINITY;
		double minY = Double.POSITIVE_INFINITY;
		double maxX = Double.NEGATIVE_INFINITY;
		double maxY = Double.NEGATIVE_INFINITY;
		for (Center center : componentCenters)
		{
			if (!isLandCenter(center) || center.loc == null)
			{
				continue;
			}
			minX = Math.min(minX, center.loc.x);
			minY = Math.min(minY, center.loc.y);
			maxX = Math.max(maxX, center.loc.x);
			maxY = Math.max(maxY, center.loc.y);
		}
		if (!Double.isFinite(minX) || maxX <= minX || maxY <= minY)
		{
			return;
		}

		List<List<Center>> cells = new ArrayList<>();
		for (int i = 0; i < gridSize * gridSize; i++)
		{
			cells.add(new ArrayList<>());
		}
		for (Center center : componentCenters)
		{
			if (!isLandCenter(center) || center.loc == null)
			{
				continue;
			}
			int x = Math.max(0, Math.min(gridSize - 1, (int) Math.floor((center.loc.x - minX) / Math.max(1.0, maxX - minX) * gridSize)));
			int y = Math.max(0, Math.min(gridSize - 1, (int) Math.floor((center.loc.y - minY) / Math.max(1.0, maxY - minY) * gridSize)));
			cells.get(y * gridSize + x).add(center);
		}

		int minimumSize = Math.max(16, (int) Math.round(componentCenters.size() * 0.025));
		for (List<Center> centers : cells)
		{
			if (centers.size() < minimumSize)
			{
				continue;
			}
			TerrainIconCounts counts = countTerrainIcons(centers, settings);
			int requiredTerrain = Math.max(28, (int) Math.ceil(centers.size() * 0.18));
			if (counts.terrain >= requiredTerrain)
			{
				continue;
			}
			int requiredRugged = Math.max(5, (int) Math.ceil(centers.size() * 0.024));
			enrichTerrainGroup(settings, centers, requiredTerrain, requiredRugged, true);
		}
	}

	private static void enrichTerrainGroup(MapSettings settings, List<Center> centers, int requiredTerrain, int requiredRugged, boolean allowSecondTreePatch)
	{
		TerrainIconCounts counts = countTerrainIcons(centers, settings);
		List<Center> anchorsUsed = new ArrayList<>();
		if (counts.rugged < requiredRugged)
		{
			Center ruggedAnchor = bestRuggedAnchor(centers, anchorsUsed);
			if (ruggedAnchor != null)
			{
				IconType type = ruggedAnchor.elevation > 0.56 ? IconType.mountains : IconType.hills;
				String groupId = type == IconType.mountains ? "sharp" : "round";
				int limit = Math.min(18, Math.max(6, requiredRugged - counts.rugged));
				int added = addTerrainIconPatch(settings, ruggedAnchor, type, groupId, 2, limit, false);
				if (added > 0)
				{
					anchorsUsed.add(ruggedAnchor);
					counts = new TerrainIconCounts(counts.terrain + added, counts.rugged + added);
				}
			}
		}

		int treePatchLimit = allowSecondTreePatch ? 2 : 1;
		for (int patch = 0; patch < treePatchLimit && counts.terrain < requiredTerrain; patch++)
		{
			Center treeAnchor = bestTreeAnchor(centers, anchorsUsed);
			if (treeAnchor != null)
			{
				int radius = patch == 0 ? 4 : 3;
				int limit = Math.min(patch == 0 ? 70 : 45, Math.max(16, requiredTerrain - counts.terrain));
				int added = addTerrainIconPatch(settings, treeAnchor, IconType.trees, "deciduous", radius, limit, true);
				if (added > 0)
				{
					anchorsUsed.add(treeAnchor);
					counts = new TerrainIconCounts(counts.terrain + added, counts.rugged);
				}
				else
				{
					break;
				}
			}
		}
	}

	private static void thinRuggedTerrainIcons(MapSettings settings, WorldGraph graph)
	{
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}

		final int gridSize = 8;
		double mapWidth = graphWidth(graph, settings);
		double mapHeight = graphHeight(graph, settings);
		double iconWidth = iconSpaceWidth(graph, settings);
		double iconHeight = iconSpaceHeight(graph, settings);
		int[] landByCell = new int[gridSize * gridSize];
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || center.loc == null)
			{
				continue;
			}
			landByCell[cellIndex(center.loc, mapWidth, mapHeight, gridSize)]++;
		}

		Map<Integer, Center> centersByIndex = centersByIndex(graph);
		List<List<FreeIcon>> mountainsByCell = new ArrayList<>();
		List<List<FreeIcon>> hillsByCell = new ArrayList<>();
		for (int i = 0; i < gridSize * gridSize; i++)
		{
			mountainsByCell.add(new ArrayList<>());
			hillsByCell.add(new ArrayList<>());
		}

		for (FreeIcon icon : settings.edits.freeIcons)
		{
			if (icon == null || icon.centerIndex == null || icon.locationResolutionInvariant == null)
			{
				continue;
			}
			if (icon.type == IconType.mountains)
			{
				mountainsByCell.get(cellIndex(icon.locationResolutionInvariant, iconWidth, iconHeight, gridSize)).add(icon);
			}
			else if (icon.type == IconType.hills)
			{
				hillsByCell.get(cellIndex(icon.locationResolutionInvariant, iconWidth, iconHeight, gridSize)).add(icon);
			}
		}

		List<FreeIcon> toRemove = new ArrayList<>();
		for (int cell = 0; cell < gridSize * gridSize; cell++)
		{
			int land = landByCell[cell];
			if (land <= 0)
			{
				continue;
			}
			thinRuggedCell(mountainsByCell.get(cell), Math.max(7, (int) Math.ceil(land * 0.105)), centersByIndex, true, toRemove);
			thinRuggedCell(hillsByCell.get(cell), Math.max(8, (int) Math.ceil(land * 0.115)), centersByIndex, false, toRemove);
		}
		settings.edits.freeIcons.removeAll(toRemove);
	}

	private static void thinRuggedCell(List<FreeIcon> icons, int keepLimit, Map<Integer, Center> centersByIndex, boolean mountains, List<FreeIcon> toRemove)
	{
		if (icons == null || icons.size() <= keepLimit)
		{
			return;
		}
		icons.sort((left, right) -> Double.compare(
				ruggedKeepScore(right, centersByIndex, mountains),
				ruggedKeepScore(left, centersByIndex, mountains)
		));
		for (int i = keepLimit; i < icons.size(); i++)
		{
			toRemove.add(icons.get(i));
		}
	}

	private static double ruggedKeepScore(FreeIcon icon, Map<Integer, Center> centersByIndex, boolean mountains)
	{
		Center center = icon == null || icon.centerIndex == null ? null : centersByIndex.get(icon.centerIndex);
		double elevationScore = 0.0;
		if (center != null)
		{
			elevationScore = mountains ? center.elevation * 1000.0 : (1.0 - Math.abs(center.elevation - 0.555)) * 700.0;
			if (center.isRiver())
			{
				elevationScore -= 40.0;
			}
		}
		return elevationScore + deterministicNoise(icon == null || icon.centerIndex == null ? 0 : icon.centerIndex, mountains ? 17 : 29);
	}

	private static double deterministicNoise(int value, int salt)
	{
		int mixed = value * 1103515245 + salt * 12345;
		mixed ^= (mixed >>> 16);
		return Math.floorMod(mixed, 10_000) / 10_000.0;
	}

	private static int cellIndex(Point point, double width, double height, int gridSize)
	{
		int x = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(point.x / Math.max(1.0, width) * gridSize)));
		int y = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(point.y / Math.max(1.0, height) * gridSize)));
		return y * gridSize + x;
	}

	private static TerrainIconCounts countTerrainIcons(List<Center> centers, MapSettings settings)
	{
		if (centers == null || settings == null || settings.edits == null || settings.edits.freeIcons == null)
		{
			return new TerrainIconCounts(0, 0);
		}

		Set<Integer> centerIndexes = new HashSet<>();
		for (Center center : centers)
		{
			if (isLandCenter(center))
			{
				centerIndexes.add(center.index);
			}
		}

		int terrain = 0;
		int rugged = 0;
		for (FreeIcon icon : settings.edits.freeIcons)
		{
			if (icon == null || icon.centerIndex == null || !centerIndexes.contains(icon.centerIndex) || !isTerrainIcon(icon.type))
			{
				continue;
			}
			terrain++;
			if (icon.type == IconType.mountains || icon.type == IconType.hills)
			{
				rugged++;
			}
		}
		return new TerrainIconCounts(terrain, rugged);
	}

	private static Center bestRuggedAnchor(List<Center> centers, List<Center> avoidCenters)
	{
		Center best = bestRuggedAnchor(centers, avoidCenters, false);
		return best != null ? best : bestRuggedAnchor(centers, avoidCenters, true);
	}

	private static Center bestRuggedAnchor(List<Center> centers, List<Center> avoidCenters, boolean allowCoast)
	{
		Center best = null;
		double bestScore = Double.NEGATIVE_INFINITY;
		for (Center center : centers)
		{
			if (center == null || center.loc == null || center.isWater || center.isLake || (!allowCoast && center.isCoast) || center.isBorder)
			{
				continue;
			}
			double score = center.elevation * 1000.0 + distanceFromAvoidedCentersScore(center, avoidCenters) * 0.0002;
			if (center.isMountain)
			{
				score += 120.0;
			}
			else if (center.isHill)
			{
				score += 80.0;
			}
			if (score > bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static Center bestTreeAnchor(List<Center> centers, List<Center> avoidCenters)
	{
		Center best = bestTreeAnchor(centers, avoidCenters, false);
		return best != null ? best : bestTreeAnchor(centers, avoidCenters, true);
	}

	private static Center bestTreeAnchor(List<Center> centers, List<Center> avoidCenters, boolean allowCoast)
	{
		Center best = null;
		double bestScore = Double.NEGATIVE_INFINITY;
		for (Center center : centers)
		{
			if (center == null || center.loc == null || !isPatchCenter(center, true, allowCoast))
			{
				continue;
			}
			double score = distanceFromAvoidedCentersScore(center, avoidCenters) * 0.0003;
			if (drawsTreeIcons(center))
			{
				score += 1000.0;
			}
			if (center.elevation > 0.30 && center.elevation < 0.53)
			{
				score += 120.0;
			}
			if (center.isRiver())
			{
				score += 40.0;
			}
			if (score > bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static double distanceFromAvoidedCentersScore(Center center, List<Center> avoidCenters)
	{
		if (center == null || center.loc == null || avoidCenters == null || avoidCenters.isEmpty())
		{
			return 1_000_000.0;
		}
		double best = Double.POSITIVE_INFINITY;
		for (Center avoided : avoidCenters)
		{
			if (avoided != null && avoided.loc != null)
			{
				double distance = center.loc.distanceTo(avoided.loc);
				best = Math.min(best, distance * distance);
			}
		}
		return Double.isFinite(best) ? best : 1_000_000.0;
	}

	private static int addTerrainIconPatch(MapSettings settings, Center anchor, IconType type, String groupId, int radius, int limit, boolean avoidMountains)
	{
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null || anchor == null)
		{
			return 0;
		}

		List<Center> centers = nearbyPatchCenters(anchor, radius, limit, avoidMountains);
		int minimumUsefulPatch = Math.min(limit, 4);
		if (centers.size() < minimumUsefulPatch)
		{
			List<Center> relaxed = nearbyPatchCenters(anchor, radius + 1, limit, avoidMountains, true);
			LinkedHashMap<Integer, Center> merged = new LinkedHashMap<>();
			for (Center center : centers)
			{
				merged.put(center.index, center);
			}
			for (Center center : relaxed)
			{
				merged.putIfAbsent(center.index, center);
				if (merged.size() >= limit)
				{
					break;
				}
			}
			centers = new ArrayList<>(merged.values());
		}
		int added = 0;
		for (Center center : centers)
		{
			if (center == null)
			{
				continue;
			}
			if (type == IconType.trees)
			{
				if (settings.edits.freeIcons.hasTrees(center.index))
				{
					continue;
				}
			}
			else
			{
				FreeIcon existing = settings.edits.freeIcons.getNonTree(center.index);
				if (existing != null && isTerrainIcon(existing.type))
				{
					continue;
				}
			}
			settings.edits.freeIcons.addOrReplace(new FreeIcon(
					settings.resolution,
					center.loc,
					semanticIconScale(type),
					type,
					Assets.installedArtPack,
					groupId,
					semanticIconIndex(center, type),
					center.index,
					type == IconType.trees ? 0.85 : 0.0,
					settings.getIconFillColorForType(type),
					settings.getIconFilterColorForType(type),
					settings.getMaximizeOpacityForType(type),
					settings.getFillWithColorForType(type)
			));
			added++;
		}
		return added;
	}

	private static TerrainCoverageReport analyzeTerrainCoverage(JSONObject brief, MapSettings settings, WorldGraph graph)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return TerrainCoverageReport.empty();
		}

		int totalLand = 0;
		for (Center center : graph.centers)
		{
			if (isLandCenter(center))
			{
				totalLand++;
			}
		}
		if (totalLand == 0)
		{
			return TerrainCoverageReport.empty();
		}

		CoverageStats landmassCoverage = analyzeLandmassCoverage(graph, totalLand);
		CoverageStats generatedRegionCoverage = analyzeGeneratedRegionCoverage(graph, totalLand);
		CoverageStats politicalRegionCoverage = analyzePoliticalRegionCoverage(brief, settings, graph, totalLand);
		CoverageStats terrainSpreadCoverage = analyzeTerrainSpread(graph, settings, totalLand);
		CoverageStats actualTerrainIconSpreadCoverage = analyzeActualTerrainIconSpread(graph, settings, totalLand);
		CoverageStats actualLandmassTerrainCoverage = analyzeActualTerrainByLandmass(graph, settings, totalLand);
		CoverageStats actualGeneratedRegionTerrainCoverage = analyzeActualTerrainByGeneratedRegion(graph, settings, totalLand);
		CoverageStats actualPoliticalRegionTerrainCoverage = analyzeActualTerrainByPoliticalRegion(brief, settings, graph, totalLand);
		CoverageStats semanticAnchorCoverage = analyzeSemanticAnchorCoverage(brief, settings, graph);
		boolean acceptable = landmassCoverage.total > 0
				&& landmassCoverage.acceptable()
				&& generatedRegionCoverage.acceptable()
				&& politicalRegionCoverage.acceptable()
				&& terrainSpreadCoverage.acceptable()
				&& actualTerrainIconSpreadCoverage.acceptable()
				&& actualLandmassTerrainCoverage.acceptable()
				&& actualGeneratedRegionTerrainCoverage.acceptable()
				&& actualPoliticalRegionTerrainCoverage.acceptable()
				&& semanticAnchorCoverage.acceptable();
		double score = landmassCoverage.score
				+ generatedRegionCoverage.score
				+ politicalRegionCoverage.score
				+ terrainSpreadCoverage.score * 3.0
				+ actualTerrainIconSpreadCoverage.score * 6.0
				+ actualLandmassTerrainCoverage.score * 12.0
				+ actualGeneratedRegionTerrainCoverage.score * 5.0
				+ actualPoliticalRegionTerrainCoverage.score * 5.0
				+ semanticAnchorCoverage.score * 8.0;
		return new TerrainCoverageReport(acceptable, score, totalLand, landmassCoverage, generatedRegionCoverage, politicalRegionCoverage, terrainSpreadCoverage, actualTerrainIconSpreadCoverage,
				actualLandmassTerrainCoverage, actualGeneratedRegionTerrainCoverage, actualPoliticalRegionTerrainCoverage, semanticAnchorCoverage);
	}

	private static CoverageStats analyzeSemanticAnchorCoverage(JSONObject brief, MapSettings settings, WorldGraph graph)
	{
		JSONArray labels = arrayValue(brief, "labels");
		if (labels == null || labels.isEmpty() || settings == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return CoverageStats.empty();
		}

		int total = 0;
		int covered = 0;
		double score = 0.0;
		double distanceLimit = Math.max(160.0, Math.min(graphWidth(graph, settings), graphHeight(graph, settings)) * 0.18);
		for (Object obj : labels)
		{
			if (!(obj instanceof JSONObject label))
			{
				continue;
			}
			String type = stringValue(label, "type", "");
			String featureType = stringValue(label, "featureType", "");
			String preference = locationPreference(label);
			if ("Title".equals(type) || (!semanticAnchorMatters(label, featureType, preference)))
			{
				continue;
			}

			total++;
			Point desired = toGraphPoint(pointValue(label, settings), settings);
			String searchPreference = semanticSearchPreference(featureType, preference);
			Center best = "inland_sea".equals(searchPreference)
					? findExistingInlandSeaPassageCenter(label, desired, graph, settings)
					: findBestCenterWithPreference(desired, graph, searchPreference, false, null);
			if (best == null || best.loc == null)
			{
				score -= 650.0;
				continue;
			}
			double distance = best.loc.distanceTo(desired);
			double proximity = Math.max(0.0, 1.0 - Math.min(1.0, distance / distanceLimit));
			double terrainFit = semanticAnchorTerrainFit(best, label, featureType, preference);
			double directionFit = semanticAnchorDirectionFit(best, label, settings, graph);
			double contribution = terrainFit * 900.0 + proximity * 700.0 + directionFit * 360.0 - Math.min(650.0, distance / 3.0);
			if (terrainFit >= 0.72 && proximity >= 0.35)
			{
				covered++;
				contribution += 420.0;
			}
			score += contribution;
		}
		return new CoverageStats(total, covered, score);
	}

	private static boolean semanticAnchorMatters(JSONObject label, String featureType, String preference)
	{
		if (boolValue(label, "drawIcon", false) || !stringValue(label, "iconName", "").isBlank())
		{
			return true;
		}
		return switch (featureType)
		{
			case "archipelago", "ocean", "inland_sea", "lake", "forest", "mountain_range", "coast", "settlement", "fortress" -> true;
			default -> !"land".equals(preference);
		};
	}

	private static String semanticSearchPreference(String featureType, String preference)
	{
		if ("archipelago".equals(featureType) || "archipelago".equals(preference))
		{
			return "ocean";
		}
		if ("inland_sea".equals(featureType) || "inland_sea".equals(preference))
		{
			return "inland_sea";
		}
		if ("lake".equals(featureType))
		{
			return "lake";
		}
		if ("mountain_range".equals(featureType))
		{
			return "mountain";
		}
		if ("settlement".equals(featureType) || "fortress".equals(featureType))
		{
			return "land";
		}
		return normalizedSearchPreference(preference);
	}

	private static String normalizedSearchPreference(String preference)
	{
		return "archipelago".equals(preference) ? "ocean" : preference;
	}

	private static double semanticAnchorTerrainFit(Center center, JSONObject label, String featureType, String preference)
	{
		String searchPreference = semanticSearchPreference(featureType, preference);
		if ("ocean".equals(searchPreference))
		{
			if (center.isWater && !center.isLake && !center.isCoast)
			{
				return 1.0;
			}
			return center.isWater && !center.isLake ? 0.72 : center.isCoast ? 0.48 : 0.05;
		}
		if ("inland_sea".equals(searchPreference))
		{
			if (center.isWater && !center.isLake)
			{
				return hasLandWithinHops(center, 4) ? 1.0 : 0.74;
			}
			return center.isCoast ? 0.62 : 0.34;
		}
		if ("lake".equals(searchPreference))
		{
			if (center.isLake)
			{
				return 1.0;
			}
			if (isLakeShore(center))
			{
				return 0.82;
			}
			return center.isWater ? 0.35 : 0.45;
		}
		if ("forest".equals(searchPreference))
		{
			return isForest(center) ? 1.0 : center.isWater ? 0.0 : 0.38;
		}
		if ("mountain".equals(searchPreference))
		{
			return center.isMountain ? 1.0 : center.isHill ? 0.82 : center.isWater ? 0.0 : 0.38;
		}
		if ("coast".equals(searchPreference))
		{
			return center.isCoast ? 1.0 : (hasWaterWithinHops(center, 2) ? 0.72 : center.isWater ? 0.18 : 0.42);
		}
		if ("hill".equals(searchPreference))
		{
			return center.isHill ? 1.0 : center.isMountain ? 0.72 : center.isWater ? 0.0 : 0.48;
		}
		if (isSettlementLikeCustomIcon(label) || "settlement".equals(featureType) || "fortress".equals(featureType))
		{
			if (center.isWater || center.isLake)
			{
				return 0.0;
			}
			double fit = 0.82;
			if (center.isCoast)
			{
				fit -= 0.12;
			}
			if (center.isMountain || center.isHill)
			{
				fit -= 0.18;
			}
			if (isForest(center))
			{
				fit -= 0.16;
			}
			return Math.max(0.28, fit);
		}
		return center.isWater ? 0.0 : 0.72;
	}

	private static double semanticAnchorDirectionFit(Center center, JSONObject label, MapSettings settings, WorldGraph graph)
	{
		String hint = stringValue(label, "positionHint", "").strip().toLowerCase();
		if (hint.isBlank() || "center".equals(hint))
		{
			return hint.isBlank() ? 0.55 : centerOfMapFit(center, settings, graph);
		}
		double x = center.loc.x / Math.max(1.0, graphWidth(graph, settings));
		double y = center.loc.y / Math.max(1.0, graphHeight(graph, settings));
		return switch (hint)
		{
			case "north" -> clamp01((0.62 - y) / 0.50);
			case "south" -> clamp01((y - 0.38) / 0.50);
			case "west" -> clamp01((0.62 - x) / 0.50);
			case "east" -> clamp01((x - 0.38) / 0.50);
			case "northwest" -> (clamp01((0.62 - x) / 0.50) + clamp01((0.62 - y) / 0.50)) / 2.0;
			case "northeast" -> (clamp01((x - 0.38) / 0.50) + clamp01((0.62 - y) / 0.50)) / 2.0;
			case "southwest" -> (clamp01((0.62 - x) / 0.50) + clamp01((y - 0.38) / 0.50)) / 2.0;
			case "southeast" -> (clamp01((x - 0.38) / 0.50) + clamp01((y - 0.38) / 0.50)) / 2.0;
			default -> 0.55;
		};
	}

	private static double centerOfMapFit(Center center, MapSettings settings, WorldGraph graph)
	{
		double x = center.loc.x / Math.max(1.0, graphWidth(graph, settings));
		double y = center.loc.y / Math.max(1.0, graphHeight(graph, settings));
		double distance = Math.hypot(x - 0.5, y - 0.5);
		return clamp01(1.0 - distance / 0.42);
	}

	private static double clamp01(double value)
	{
		return Math.max(0.0, Math.min(1.0, value));
	}

	private static CoverageStats analyzeLandmassCoverage(WorldGraph graph, int totalLand)
	{
		Set<Center> visited = new HashSet<>();
		int majorComponentThreshold = Math.max(35, (int) Math.round(totalLand * 0.08));
		List<CenterGroup> groups = new ArrayList<>();

		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || visited.contains(center))
			{
				continue;
			}
			LandComponent component = collectLandComponent(center, visited);
			groups.add(new CenterGroup("landmass", component.centers));
		}

		return analyzeTerrainGroups(groups, majorComponentThreshold, 0.03, 3);
	}

	private static CoverageStats analyzeGeneratedRegionCoverage(WorldGraph graph, int totalLand)
	{
		if (graph.regions == null || graph.regions.isEmpty())
		{
			return CoverageStats.empty();
		}

		List<CenterGroup> groups = new ArrayList<>();
		for (Region region : graph.regions.values())
		{
			List<Center> centers = new ArrayList<>();
			for (Center center : region.getCenters())
			{
				if (isLandCenter(center))
				{
					centers.add(center);
				}
			}
			groups.add(new CenterGroup("region-" + region.id, centers));
		}

		int minimumRegionSize = Math.max(45, (int) Math.round(totalLand * 0.035));
		return analyzeTerrainGroups(groups, minimumRegionSize, 0.025, 2);
	}

	private static CoverageStats analyzePoliticalRegionCoverage(JSONObject brief, MapSettings settings, WorldGraph graph, int totalLand)
	{
		List<PoliticalRegionAnchor> anchors = politicalRegionAnchorsForBrief(brief, settings, graph);
		if (anchors.isEmpty())
		{
			return CoverageStats.empty();
		}

		Map<Integer, List<Center>> centersByRegion = new HashMap<>();
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center))
			{
				continue;
			}
			PoliticalRegionAnchor closest = closestAnchor(center.loc, anchors);
			if (closest == null)
			{
				continue;
			}
			centersByRegion.computeIfAbsent(closest.regionId, ignored -> new ArrayList<>()).add(center);
		}

		List<CenterGroup> groups = new ArrayList<>();
		for (Map.Entry<Integer, List<Center>> entry : centersByRegion.entrySet())
		{
			groups.add(new CenterGroup("political-" + entry.getKey(), entry.getValue()));
		}

		int minimumRegionSize = Math.max(35, (int) Math.round(totalLand * 0.025));
		return analyzeTerrainGroups(groups, minimumRegionSize, 0.025, 2);
	}

	private static CoverageStats analyzeTerrainSpread(WorldGraph graph, MapSettings settings, int totalLand)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty() || settings == null)
		{
			return CoverageStats.empty();
		}

		final int gridSize = 8;
		int[][] landByCell = new int[gridSize][gridSize];
		int[][] featuresByCell = new int[gridSize][gridSize];
		double mapWidth = graphWidth(graph, settings);
		double mapHeight = graphHeight(graph, settings);

		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || center.loc == null)
			{
				continue;
			}
			int x = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(center.loc.x / mapWidth * gridSize)));
			int y = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(center.loc.y / mapHeight * gridSize)));
			landByCell[x][y]++;
			if (isVisualTerrainCenter(center))
			{
				featuresByCell[x][y]++;
			}
		}

		int total = 0;
		int covered = 0;
		double score = 0.0;
		int minimumLandPerCell = Math.max(20, (int) Math.round(totalLand * 0.015));
		for (int x = 0; x < gridSize; x++)
		{
			for (int y = 0; y < gridSize; y++)
			{
				int land = landByCell[x][y];
				if (land < minimumLandPerCell)
				{
					continue;
				}
				total++;
				int requiredFeatures = Math.max(3, (int) Math.ceil(land * 0.025));
				double coverage = Math.min(1.0, featuresByCell[x][y] / (double) requiredFeatures);
				score += coverage;
				if (featuresByCell[x][y] >= requiredFeatures)
				{
					covered++;
				}
			}
		}
		if (total > 0)
		{
			score += covered / (double) total;
		}
		return new CoverageStats(total, covered, score);
	}

	private static CoverageStats analyzeActualTerrainIconSpread(WorldGraph graph, MapSettings settings, int totalLand)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty() || settings == null || settings.edits == null || settings.edits.freeIcons == null)
		{
			return CoverageStats.empty();
		}

		final int gridSize = 8;
		int[][] landByCell = new int[gridSize][gridSize];
		int[][] iconsByCell = new int[gridSize][gridSize];
		double graphWidth = graphWidth(graph, settings);
		double graphHeight = graphHeight(graph, settings);
		double iconWidth = iconSpaceWidth(graph, settings);
		double iconHeight = iconSpaceHeight(graph, settings);

		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || center.loc == null)
			{
				continue;
			}
			int x = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(center.loc.x / graphWidth * gridSize)));
			int y = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(center.loc.y / graphHeight * gridSize)));
			landByCell[x][y]++;
		}

		int ruggedIcons = 0;
		int terrainIcons = 0;
		for (FreeIcon icon : settings.edits.freeIcons)
		{
			if (icon == null || !isTerrainIcon(icon.type) || icon.locationResolutionInvariant == null)
			{
				continue;
			}
			terrainIcons++;
			if (icon.type == IconType.mountains || icon.type == IconType.hills)
			{
				ruggedIcons++;
			}
			int x = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(icon.locationResolutionInvariant.x / iconWidth * gridSize)));
			int y = Math.max(0, Math.min(gridSize - 1, (int) Math.floor(icon.locationResolutionInvariant.y / iconHeight * gridSize)));
			iconsByCell[x][y]++;
		}

		int total = 0;
		int covered = 0;
		double score = 0.0;
		int minimumLandPerCell = Math.max(20, (int) Math.round(totalLand * 0.015));
		for (int x = 0; x < gridSize; x++)
		{
			for (int y = 0; y < gridSize; y++)
			{
				int land = landByCell[x][y];
				if (land < minimumLandPerCell)
				{
					continue;
				}
				total++;
				int requiredIcons = Math.max(12, (int) Math.ceil(land * 0.040));
				double coverage = Math.min(1.0, iconsByCell[x][y] / (double) requiredIcons);
				score += coverage;
				if (iconsByCell[x][y] >= requiredIcons)
				{
					covered++;
				}
			}
		}
		if (total > 0)
		{
			score += covered / (double) total;
		}
		score += Math.min(2.2, terrainIcons / 1600.0);
		score += Math.min(1.4, ruggedIcons / 450.0);
		return new CoverageStats(total, covered, score);
	}

	private static CoverageStats analyzeActualTerrainByLandmass(WorldGraph graph, MapSettings settings, int totalLand)
	{
		Set<Center> visited = new HashSet<>();
		List<CenterGroup> groups = new ArrayList<>();
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || visited.contains(center))
			{
				continue;
			}
			LandComponent component = collectLandComponent(center, visited);
			groups.add(new CenterGroup("landmass", component.centers));
		}
		int minimumSize = Math.max(35, (int) Math.round(totalLand * 0.08));
		return analyzeActualTerrainIconGroups(groups, settings, minimumSize, 0.06, 45);
	}

	private static CoverageStats analyzeActualTerrainByGeneratedRegion(WorldGraph graph, MapSettings settings, int totalLand)
	{
		if (graph.regions == null || graph.regions.isEmpty())
		{
			return CoverageStats.empty();
		}
		List<CenterGroup> groups = new ArrayList<>();
		for (Region region : graph.regions.values())
		{
			List<Center> centers = new ArrayList<>();
			for (Center center : region.getCenters())
			{
				if (isLandCenter(center))
				{
					centers.add(center);
				}
			}
			groups.add(new CenterGroup("region-" + region.id, centers));
		}
		int minimumSize = Math.max(45, (int) Math.round(totalLand * 0.035));
		return analyzeActualTerrainIconGroups(groups, settings, minimumSize, 0.05, 24);
	}

	private static CoverageStats analyzeActualTerrainByPoliticalRegion(JSONObject brief, MapSettings settings, WorldGraph graph, int totalLand)
	{
		List<PoliticalRegionAnchor> anchors = politicalRegionAnchorsForBrief(brief, settings, graph);
		if (anchors.isEmpty())
		{
			return CoverageStats.empty();
		}

		Map<Integer, List<Center>> centersByRegion = new HashMap<>();
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center))
			{
				continue;
			}
			PoliticalRegionAnchor closest = closestAnchor(center.loc, anchors);
			if (closest == null)
			{
				continue;
			}
			centersByRegion.computeIfAbsent(closest.regionId, ignored -> new ArrayList<>()).add(center);
		}

		List<CenterGroup> groups = new ArrayList<>();
		for (Map.Entry<Integer, List<Center>> entry : centersByRegion.entrySet())
		{
			groups.add(new CenterGroup("political-" + entry.getKey(), entry.getValue()));
		}

		int minimumSize = Math.max(35, (int) Math.round(totalLand * 0.025));
		return analyzeActualTerrainIconGroups(groups, settings, minimumSize, 0.05, 24);
	}

	private static CoverageStats analyzeActualTerrainIconGroups(List<CenterGroup> groups, MapSettings settings, int minimumSize, double iconRatio, int minimumIcons)
	{
		if (groups == null || groups.isEmpty() || settings == null || settings.edits == null || settings.edits.freeIcons == null)
		{
			return CoverageStats.empty();
		}

		Map<Integer, Integer> terrainIconsByCenter = new HashMap<>();
		Map<Integer, Integer> ruggedIconsByCenter = new HashMap<>();
		for (FreeIcon icon : settings.edits.freeIcons)
		{
			if (icon == null || icon.centerIndex == null || !isTerrainIcon(icon.type))
			{
				continue;
			}
			terrainIconsByCenter.merge(icon.centerIndex, 1, Integer::sum);
			if (icon.type == IconType.mountains || icon.type == IconType.hills)
			{
				ruggedIconsByCenter.merge(icon.centerIndex, 1, Integer::sum);
			}
		}

		int qualifyingGroups = 0;
		int coveredGroups = 0;
		double score = 0.0;
		for (CenterGroup group : groups)
		{
			if (group.centers == null)
			{
				continue;
			}
			int size = 0;
			int iconCount = 0;
			int ruggedCount = 0;
			for (Center center : group.centers)
			{
				if (!isLandCenter(center))
				{
					continue;
				}
				size++;
				iconCount += terrainIconsByCenter.getOrDefault(center.index, 0);
				ruggedCount += ruggedIconsByCenter.getOrDefault(center.index, 0);
			}
			if (size < minimumSize)
			{
				continue;
			}

			qualifyingGroups++;
			int requiredIcons = Math.max(minimumIcons, (int) Math.ceil(size * iconRatio));
			double coverage = Math.min(1.0, iconCount / (double) requiredIcons);
			score += coverage;
			score += Math.min(0.35, ruggedCount / (double) Math.max(8, requiredIcons / 3));
			if (iconCount >= requiredIcons)
			{
				coveredGroups++;
			}
		}

		if (qualifyingGroups == 1 && coveredGroups == 1)
		{
			score += 0.5;
		}
		else if (qualifyingGroups > 1)
		{
			score += coveredGroups / (double) qualifyingGroups;
		}
		return new CoverageStats(qualifyingGroups, coveredGroups, score);
	}

	private static CoverageStats analyzeTerrainGroups(List<CenterGroup> groups, int minimumSize, double featureRatio, int minimumFeatures)
	{
		int qualifyingGroups = 0;
		int coveredGroups = 0;
		double score = 0.0;

		for (CenterGroup group : groups)
		{
			if (group.centers == null)
			{
				continue;
			}
			int size = 0;
			int featureCount = 0;
			for (Center center : group.centers)
			{
				if (!isLandCenter(center))
				{
					continue;
				}
				size++;
				if (isVisualTerrainCenter(center))
				{
					featureCount++;
				}
			}
			if (size < minimumSize)
			{
				continue;
			}

			qualifyingGroups++;
			int requiredFeatures = Math.max(minimumFeatures, (int) Math.ceil(size * featureRatio));
			double coverage = Math.min(1.0, featureCount / (double) requiredFeatures);
			score += coverage;
			if (featureCount >= requiredFeatures)
			{
				coveredGroups++;
			}
		}

		if (qualifyingGroups == 1 && coveredGroups == 1)
		{
			score += 0.5;
		}
		else if (qualifyingGroups > 1)
		{
			score += coveredGroups / (double) qualifyingGroups;
		}
		return new CoverageStats(qualifyingGroups, coveredGroups, score);
	}

	private static LandComponent collectLandComponent(Center start, Set<Center> visited)
	{
		List<Center> stack = new ArrayList<>();
		List<Center> centers = new ArrayList<>();
		stack.add(start);
		visited.add(start);
		int size = 0;
		int featureCount = 0;

		while (!stack.isEmpty())
		{
			Center center = stack.remove(stack.size() - 1);
			centers.add(center);
			size++;
			if (isVisualTerrainCenter(center))
			{
				featureCount++;
			}
			for (Center neighbor : center.neighbors)
			{
				if (isLandCenter(neighbor) && !visited.contains(neighbor))
				{
					visited.add(neighbor);
					stack.add(neighbor);
				}
			}
		}
		return new LandComponent(size, featureCount, centers);
	}

	private static Set<Center> largestLandComponent(WorldGraph graph)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return Set.of();
		}

		Set<Center> visited = new HashSet<>();
		List<Center> largest = List.of();
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || visited.contains(center))
			{
				continue;
			}
			LandComponent component = collectLandComponent(center, visited);
			if (component.centers.size() > largest.size())
			{
				largest = component.centers;
			}
		}
		return new HashSet<>(largest);
	}

	private static Set<Center> majorLandComponents(WorldGraph graph)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return Set.of();
		}

		int totalLand = 0;
		for (Center center : graph.centers)
		{
			if (isLandCenter(center))
			{
				totalLand++;
			}
		}
		int threshold = Math.max(60, (int) Math.round(totalLand * 0.02));
		Set<Center> visited = new HashSet<>();
		Set<Center> result = new HashSet<>();
		List<Center> largest = List.of();
		for (Center center : graph.centers)
		{
			if (!isLandCenter(center) || visited.contains(center))
			{
				continue;
			}
			LandComponent component = collectLandComponent(center, visited);
			if (component.centers.size() > largest.size())
			{
				largest = component.centers;
			}
			if (component.centers.size() >= threshold)
			{
				result.addAll(component.centers);
			}
		}
		if (result.isEmpty())
		{
			result.addAll(largest);
		}
		return result;
	}

	private static List<PoliticalRegionAnchor> politicalRegionAnchorsForBrief(JSONObject brief, MapSettings settings, WorldGraph graph)
	{
		JSONArray regions = arrayValue(brief, "politicalRegions");
		if (regions == null || regions.isEmpty() || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return List.of();
		}

		Map<String, JSONObject> labelsByText = labelsByText(brief);
		List<PoliticalRegionAnchor> anchors = new ArrayList<>();
		for (int index = 0; index < regions.size(); index++)
		{
			Object obj = regions.get(index);
			if (!(obj instanceof JSONObject region))
			{
				continue;
			}
			JSONArray anchorJson = arrayValue(region, "anchors");
			if (anchorJson == null || anchorJson.isEmpty())
			{
				continue;
			}
			int regionId = 9000 + index;
			for (Object anchorObj : anchorJson)
			{
				if (!(anchorObj instanceof JSONObject anchor))
				{
					continue;
				}
				String name = stringValue(anchor, "name", "");
				JSONObject hint = name.isBlank() ? anchor : labelsByText.getOrDefault(name, anchor);
				Point point = pointValue(hint, settings);
				Center labelCenter = snapCenterToGraph(hint, point, settings, graph);
				Center iconCenter = iconCenterForLabel(hint, labelCenter, point, settings, graph);
				Center center = iconCenter != null ? iconCenter : labelCenter;
				if (center != null && center.loc != null)
				{
					anchors.add(new PoliticalRegionAnchor(regionId, center.loc));
				}
				else
				{
					anchors.add(new PoliticalRegionAnchor(regionId, toGraphPoint(point, settings)));
				}
			}
		}
		return anchors;
	}

	private static Map<String, JSONObject> labelsByText(JSONObject brief)
	{
		Map<String, JSONObject> labelsByText = new HashMap<>();
		JSONArray labels = arrayValue(brief, "labels");
		if (labels == null)
		{
			return labelsByText;
		}
		for (Object obj : labels)
		{
			if (obj instanceof JSONObject label)
			{
				String text = stringValue(label, "text", "");
				if (!text.isBlank())
				{
					labelsByText.put(text, label);
				}
			}
		}
		return labelsByText;
	}

	private static boolean isLandCenter(Center center)
	{
		return center != null && !center.isWater && !center.isBorder;
	}

	private static boolean isVisualTerrainCenter(Center center)
	{
		if (center == null)
		{
			return false;
		}
		if (center.elevation > 0.53)
		{
			return true;
		}
		if (center.biome == null)
		{
			return false;
		}
		return drawsTerrainIcons(center);
	}

	private static boolean drawsTerrainIcons(Center center)
	{
		if (center == null || center.biome == null)
		{
			return false;
		}
		String biome = center.biome.name();
		return drawsTreeIcons(center)
				|| biome.equals("HIGH_TEMPERATE_DESERT")
				|| biome.equals("TEMPERATE_DESERT");
	}

	private static boolean drawsTreeIcons(Center center)
	{
		if (center == null || center.biome == null)
		{
			return false;
		}
		String biome = center.biome.name();
		return biome.equals("TEMPERATE_RAIN_FOREST")
				|| biome.equals("TAIGA")
				|| biome.equals("SHRUBLAND")
				|| biome.equals("HIGH_TEMPERATE_DECIDUOUS_FOREST");
	}

	private static void clearCachedMapBeforeFinalDraw(MapParts mapParts)
	{
		if (mapParts == null)
		{
			return;
		}
		if (mapParts.mapBeforeAddingText != null)
		{
			mapParts.mapBeforeAddingText.close();
			mapParts.mapBeforeAddingText = null;
		}
		if (mapParts.textBackground != null)
		{
			mapParts.textBackground.close();
			mapParts.textBackground = null;
		}
	}

	private void applySettings(JSONObject brief, MapSettings settings)
	{
		settings.resolution = doubleValue(brief, "resolution", settings.resolution);
		settings.worldSize = intValue(brief, "worldSize", settings.worldSize);
		settings.regionCount = intValue(brief, "regionCount", settings.regionCount);
		settings.generatedWidth = intValue(brief, "generatedWidth", settings.generatedWidth);
		settings.generatedHeight = intValue(brief, "generatedHeight", settings.generatedHeight);
		settings.edgeLandToWaterProbability = doubleValue(brief, "edgeLandToWaterProbability", settings.edgeLandToWaterProbability);
		settings.centerLandToWaterProbability = doubleValue(brief, "centerLandToWaterProbability", settings.centerLandToWaterProbability);
		settings.backgroundRandomSeed = longValue(brief, "backgroundRandomSeed", settings.backgroundRandomSeed);
		settings.frayedBorderSeed = longValue(brief, "frayedBorderSeed", settings.frayedBorderSeed);
		settings.regionsRandomSeed = longValue(brief, "regionsRandomSeed", settings.regionsRandomSeed);
		settings.textRandomSeed = longValue(brief, "textRandomSeed", settings.textRandomSeed);
		settings.cityProbability = doubleValue(brief, "cityProbability", settings.cityProbability);

		String landShape = stringValue(brief, "landShape", null);
		if (landShape != null && !landShape.isBlank())
		{
			settings.landShape = LandShape.valueOf(landShape);
		}
		String lineStyle = stringValue(brief, "lineStyle", null);
		if (lineStyle != null && !lineStyle.isBlank())
		{
			settings.lineStyle = MapSettings.LineStyle.valueOf(lineStyle);
		}

		settings.drawText = boolValue(brief, "drawText", settings.drawText);
		settings.drawRoads = boolValue(brief, "drawRoads", settings.drawRoads);
		settings.drawGridOverlay = boolValue(brief, "drawGridOverlay", settings.drawGridOverlay);
		settings.drawRegionColors = boolValue(brief, "drawRegionColors", settings.drawRegionColors);
		settings.drawRegionBoundaries = boolValue(brief, "drawRegionBoundaries", settings.drawRegionBoundaries);
		settings.drawBorder = boolValue(brief, "drawBorder", settings.drawBorder);
		settings.drawGrunge = boolValue(brief, "drawGrunge", settings.drawGrunge);
		settings.drawBoldBackground = boolValue(brief, "drawBoldBackground", settings.drawBoldBackground);
		settings.generateBackground = boolValue(brief, "generateBackground", settings.generateBackground);
		settings.generateBackgroundFromTexture = boolValue(brief, "generateBackgroundFromTexture", settings.generateBackgroundFromTexture);
		settings.solidColorBackground = boolValue(brief, "solidColorBackground", settings.solidColorBackground);
		settings.frayedBorder = boolValue(brief, "frayedBorder", settings.frayedBorder);
		settings.frayedBorderSize = intValue(brief, "frayedBorderSize", settings.frayedBorderSize);
		settings.frayedBorderBlurLevel = intValue(brief, "frayedBorderBlurLevel", settings.frayedBorderBlurLevel);
		settings.grungeWidth = intValue(brief, "grungeWidth", settings.grungeWidth);
		settings.coastlineWidth = doubleValue(brief, "coastlineWidth", settings.coastlineWidth);
		settings.coastShadingLevel = intValue(brief, "coastShadingLevel", settings.coastShadingLevel);
		settings.oceanShadingLevel = intValue(brief, "oceanShadingLevel", settings.oceanShadingLevel);
		settings.oceanWavesLevel = intValue(brief, "oceanWavesLevel", settings.oceanWavesLevel);
		settings.concentricWaveCount = intValue(brief, "concentricWaveCount", settings.concentricWaveCount);
		settings.fadeConcentricWaves = boolValue(brief, "fadeConcentricWaves", settings.fadeConcentricWaves);
		settings.jitterToConcentricWaves = boolValue(brief, "jitterToConcentricWaves", settings.jitterToConcentricWaves);
		settings.brokenLinesForConcentricWaves = boolValue(brief, "brokenLinesForConcentricWaves", settings.brokenLinesForConcentricWaves);
		settings.drawOceanEffectsInLakes = boolValue(brief, "drawOceanEffectsInLakes", settings.drawOceanEffectsInLakes);
		settings.borderWidth = intValue(brief, "borderWidth", settings.borderWidth);
		settings.mountainScale = doubleValue(brief, "mountainScale", settings.mountainScale);
		settings.hillScale = doubleValue(brief, "hillScale", settings.hillScale);
		settings.duneScale = doubleValue(brief, "duneScale", settings.duneScale);
		settings.treeHeightScale = doubleValue(brief, "treeHeightScale", settings.treeHeightScale);
		settings.cityScale = doubleValue(brief, "cityScale", settings.cityScale);

		String oceanWavesType = stringValue(brief, "oceanWavesType", null);
		if (oceanWavesType != null && !oceanWavesType.isBlank())
		{
			settings.oceanWavesType = MapSettings.OceanWaves.valueOf(oceanWavesType);
		}
		String borderPosition = stringValue(brief, "borderPosition", null);
		if (borderPosition != null && !borderPosition.isBlank())
		{
			settings.borderPosition = BorderPosition.valueOf(borderPosition);
		}
		String borderColorOption = stringValue(brief, "borderColorOption", null);
		if (borderColorOption != null && !borderColorOption.isBlank())
		{
			settings.borderColorOption = BorderColorOption.valueOf(borderColorOption);
		}
		String backgroundTexture = stringValue(brief, "backgroundTexture", null);
		if (backgroundTexture != null && !backgroundTexture.isBlank())
		{
			settings.generateBackground = false;
			settings.generateBackgroundFromTexture = true;
			settings.solidColorBackground = false;
			settings.colorizeOcean = true;
			settings.colorizeLand = true;
			settings.backgroundTextureSource = TextureSource.Assets;
			settings.backgroundTextureResource = new NamedResource(settings.artPack, backgroundTexture);
		}
		String borderType = stringValue(brief, "borderType", null);
		if (borderType != null && !borderType.isBlank())
		{
			settings.borderResource = new NamedResource(settings.artPack, borderType);
			settings.borderType = null;
		}
		settings.regionBoundaryStyle = strokeValue(
				brief,
				"regionBoundaryStyleType",
				"regionBoundaryWidth",
				settings.regionBoundaryStyle
		);
		settings.roadStyle = strokeValue(brief, "roadStyleType", "roadWidth", settings.roadStyle);

		settings.landColor = colorValue(brief, "landColor", settings.landColor);
		settings.regionBaseColor = colorValue(brief, "regionBaseColor", settings.regionBaseColor);
		settings.oceanColor = colorValue(brief, "oceanColor", settings.oceanColor);
		settings.riverColor = colorValue(brief, "riverColor", settings.riverColor);
		settings.roadColor = colorValue(brief, "roadColor", settings.roadColor);
		settings.textColor = colorValue(brief, "textColor", settings.textColor);
		settings.coastlineColor = colorValue(brief, "coastlineColor", settings.coastlineColor);
		settings.coastShadingColor = colorValue(brief, "coastShadingColor", settings.coastShadingColor);
		settings.oceanShadingColor = colorValue(brief, "oceanShadingColor", settings.oceanShadingColor);
		settings.oceanWavesColor = colorValue(brief, "oceanWavesColor", settings.oceanWavesColor);
		settings.regionBoundaryColor = colorValue(brief, "regionBoundaryColor", settings.regionBoundaryColor);
		settings.borderColor = colorValue(brief, "borderColor", settings.borderColor);
		settings.frayedBorderColor = colorValue(brief, "frayedBorderColor", settings.frayedBorderColor);
		settings.boldBackgroundColor = colorValue(brief, "boldBackgroundColor", settings.boldBackgroundColor);
		applyFonts(brief, settings);
	}

	private void applyFonts(JSONObject brief, MapSettings settings)
	{
		String fontFamily = stringValue(brief, "fontFamily", null);
		String registeredFontFamily = registerFontFile(stringValue(brief, "fontFile", ""));
		if (registeredFontFamily != null
				&& (fontFamily == null || fontFamily.isBlank() || !Font.isInstalled(fontFamily)))
		{
			fontFamily = registeredFontFamily;
		}
		if ((fontFamily == null || fontFamily.isBlank()) && labelsContainNonAscii(brief))
		{
			fontFamily = firstInstalledFont("Noto Serif SC", "Noto Sans SC", "Microsoft YaHei UI", "Microsoft JhengHei UI", "SimSun-ExtG", "SimSun-ExtB");
		}
		if (fontFamily == null || fontFamily.isBlank())
		{
			return;
		}
		if (!Font.isInstalled(fontFamily))
		{
			System.err.println("Warning: font family is not installed, using platform fallback: " + fontFamily);
		}

		int titleFontSize = intValue(brief, "titleFontSize", Math.round(settings.titleFont.getSize()));
		int regionFontSize = intValue(brief, "regionFontSize", Math.round(settings.regionFont.getSize()));
		int mountainRangeFontSize = intValue(brief, "mountainRangeFontSize", Math.round(settings.mountainRangeFont.getSize()));
		int otherMountainsFontSize = intValue(brief, "otherMountainsFontSize", Math.round(settings.otherMountainsFont.getSize()));
		int citiesFontSize = intValue(brief, "citiesFontSize", Math.round(settings.citiesFont.getSize()));
		int riverFontSize = intValue(brief, "riverFontSize", Math.round(settings.riverFont.getSize()));
		settings.titleFont = Font.create(fontFamily, settings.titleFont.getStyle(), titleFontSize);
		settings.regionFont = Font.create(fontFamily, settings.regionFont.getStyle(), regionFontSize);
		settings.mountainRangeFont = Font.create(fontFamily, settings.mountainRangeFont.getStyle(), mountainRangeFontSize);
		settings.otherMountainsFont = Font.create(fontFamily, settings.otherMountainsFont.getStyle(), otherMountainsFontSize);
		settings.citiesFont = Font.create(fontFamily, settings.citiesFont.getStyle(), citiesFontSize);
		settings.riverFont = Font.create(fontFamily, settings.riverFont.getStyle(), riverFontSize);
	}

	private static String registerFontFile(String fontFile)
	{
		if (fontFile == null || fontFile.isBlank())
		{
			return null;
		}
		try
		{
			Path path = Paths.get(fontFile);
			if (!Files.exists(path))
			{
				System.err.println("Warning: font file does not exist: " + fontFile);
				return null;
			}
			java.awt.Font font = java.awt.Font.createFont(java.awt.Font.TRUETYPE_FONT, path.toFile());
			java.awt.GraphicsEnvironment.getLocalGraphicsEnvironment().registerFont(font);
			return font.getFamily();
		}
		catch (Exception e)
		{
			System.err.println("Warning: failed to register font file " + fontFile + ": " + e.getMessage());
			return null;
		}
	}

	private Map<String, LocationAnchor> applyLabels(JSONObject brief, MapSettings settings, WorldGraph graph)
	{
		Map<String, LocationAnchor> locationAnchorsByName = new HashMap<>();
		Set<Center> primaryLand = largestLandComponent(graph);
		Set<Center> majorLand = majorLandComponents(graph);
		Set<String> routePointNames = routePointNames(brief);
		Set<Integer> buildingIconCenters = new HashSet<>();
		List<IconFootprint> customIconFootprints = new ArrayList<>();
		List<PendingLabel> pendingLabels = new ArrayList<>();
		int minCityHopDistance = minCityHopDistance(brief);
		JSONArray labels = arrayValue(brief, "labels");
		if (labels == null)
		{
			return locationAnchorsByName;
		}

		for (Object obj : labels)
		{
			if (!(obj instanceof JSONObject label))
			{
				continue;
			}

			String text = stringValue(label, "text", "");
			if (text.isBlank())
			{
				continue;
			}

			TextType type = TextType.valueOf(stringValue(label, "type", TextType.Region.name()));
			Point location = pointValue(label, settings);
			Center labelCenter = null;
			Center iconCenter = null;
			Center customIconCenter = null;
			String customIconName = stringValue(label, "iconName", "");
			if (boolValue(label, "snapToOcean", false))
			{
				labelCenter = findBestOceanTitleCenter(toGraphPoint(location, settings), graph, text, settings);
				if (labelCenter != null)
				{
					location = fromGraphPoint(labelCenter.loc, settings);
				}
			}
			else if (boolValue(label, "snapToLand", type != TextType.Title))
			{
				String preference = locationPreference(label);
				String iconPlacement = stringValue(label, "iconPlacement", "land");
				boolean freePlacement = "archipelago".equals(stringValue(label, "featureType", ""))
						|| "ocean".equals(preference)
						|| "island".equals(iconPlacement);
				Set<Center> preferredLand = freePlacement ? null : (!customIconName.isBlank() ? majorLand : primaryLand);
				labelCenter = snapCenterToGraph(label, location, settings, graph, preferredLand);
				if (labelCenter != null)
				{
					location = fromGraphPoint(labelCenter.loc, settings);
					iconCenter = iconCenterForLabel(label, labelCenter, location, settings, graph, preferredLand);
					if (customIconName.isBlank() && needsBuildingIcon(type, label, routePointNames))
					{
						Center plainIconCenter = findNearestPlainBuildingCenter(iconCenter == null ? labelCenter.loc : iconCenter.loc, graph, settings, buildingIconCenters, minCityHopDistance);
						if (plainIconCenter == null)
						{
							plainIconCenter = findNearestPlainBuildingCenter(labelCenter.loc, graph, settings, buildingIconCenters, 1);
						}
						if (plainIconCenter != null)
						{
							iconCenter = plainIconCenter;
							location = fromGraphPoint(iconCenter.loc, settings);
							buildingIconCenters.add(iconCenter.index);
						}
					}
				}
			}

			if (!customIconName.isBlank())
			{
				Center desiredCustomCenter = type == TextType.City && iconCenter != null ? iconCenter : labelCenter;
				if (desiredCustomCenter == null)
				{
					desiredCustomCenter = iconCenter;
				}
				customIconCenter = findNearestAvailableCustomIconCenter(desiredCustomCenter, label, graph, settings, customIconFootprints);
				if (customIconCenter != null)
				{
					customIconFootprints.add(new IconFootprint(customIconCenter.index, customIconBoundsAt(label, customIconCenter, settings)));
					buildingIconCenters.add(customIconCenter.index);
					location = fromGraphPoint(customIconCenter.loc, settings);
				}
			}
			pendingLabels.add(new PendingLabel(label, text, type, location, labelCenter, iconCenter, customIconCenter));
		}

		List<LabelBounds> blockedBounds = new ArrayList<>();
		for (PendingLabel pending : pendingLabels)
		{
			LabelBounds iconBounds = iconBounds(pending, settings);
			if (iconBounds != null)
			{
				blockedBounds.add(iconBounds);
			}
		}

		List<PendingLabel> renderLabels = new ArrayList<>();
		List<PendingLabel> titleLabels = new ArrayList<>();
		for (PendingLabel pending : pendingLabels)
		{
			if (pending.type == TextType.Title)
			{
				titleLabels.add(pending);
			}
			else
			{
				renderLabels.add(pending);
			}
		}
		renderLabels.addAll(titleLabels);

		for (PendingLabel pending : renderLabels)
		{
			JSONObject label = pending.label;
			String text = pending.text;
			TextType type = pending.type;
			TextType renderType = textTypeForRenderedLabel(label, type, pending.customIconCenter != null);
			Point location = pending.location;
			if (pending.customIconCenter != null)
			{
				LabelBounds customBounds = customIconBoundsAt(label, pending.customIconCenter, settings);
				location = new Point(location.x, customBounds.bottom + doubleValue(label, "iconLabelOffset", 14.0));
			}
			else if (type == TextType.City && pending.iconCenter != null)
			{
				location = new Point(location.x, location.y + 22.0);
			}
			if (pending.customIconCenter != null)
			{
				location = boolValue(label, "iconLabelLockBelow", false)
						? findNonOverlappingLabelLocationLockedXBelow(location, text, renderType, brief, settings, blockedBounds)
						: findNonOverlappingLabelLocationLockedX(location, text, renderType, brief, settings, blockedBounds);
			}
			else if ("inland_sea".equals(locationPreference(label)))
			{
				location = findInlandSeaLabelLocation(location, text, renderType, brief, settings, graph, blockedBounds);
			}
			else
			{
				location = findNonOverlappingLabelLocation(location, text, renderType, brief, settings, blockedBounds);
			}
			blockedBounds.add(labelBounds(location, text, renderType, brief, settings));
			double angle = doubleValue(label, "angle", 0.0);
			double curvature = doubleValue(label, "curvature", 0.0);
			int spacing = intValue(label, "spacing", 0);

			settings.edits.text.add(new MapText(text, location, angle, renderType, LineBreak.Auto, null, null, curvature, spacing, null, MapText.defaultBackgroundFade));
			if (type != TextType.Title)
			{
				locationAnchorsByName.put(text, new LocationAnchor(location, pending.labelCenter, pending.iconCenter, pending.customIconCenter));
			}
		}
		return locationAnchorsByName;
	}

	private static TextType textTypeForRenderedLabel(JSONObject label, TextType fallback, boolean hasCustomIcon)
	{
		if (!hasCustomIcon || fallback == TextType.Title || "ocean".equals(stringValue(label, "iconPlacement", "land")))
		{
			return fallback;
		}
		return TextType.City;
	}

	private static Center findNearestAvailableCustomIconCenter(
			Center desired,
			JSONObject label,
			WorldGraph graph,
			MapSettings settings,
			List<IconFootprint> occupied)
	{
		if (desired == null || graph == null || graph.centers == null)
		{
			return null;
		}
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		double bestOverlap = Double.POSITIVE_INFINITY;
		String placement = stringValue(label, "iconPlacement", "land");
		for (Center candidate : graph.centers)
		{
			if (!compatibleCustomIconCenter(desired, candidate, label))
			{
				continue;
			}
			if (isOccupiedCustomIconCenter(candidate, occupied, false))
			{
				continue;
			}
			if (isTooCloseToCustomIconCenter(candidate, occupied, CUSTOM_ICON_MIN_HOP_DISTANCE))
			{
				continue;
			}
			LabelBounds bounds = customIconBoundsAt(label, candidate, settings);
			if (!insideMap(bounds, settings))
			{
				continue;
			}
			if (customIconFootprintHasWater(label, candidate, settings, graph))
			{
				continue;
			}
			double overlap = iconOverlapArea(bounds, occupied, CUSTOM_ICON_OVERLAP_GAP);
			if (overlap > 0.0)
			{
				continue;
			}
			double score = candidate.loc.distanceTo(desired.loc)
					+ customIconPlacementPenalty(label, candidate)
					+ customIconFootprintRegionPenalty(label, candidate, settings, graph)
					+ customIconFootprintTerrainPenalty(label, candidate, settings, graph)
					+ customIconSpacingPenalty(candidate, occupied, CUSTOM_ICON_SPACING_HOPS);
			if (candidate.isCoast != desired.isCoast)
			{
				score += 80.0;
			}
			if (overlap < bestOverlap || (overlap == bestOverlap && score < bestScore))
			{
				bestOverlap = overlap;
				bestScore = score;
				best = candidate;
			}
		}
		return best;
	}

	private static double customIconPlacementPenalty(JSONObject label, Center candidate)
	{
		String placement = stringValue(label, "iconPlacement", "land");
		String preference = locationPreference(label);
		double penalty = 0.0;
		if ("ocean".equals(placement))
		{
			penalty += hasLandWithinHops(candidate, 5) ? 3600.0 : 0.0;
			penalty += candidate.isCoast ? 1800.0 : 0.0;
			return penalty;
		}
		if ("island".equals(placement))
		{
			return penalty;
		}
		if (isSkyIslandCustomIcon(label))
		{
			penalty += candidate.isMountain ? -600.0 : candidate.isHill ? -200.0 : 160.0;
			penalty += candidate.isCoast ? 1800.0 : 0.0;
			penalty += hasAnyWaterWithinHops(candidate, 2) ? 1400.0 : 0.0;
			penalty += isForest(candidate) ? 220.0 : 0.0;
			return penalty;
		}
		if ("ground".equals(stringValue(label, "iconAnchorMode", "ground")) && !"coast".equals(preference) && !customIconPrefersAny(label, "coast", "sea", "island"))
		{
			penalty += hasAnyWaterWithinHops(candidate, 3) ? 3200.0 : 0.0;
			penalty += hasAnyWaterWithinHops(candidate, 5) ? 650.0 : 0.0;
		}
		if (isSettlementLikeCustomIcon(label))
		{
			boolean plainPreferred = settlementPrefersPlain(label);
			penalty += candidate.isCoast ? 3800.0 : 0.0;
			penalty += candidate.isMountain ? (plainPreferred ? 6200.0 : 1800.0) : 0.0;
			penalty += candidate.isHill ? (plainPreferred ? 3600.0 : 900.0) : 0.0;
			penalty += isForest(candidate) ? (plainPreferred ? 5200.0 : 1600.0) : 0.0;
			penalty += hasAnyWaterWithinHops(candidate, 2) ? 3600.0 : 0.0;
			penalty += hasAnyWaterWithinHops(candidate, 4) ? 850.0 : 0.0;
		}
		else if ("forest".equals(preference))
		{
			penalty += isForest(candidate) ? -650.0 : 650.0;
			penalty += candidate.isCoast ? 1000.0 : 0.0;
			penalty += hasWaterWithinHops(candidate, 2) ? 700.0 : 0.0;
			penalty += candidate.isMountain ? 800.0 : 0.0;
			penalty += candidate.isHill ? 350.0 : 0.0;
		}
		else if ("mountain".equals(preference))
		{
			penalty += candidate.isMountain ? -500.0 : candidate.isHill ? 120.0 : 520.0;
			penalty += candidate.isCoast ? 1200.0 : 0.0;
			penalty += hasWaterWithinHops(candidate, 2) ? 800.0 : 0.0;
		}
		return penalty;
	}

	private static boolean isSettlementLikeCustomIcon(JSONObject label)
	{
		String kind = stringValue(label, "iconPlaceKind", "").toLowerCase();
		return kind.contains("settlement")
				|| kind.contains("city")
				|| kind.contains("town")
				|| kind.contains("village")
				|| kind.contains("kingdom")
				|| kind.contains("realm")
				|| kind.contains("facility");
	}

	private static boolean settlementPrefersPlain(JSONObject label)
	{
		return isSettlementLikeCustomIcon(label)
				&& !customIconPrefersAny(label, "mountain", "hill", "forest", "woods", "woodland", "coast", "sea", "island", "sky_island", "sky", "floating");
	}

	private static boolean isSkyIslandCustomIcon(JSONObject label)
	{
		String kind = stringValue(label, "iconPlaceKind", "").toLowerCase();
		return kind.contains("sky_island")
				|| customIconPrefersAny(label, "sky_island", "sky", "floating");
	}

	private static boolean isLakeCustomIcon(JSONObject label)
	{
		String kind = stringValue(label, "iconPlaceKind", "").toLowerCase();
		return kind.contains("lake");
	}

	private static boolean customIconPrefersAny(JSONObject label, String... values)
	{
		JSONArray terrain = arrayValue(label, "iconPreferredTerrain");
		if (terrain == null)
		{
			return false;
		}
		for (Object item : terrain)
		{
			String normalized = String.valueOf(item).strip().toLowerCase();
			for (String value : values)
			{
				if (normalized.equals(value))
				{
					return true;
				}
			}
		}
		return false;
	}

	private static boolean isOccupiedCustomIconCenter(Center candidate, List<IconFootprint> occupied, boolean includeNeighbors)
	{
		if (candidate == null || occupied == null || occupied.isEmpty())
		{
			return false;
		}
		for (IconFootprint footprint : occupied)
		{
			if (footprint.centerIndex == candidate.index)
			{
				return true;
			}
			if (includeNeighbors && candidate.neighbors != null)
			{
				for (Center neighbor : candidate.neighbors)
				{
					if (neighbor != null && neighbor.index == footprint.centerIndex)
					{
						return true;
					}
				}
			}
		}
		return false;
	}

	private static boolean isTooCloseToCustomIconCenter(Center candidate, List<IconFootprint> occupied, int minHops)
	{
		if (candidate == null || occupied == null || occupied.isEmpty())
		{
			return false;
		}
		for (IconFootprint footprint : occupied)
		{
			int distance = centerHopDistance(candidate, footprint.centerIndex, minHops);
			if (distance >= 0 && distance < minHops)
			{
				return true;
			}
		}
		return false;
	}

	private static double customIconSpacingPenalty(Center candidate, List<IconFootprint> occupied, int hops)
	{
		if (candidate == null || occupied == null || occupied.isEmpty())
		{
			return 0.0;
		}
		double penalty = 0.0;
		for (IconFootprint footprint : occupied)
		{
			int distance = centerHopDistance(candidate, footprint.centerIndex, hops);
			if (distance >= 0)
			{
				penalty += (hops + 1 - distance) * 850.0;
			}
		}
		return penalty;
	}

	private static int centerHopDistance(Center start, int targetIndex, int maxHops)
	{
		if (start == null)
		{
			return -1;
		}
		if (start.index == targetIndex)
		{
			return 0;
		}
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(start);
		seen.add(start.index);
		for (int step = 1; step <= maxHops && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				if (current.neighbors == null)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor == null || !seen.add(neighbor.index))
					{
						continue;
					}
					if (neighbor.index == targetIndex)
					{
						return step;
					}
					next.add(neighbor);
				}
			}
			frontier = next;
		}
		return -1;
	}

	private static boolean compatibleCustomIconCenter(Center desired, Center candidate, JSONObject label)
	{
		if (candidate == null || candidate.loc == null || candidate.isBorder)
		{
			return false;
		}
		String placement = stringValue(label, "iconPlacement", "land");
		String preference = locationPreference(label);
		if ("ocean".equals(placement))
		{
			return candidate.isWater && !candidate.isLake;
		}
		if ("island".equals(placement))
		{
			return !candidate.isWater && !candidate.isLake;
		}
		if (isSkyIslandCustomIcon(label))
		{
			return !candidate.isWater && !candidate.isLake && !candidate.isCoast && !hasAnyWaterWithinHops(candidate, 1);
		}
		if ("lake".equals(preference))
		{
			if (!isLakeCustomIcon(label))
			{
				return !candidate.isWater && !candidate.isLake && isLakeShore(candidate);
			}
			return candidate.isWater && candidate.isLake;
		}
		if ("lake_shore".equals(preference))
		{
			return !candidate.isWater && !candidate.isLake && isLakeShore(candidate);
		}
		if ("coast".equals(preference))
		{
			return !candidate.isWater && !candidate.isLake;
		}
		return !candidate.isWater && !candidate.isLake;
	}

	private static boolean customIconFootprintHasWater(JSONObject label, Center center, MapSettings settings, WorldGraph graph)
	{
		if (!shouldKeepCustomIconFootprintDry(label) || center == null || graph == null || graph.centers == null)
		{
			return false;
		}
		LabelBounds bounds = customIconBoundsAt(label, center, settings);
		for (Center candidate : graph.centers)
		{
			if (candidate == null || candidate.loc == null)
			{
				continue;
			}
			Point point = fromGraphPoint(candidate.loc, settings);
			if (point.x < bounds.left || point.x > bounds.right || point.y < bounds.top || point.y > bounds.bottom)
			{
				continue;
			}
			if (candidate.isWater || candidate.isLake)
			{
				return true;
			}
		}
		return false;
	}

	private static double customIconFootprintRegionPenalty(JSONObject label, Center center, MapSettings settings, WorldGraph graph)
	{
		if (!shouldKeepCustomIconFootprintDry(label) || center == null || graph == null || graph.centers == null)
		{
			return 0.0;
		}
		LabelBounds bounds = customIconBoundsAt(label, center, settings);
		Set<Integer> regionIds = new HashSet<>();
		for (Center candidate : graph.centers)
		{
			if (candidate == null || candidate.loc == null || candidate.isWater || candidate.isLake)
			{
				continue;
			}
			Point point = fromGraphPoint(candidate.loc, settings);
			if (point.x < bounds.left || point.x > bounds.right || point.y < bounds.top || point.y > bounds.bottom)
			{
				continue;
			}
			Integer regionId = regionIdAt(candidate, settings);
			if (regionId != null)
			{
				regionIds.add(regionId);
			}
		}
		return Math.max(0, regionIds.size() - 1) * 2200.0;
	}

	private static double customIconFootprintTerrainPenalty(JSONObject label, Center center, MapSettings settings, WorldGraph graph)
	{
		if (!settlementPrefersPlain(label) || center == null || graph == null || graph.centers == null)
		{
			return 0.0;
		}
		LabelBounds bounds = customIconBoundsAt(label, center, settings);
		int landCount = 0;
		int ruggedCount = 0;
		int forestCount = 0;
		for (Center candidate : graph.centers)
		{
			if (candidate == null || candidate.loc == null || candidate.isWater || candidate.isLake)
			{
				continue;
			}
			Point point = fromGraphPoint(candidate.loc, settings);
			if (point.x < bounds.left || point.x > bounds.right || point.y < bounds.top || point.y > bounds.bottom)
			{
				continue;
			}
			landCount++;
			if (candidate.isMountain || candidate.isHill)
			{
				ruggedCount++;
			}
			if (isForest(candidate))
			{
				forestCount++;
			}
		}
		if (landCount == 0)
		{
			return 0.0;
		}
		double ruggedRatio = (double) ruggedCount / (double) landCount;
		double forestRatio = (double) forestCount / (double) landCount;
		return ruggedRatio * 7800.0 + forestRatio * 6200.0;
	}

	private static boolean shouldKeepCustomIconFootprintDry(JSONObject label)
	{
		if (!"land".equals(stringValue(label, "iconPlacement", "land")))
		{
			return false;
		}
		if (!"ground".equals(stringValue(label, "iconAnchorMode", "ground")))
		{
			return false;
		}
		if (customIconPrefersAny(label, "coast", "sea", "island", "lake", "inland_lake", "ocean", "underwater"))
		{
			return false;
		}
		return !isLakeCustomIcon(label);
	}

	private static double iconOverlapArea(LabelBounds candidate, List<IconFootprint> occupied, double gap)
	{
		double overlap = 0.0;
		LabelBounds expanded = candidate.expand(gap);
		for (IconFootprint footprint : occupied)
		{
			overlap += overlapArea(expanded, footprint.bounds.expand(gap));
		}
		return overlap;
	}

	private static LabelBounds iconBounds(PendingLabel pending, MapSettings settings)
	{
		if (pending.customIconCenter != null)
		{
			return customIconBoundsAt(pending.label, pending.customIconCenter, settings).expand(9.0);
		}
		if (pending.iconCenter != null && pending.type == TextType.City)
		{
			double width = 32.0 * Math.max(0.1, settings.cityScale) + 12.0;
			double height = width * 1.25;
			Point point = fromGraphPoint(pending.iconCenter.loc, settings);
			return new LabelBounds(point.x - width / 2.0, point.y - height / 2.0, point.x + width / 2.0, point.y + height / 2.0);
		}
		return null;
	}

	private static LabelBounds customIconBoundsAt(JSONObject label, Center center, MapSettings settings)
	{
		double scale = Math.max(0.1, doubleValue(label, "iconScale", 1.0));
		double boundsScale = Math.max(1.0, doubleValue(label, "iconBoundsScale", 2.45));
		double width = Math.max(1.0, doubleValue(label, "iconBaseWidth", 48.0)) * scale * boundsScale;
		double height = width * Math.max(0.1, doubleValue(label, "iconAspectRatio", 1.0));
		Point point = fromGraphPoint(center.loc, settings);
		if ("ground".equals(stringValue(label, "iconAnchorMode", "ground")))
		{
			return new LabelBounds(point.x - width / 2.0, point.y - height, point.x + width / 2.0, point.y);
		}
		return new LabelBounds(point.x - width / 2.0, point.y - height / 2.0, point.x + width / 2.0, point.y + height / 2.0);
	}

	private static Point findNonOverlappingLabelLocation(Point desired, String text, TextType type, JSONObject brief, MapSettings settings, List<LabelBounds> occupied)
	{
		LabelBounds desiredBounds = labelBounds(desired, text, type, brief, settings);
		if (insideMap(desiredBounds, settings) && !overlapsAny(desiredBounds, occupied))
		{
			return desired;
		}

		double stepX = Math.max(36.0, desiredBounds.width() * 0.58);
		double stepY = Math.max(25.0, desiredBounds.height() * 1.15);
		Point best = desired;
		double bestOverlap = overlapArea(desiredBounds, occupied);
		for (int ring = 1; ring <= 9; ring++)
		{
			for (int dx = -ring; dx <= ring; dx++)
			{
				for (int dy = -ring; dy <= ring; dy++)
				{
					if (Math.max(Math.abs(dx), Math.abs(dy)) != ring)
					{
						continue;
					}
					Point candidate = new Point(desired.x + dx * stepX, desired.y + dy * stepY);
					LabelBounds candidateBounds = labelBounds(candidate, text, type, brief, settings);
					if (!insideMap(candidateBounds, settings))
					{
						continue;
					}
					double overlap = overlapArea(candidateBounds, occupied);
					if (overlap == 0.0)
					{
						return candidate;
					}
					if (overlap < bestOverlap)
					{
						bestOverlap = overlap;
						best = candidate;
					}
				}
			}
		}
		return best;
	}

	private static Point findNonOverlappingLabelLocationLockedX(Point desired, String text, TextType type, JSONObject brief, MapSettings settings, List<LabelBounds> occupied)
	{
		LabelBounds desiredBounds = labelBounds(desired, text, type, brief, settings);
		if (insideMap(desiredBounds, settings) && !overlapsAny(desiredBounds, occupied))
		{
			return desired;
		}

		double stepY = Math.max(18.0, desiredBounds.height() * 0.75);
		Point best = desired;
		double bestOverlap = overlapArea(desiredBounds, occupied);
		for (int ring = 1; ring <= 14; ring++)
		{
			for (int direction : new int[] { 1, -1 })
			{
				Point candidate = new Point(desired.x, desired.y + direction * ring * stepY);
				LabelBounds candidateBounds = labelBounds(candidate, text, type, brief, settings);
				if (!insideMap(candidateBounds, settings))
				{
					continue;
				}
				double overlap = overlapArea(candidateBounds, occupied);
				if (overlap == 0.0)
				{
					return candidate;
				}
				if (overlap < bestOverlap)
				{
					bestOverlap = overlap;
					best = candidate;
				}
			}
		}
		return best;
	}

	private static Point findNonOverlappingLabelLocationLockedXBelow(Point desired, String text, TextType type, JSONObject brief, MapSettings settings, List<LabelBounds> occupied)
	{
		LabelBounds desiredBounds = labelBounds(desired, text, type, brief, settings);
		if (insideMap(desiredBounds, settings) && !overlapsAny(desiredBounds, occupied))
		{
			return desired;
		}

		double stepY = Math.max(18.0, desiredBounds.height() * 0.75);
		Point best = desired;
		double bestOverlap = overlapArea(desiredBounds, occupied);
		for (int ring = 1; ring <= 14; ring++)
		{
			Point candidate = new Point(desired.x, desired.y + ring * stepY);
			LabelBounds candidateBounds = labelBounds(candidate, text, type, brief, settings);
			if (!insideMap(candidateBounds, settings))
			{
				continue;
			}
			double overlap = overlapArea(candidateBounds, occupied);
			if (overlap == 0.0)
			{
				return candidate;
			}
			if (overlap < bestOverlap)
			{
				bestOverlap = overlap;
				best = candidate;
			}
		}
		return best;
	}

	private static Point findWaterDominantLabelLocation(Point desired, String text, TextType type, JSONObject brief, MapSettings settings, WorldGraph graph, List<LabelBounds> occupied)
	{
		Point best = desired;
		double bestScore = Double.POSITIVE_INFINITY;
		for (double dy = -260.0; dy <= 220.0; dy += 24.0)
		{
			for (double dx = -300.0; dx <= 300.0; dx += 24.0)
			{
				Point candidate = new Point(desired.x + dx, desired.y + dy);
				LabelBounds bounds = labelBounds(candidate, text, type, brief, settings);
				if (!insideMap(bounds, settings))
				{
					continue;
				}
				LabelBounds safetyBounds = bounds.expand(96.0);
				int landUnderLabel = countCentersInMapBounds(graph, safetyBounds, settings, false);
				int waterUnderLabel = countCentersInMapBounds(graph, safetyBounds, settings, true);
				double overlap = overlapArea(bounds.expand(4.0), occupied);
				double distance = Math.hypot(dx, dy);
				double score = distance * 0.72 + overlap * 4.0 + landUnderLabel * 1200.0 - waterUnderLabel * 18.0;
				if (landUnderLabel > 0)
				{
					score += 3200.0;
				}
				if (score < bestScore)
				{
					bestScore = score;
					best = candidate;
				}
			}
		}
		return best;
	}

	private static Point findInlandSeaLabelLocation(Point desired, String text, TextType type, JSONObject brief, MapSettings settings, WorldGraph graph, List<LabelBounds> occupied)
	{
		Point best = desired;
		double bestScore = Double.POSITIVE_INFINITY;
		for (double dy = -132.0; dy <= 132.0; dy += 18.0)
		{
			for (double dx = -156.0; dx <= 156.0; dx += 18.0)
			{
				Point candidate = new Point(desired.x + dx, desired.y + dy);
				LabelBounds bounds = labelBounds(candidate, text, type, brief, settings);
				if (!insideMap(bounds, settings))
				{
					continue;
				}
				LabelBounds safetyBounds = bounds.expand(22.0);
				int landUnderLabel = countCentersInMapBounds(graph, safetyBounds, settings, false);
				int waterUnderLabel = countCentersInMapBounds(graph, safetyBounds, settings, true);
				double overlap = overlapArea(bounds.expand(4.0), occupied);
				double distance = Math.hypot(dx, dy);
				double score = distance * 8.0 + overlap * 4.0 + landUnderLabel * 210.0 - waterUnderLabel * 30.0;
				if (waterUnderLabel == 0)
				{
					score += 2600.0;
				}
				if (score < bestScore)
				{
					bestScore = score;
					best = candidate;
				}
			}
		}
		return best;
	}

	private static LabelBounds labelBounds(Point location, String text, TextType type, JSONObject brief, MapSettings settings)
	{
		int fontSize = switch (type)
		{
			case Title -> intValue(brief, "titleFontSize", Math.round(settings.titleFont.getSize()));
			case Region -> intValue(brief, "regionFontSize", Math.round(settings.regionFont.getSize()));
			case Mountain_range -> intValue(brief, "mountainRangeFontSize", Math.round(settings.mountainRangeFont.getSize()));
			case Other_mountains -> intValue(brief, "otherMountainsFontSize", Math.round(settings.otherMountainsFont.getSize()));
			case City -> intValue(brief, "citiesFontSize", Math.round(settings.citiesFont.getSize()));
			case River -> intValue(brief, "riverFontSize", Math.round(settings.riverFont.getSize()));
			case Lake -> intValue(brief, "regionFontSize", Math.round(settings.regionFont.getSize()));
		};
		double units = 0.0;
		for (int offset = 0; offset < text.length();)
		{
			int codePoint = text.codePointAt(offset);
			offset += Character.charCount(codePoint);
			if (Character.isWhitespace(codePoint))
			{
				units += 0.35;
			}
			else if (codePoint <= 0x7f)
			{
				units += 0.62;
			}
			else
			{
				units += 1.0;
			}
		}
		double width = Math.max(fontSize * 2.0, units * fontSize * 1.12) + 18.0;
		double height = fontSize * 1.7 + 12.0;
		return new LabelBounds(location.x - width / 2.0, location.y - height / 2.0, location.x + width / 2.0, location.y + height / 2.0);
	}

	private static boolean insideMap(LabelBounds bounds, MapSettings settings)
	{
		double margin = 8.0;
		return bounds.left >= margin
				&& bounds.top >= margin
				&& bounds.right <= settings.generatedWidth - margin
				&& bounds.bottom <= settings.generatedHeight - margin;
	}

	private static boolean overlapsAny(LabelBounds candidate, List<LabelBounds> occupied)
	{
		return overlapArea(candidate, occupied) > 0.0;
	}

	private static double overlapArea(LabelBounds candidate, List<LabelBounds> occupied)
	{
		double total = 0.0;
		for (LabelBounds other : occupied)
		{
			double width = Math.max(0.0, Math.min(candidate.right, other.right) - Math.max(candidate.left, other.left));
			double height = Math.max(0.0, Math.min(candidate.bottom, other.bottom) - Math.max(candidate.top, other.top));
			total += width * height;
		}
		return total;
	}

	private static double overlapArea(LabelBounds first, LabelBounds second)
	{
		double width = Math.max(0.0, Math.min(first.right, second.right) - Math.max(first.left, second.left));
		double height = Math.max(0.0, Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top));
		return width * height;
	}

	private void applyLocationTerrainEdits(JSONObject brief, MapSettings settings, WorldGraph graph, Map<String, LocationAnchor> locationAnchorsByName)
	{
		JSONArray labels = arrayValue(brief, "labels");
		if (labels == null || graph == null || locationAnchorsByName.isEmpty())
		{
			return;
		}

		for (Object obj : labels)
		{
			if (!(obj instanceof JSONObject label))
			{
				continue;
			}
			String text = stringValue(label, "text", "");
			LocationAnchor anchor = locationAnchorsByName.get(text);
			if (anchor == null)
			{
				continue;
			}
			String preference = locationPreference(label);
			Center patchAnchor = anchor.labelCenter != null ? anchor.labelCenter : anchor.iconCenter;
			if ("forest".equals(preference))
			{
				applySemanticIconPatch(settings, patchAnchor, IconType.trees, "deciduous", 4, 30, true);
			}
			else if ("mountain".equals(preference))
			{
				boolean range = "mountain_range".equals(stringValue(label, "featureType", ""));
				applySemanticIconPatch(settings, patchAnchor, IconType.mountains, "sharp", range ? 5 : 2, range ? 28 : 10, false);
			}
			else if ("hill".equals(preference))
			{
				applySemanticIconPatch(settings, patchAnchor, IconType.hills, "round", 2, 9, false);
			}
		}
	}

	private static void applyLocationGeography(JSONObject brief, MapSettings settings, WorldGraph graph)
	{
		JSONArray labels = arrayValue(brief, "labels");
		if (labels == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}
		boolean changed = false;
		Set<Integer> reservedIslandCenters = new HashSet<>();
		for (Object obj : labels)
		{
			if (!(obj instanceof JSONObject label))
			{
				continue;
			}
			String featureType = stringValue(label, "featureType", "");
			String iconPlacement = stringValue(label, "iconPlacement", "land");
			if ("island".equals(iconPlacement))
			{
				IslandSelection island = findSmallIslandNear(
						toGraphPoint(pointValue(label, settings), settings), graph, settings, reservedIslandCenters);
				if (island == null)
				{
					island = createSemanticIconIslandNear(
							toGraphPoint(pointValue(label, settings), settings), graph, settings, reservedIslandCenters);
				}
				if (island != null)
				{
					clearIslandSurface(settings, island.centers);
					for (Center center : island.centers)
					{
						reservedIslandCenters.add(center.index);
					}
					label.put("x", island.anchor.loc.x / (settings.resolution * settings.generatedWidth));
					label.put("y", island.anchor.loc.y / (settings.resolution * settings.generatedHeight));
					label.put("semanticAnchorIndex", island.anchor.index);
					changed = true;
				}
			}
			else if ("archipelago".equals(featureType))
			{
				Center islandAnchor = createSemanticArchipelagoNear(toGraphPoint(pointValue(label, settings), settings), graph, settings);
				if (islandAnchor != null)
				{
					label.put("x", islandAnchor.loc.x / (settings.resolution * settings.generatedWidth));
					label.put("y", islandAnchor.loc.y / (settings.resolution * settings.generatedHeight));
					changed = true;
				}
			}
				else if ("inland_sea".equals(featureType))
				{
					Point desired = toGraphPoint(pointValue(label, settings), settings);
					Center seaAnchor = createSemanticInlandSeaNear(desired, graph, settings);
					if (seaAnchor == null)
					{
						seaAnchor = findReusableInlandSeaCenter(label, desired, graph, settings);
						if (seaAnchor != null)
						{
							seaAnchor = broadenReusableInlandSea(label, seaAnchor, desired, graph, settings);
						}
					}
					if (seaAnchor != null)
					{
						label.put("x", seaAnchor.loc.x / (settings.resolution * settings.generatedWidth));
					label.put("y", seaAnchor.loc.y / (settings.resolution * settings.generatedHeight));
					label.put("semanticAnchorIndex", seaAnchor.index);
					changed = changed || seaAnchor.isWater;
				}
			}
		}
		if (changed)
		{
			graph.updateCoastAndCornerFlags();
		}
	}

	private static IslandSelection findSmallIslandNear(
			Point desired,
			WorldGraph graph,
			MapSettings settings,
			Set<Integer> reservedCenters)
	{
		Set<Integer> visited = new HashSet<>();
		IslandSelection best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center start : graph.centers)
		{
			if (start == null || start.loc == null || start.isWater || start.isBorder || !visited.add(start.index))
			{
				continue;
			}
			List<Center> component = new ArrayList<>();
			List<Center> queue = new ArrayList<>();
			queue.add(start);
			for (int cursor = 0; cursor < queue.size(); cursor++)
			{
				Center current = queue.get(cursor);
				component.add(current);
				if (current.neighbors == null)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && neighbor.loc != null && !neighbor.isWater && !neighbor.isBorder && visited.add(neighbor.index))
					{
						queue.add(neighbor);
					}
				}
			}
			if (component.isEmpty() || component.size() > 40 || intersectsIndexes(component, reservedCenters))
			{
				continue;
			}
			Center anchor = islandComponentAnchor(component);
			if (anchor == null)
			{
				continue;
			}
			double score = anchor.loc.distanceTo(desired) + Math.abs(component.size() - 5) * 16.0;
			if (score < bestScore)
			{
				bestScore = score;
				best = new IslandSelection(anchor, component);
			}
		}
		return best;
	}

	private static IslandSelection createSemanticIconIslandNear(
			Point desired,
			WorldGraph graph,
			MapSettings settings,
			Set<Integer> reservedCenters)
	{
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (center == null || center.loc == null || !center.isWater || center.isLake || center.isBorder || center.isCoast || reservedCenters.contains(center.index))
			{
				continue;
			}
			double score = center.loc.distanceTo(desired) + Math.max(0.0, 120.0 - nearestLandDistance(center, graph)) * 4.0;
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		if (best == null)
		{
			return null;
		}
		convertCenterToSemanticLand(settings, best);
		return new IslandSelection(best, List.of(best));
	}

	private static Region nearestLandRegion(Center origin, WorldGraph graph)
	{
		Center best = null;
		double bestDistance = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (center == null || center.loc == null || center.isWater || center.region == null)
			{
				continue;
			}
			double distance = center.loc.distanceTo(origin.loc);
			if (distance < bestDistance)
			{
				bestDistance = distance;
				best = center;
			}
		}
		return best == null ? null : best.region;
	}

	private static Center islandComponentAnchor(List<Center> component)
	{
		double x = 0.0;
		double y = 0.0;
		for (Center center : component)
		{
			x += center.loc.x;
			y += center.loc.y;
		}
		Point centroid = new Point(x / component.size(), y / component.size());
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : component)
		{
			double score = center.loc.distanceTo(centroid);
			if (center.isMountain || center.isHill)
			{
				score += 120.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static boolean intersectsIndexes(List<Center> centers, Set<Integer> indexes)
	{
		for (Center center : centers)
		{
			if (indexes.contains(center.index))
			{
				return true;
			}
		}
		return false;
	}

	private static void clearIslandSurface(MapSettings settings, List<Center> centers)
	{
		for (Center center : centers)
		{
			clearTerrainAtCenter(settings, center);
			center.isCity = false;
			if (center.region != null)
			{
				center.region.remove(center);
				center.region = null;
			}
			FreeIcon existing = settings.edits.freeIcons.getNonTree(center.index);
			if (existing != null && existing.type == IconType.cities)
			{
				settings.edits.freeIcons.remove(existing);
			}
			ensureSemanticIslandRegion(settings);
			settings.edits.centerEdits.put(center.index, new CenterEdit(center.index, false, false, FU_GM_SEMANTIC_ISLAND_REGION_ID, null, null));
		}
	}

	private static Center createSemanticArchipelagoNear(Point desired, WorldGraph graph, MapSettings settings)
	{
		List<Center> seeds = new ArrayList<>();
		double minSpacing = Math.max(90.0, Math.min(graphWidth(graph, settings), graphHeight(graph, settings)) * 0.035);
		double searchRadius = Math.max(500.0, Math.min(graphWidth(graph, settings), graphHeight(graph, settings)) * 0.22);
		for (int island = 0; island < 5; island++)
		{
			Center best = null;
			double bestScore = Double.POSITIVE_INFINITY;
			for (Center center : graph.centers)
			{
				if (center == null || center.loc == null || !center.isWater || center.isLake || center.isBorder || center.isCoast)
				{
					continue;
				}
				double distance = center.loc.distanceTo(desired);
				if (distance > searchRadius || nearestLandDistance(center, graph) < minSpacing * 0.75)
				{
					continue;
				}
				boolean tooClose = false;
				for (Center seed : seeds)
				{
					if (seed.loc.distanceTo(center.loc) < minSpacing)
					{
						tooClose = true;
						break;
					}
				}
				if (tooClose)
				{
					continue;
				}
				double angleBias = Math.abs((center.loc.x - desired.x) * 0.35) + Math.abs((center.loc.y - desired.y) * 0.15);
				double score = distance + angleBias + island * Math.abs(center.loc.y - desired.y) * 0.03;
				if (score < bestScore)
				{
					bestScore = score;
					best = center;
				}
			}
			if (best != null)
			{
				seeds.add(best);
			}
		}
		if (seeds.size() < 3)
		{
			return null;
		}

		for (int index = 0; index < seeds.size(); index++)
		{
			Center seed = seeds.get(index);
			convertCenterToSemanticLand(settings, seed);
			if (index < 2 && seed.neighbors != null)
			{
				Center companion = null;
				for (Center neighbor : seed.neighbors)
				{
					if (neighbor != null && neighbor.isWater && !neighbor.isLake && !neighbor.isBorder && !neighbor.isCoast)
					{
						companion = neighbor;
						break;
					}
				}
				if (companion != null)
				{
					convertCenterToSemanticLand(settings, companion);
				}
			}
		}
		return seeds.get(0);
	}

	private static Center findReusableInlandSeaCenter(JSONObject label, Point desired, WorldGraph graph, MapSettings settings)
	{
		Center passage = findExistingInlandSeaPassageCenter(label, desired, graph, settings);
		if (passage != null)
		{
			return passage;
		}
		Center candidate = findBestInlandSeaLabelCenter(label, desired, graph, settings, null);
		if (candidate == null || candidate.loc == null)
		{
			return null;
		}
		double reuseDistance = semanticLakeReuseDistance(graph, settings) * 1.45;
		if (candidate.loc.distanceTo(desired) > reuseDistance)
		{
			return null;
		}
		int nearbyWater = countWaterWithinHops(candidate, 5);
		int enclosingLand = countLandWithinHops(candidate, 7);
		int landDirections = landDirectionCount(candidate, 7);
		int waterDirections = waterDirectionCount(candidate, 5);
		if (nearbyWater < 20 || enclosingLand < 10 || waterDirections < 3 || (landDirections < 3 && !hasOpposingLandDirections(candidate, 7)))
		{
			return null;
		}
		return candidate;
	}

	private static Center broadenReusableInlandSea(JSONObject label, Center anchor, Point desired, WorldGraph graph, MapSettings settings)
	{
		if (anchor == null || graph == null || settings == null)
		{
			return anchor;
		}
		List<Center> expansion = reusableInlandSeaExpansionCenters(anchor, desired, 4, 44, false);
		if (expansion.size() < 22)
		{
			expansion = reusableInlandSeaExpansionCenters(anchor, desired, 5, 72, true);
		}
		if (!expansion.isEmpty())
		{
			for (Center center : expansion)
			{
				convertCenterToSemanticSea(settings, center);
			}
			graph.updateCoastAndCornerFlags();
			Center relabeled = findBestInlandSeaLabelCenter(label, desired, graph, settings, null);
			if (relabeled != null)
			{
				return relabeled;
			}
		}
		return anchor;
	}

	private static List<Center> reusableInlandSeaExpansionCenters(Center anchor, Point desired, int radius, int limit, boolean allowRugged)
	{
		if (anchor == null || anchor.loc == null || anchor.neighbors == null)
		{
			return List.of();
		}
		List<Center> candidates = new ArrayList<>();
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(anchor);
		seen.add(anchor.index);
		for (int step = 0; step <= radius && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				if (isReusableInlandSeaExpansionCenter(current, allowRugged))
				{
					candidates.add(current);
				}
				if (current.neighbors == null || step == radius)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && neighbor.loc != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		candidates.sort((a, b) -> Double.compare(
				reusableInlandSeaExpansionScore(a, anchor, desired),
				reusableInlandSeaExpansionScore(b, anchor, desired)));
		if (candidates.size() <= limit)
		{
			return candidates;
		}
		return new ArrayList<>(candidates.subList(0, limit));
	}

	private static boolean isReusableInlandSeaExpansionCenter(Center center, boolean allowRugged)
	{
		return center != null
				&& center.loc != null
				&& !center.isWater
				&& !center.isLake
				&& !center.isBorder
				&& !center.isCity
				&& countWaterWithinHops(center, 1) >= 1
				&& (allowRugged || (!center.isMountain && !center.isHill));
	}

	private static double reusableInlandSeaExpansionScore(Center center, Center anchor, Point desired)
	{
		double score = center.loc.distanceTo(anchor.loc) * 0.72 + center.loc.distanceTo(desired) * 0.28;
		score -= Math.min(countWaterWithinHops(center, 1), 4) * 120.0;
		score -= Math.min(countLandWithinHops(center, 2), 8) * 12.0;
		if (center.isMountain)
		{
			score += 260.0;
		}
		if (center.isHill)
		{
			score += 120.0;
		}
		return score;
	}

	private static Center findExistingInlandSeaPassageCenter(JSONObject label, Point desired, WorldGraph graph, MapSettings settings)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}
		double minDimension = Math.min(graphWidth(graph, settings), graphHeight(graph, settings));
		double searchRadius = Math.max(360.0, minDimension * 0.42);
		double edgeMargin = Math.max(130.0, minDimension * 0.055);
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (center == null
					|| center.loc == null
					|| center.isBorder
					|| !center.isWater
					|| center.isLake)
			{
				continue;
			}
			double desiredDistance = center.loc.distanceTo(desired);
			if (desiredDistance > searchRadius)
			{
				continue;
			}
			int nearbyWater = countWaterWithinHops(center, 5);
			int enclosingLand = countLandWithinHops(center, 6);
			int landDirections = landDirectionCount(center, 7);
			int waterDirections = waterDirectionCount(center, 5);
			if (nearbyWater < 20 || enclosingLand < 6 || landDirections < 2 || waterDirections < 3)
			{
				continue;
			}
			LabelBounds bounds = estimatedInlandSeaLabelBounds(label, center, settings).expand(16.0);
			int waterUnderLabel = countCentersInMapBounds(graph, bounds, settings, true);
			int landUnderLabel = countCentersInMapBounds(graph, bounds, settings, false);
			if (waterUnderLabel < 2)
			{
				continue;
			}
			double edgeGap = Math.min(
					Math.min(center.loc.x, graphWidth(graph, settings) - center.loc.x),
					Math.min(center.loc.y, graphHeight(graph, settings) - center.loc.y));
			double score = desiredDistance * 1.18;
			score -= Math.min(nearbyWater, 34) * 34.0;
			score -= Math.min(enclosingLand, 32) * 22.0;
			score -= landDirections * 280.0;
			score -= waterDirections * 260.0;
			score -= hasOpposingLandDirections(center, 7) ? 360.0 : 0.0;
			score += center.isCoast ? 110.0 : 0.0;
			score += landUnderLabel * 180.0;
			score -= waterUnderLabel * 16.0;
			if (edgeGap < edgeMargin)
			{
				score += (edgeMargin - edgeGap) * 9.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static Center createSemanticInlandSeaNear(Point desired, WorldGraph graph, MapSettings settings)
	{
		Set<Integer> outerOcean = outerOceanComponent(graph, settings);
		Center seed = findBestSemanticInlandSeaSeedCenter(desired, graph, settings, outerOcean, false);
		if (seed == null)
		{
			seed = findBestSemanticInlandSeaSeedCenter(desired, graph, settings, outerOcean, true);
		}
		if (seed == null)
		{
			return null;
		}
		List<Center> seaCenters = nearbySemanticInlandSeaBasinCenters(seed, 6, 84, false);
		if (seaCenters.size() < 44)
		{
			seaCenters = nearbySemanticInlandSeaBasinCenters(seed, 8, 116, true);
		}
		if (seaCenters.size() < 18)
		{
			seaCenters = nearbySemanticLakeCenters(seed, 7, 64, true);
		}
		if (seaCenters.isEmpty())
		{
			return null;
		}
		for (Center center : seaCenters)
		{
			convertCenterToSemanticSea(settings, center);
		}
		List<Center> channel = shortestSeaChannelToOuterOcean(seaCenters, graph, settings);
		for (Center center : widenedSeaChannel(channel))
		{
			convertCenterToSemanticSea(settings, center);
		}
		graph.updateCoastAndCornerFlags();
		return inlandSeaBasinAnchor(seaCenters, desired);
	}

	private static Center findBestSemanticInlandSeaSeedCenter(Point desired, WorldGraph graph, MapSettings settings, Set<Integer> outerOcean, boolean relaxed)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}
		double minDimension = Math.min(graphWidth(graph, settings), graphHeight(graph, settings));
		double targetOceanDistance = Math.max(260.0, minDimension * 0.22);
		double minOceanDistance = Math.max(140.0, minDimension * 0.085);
		double maxOceanDistance = Math.max(520.0, minDimension * 0.48);
		double maxDesiredDistance = minDimension * (relaxed ? 0.46 : 0.30);
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (!isSemanticInlandSeaSeedCenter(center, relaxed))
			{
				continue;
			}
			double oceanDistance = nearestOuterOceanDistance(center, graph, outerOcean);
			if (!relaxed && (oceanDistance < minOceanDistance || oceanDistance > maxOceanDistance))
			{
				continue;
			}
			double desiredDistance = center.loc.distanceTo(desired);
			if (desiredDistance > maxDesiredDistance)
			{
				continue;
			}
			double score = desiredDistance * (relaxed ? 1.45 : 2.15);
			score += Math.abs(oceanDistance - targetOceanDistance) * (relaxed ? 0.34 : 0.24);
			score -= Math.min(36, countLandWithinHops(center, 4)) * 24.0;
			if (center.isCoast)
			{
				score += relaxed ? 900.0 : 5000.0;
			}
			if (hasAnyWaterWithinHops(center, relaxed ? 1 : 2))
			{
				score += relaxed ? 650.0 : 3600.0;
			}
			if (center.isMountain)
			{
				score += 420.0;
			}
			if (center.isHill)
			{
				score += 180.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static boolean isSemanticInlandSeaSeedCenter(Center center, boolean relaxed)
	{
		if (center == null
				|| center.loc == null
				|| center.isWater
				|| center.isLake
				|| center.isBorder
				|| center.isCity)
		{
			return false;
		}
		if (!relaxed && (center.isCoast || center.isMountain || center.isHill || hasAnyWaterWithinHops(center, 2)))
		{
			return false;
		}
		return true;
	}

	private static List<Center> nearbySemanticInlandSeaBasinCenters(Center anchor, int radius, int limit, boolean allowRugged)
	{
		List<Center> result = new ArrayList<>();
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(anchor);
		seen.add(anchor.index);

		for (int step = 0; step <= radius && !frontier.isEmpty() && result.size() < limit; step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center center : frontier)
			{
				if (isSemanticInlandSeaBasinCenter(center, allowRugged))
				{
					result.add(center);
					if (result.size() >= limit)
					{
						break;
					}
				}
				if (center.neighbors == null)
				{
					continue;
				}
				for (Center neighbor : center.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return result;
	}

	private static boolean isSemanticInlandSeaBasinCenter(Center center, boolean allowRugged)
	{
		if (center == null
				|| center.loc == null
				|| center.isWater
				|| center.isLake
				|| center.isBorder
				|| center.isCity
				|| center.isCoast)
		{
			return false;
		}
		return allowRugged || (!center.isMountain && !center.isHill);
	}

	private static Center inlandSeaBasinAnchor(List<Center> seaCenters, Point desired)
	{
		if (seaCenters == null || seaCenters.isEmpty())
		{
			return null;
		}
		double x = 0.0;
		double y = 0.0;
		for (Center center : seaCenters)
		{
			x += center.loc.x;
			y += center.loc.y;
		}
		Point centroid = new Point(x / seaCenters.size(), y / seaCenters.size());
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : seaCenters)
		{
			if (center == null || center.loc == null)
			{
				continue;
			}
			double score = center.loc.distanceTo(centroid) * 1.18 + center.loc.distanceTo(desired) * 0.04;
			score -= Math.min(countWaterWithinHops(center, 2), 10) * 34.0;
			score += Math.min(countLandWithinHops(center, 1), 6) * 86.0;
			if (center.isCoast)
			{
				score += 520.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static double nearestOuterOceanDistance(Center origin, WorldGraph graph, Set<Integer> outerOcean)
	{
		if (origin == null || origin.loc == null || graph == null || graph.centers == null || outerOcean == null || outerOcean.isEmpty())
		{
			return Double.POSITIVE_INFINITY;
		}
		double best = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (center == null || center.loc == null || !outerOcean.contains(center.index))
			{
				continue;
			}
			best = Math.min(best, origin.loc.distanceTo(center.loc));
		}
		return best;
	}

	private static List<Center> shortestSeaChannelToOuterOcean(List<Center> seaCenters, WorldGraph graph, MapSettings settings)
	{
		if (seaCenters == null || seaCenters.isEmpty() || graph == null || graph.centers == null)
		{
			return List.of();
		}

		Set<Integer> outerOcean = outerOceanComponent(graph, settings);
		if (outerOcean.isEmpty())
		{
			for (Center center : graph.centers)
			{
				if (center != null && center.isWater && !center.isLake)
				{
					outerOcean.add(center.index);
				}
			}
		}

		Set<Integer> seaIndexes = new HashSet<>();
		Set<Integer> visited = new HashSet<>();
		List<Center> queue = new ArrayList<>();
		Map<Center, Center> parent = new HashMap<>();
		for (Center center : seaCenters)
		{
			if (center == null || center.loc == null || center.isBorder)
			{
				continue;
			}
			seaIndexes.add(center.index);
			if (visited.add(center.index))
			{
				queue.add(center);
				parent.put(center, null);
			}
		}

		for (int cursor = 0; cursor < queue.size(); cursor++)
		{
			Center current = queue.get(cursor);
			if (current.neighbors == null)
			{
				continue;
			}
			for (Center neighbor : current.neighbors)
			{
				if (neighbor == null || neighbor.loc == null || (neighbor.isBorder && !outerOcean.contains(neighbor.index)) || !visited.add(neighbor.index))
				{
					continue;
				}
				parent.put(neighbor, current);
				if (outerOcean.contains(neighbor.index) && !seaIndexes.contains(neighbor.index))
				{
					return reconstructChannel(parent, neighbor, seaIndexes);
				}
				if (neighbor.isBorder)
				{
					continue;
				}
				queue.add(neighbor);
			}
		}
		return List.of();
	}

	private static Set<Integer> outerOceanComponent(WorldGraph graph, MapSettings settings)
	{
		Set<Integer> result = new HashSet<>();
		List<Center> queue = new ArrayList<>();
		for (Center center : graph.centers)
		{
			if (isOuterOceanSeed(center, graph, settings) && result.add(center.index))
			{
				queue.add(center);
			}
		}
		for (int cursor = 0; cursor < queue.size(); cursor++)
		{
			Center current = queue.get(cursor);
			if (current.neighbors == null)
			{
				continue;
			}
			for (Center neighbor : current.neighbors)
			{
				if (neighbor != null && neighbor.isWater && !neighbor.isLake && result.add(neighbor.index))
				{
					queue.add(neighbor);
				}
			}
		}
		return result;
	}

	private static boolean isOuterOceanSeed(Center center, WorldGraph graph, MapSettings settings)
	{
		if (center == null || center.loc == null || !center.isWater || center.isLake)
		{
			return false;
		}
		if (center.isBorder)
		{
			return true;
		}
		double margin = Math.max(120.0, Math.min(graphWidth(graph, settings), graphHeight(graph, settings)) * 0.035);
		return center.loc.x <= margin
				|| center.loc.y <= margin
				|| graphWidth(graph, settings) - center.loc.x <= margin
				|| graphHeight(graph, settings) - center.loc.y <= margin;
	}

	private static List<Center> widenedSeaChannel(List<Center> channel)
	{
		if (channel == null || channel.isEmpty())
		{
			return List.of();
		}
		List<Center> widened = new ArrayList<>();
		Set<Integer> added = new HashSet<>();
		for (Center center : channel)
		{
			if (center == null || center.loc == null)
			{
				continue;
			}
			if (added.add(center.index))
			{
				widened.add(center);
			}
			if ((center.isWater && !center.isLake) || center.neighbors == null)
			{
				continue;
			}
			for (Center neighbor : center.neighbors)
			{
				if (neighbor == null || neighbor.loc == null || neighbor.isBorder || neighbor.isCity)
				{
					continue;
				}
				if ((!neighbor.isWater || neighbor.isLake) && added.add(neighbor.index))
				{
					widened.add(neighbor);
				}
			}
		}
		return widened;
	}

	private static List<Center> narrowSeaChannel(List<Center> channel)
	{
		if (channel == null || channel.isEmpty())
		{
			return List.of();
		}
		List<Center> narrowed = new ArrayList<>();
		Set<Integer> added = new HashSet<>();
		for (Center center : channel)
		{
			if (center == null || center.loc == null || center.isBorder || center.isCity)
			{
				continue;
			}
			if (added.add(center.index))
			{
				narrowed.add(center);
			}
		}
		return narrowed;
	}

	private static List<Center> reconstructChannel(Map<Center, Center> parent, Center ocean, Set<Integer> seaIndexes)
	{
		List<Center> reversed = new ArrayList<>();
		Center cursor = ocean;
		while (cursor != null && !seaIndexes.contains(cursor.index))
		{
			reversed.add(cursor);
			cursor = parent.get(cursor);
		}
		List<Center> channel = new ArrayList<>();
		for (int index = reversed.size() - 1; index >= 0; index--)
		{
			channel.add(reversed.get(index));
		}
		return channel;
	}

	private static void convertCenterToSemanticLand(MapSettings settings, Center center)
	{
		if (center == null)
		{
			return;
		}
		center.isWater = false;
		center.isLake = false;
		center.isCity = false;
		if (settings != null && settings.edits != null && settings.edits.centerEdits != null)
		{
			ensureSemanticIslandRegion(settings);
			settings.edits.centerEdits.put(center.index, new CenterEdit(center.index, false, false, FU_GM_SEMANTIC_ISLAND_REGION_ID, null, null));
		}
	}

	private static void ensureSemanticIslandRegion(MapSettings settings)
	{
		if (settings == null || settings.edits == null || settings.edits.regionEdits == null)
		{
			return;
		}
		if (!settings.edits.regionEdits.containsKey(FU_GM_SEMANTIC_ISLAND_REGION_ID))
		{
			settings.edits.regionEdits.put(FU_GM_SEMANTIC_ISLAND_REGION_ID, new RegionEdit(FU_GM_SEMANTIC_ISLAND_REGION_ID, semanticIslandColor()));
		}
		settings.drawRegionColors = true;
	}

	private static Color semanticIslandColor()
	{
		return Color.create(216, 197, 139, 255);
	}

	private static void applySemanticIconPatch(MapSettings settings, Center anchor, IconType type, String groupId, int radius, int limit, boolean avoidMountains)
	{
		if (settings == null || settings.edits == null || anchor == null)
		{
			return;
		}

		List<Center> centers = nearbyPatchCenters(anchor, radius, limit, avoidMountains);
		if (centers.isEmpty())
		{
			centers = nearbyPatchCenters(anchor, radius, Math.max(4, limit / 2), false);
		}
		clearConflictingTerrainIcons(settings, centers, type);
		for (Center center : centers)
		{
			settings.edits.freeIcons.addOrReplace(new FreeIcon(
					settings.resolution,
					center.loc,
					semanticIconScale(type),
					type,
					Assets.installedArtPack,
					groupId,
					semanticIconIndex(center, type),
					center.index,
					type == IconType.trees ? 0.85 : 0.0,
					settings.getIconFillColorForType(type),
					settings.getIconFilterColorForType(type),
					settings.getMaximizeOpacityForType(type),
					settings.getFillWithColorForType(type)
			));
		}
	}

	private static void clearConflictingTerrainIcons(MapSettings settings, List<Center> centers, IconType desiredType)
	{
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null || centers == null || centers.isEmpty())
		{
			return;
		}
		Set<Integer> centerIndexes = new HashSet<>();
		for (Center center : centers)
		{
			if (center != null)
			{
				centerIndexes.add(center.index);
				if (center.neighbors != null)
				{
					for (Center neighbor : center.neighbors)
					{
						if (neighbor != null)
						{
							centerIndexes.add(neighbor.index);
						}
					}
				}
			}
		}
		List<FreeIcon> toRemove = new ArrayList<>();
		for (FreeIcon icon : settings.edits.freeIcons)
		{
			if (icon == null || icon.centerIndex == null || !centerIndexes.contains(icon.centerIndex) || !isTerrainIcon(icon.type))
			{
				continue;
			}
			if (desiredType == IconType.trees && icon.type != IconType.trees)
			{
				toRemove.add(icon);
			}
			else if ((desiredType == IconType.mountains || desiredType == IconType.hills) && icon.type != desiredType)
			{
				toRemove.add(icon);
			}
		}
		settings.edits.freeIcons.removeAll(toRemove);
	}

	private static boolean isTerrainIcon(IconType type)
	{
		return type == IconType.mountains || type == IconType.hills || type == IconType.trees || type == IconType.sand;
	}

	private static double semanticIconScale(IconType type)
	{
		if (type == IconType.mountains)
		{
			return 1.15;
		}
		if (type == IconType.hills)
		{
			return 1.05;
		}
		return 0.95;
	}

	private static int semanticIconIndex(Center center, IconType type)
	{
		return Math.floorMod(center.index * 734287 + type.ordinal() * 9187, Integer.MAX_VALUE);
	}

	private static List<Center> nearbyPatchCenters(Center anchor, int radius, int limit, boolean avoidMountains)
	{
		return nearbyPatchCenters(anchor, radius, limit, avoidMountains, false);
	}

	private static List<Center> nearbyPatchCenters(Center anchor, int radius, int limit, boolean avoidMountains, boolean allowCoast)
	{
		List<Center> result = new ArrayList<>();
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(anchor);
		seen.add(anchor.index);

		for (int step = 0; step <= radius && !frontier.isEmpty() && result.size() < limit; step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center center : frontier)
			{
				if (isPatchCenter(center, avoidMountains, allowCoast))
				{
					result.add(center);
					if (result.size() >= limit)
					{
						break;
					}
				}
				if (center.neighbors == null)
				{
					continue;
				}
				for (Center neighbor : center.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return result;
	}

	private static boolean isPatchCenter(Center center, boolean avoidMountains)
	{
		return isPatchCenter(center, avoidMountains, false);
	}

	private static boolean isPatchCenter(Center center, boolean avoidMountains, boolean allowCoast)
	{
		if (center == null || center.isWater || center.isLake || (!allowCoast && center.isCoast) || center.isBorder)
		{
			return false;
		}
		return !avoidMountains || (!center.isMountain && !center.isHill);
	}

	private void applyLocationIcons(JSONObject brief, MapSettings settings, WorldGraph graph, Map<String, LocationAnchor> locationAnchorsByName)
	{
		JSONArray labels = arrayValue(brief, "labels");
		if (labels == null || locationAnchorsByName.isEmpty())
		{
			return;
		}

		Set<String> routePointNames = routePointNames(brief);
		for (Object obj : labels)
		{
			if (!(obj instanceof JSONObject label))
			{
				continue;
			}
			String text = stringValue(label, "text", "");
			LocationAnchor anchor = locationAnchorsByName.get(text);
			if (anchor == null)
			{
				continue;
			}
			TextType type = TextType.valueOf(stringValue(label, "type", TextType.Region.name()));
			String customIconName = stringValue(label, "iconName", "");
			if (!customIconName.isBlank())
			{
				Center customCenter = anchor.customIconCenter;
				if (customCenter == null)
				{
					continue;
				}
				clearCustomIconUnderlay(settings, customCenter, label);
				IconType customType = "decorations".equals(stringValue(label, "iconRenderType", "cities"))
						? IconType.decorations : IconType.cities;
				double scale = Math.max(0.1, doubleValue(label, "iconScale", 1.0));
				double baseWidth = Math.max(1.0, doubleValue(label, "iconBaseWidth", 48.0));
				double aspectRatio = Math.max(0.1, doubleValue(label, "iconAspectRatio", 1.0));
				Point drawPoint = customCenter.loc;
				if ("ground".equals(stringValue(label, "iconAnchorMode", "ground")))
				{
					drawPoint = new Point(drawPoint.x, drawPoint.y - baseWidth * scale * aspectRatio * settings.resolution / 2.0);
				}
				Color fillColor = customIconFillColor(label, customCenter, settings, graph);
				HSBColor filterColor = customIconFilterColor(label, fillColor);
				boolean fillWithColor = customIconShouldFillWithColor(label, fillColor);
				settings.edits.freeIcons.addOrReplace(new FreeIcon(
						settings.resolution,
						drawPoint,
						scale,
						customType,
						Assets.customArtPack,
						stringValue(label, "iconGroup", "fu_gm_world_wonders"),
						customIconName,
						customCenter.index,
						fillColor,
						filterColor,
						false,
						fillWithColor
				));
				continue;
			}
			if (anchor.iconCenter == null || anchor.iconCenter.isWater)
			{
				continue;
			}
			if (!needsBuildingIcon(type, label, routePointNames))
			{
				continue;
			}

			clearTerrainAtCenter(settings, anchor.iconCenter);
			anchor.iconCenter.isCity = true;
			settings.edits.freeIcons.addOrReplace(new FreeIcon(
					settings.resolution,
					anchor.iconCenter.loc,
					1.0,
					IconType.cities,
					Assets.installedArtPack,
					"middle ages",
					cityIconName(label),
					anchor.iconCenter.index,
					settings.copyIconFillColorsByType().get(IconType.cities),
					settings.copyIconFilterColorsByType().get(IconType.cities),
					settings.getMaximizeOpacityForType(IconType.cities),
					settings.getFillWithColorForType(IconType.cities)
			));
		}
	}

	private static void relocateGeneratedCitiesToPlains(JSONObject brief, MapSettings settings, WorldGraph graph)
	{
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}

		Map<Integer, Center> centersByIndex = centersByIndex(graph);
		int minCityHopDistance = minCityHopDistance(brief);
		Set<Integer> occupied = new HashSet<>();
		List<FreeIcon> cityIcons = new ArrayList<>();
		for (FreeIcon icon : settings.edits.freeIcons)
		{
			if (icon != null && icon.centerIndex != null && Assets.customArtPack.equals(icon.artPack))
			{
				occupied.add(icon.centerIndex);
				continue;
			}
			if (icon != null && icon.type == IconType.cities && icon.centerIndex != null)
			{
				occupied.add(icon.centerIndex);
				if (!Assets.customArtPack.equals(icon.artPack))
				{
					cityIcons.add(icon);
				}
			}
		}
		for (Center center : graph.centers)
		{
			if (center != null && hasVisibleCityIcon(settings, center))
			{
				occupied.add(center.index);
			}
		}

		for (FreeIcon icon : cityIcons)
		{
			Center source = centersByIndex.get(icon.centerIndex);
			if (source == null || source.loc == null)
			{
				continue;
			}
			occupied.remove(source.index);
			if (isPlainBuildingCenter(source, settings) && isFarEnoughFromOccupiedBuildings(source, occupied, minCityHopDistance))
			{
				occupied.add(source.index);
				continue;
			}

			Center target = findNearestPlainBuildingCenter(source.loc, graph, settings, occupied, minCityHopDistance);
			if (target == null)
			{
				source.isCity = false;
				settings.edits.freeIcons.remove(icon);
				continue;
			}

			source.isCity = false;
			target.isCity = true;
			clearTerrainAtCenter(settings, target);
			settings.edits.freeIcons.replace(icon, moveIconToCenter(icon, target, settings));
			occupied.add(target.index);
		}
	}

	private static FreeIcon moveIconToCenter(FreeIcon icon, Center center, MapSettings settings)
	{
		double resolution = settings == null ? 1.0 : Math.max(0.0001, settings.resolution);
		return new FreeIcon(
				center.loc.mult(1.0 / resolution),
				icon.scale,
				icon.type,
				icon.artPack,
				icon.groupId,
				icon.iconIndex,
				icon.iconName,
				center.index,
				icon.density,
				icon.fillColor,
				icon.filterColor,
				icon.maximizeOpacity,
				icon.fillWithColor,
				icon.originalScale
		);
	}

	private static Map<Integer, Center> centersByIndex(WorldGraph graph)
	{
		Map<Integer, Center> centersByIndex = new HashMap<>();
		if (graph == null || graph.centers == null)
		{
			return centersByIndex;
		}
		for (Center center : graph.centers)
		{
			if (center != null)
			{
				centersByIndex.put(center.index, center);
			}
		}
		return centersByIndex;
	}

	private static Center findNearestPlainBuildingCenter(Point desired, WorldGraph graph, MapSettings settings, Set<Integer> occupied, int minHopDistance)
	{
		if (desired == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}

		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (!isPlainBuildingCenter(center, settings) || !isFarEnoughFromOccupiedBuildings(center, occupied, minHopDistance))
			{
				continue;
			}
			double score = center.loc.distanceTo(desired);
			if (center.isRiver())
			{
				score -= 85.0;
			}
			if (center.isCoast)
			{
				score += 300.0;
			}
			if (center.isBorder)
			{
				score += 600.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static boolean isFarEnoughFromOccupiedBuildings(Center center, Set<Integer> occupied, int minHopDistance)
	{
		if (center == null)
		{
			return false;
		}
		if (occupied == null || occupied.isEmpty())
		{
			return true;
		}
		if (occupied.contains(center.index))
		{
			return false;
		}
		if (minHopDistance <= 1)
		{
			return true;
		}
		return nearestOccupiedHopDistance(center, occupied, minHopDistance) >= minHopDistance;
	}

	private static int nearestOccupiedHopDistance(Center start, Set<Integer> occupied, int maxDistance)
	{
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(start);
		seen.add(start.index);
		for (int distance = 0; distance <= maxDistance && !frontier.isEmpty(); distance++)
		{
			List<Center> next = new ArrayList<>();
			for (Center center : frontier)
			{
				if (center != null && occupied.contains(center.index))
				{
					return distance;
				}
				if (center == null || center.neighbors == null)
				{
					continue;
				}
				for (Center neighbor : center.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return maxDistance + 1;
	}

	private static int minCityHopDistance(JSONObject brief)
	{
		return Math.max(0, intValue(brief, "minCityHopDistance", 5));
	}

	private static void normalizeCityFlagsToVisibleIcons(MapSettings settings, WorldGraph graph)
	{
		if (graph == null || graph.centers == null)
		{
			return;
		}
		for (Center center : graph.centers)
		{
			if (center == null)
			{
				continue;
			}
			center.isCity = hasVisibleCityIcon(settings, center);
		}
	}

	private static boolean hasVisibleCityIcon(MapSettings settings, Center center)
	{
		if (center == null || center.isWater)
		{
			return false;
		}
		if (settings == null || settings.edits == null)
		{
			return center.isCity;
		}
		if (settings.edits.freeIcons != null)
		{
			FreeIcon existing = settings.edits.freeIcons.getNonTree(center.index);
			if (existing != null && existing.type == IconType.cities)
			{
				return true;
			}
		}
		CenterEdit edit = settings.edits.centerEdits == null ? null : settings.edits.centerEdits.get(center.index);
		return edit != null && edit.icon != null && edit.icon.iconType == CenterIconType.City;
	}

	private static boolean isBuildingOccupied(Center center, Set<Integer> occupied, boolean includeNeighbors)
	{
		if (center == null || occupied == null || occupied.isEmpty())
		{
			return false;
		}
		if (occupied.contains(center.index))
		{
			return true;
		}
		if (!includeNeighbors || center.neighbors == null)
		{
			return false;
		}
		for (Center neighbor : center.neighbors)
		{
			if (neighbor != null && occupied.contains(neighbor.index))
			{
				return true;
			}
		}
		return false;
	}

	private static boolean isPlainBuildingCenter(Center center, MapSettings settings)
	{
		return isPlainBuildingCenter(center, settings, 2);
	}

	private static boolean isPlainBuildingCenter(Center center, MapSettings settings, int waterBufferHops)
	{
		if (center == null
				|| center.loc == null
				|| center.isWater
				|| center.isLake
				|| center.isCoast
				|| center.isBorder
				|| center.isMountain
				|| center.isHill
				|| drawsTerrainIcons(center))
		{
			return false;
		}
		if (waterBufferHops > 0 && hasAnyWaterWithinHops(center, waterBufferHops))
		{
			return false;
		}
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null)
		{
			return true;
		}
		if (settings.edits.freeIcons.hasTrees(center.index))
		{
			return false;
		}
		FreeIcon existing = settings.edits.freeIcons.getNonTree(center.index);
		return existing == null || existing.type == IconType.cities;
	}

	private static void clearTerrainAtCenter(MapSettings settings, Center center)
	{
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null || center == null)
		{
			return;
		}
		settings.edits.freeIcons.clearTrees(center.index);
		FreeIcon existing = settings.edits.freeIcons.getNonTree(center.index);
		if (existing != null && isTerrainIcon(existing.type))
		{
			settings.edits.freeIcons.remove(existing);
		}
	}

	private static void clearCustomIconUnderlay(MapSettings settings, Center center, JSONObject label)
	{
		if (center == null || center.isWater)
		{
			return;
		}
		if (isSkyIslandCustomIcon(label))
		{
			for (Center nearby : centersWithinHops(center, 2))
			{
				clearMapIconAtCenter(settings, nearby, true);
			}
			return;
		}
		clearTerrainAtCenter(settings, center);
	}

	private static List<Center> centersWithinHops(Center center, int hops)
	{
		List<Center> result = new ArrayList<>();
		if (center == null)
		{
			return result;
		}
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(center);
		seen.add(center.index);
		for (int step = 0; step <= hops && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				result.add(current);
				if (current.neighbors == null || step == hops)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return result;
	}

	private static void clearMapIconAtCenter(MapSettings settings, Center center, boolean clearBuildings)
	{
		if (settings == null || settings.edits == null || settings.edits.freeIcons == null || center == null)
		{
			return;
		}
		settings.edits.freeIcons.clearTrees(center.index);
		FreeIcon existing = settings.edits.freeIcons.getNonTree(center.index);
		if (existing != null && (isTerrainIcon(existing.type) || (clearBuildings && (existing.type == IconType.cities || existing.type == IconType.decorations))))
		{
			settings.edits.freeIcons.remove(existing);
		}
		CenterEdit edit = settings.edits.centerEdits == null ? null : settings.edits.centerEdits.get(center.index);
		if (clearBuildings && edit != null && edit.icon != null)
		{
			settings.edits.centerEdits.put(center.index, edit.copyWithIcon(null));
		}
	}

	private static Color customIconFillColor(JSONObject label, Center center, MapSettings settings, WorldGraph graph)
	{
		String placement = stringValue(label, "iconPlacement", "land");
		if ("ocean".equals(placement))
		{
			return Color.transparentBlack;
		}
		if ("island".equals(placement))
		{
			return withAlpha(semanticIslandColor(), 225);
		}
		Color regionColor = dominantFootprintRegionColor(label, center, settings, graph);
		if (regionColor == null)
		{
			regionColor = regionColorAt(center, settings);
		}
		if (regionColor == null)
		{
			regionColor = settings == null ? null : settings.landColor;
		}
		if (regionColor == null)
		{
			return Color.transparentBlack;
		}
		return withAlpha(regionColor, 215);
	}

	private static HSBColor customIconFilterColor(JSONObject label, Color fillColor)
	{
		if ("ocean".equals(stringValue(label, "iconPlacement", "land")) || fillColor == null || fillColor.getAlpha() == 0)
		{
			return MapSettings.defaultIconFilterColor;
		}
		Color inkColor = darkenColor(fillColor, 0.32);
		float[] hsb = inkColor.getHSB();
		return new HSBColor(
				(int) Math.round(hsb[0] * 360.0),
				(int) Math.round(hsb[1] * 100.0),
				(int) Math.round(hsb[2] * 100.0),
				0
		);
	}

	private static boolean customIconShouldFillWithColor(JSONObject label, Color fillColor)
	{
		if (fillColor == null || fillColor.getAlpha() == 0)
		{
			return false;
		}
		return "island".equals(stringValue(label, "iconPlacement", "land"));
	}

	private static Color regionColorAt(Center center, MapSettings settings)
	{
		Integer regionId = regionIdAt(center, settings);
		return regionColorById(regionId, settings);
	}

	private static Color dominantFootprintRegionColor(JSONObject label, Center center, MapSettings settings, WorldGraph graph)
	{
		if (center == null || settings == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}
		LabelBounds bounds = customIconBoundsAt(label, center, settings);
		double height = Math.max(1.0, bounds.bottom - bounds.top);
		double sampleTop = "ground".equals(stringValue(label, "iconAnchorMode", "ground"))
				? bounds.bottom - (height * 0.32)
				: bounds.top;
		Map<Integer, Double> weightsByRegion = new HashMap<>();
		for (Center candidate : graph.centers)
		{
			if (candidate == null || candidate.loc == null || candidate.isWater || candidate.isLake)
			{
				continue;
			}
			Point point = fromGraphPoint(candidate.loc, settings);
			if (point.x < bounds.left || point.x > bounds.right || point.y < sampleTop || point.y > bounds.bottom)
			{
				continue;
			}
			Integer regionId = regionIdAt(candidate, settings);
			if (regionId == null)
			{
				continue;
			}
			double distance = Math.max(1.0, point.distanceTo(fromGraphPoint(center.loc, settings)));
			double weight = 1.0 / distance;
			weightsByRegion.put(regionId, weightsByRegion.getOrDefault(regionId, 0.0) + weight);
		}
		Integer bestRegion = null;
		double bestWeight = Double.NEGATIVE_INFINITY;
		for (Map.Entry<Integer, Double> entry : weightsByRegion.entrySet())
		{
			if (entry.getValue() > bestWeight)
			{
				bestWeight = entry.getValue();
				bestRegion = entry.getKey();
			}
		}
		return regionColorById(bestRegion, settings);
	}

	private static Integer regionIdAt(Center center, MapSettings settings)
	{
		if (center == null || settings == null || settings.edits == null)
		{
			return null;
		}
		Integer regionId = null;
		CenterEdit edit = settings.edits.centerEdits == null ? null : settings.edits.centerEdits.get(center.index);
		if (edit != null)
		{
			regionId = edit.regionId;
		}
		if (regionId == null && center.region != null)
		{
			regionId = center.region.id;
		}
		if (regionId == null)
		{
			return null;
		}
		return regionId;
	}

	private static Color regionColorById(Integer regionId, MapSettings settings)
	{
		if (regionId == null || settings == null || settings.edits == null || settings.edits.regionEdits == null)
		{
			return null;
		}
		RegionEdit regionEdit = settings.edits.regionEdits.get(regionId);
		return regionEdit == null ? null : regionEdit.color;
	}

	private static Color withAlpha(Color color, int alpha)
	{
		return Color.create(color.getRed(), color.getGreen(), color.getBlue(), Math.max(0, Math.min(255, alpha)));
	}

	private static Color darkenColor(Color color, double factor)
	{
		double clamped = Math.max(0.0, Math.min(1.0, factor));
		return Color.create(
				clampColor(color.getRed() * clamped),
				clampColor(color.getGreen() * clamped),
				clampColor(color.getBlue() * clamped),
				color.getAlpha()
		);
	}

	private static int clampColor(double value)
	{
		return Math.max(0, Math.min(255, (int) Math.round(value)));
	}

	private void applyGeneratedCityNames(JSONObject brief, MapSettings settings, WorldGraph graph, Map<String, LocationAnchor> locationAnchorsByName)
	{
		JSONArray namesJson = arrayValue(brief, "generatedNamePool");
		if (namesJson == null || namesJson.isEmpty() || graph == null || graph.centers == null)
		{
			return;
		}

		List<String> names = new ArrayList<>();
		for (Object obj : namesJson)
		{
			String name = String.valueOf(obj == null ? "" : obj).strip();
			if (!name.isBlank())
			{
				names.add(name);
			}
		}
		if (names.isEmpty())
		{
			return;
		}

		Set<Integer> explicitCenters = new HashSet<>();
		List<Center> explicitAnchorCenters = new ArrayList<>();
		for (LocationAnchor anchor : locationAnchorsByName.values())
		{
			if (anchor.iconCenter != null)
			{
				explicitCenters.add(anchor.iconCenter.index);
				explicitAnchorCenters.add(anchor.iconCenter);
			}
		}

		int index = 0;
		for (Center center : graph.centers)
		{
			if (index >= names.size())
			{
				break;
			}
			if (center == null || !center.isCity || center.isWater || explicitCenters.contains(center.index) || isNearAnyCenter(center, explicitAnchorCenters, 110.0))
			{
				continue;
			}
			if (!hasVisibleCityIcon(settings, center))
			{
				continue;
			}
			String name = names.get(index++);
			Point labelPoint = fromGraphPoint(center.loc, settings);
			settings.edits.text.add(new MapText(name, new Point(labelPoint.x, labelPoint.y + 18.0), 0.0, TextType.City, LineBreak.Auto, null, null, 0.0, 0, null, MapText.defaultBackgroundFade));
		}
	}

	private static boolean isNearAnyCenter(Center center, List<Center> others, double maxDistance)
	{
		if (center == null || center.loc == null || others == null)
		{
			return false;
		}
		for (Center other : others)
		{
			if (other != null && other.loc != null && center.loc.distanceTo(other.loc) < maxDistance)
			{
				return true;
			}
		}
		return false;
	}

	private static Set<String> routePointNames(JSONObject brief)
	{
		Set<String> names = new HashSet<>();
		JSONArray roads = arrayValue(brief, "roads");
		if (roads == null)
		{
			return names;
		}
		for (Object roadObj : roads)
		{
			if (!(roadObj instanceof JSONObject road))
			{
				continue;
			}
			JSONArray path = arrayValue(road, "path");
			if (path == null)
			{
				continue;
			}
			for (Object pointObj : path)
			{
				if (pointObj instanceof JSONObject point)
				{
					String name = stringValue(point, "name", "");
					if (!name.isBlank())
					{
						names.add(name);
					}
				}
			}
		}
		return names;
	}

	private static boolean needsBuildingIcon(TextType type, JSONObject label, Set<String> routePointNames)
	{
		boolean drawIcon = boolValue(label, "drawIcon", type == TextType.City);
		if (!drawIcon)
		{
			return false;
		}
		if (type != TextType.City && isNaturalMapFeature(label))
		{
			return false;
		}
		String text = stringValue(label, "text", "");
		return type == TextType.City || (routePointNames != null && routePointNames.contains(text));
	}

	private static boolean isNaturalMapFeature(JSONObject label)
	{
		String preference = locationPreference(label);
		return "lake".equals(preference)
				|| "lake_shore".equals(preference)
				|| "forest".equals(preference)
				|| "mountain".equals(preference)
				|| "hill".equals(preference)
				|| "ocean".equals(preference);
	}

	private static String cityIconName(JSONObject label)
	{
		String text = (
				stringValue(label, "text", "") + " "
						+ stringValue(label, "terrain", "") + " "
						+ stringValue(label, "tags", "")
		).toLowerCase();
		if (containsAny(text, "农场", "牧场", "farm", "ranch"))
		{
			return "small farm";
		}
		if (containsAny(text, "村", "village"))
		{
			return "small village";
		}
		if (containsAny(text, "堡", "要塞", "城堡", "王都", "旧都", "首都", "walled", "fort", "castle", "citadel"))
		{
			return "walled city";
		}
		return "town";
	}

	private void applyPoliticalRegions(JSONObject brief, MapSettings settings, WorldGraph graph, Map<String, LocationAnchor> locationAnchorsByName)
	{
		JSONArray regions = arrayValue(brief, "politicalRegions");
		if (regions == null || regions.isEmpty() || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}

		List<PoliticalRegionAnchor> anchors = new ArrayList<>();
		for (int index = 0; index < regions.size(); index++)
		{
			Object obj = regions.get(index);
			if (!(obj instanceof JSONObject region))
			{
				continue;
			}
			JSONArray anchorJson = arrayValue(region, "anchors");
			if (anchorJson == null || anchorJson.isEmpty())
			{
				continue;
			}
			int regionId = 9000 + index;
			Color color = colorValue(region, "color", settings.regionBaseColor);
			settings.edits.regionEdits.put(regionId, new RegionEdit(regionId, color));

			for (Object anchorObj : anchorJson)
			{
				if (!(anchorObj instanceof JSONObject anchor))
				{
					continue;
				}
				Point point = null;
				String name = stringValue(anchor, "name", "");
				if (!name.isBlank())
				{
					LocationAnchor locationAnchor = locationAnchorsByName.get(name);
					if (locationAnchor != null)
					{
						Center center = locationAnchor.customIconCenter != null
								? locationAnchor.customIconCenter
								: locationAnchor.iconCenter != null ? locationAnchor.iconCenter : locationAnchor.labelCenter;
						if (center != null)
						{
							point = fromGraphPoint(center.loc, settings);
						}
					}
				}
				if (point == null)
				{
					point = snapPointToGraph(anchor, pointValue(anchor, settings), settings, graph);
				}
				anchors.add(new PoliticalRegionAnchor(regionId, toGraphPoint(point, settings)));
			}
		}
		if (anchors.isEmpty())
		{
			return;
		}

		settings.drawRegionColors = true;
		for (Center center : graph.centers)
		{
			if (center == null || center.loc == null || center.isWater || center.isLake)
			{
				continue;
			}
			PoliticalRegionAnchor closest = closestAnchor(center.loc, anchors);
			if (closest == null)
			{
				continue;
			}
			CenterEdit edit = settings.edits.centerEdits.get(center.index);
			if (edit == null)
			{
				edit = new CenterEdit(center.index, center.isWater, center.isLake, null, null, null);
			}
			else if (edit.regionId != null && edit.regionId == FU_GM_SEMANTIC_ISLAND_REGION_ID)
			{
				continue;
			}
			settings.edits.centerEdits.put(center.index, edit.copyWithRegionId(closest.regionId));
		}
	}

	private static PoliticalRegionAnchor closestAnchor(Point point, List<PoliticalRegionAnchor> anchors)
	{
		PoliticalRegionAnchor closest = null;
		double bestDistance = Double.POSITIVE_INFINITY;
		for (PoliticalRegionAnchor anchor : anchors)
		{
			double distance = point.distanceTo(anchor.point);
			if (distance < bestDistance)
			{
				bestDistance = distance;
				closest = anchor;
			}
		}
		return closest;
	}

	private void applyRoads(JSONObject brief, MapSettings settings, WorldGraph graph, Map<String, LocationAnchor> locationAnchorsByName)
	{
		JSONArray roads = arrayValue(brief, "roads");
		if (roads == null || graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}

		settings.drawRoads = true;
		Set<Edge> edgesAddedRoadsFor = new HashSet<>();
		for (Object obj : roads)
		{
			if (!(obj instanceof JSONObject road))
			{
				continue;
			}

			JSONArray pathJson = arrayValue(road, "path");
			if (pathJson == null || pathJson.size() < 2)
			{
				continue;
			}
			String routeId = stringValue(road, "route_id", "");

			List<Center> centers = new ArrayList<>();
			for (Object pointObj : pathJson)
			{
				if (pointObj instanceof JSONObject pointJson)
				{
					Center center = roadCenter(pointJson, settings, graph, locationAnchorsByName);
					if (center != null && (centers.isEmpty() || centers.get(centers.size() - 1) != center))
					{
						centers.add(center);
					}
				}
			}

			for (int i = 0; i < centers.size() - 1; i++)
			{
				List<Edge> edges = findRoadEdges(graph, centers.get(i), centers.get(i + 1), settings, edgesAddedRoadsFor);
				if (edges != null && !edges.isEmpty())
				{
					RoadDrawer.addRoadsFromEdgesInEditor(edges, graph, settings.edits.roads, settings.resolution);
					edgesAddedRoadsFor.addAll(edges);
				}
				else
				{
					System.err.println("Skipping road segment without a land path"
							+ (routeId.isBlank() ? "" : " for route " + routeId)
							+ ": " + centers.get(i).index + " -> " + centers.get(i + 1).index);
				}
			}
		}
	}

	private void applyGeneratedCityRoads(MapSettings settings, WorldGraph graph)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return;
		}
		int cityCount = 0;
		for (Center center : graph.centers)
		{
			if (center != null && center.isCity && !center.isWater && hasVisibleCityIcon(settings, center))
			{
				cityCount++;
			}
		}
		if (cityCount < 2)
		{
			return;
		}
		settings.drawRoads = true;
		new RoadDrawer(new Random(settings.randomSeed), settings, graph).createRoads();
	}

	private static Center roadCenter(JSONObject pointJson, MapSettings settings, WorldGraph graph, Map<String, LocationAnchor> locationAnchorsByName)
	{
		String name = stringValue(pointJson, "name", "");
		if (!name.isBlank())
		{
			LocationAnchor anchor = locationAnchorsByName.get(name);
			if (anchor != null && anchor.customIconCenter != null && !anchor.customIconCenter.isWater)
			{
				return anchor.customIconCenter;
			}
			if (anchor != null && anchor.iconCenter != null)
			{
				return anchor.iconCenter;
			}
		}
		return routeCenterForHint(pointJson, pointValue(pointJson, settings), settings, graph);
	}

	private static List<Edge> findRoadEdges(WorldGraph graph, Center start, Center end, MapSettings settings, Set<Edge> edgesAddedRoadsFor)
	{
		if (start == null || end == null || start.isWater || end.isWater)
		{
			return null;
		}
		return graph.findShortestPath(start, end, (edge, center, distanceToEnd) -> roadWeight(edge, center, distanceToEnd, settings, edgesAddedRoadsFor));
	}

	private static double roadWeight(Edge edge, Center center, double distanceToEnd, MapSettings settings, Set<Edge> edgesAddedRoadsFor)
	{
		if (center == null || center.isWater)
		{
			return Double.POSITIVE_INFINITY;
		}

		double terrainPenalty;
		if (center.isMountain)
		{
			terrainPenalty = 5.0;
		}
		else if (center.isHill)
		{
			terrainPenalty = 1.5;
		}
		else if (center.biome != null && center.biome.name().contains("DESERT"))
		{
			terrainPenalty = 4.0;
		}
		else
		{
			terrainPenalty = 1.0;
		}

		double distanceNormalized = Center.distanceBetween(edge.d0, edge.d1) * (1.0 / settings.resolution);
		return (distanceNormalized * terrainPenalty + distanceToEnd) * (edgesAddedRoadsFor.contains(edge) ? 0.3 : 1.0);
	}

	private static Point snapPointToGraph(JSONObject hint, Point desired, MapSettings settings, WorldGraph graph)
	{
		Center center = snapCenterToGraph(hint, desired, settings, graph, null);
		return center == null ? desired : fromGraphPoint(center.loc, settings);
	}

	private static Center snapCenterToGraph(JSONObject hint, Point desired, MapSettings settings, WorldGraph graph)
	{
		return snapCenterToGraph(hint, desired, settings, graph, null);
	}

	private static Center snapCenterToGraph(JSONObject hint, Point desired, MapSettings settings, WorldGraph graph, Set<Center> preferredCenters)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}
		Point desiredOnGraph = toGraphPoint(desired, settings);
		String preference = locationPreference(hint);
		if ("inland_sea".equals(preference))
		{
			Center existingSea = findBestInlandSeaLabelCenter(hint, desiredOnGraph, graph, settings, preferredCenters);
			if (existingSea != null && existingSea.loc.distanceTo(desiredOnGraph) <= semanticLakeReuseDistance(graph, settings))
			{
				return existingSea;
			}
			Center createdAnchor = createSemanticInlandSeaNear(desiredOnGraph, graph, settings);
			if (createdAnchor != null)
			{
				Center createdSea = findBestInlandSeaLabelCenter(hint, desiredOnGraph, graph, settings, preferredCenters);
				if (createdSea != null)
				{
					return createdSea;
				}
				return createdAnchor;
			}
			if (existingSea != null)
			{
				return existingSea;
			}
		}
		if ("lake".equals(preference))
		{
			Center existingLake = findBestCenterWithPreference(desiredOnGraph, graph, "lake", true, preferredCenters);
			double reuseDistance = semanticLakeReuseDistance(graph, settings);
			if (existingLake != null && existingLake.loc.distanceTo(desiredOnGraph) <= reuseDistance)
			{
				return existingLake;
			}
			Center createdLake = createSemanticLakeNear(desiredOnGraph, graph, settings, preferredCenters);
			if (createdLake != null)
			{
				return createdLake;
			}
			if (existingLake != null)
			{
				return existingLake;
			}
		}
		Center best = findBestCenter(hint, desiredOnGraph, graph, true, preferredCenters);
		if (best == null)
		{
			best = findBestCenter(hint, desiredOnGraph, graph, false, preferredCenters);
		}
		if (best == null && preferredCenters != null && !preferredCenters.isEmpty())
		{
			best = findBestCenter(hint, desiredOnGraph, graph, true, null);
		}
		if (best == null && preferredCenters != null && !preferredCenters.isEmpty())
		{
			best = findBestCenter(hint, desiredOnGraph, graph, false, null);
		}
		return best;
	}

	private static Center findBestInlandSeaLabelCenter(JSONObject hint, Point desired, WorldGraph graph, MapSettings settings, Set<Center> preferredCenters)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}
		double localRadius = semanticLakeReuseDistance(graph, settings) * 1.35;
		List<Center> candidates = new ArrayList<>();
		double centroidX = 0.0;
		double centroidY = 0.0;
		for (Center center : graph.centers)
		{
			if (center == null
					|| center.loc == null
					|| center.isBorder
					|| !center.isWater
					|| center.isLake
					|| !isInPreferredArea(center, preferredCenters)
					|| center.loc.distanceTo(desired) > localRadius
					|| !hasLandWithinHops(center, 8))
			{
				continue;
			}
			candidates.add(center);
			centroidX += center.loc.x;
			centroidY += center.loc.y;
		}
		Point centroid = desired;
		if (!candidates.isEmpty())
		{
			centroid = new Point(centroidX / candidates.size(), centroidY / candidates.size());
		}
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : candidates)
		{
			double distance = center.loc.distanceTo(desired);
			double centroidDistance = center.loc.distanceTo(centroid);
			int nearbyWater = countWaterWithinHops(center, 4);
			int waterDirections = waterDirectionCount(center, 4);
			int immediateLand = countLandWithinHops(center, 1);
			int nearLand = countLandWithinHops(center, 2);
			int enclosingLand = countLandWithinHops(center, 5);
			LabelBounds textBounds = estimatedInlandSeaLabelBounds(hint, center, settings).expand(18.0);
			int waterUnderLabel = countCentersInMapBounds(graph, textBounds, settings, true);
			int landUnderLabel = countCentersInMapBounds(graph, textBounds, settings, false);
			double score = centroidDistance * 0.38 + distance * 0.95;
			score -= Math.min(nearbyWater, 28) * 14.0;
			score -= waterDirections * 210.0;
			score -= Math.min(enclosingLand, 24) * 42.0;
			score -= waterUnderLabel * 10.0;
			score += center.isCoast ? 900.0 : 0.0;
			score += immediateLand * 260.0;
			score += nearLand * 52.0;
			score += landUnderLabel * 620.0;
			if (enclosingLand < 5)
			{
				score += 1800.0;
			}
			if (landUnderLabel > 0)
			{
				score += 2400.0;
			}
			if (waterDirections < 3)
			{
				score += 900.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		if (best != null)
		{
			return best;
		}
		return findBestCenterWithPreference(desired, graph, "inland_sea", true, preferredCenters);
	}

	private static double semanticLakeReuseDistance(WorldGraph graph, MapSettings settings)
	{
		return Math.max(240.0, Math.min(graphWidth(graph, settings), graphHeight(graph, settings)) * 0.16);
	}

	private static Center createSemanticLakeNear(Point desired, WorldGraph graph, MapSettings settings, Set<Center> preferredCenters)
	{
		Center seed = findBestSemanticLakeSeedCenter(desired, graph, settings, preferredCenters, false);
		if (seed == null)
		{
			seed = findBestSemanticLakeSeedCenter(desired, graph, settings, preferredCenters, true);
		}
		if (seed == null && preferredCenters != null && !preferredCenters.isEmpty())
		{
			seed = findBestSemanticLakeSeedCenter(desired, graph, settings, null, false);
		}
		if (seed == null && preferredCenters != null && !preferredCenters.isEmpty())
		{
			seed = findBestSemanticLakeSeedCenter(desired, graph, settings, null, true);
		}
		if (seed == null)
		{
			return null;
		}

		List<Center> lakeCenters = nearbySemanticLakeCenters(seed, 1, 6, false);
		if (lakeCenters.size() < 3)
		{
			lakeCenters = nearbySemanticLakeCenters(seed, 2, 7, true);
		}
		if (lakeCenters.isEmpty())
		{
			lakeCenters = List.of(seed);
		}

		for (Center center : lakeCenters)
		{
			convertCenterToSemanticLake(settings, center);
		}
		graph.updateCoastAndCornerFlags();
		return seed;
	}

	private static Center findBestSemanticLakeSeedCenter(Point desired, WorldGraph graph, MapSettings settings, Set<Center> preferredCenters, boolean allowRugged)
	{
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (!isSemanticLakeSeedCenter(center, settings, allowRugged) || !isInPreferredArea(center, preferredCenters))
			{
				continue;
			}
			double score = center.loc.distanceTo(desired);
			if (center.isRiver())
			{
				score -= 80.0;
			}
			if (center.isCoast)
			{
				score += 900.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static boolean isSemanticLakeSeedCenter(Center center, MapSettings settings, boolean allowRugged)
	{
		if (center == null
				|| center.loc == null
				|| center.isWater
				|| center.isLake
				|| center.isCoast
				|| center.isBorder
				|| center.isCity
				|| (!allowRugged && (center.isMountain || center.isHill)))
		{
			return false;
		}
		if (settings != null && settings.edits != null && settings.edits.freeIcons != null)
		{
			FreeIcon icon = settings.edits.freeIcons.getNonTree(center.index);
			if (icon != null && icon.type == IconType.cities)
			{
				return false;
			}
		}
		return true;
	}

	private static List<Center> nearbySemanticLakeCenters(Center anchor, int radius, int limit, boolean allowRugged)
	{
		List<Center> result = new ArrayList<>();
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(anchor);
		seen.add(anchor.index);

		for (int step = 0; step <= radius && !frontier.isEmpty() && result.size() < limit; step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center center : frontier)
			{
				if (isSemanticLakePatchCenter(center, allowRugged))
				{
					result.add(center);
					if (result.size() >= limit)
					{
						break;
					}
				}
				if (center.neighbors == null)
				{
					continue;
				}
				for (Center neighbor : center.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return result;
	}

	private static boolean isSemanticLakePatchCenter(Center center, boolean allowRugged)
	{
		return center != null
				&& center.loc != null
				&& !center.isWater
				&& !center.isLake
				&& !center.isCoast
				&& !center.isBorder
				&& !center.isCity
				&& (allowRugged || (!center.isMountain && !center.isHill));
	}

	private static void convertCenterToSemanticLake(MapSettings settings, Center center)
	{
		if (center == null)
		{
			return;
		}
		clearTerrainAtCenter(settings, center);
		center.isWater = true;
		center.isLake = true;
		center.isCity = false;
		center.isMountain = false;
		center.isHill = false;
		if (center.region != null)
		{
			center.region.remove(center);
			center.region = null;
		}
		if (settings != null && settings.edits != null && settings.edits.centerEdits != null)
		{
			settings.edits.centerEdits.put(center.index, new CenterEdit(center.index, true, true, null, null, null));
		}
	}

	private static void convertCenterToSemanticSea(MapSettings settings, Center center)
	{
		if (center == null)
		{
			return;
		}
		clearTerrainAtCenter(settings, center);
		center.isWater = true;
		center.isLake = false;
		center.isCity = false;
		center.isMountain = false;
		center.isHill = false;
		if (center.region != null)
		{
			center.region.remove(center);
			center.region = null;
		}
		if (settings != null && settings.edits != null && settings.edits.centerEdits != null)
		{
			settings.edits.centerEdits.put(center.index, new CenterEdit(center.index, true, false, null, null, null));
		}
	}

	private static Center iconCenterForLabel(JSONObject hint, Center labelCenter, Point labelPoint, MapSettings settings, WorldGraph graph)
	{
		return iconCenterForLabel(hint, labelCenter, labelPoint, settings, graph, null);
	}

	private static Center iconCenterForLabel(JSONObject hint, Center labelCenter, Point labelPoint, MapSettings settings, WorldGraph graph, Set<Center> preferredCenters)
	{
		if (labelCenter == null)
		{
			return routeCenterForHint(hint, labelPoint, settings, graph, preferredCenters);
		}
		if (!labelCenter.isWater)
		{
			return labelCenter;
		}
		String preference = locationPreference(hint);
		String routePreference = "lake".equals(preference) ? "lake_shore" : "coast";
		Center routeCenter = findBestCenterWithPreference(toGraphPoint(labelPoint, settings), graph, routePreference, true, preferredCenters);
		if (routeCenter == null)
		{
			routeCenter = findBestCenterWithPreference(toGraphPoint(labelPoint, settings), graph, "land", true, preferredCenters);
		}
		if (routeCenter == null && preferredCenters != null && !preferredCenters.isEmpty())
		{
			routeCenter = findBestCenterWithPreference(toGraphPoint(labelPoint, settings), graph, routePreference, true, null);
		}
		if (routeCenter == null && preferredCenters != null && !preferredCenters.isEmpty())
		{
			routeCenter = findBestCenterWithPreference(toGraphPoint(labelPoint, settings), graph, "land", true, null);
		}
		return routeCenter;
	}

	private static Center routeCenterForHint(JSONObject hint, Point desired, MapSettings settings, WorldGraph graph)
	{
		return routeCenterForHint(hint, desired, settings, graph, null);
	}

	private static Center routeCenterForHint(JSONObject hint, Point desired, MapSettings settings, WorldGraph graph, Set<Center> preferredCenters)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}
		Point desiredOnGraph = toGraphPoint(desired, settings);
		String preference = locationPreference(hint);
		String routePreference;
		if ("coast".equals(preference) || "ocean".equals(preference) || "inland_sea".equals(preference))
		{
			routePreference = "coast";
		}
		else if ("lake".equals(preference) || "lake_shore".equals(preference))
		{
			routePreference = "lake_shore";
		}
		else
		{
			routePreference = preference;
		}

		Center best = findBestCenterWithPreference(desiredOnGraph, graph, routePreference, true, preferredCenters);
		if (best == null || best.isWater)
		{
			best = findBestCenterWithPreference(desiredOnGraph, graph, "land", true, preferredCenters);
		}
		if ((best == null || best.isWater) && preferredCenters != null && !preferredCenters.isEmpty())
		{
			best = findBestCenterWithPreference(desiredOnGraph, graph, routePreference, true, null);
		}
		if ((best == null || best.isWater) && preferredCenters != null && !preferredCenters.isEmpty())
		{
			best = findBestCenterWithPreference(desiredOnGraph, graph, "land", true, null);
		}
		return best;
	}

	private static Point toGraphPoint(Point point, MapSettings settings)
	{
		return point.mult(settings.resolution, settings.resolution);
	}

	private static Point fromGraphPoint(Point point, MapSettings settings)
	{
		return new Point(point.x / settings.resolution, point.y / settings.resolution);
	}

	private static double graphWidth(WorldGraph graph, MapSettings settings)
	{
		if (graph != null && graph.getWidth() > 0)
		{
			return graph.getWidth();
		}
		if (settings != null)
		{
			return Math.max(1.0, settings.generatedWidth * settings.resolution);
		}
		return 1.0;
	}

	private static double graphHeight(WorldGraph graph, MapSettings settings)
	{
		if (graph != null && graph.getHeight() > 0)
		{
			return graph.getHeight();
		}
		if (settings != null)
		{
			return Math.max(1.0, settings.generatedHeight * settings.resolution);
		}
		return 1.0;
	}

	private static double iconSpaceWidth(WorldGraph graph, MapSettings settings)
	{
		double resolution = settings == null ? 1.0 : Math.max(0.0001, settings.resolution);
		return Math.max(1.0, graphWidth(graph, settings) / resolution);
	}

	private static double iconSpaceHeight(WorldGraph graph, MapSettings settings)
	{
		double resolution = settings == null ? 1.0 : Math.max(0.0001, settings.resolution);
		return Math.max(1.0, graphHeight(graph, settings) / resolution);
	}

	private static Center findBestCenter(JSONObject hint, Point desired, WorldGraph graph, boolean enforcePreference)
	{
		return findBestCenter(hint, desired, graph, enforcePreference, null);
	}

	private static Center findBestCenter(JSONObject hint, Point desired, WorldGraph graph, boolean enforcePreference, Set<Center> preferredCenters)
	{
		return findBestCenterWithPreference(desired, graph, locationPreference(hint), enforcePreference, preferredCenters);
	}

	private static Center findBestCenterWithPreference(Point desired, WorldGraph graph, String preference, boolean enforcePreference)
	{
		return findBestCenterWithPreference(desired, graph, preference, enforcePreference, null);
	}

	private static Center findBestCenterWithPreference(Point desired, WorldGraph graph, String preference, boolean enforcePreference, Set<Center> preferredCenters)
	{
		preference = normalizedSearchPreference(preference);
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (center == null
					|| center.loc == null
					|| !isEligibleCenter(center, preference, enforcePreference)
					|| !isInPreferredArea(center, preferredCenters))
			{
				continue;
			}
			double score = center.loc.distanceTo(desired) + preferencePenalty(center, preference);
			if (center.isBorder)
			{
				score += 400.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static Center findBestOceanTitleCenter(Point desired, WorldGraph graph, String title, MapSettings settings)
	{
		if (graph == null || graph.centers == null || graph.centers.isEmpty())
		{
			return null;
		}
		Center best = null;
		double bestScore = Double.POSITIVE_INFINITY;
		double titleClearance = estimatedTitleClearance(title, settings);
		for (Center center : graph.centers)
		{
			if (center == null || center.loc == null || !center.isWater || center.isLake)
			{
				continue;
			}
			double distanceToLand = nearestLandDistance(center, graph);
			double score = center.loc.distanceTo(desired) - Math.min(distanceToLand, 1400.0) * 1.2;
			score += titleBorderPenalty(center, settings, titleClearance);
			if (distanceToLand < titleClearance)
			{
				score += (titleClearance - distanceToLand) * 10.0;
			}
			if (center.isBorder)
			{
				score += 300.0;
			}
			if (score < bestScore)
			{
				bestScore = score;
				best = center;
			}
		}
		return best;
	}

	private static double titleBorderPenalty(Center center, MapSettings settings, double titleClearance)
	{
		if (center == null || center.loc == null || settings == null)
		{
			return 0.0;
		}
		double mapWidth = Math.max(1.0, settings.generatedWidth * settings.resolution);
		double mapHeight = Math.max(1.0, settings.generatedHeight * settings.resolution);
		double horizontalGap = Math.min(center.loc.x, mapWidth - center.loc.x);
		double verticalGap = Math.min(center.loc.y, mapHeight - center.loc.y);
		double requiredHorizontal = Math.max(mapWidth * 0.10, titleClearance * 0.85);
		double requiredVertical = Math.max(mapHeight * 0.12, titleClearance * 0.45);
		double penalty = 0.0;
		if (horizontalGap < requiredHorizontal)
		{
			penalty += (requiredHorizontal - horizontalGap) * 14.0;
		}
		if (verticalGap < requiredVertical)
		{
			penalty += (requiredVertical - verticalGap) * 14.0;
		}
		return penalty;
	}

	private static double estimatedTitleClearance(String title, MapSettings settings)
	{
		int length = title == null ? 4 : Math.max(4, title.strip().length());
		double fontSize = settings == null || settings.titleFont == null ? 50.0 : settings.titleFont.getSize();
		return Math.max(260.0, length * fontSize * 1.25);
	}

	private static double nearestLandDistance(Center oceanCenter, WorldGraph graph)
	{
		double best = Double.POSITIVE_INFINITY;
		for (Center center : graph.centers)
		{
			if (isLandCenter(center))
			{
				best = Math.min(best, oceanCenter.loc.distanceTo(center.loc));
			}
		}
		return best;
	}

	private static boolean isEligibleCenter(Center center, String preference, boolean enforcePreference)
	{
		if ("ocean".equals(preference))
		{
			return !enforcePreference || (center.isWater && !center.isLake);
		}
		if ("inland_sea".equals(preference))
		{
			return !enforcePreference || (center.isWater && !center.isLake);
		}
		if ("lake".equals(preference))
		{
			return !enforcePreference || center.isLake;
		}
		if ("lake_shore".equals(preference))
		{
			return !enforcePreference || (!center.isWater && isLakeShore(center));
		}
		if ("forest".equals(preference))
		{
			return !center.isWater && (!enforcePreference || (!center.isCoast && isForest(center)));
		}
		if ("mountain".equals(preference))
		{
			return !center.isWater && (!enforcePreference || (!center.isCoast && (center.isMountain || center.isHill)));
		}
		if ("sky_island".equals(preference))
		{
			return !center.isWater && !center.isLake && (!enforcePreference || !center.isCoast);
		}
		if ("hill".equals(preference))
		{
			return !center.isWater && (!enforcePreference || !center.isCoast);
		}
		return !enforcePreference || !center.isWater;
	}

	private static boolean isInPreferredArea(Center center, Set<Center> preferredCenters)
	{
		if (center == null || preferredCenters == null || preferredCenters.isEmpty())
		{
			return true;
		}
		if (preferredCenters.contains(center))
		{
			return true;
		}
		if (center.neighbors == null)
		{
			return false;
		}
		for (Center neighbor : center.neighbors)
		{
			if (preferredCenters.contains(neighbor))
			{
				return true;
			}
		}
		return false;
	}

	private static double preferencePenalty(Center center, String preference)
	{
		if ("ocean".equals(preference))
		{
			if (center.isLake)
			{
				return 1500.0;
			}
			return center.isWater ? 0.0 : 1000.0;
		}
		if ("inland_sea".equals(preference))
		{
			if (center.isWater && !center.isLake)
			{
				double penalty = 0.0;
				penalty += hasLandWithinHops(center, 5) ? 0.0 : 520.0;
				penalty += center.isCoast ? 120.0 : 0.0;
				penalty += waterDirectionCount(center, 5) < 3 ? 900.0 : 0.0;
				penalty -= Math.min(countWaterWithinHops(center, 5), 34) * 22.0;
				return penalty;
			}
			return center.isLake ? 2200.0 : 1100.0;
		}
		if ("lake".equals(preference))
		{
			if (center.isLake)
			{
				return 0.0;
			}
			if (!center.isWater && isLakeShore(center))
			{
				return 350.0;
			}
			return center.isWater ? 5000.0 : 1200.0;
		}
		if ("lake_shore".equals(preference))
		{
			double penalty = center.isWater ? 10000.0 : 0.0;
			penalty += isLakeShore(center) ? 0.0 : 800.0;
			return penalty;
		}
		double penalty = center.isWater ? 10000.0 : 0.0;
		if ("coast".equals(preference))
		{
			penalty += center.isCoast ? 0.0 : 600.0;
		}
		else if ("forest".equals(preference))
		{
			penalty += isForest(center) ? 0.0 : 150.0;
			penalty += center.isCoast ? 250.0 : 0.0;
			penalty += center.isMountain ? 2200.0 : 0.0;
			penalty += center.isHill ? 1200.0 : 0.0;
		}
		else if ("mountain".equals(preference))
		{
			penalty += center.isMountain ? 0.0 : center.isHill ? 120.0 : 220.0;
		}
		else if ("sky_island".equals(preference))
		{
			penalty += center.isMountain ? 0.0 : center.isHill ? 120.0 : 300.0;
			penalty += center.isCoast ? 1200.0 : 0.0;
			penalty += hasAnyWaterWithinHops(center, 1) ? 700.0 : 0.0;
		}
		else if ("hill".equals(preference))
		{
			penalty += center.isHill ? 0.0 : center.isMountain ? 260.0 : 140.0;
		}
		else if ("land".equals(preference))
		{
			penalty += center.isCoast ? 100.0 : 0.0;
		}
		return penalty;
	}

	private static boolean isForest(Center center)
	{
		if (center.biome == null)
		{
			return false;
		}
		return drawsTreeIcons(center);
	}

	private static boolean isLakeShore(Center center)
	{
		if (center == null || center.neighbors == null)
		{
			return false;
		}
		for (Center neighbor : center.neighbors)
		{
			if (neighbor != null && neighbor.isLake)
			{
				return true;
			}
		}
		return false;
	}

	private static boolean hasWaterWithinHops(Center center, int hops)
	{
		return hasCenterWithinHops(center, hops, true);
	}

	private static boolean hasAnyWaterWithinHops(Center center, int hops)
	{
		if (center == null || center.neighbors == null)
		{
			return false;
		}
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(center);
		seen.add(center.index);
		for (int step = 0; step <= hops && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				if (current != center && current.isWater)
				{
					return true;
				}
				if (current.neighbors == null || step == hops)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return false;
	}

	private static boolean hasLandWithinHops(Center center, int hops)
	{
		return hasCenterWithinHops(center, hops, false);
	}

	private static int landDirectionCount(Center center, int hops)
	{
		return Integer.bitCount(landDirectionMask(center, hops));
	}

	private static int waterDirectionCount(Center center, int hops)
	{
		return Integer.bitCount(waterDirectionMask(center, hops));
	}

	private static boolean hasOpposingLandDirections(Center center, int hops)
	{
		int mask = landDirectionMask(center, hops);
		boolean west = (mask & 1) != 0;
		boolean east = (mask & 2) != 0;
		boolean north = (mask & 4) != 0;
		boolean south = (mask & 8) != 0;
		return (west && east) || (north && south);
	}

	private static int waterDirectionMask(Center center, int hops)
	{
		if (center == null || center.neighbors == null || center.loc == null)
		{
			return 0;
		}
		int mask = 0;
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(center);
		seen.add(center.index);
		for (int step = 0; step <= hops && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				if (current != center && current.loc != null && current.isWater && !current.isLake)
				{
					double dx = current.loc.x - center.loc.x;
					double dy = current.loc.y - center.loc.y;
					if (Math.abs(dx) >= Math.abs(dy))
					{
						mask |= dx < 0.0 ? 1 : 2;
					}
					else
					{
						mask |= dy < 0.0 ? 4 : 8;
					}
				}
				if (current.neighbors == null || step == hops)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return mask;
	}

	private static int landDirectionMask(Center center, int hops)
	{
		if (center == null || center.neighbors == null || center.loc == null)
		{
			return 0;
		}
		int mask = 0;
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(center);
		seen.add(center.index);
		for (int step = 0; step <= hops && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				if (current != center && current.loc != null && !current.isWater)
				{
					double dx = current.loc.x - center.loc.x;
					double dy = current.loc.y - center.loc.y;
					if (Math.abs(dx) >= Math.abs(dy))
					{
						mask |= dx < 0.0 ? 1 : 2;
					}
					else
					{
						mask |= dy < 0.0 ? 4 : 8;
					}
				}
				if (current.neighbors == null || step == hops)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return mask;
	}

	private static int countWaterWithinHops(Center center, int hops)
	{
		return countCentersWithinHops(center, hops, true);
	}

	private static int countLandWithinHops(Center center, int hops)
	{
		return countCentersWithinHops(center, hops, false);
	}

	private static LabelBounds estimatedInlandSeaLabelBounds(JSONObject hint, Center center, MapSettings settings)
	{
		String text = stringValue(hint, "text", "");
		int fontSize = settings.regionFont == null ? 22 : Math.round(settings.regionFont.getSize());
		double units = 0.0;
		for (int offset = 0; offset < text.length();)
		{
			int codePoint = text.codePointAt(offset);
			offset += Character.charCount(codePoint);
			if (Character.isWhitespace(codePoint))
			{
				units += 0.35;
			}
			else if (codePoint <= 0x7f)
			{
				units += 0.62;
			}
			else
			{
				units += 1.0;
			}
		}
		double width = Math.max(fontSize * 4.0, units * fontSize * 2.25) + 36.0;
		double height = fontSize * 3.2 + 24.0;
		Point point = fromGraphPoint(center.loc, settings);
		return new LabelBounds(point.x - width / 2.0, point.y - height / 2.0, point.x + width / 2.0, point.y + height / 2.0);
	}

	private static int countCentersInMapBounds(WorldGraph graph, LabelBounds bounds, MapSettings settings, boolean water)
	{
		if (graph == null || graph.centers == null)
		{
			return 0;
		}
		int count = 0;
		for (Center center : graph.centers)
		{
			if (center == null || center.loc == null || center.isWater != water)
			{
				continue;
			}
			Point point = fromGraphPoint(center.loc, settings);
			if (point.x >= bounds.left && point.x <= bounds.right && point.y >= bounds.top && point.y <= bounds.bottom)
			{
				count++;
			}
		}
		return count;
	}

	private static int countCentersWithinHops(Center center, int hops, boolean water)
	{
		if (center == null || center.neighbors == null)
		{
			return 0;
		}
		int count = 0;
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(center);
		seen.add(center.index);
		for (int step = 0; step <= hops && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				if (current != center && current.isWater == water && !current.isLake)
				{
					count++;
				}
				if (current.neighbors == null || step == hops)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return count;
	}

	private static boolean hasCenterWithinHops(Center center, int hops, boolean water)
	{
		if (center == null || center.neighbors == null)
		{
			return false;
		}
		Set<Integer> seen = new HashSet<>();
		List<Center> frontier = new ArrayList<>();
		frontier.add(center);
		seen.add(center.index);
		for (int step = 0; step <= hops && !frontier.isEmpty(); step++)
		{
			List<Center> next = new ArrayList<>();
			for (Center current : frontier)
			{
				if (current != center && current.isWater == water && !current.isLake)
				{
					return true;
				}
				if (current.neighbors == null || step == hops)
				{
					continue;
				}
				for (Center neighbor : current.neighbors)
				{
					if (neighbor != null && seen.add(neighbor.index))
					{
						next.add(neighbor);
					}
				}
			}
			frontier = next;
		}
		return false;
	}

	private static String locationPreference(JSONObject hint)
	{
		String explicit = stringValue(hint, "preference", "").strip().toLowerCase();
		if (!explicit.isBlank())
		{
			return explicit;
		}
		String text = (
				stringValue(hint, "terrain", "") + " "
						+ stringValue(hint, "tags", "")
		).toLowerCase();

		if (containsAny(text, "港", "码头", "海港", "port", "harbor", "harbour"))
		{
			return "coast";
		}
		if (containsAny(text, "森林", "林海", "林地", "树海", "forest", "woods", "woodland"))
		{
			return "forest";
		}
		if (containsAny(text, "丘", "丘陵", "丘地", "hill", "hills"))
		{
			return "hill";
		}
		if (containsAny(text, "山", "峰", "岭", "山脉", "mountain"))
		{
			return "mountain";
		}
		if (containsAny(text, "内海", "内湾", "海峡内", "inland sea", "inner sea"))
		{
			return "inland_sea";
		}
		if (containsAny(text, "湖", "湖泊", "湖心", "湖畔", "lake"))
		{
			return "lake";
		}
		if (containsAny(text, "公海", "远海", "海域", "海洋", "水下", "ocean", "sea", "underwater"))
		{
			return "ocean";
		}
		return "land";
	}

	private static boolean containsAny(String text, String... needles)
	{
		for (String needle : needles)
		{
			if (text.contains(needle))
			{
				return true;
			}
		}
		return false;
	}

	private static Path parseBriefPath(String[] args)
	{
		if (args.length == 1)
		{
			return Paths.get(args[0]);
		}
		if (args.length == 2 && "--brief".equals(args[0]))
		{
			return Paths.get(args[1]);
		}
		throw new IllegalArgumentException("Usage: FuGmHeadlessExporter --brief <brief.json>");
	}

	private static JSONObject readBrief(Path briefPath) throws IOException
	{
		String content = Files.readString(briefPath, StandardCharsets.UTF_8);
		try
		{
			Object parsed = JSONValue.parseWithException(content);
			if (parsed instanceof JSONObject json)
			{
				return json;
			}
		}
		catch (ParseException e)
		{
			throw new IllegalArgumentException("Invalid brief JSON: " + briefPath, e);
		}
		throw new IllegalArgumentException("Brief root must be a JSON object: " + briefPath);
	}

	private static Path requiredOutputPath(JSONObject obj, Path baseDir, String key)
	{
		String value = stringValue(obj, key, null);
		if (value == null || value.isBlank())
		{
			throw new IllegalArgumentException("Missing required brief field: " + key);
		}
		return resolvePath(baseDir, value);
	}

	private static String optionalPathString(JSONObject obj, Path baseDir, String key)
	{
		String value = stringValue(obj, key, null);
		if (value == null || value.isBlank())
		{
			return null;
		}
		return resolvePath(baseDir, value).toString();
	}

	private static Path resolvePath(Path baseDir, String value)
	{
		Path path = Paths.get(value);
		if (!path.isAbsolute())
		{
			path = baseDir.resolve(path);
		}
		return path.normalize().toAbsolutePath();
	}

	private static Point pointValue(JSONObject obj, MapSettings settings)
	{
		double x = doubleValue(obj, "x", 0.5);
		double y = doubleValue(obj, "y", 0.5);
		if (x >= 0.0 && x <= 1.0 && y >= 0.0 && y <= 1.0)
		{
			x *= settings.generatedWidth;
			y *= settings.generatedHeight;
		}
		return new Point(x, y);
	}

	private static JSONArray arrayValue(JSONObject obj, String key)
	{
		Object value = obj.get(key);
		return value instanceof JSONArray array ? array : null;
	}

	private static boolean labelsContainNonAscii(JSONObject brief)
	{
		JSONArray labels = arrayValue(brief, "labels");
		if (labels == null)
		{
			return false;
		}
		for (Object obj : labels)
		{
			if (obj instanceof JSONObject label)
			{
				String text = stringValue(label, "text", "");
				for (int i = 0; i < text.length(); i++)
				{
					if (text.charAt(i) > 127)
					{
						return true;
					}
				}
			}
		}
		return false;
	}

	private static String firstInstalledFont(String... families)
	{
		for (String family : families)
		{
			if (Font.isInstalled(family))
			{
				return family;
			}
		}
		return null;
	}

	private static String stringValue(JSONObject obj, String key, String defaultValue)
	{
		Object value = obj.get(key);
		return value == null ? defaultValue : value.toString();
	}

	private static boolean boolValue(JSONObject obj, String key, boolean defaultValue)
	{
		Object value = obj.get(key);
		return value instanceof Boolean bool ? bool : defaultValue;
	}

	private static int intValue(JSONObject obj, String key, int defaultValue)
	{
		Object value = obj.get(key);
		if (value instanceof Number number)
		{
			return number.intValue();
		}
		return defaultValue;
	}

	private static long longValue(JSONObject obj, String key, long defaultValue)
	{
		Object value = obj.get(key);
		if (value instanceof Number number)
		{
			return number.longValue();
		}
		return defaultValue;
	}

	private static double doubleValue(JSONObject obj, String key, double defaultValue)
	{
		Object value = obj.get(key);
		if (value instanceof Number number)
		{
			return number.doubleValue();
		}
		return defaultValue;
	}

	private static Stroke strokeValue(JSONObject obj, String typeKey, String widthKey, Stroke defaultValue)
	{
		StrokeType type = defaultValue == null ? StrokeType.Solid : defaultValue.type;
		float width = defaultValue == null ? 1.0f : defaultValue.width;
		String typeValue = stringValue(obj, typeKey, null);
		if (typeValue != null && !typeValue.isBlank())
		{
			type = StrokeType.valueOf(typeValue);
		}
		width = (float) doubleValue(obj, widthKey, width);
		return new Stroke(type, width);
	}

	private static Color colorValue(JSONObject obj, String key, Color defaultValue)
	{
		String value = stringValue(obj, key, null);
		if (value == null || value.isBlank())
		{
			return defaultValue;
		}
		if (value.contains(","))
		{
			String[] parts = value.split(",");
			if (parts.length != 3 && parts.length != 4)
			{
				throw new IllegalArgumentException("Color must use R,G,B or R,G,B,A: " + key);
			}
			int red = Integer.parseInt(parts[0].trim());
			int green = Integer.parseInt(parts[1].trim());
			int blue = Integer.parseInt(parts[2].trim());
			int alpha = parts.length == 4 ? Integer.parseInt(parts[3].trim()) : 255;
			return Color.create(red, green, blue, alpha);
		}
		String hex = value.startsWith("#") ? value.substring(1) : value;
		if (hex.length() != 6 && hex.length() != 8)
		{
			throw new IllegalArgumentException("Color must use #RRGGBB, #RRGGBBAA, R,G,B, or R,G,B,A: " + key);
		}
		if (hex.length() == 6)
		{
			int rgb = Integer.parseInt(hex, 16);
			return Color.create((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);
		}
		long rgba = Long.parseLong(hex, 16);
		return Color.create((int) ((rgba >> 24) & 0xFF), (int) ((rgba >> 16) & 0xFF), (int) ((rgba >> 8) & 0xFF), (int) (rgba & 0xFF));
	}

	private static String escapeJson(String value)
	{
		return value.replace("\\", "\\\\").replace("\"", "\\\"");
	}

	private record CenterGroup(String name, List<Center> centers)
	{
	}

	private record LandComponent(int size, int featureCount, List<Center> centers)
	{
	}

	private static final class TerrainIconCounts
	{
		private final int terrain;
		private final int rugged;

		private TerrainIconCounts(int terrain, int rugged)
		{
			this.terrain = terrain;
			this.rugged = rugged;
		}
	}

	private record CoverageStats(int total, int covered, double score)
	{
		private static CoverageStats empty()
		{
			return new CoverageStats(0, 0, 0.0);
		}

		private boolean acceptable()
		{
			return total == 0 || covered == total;
		}

		private String summary()
		{
			return covered + "/" + total;
		}
	}

	private record TerrainCoverageReport(
			boolean acceptable,
			double score,
			int totalLand,
			CoverageStats landmasses,
			CoverageStats generatedRegions,
			CoverageStats politicalRegions,
			CoverageStats terrainSpread,
			CoverageStats actualTerrainIcons,
			CoverageStats actualLandmassTerrain,
			CoverageStats actualGeneratedRegionTerrain,
			CoverageStats actualPoliticalRegionTerrain,
			CoverageStats semanticAnchors
	)
	{
		private static TerrainCoverageReport empty()
		{
			return new TerrainCoverageReport(false, 0.0, 0, CoverageStats.empty(), CoverageStats.empty(), CoverageStats.empty(), CoverageStats.empty(), CoverageStats.empty(), CoverageStats.empty(),
					CoverageStats.empty(), CoverageStats.empty(), CoverageStats.empty());
		}

		private String summary()
		{
			return "land=" + totalLand
					+ ", landmasses=" + landmasses.summary()
					+ ", generatedRegions=" + generatedRegions.summary()
					+ ", politicalRegions=" + politicalRegions.summary()
					+ ", terrainSpread=" + terrainSpread.summary()
					+ ", actualTerrainIcons=" + actualTerrainIcons.summary()
					+ ", actualLandmassTerrain=" + actualLandmassTerrain.summary()
					+ ", actualGeneratedRegionTerrain=" + actualGeneratedRegionTerrain.summary()
					+ ", actualPoliticalRegionTerrain=" + actualPoliticalRegionTerrain.summary()
					+ ", semanticAnchors=" + semanticAnchors.summary()
					+ ", score=" + score;
		}
	}

	private record LocationAnchor(Point labelPoint, Center labelCenter, Center iconCenter, Center customIconCenter)
	{
	}

	private record PendingLabel(
			JSONObject label,
			String text,
			TextType type,
			Point location,
			Center labelCenter,
			Center iconCenter,
			Center customIconCenter
	)
	{
	}

	private record IconFootprint(int centerIndex, LabelBounds bounds)
	{
	}

	private record IslandSelection(Center anchor, List<Center> centers)
	{
	}

	private record LabelBounds(double left, double top, double right, double bottom)
	{
		private LabelBounds expand(double amount)
		{
			return new LabelBounds(left - amount, top - amount, right + amount, bottom + amount);
		}

		private double width()
		{
			return right - left;
		}

		private double height()
		{
			return bottom - top;
		}
	}

	private record PoliticalRegionAnchor(int regionId, Point point)
	{
	}
}
