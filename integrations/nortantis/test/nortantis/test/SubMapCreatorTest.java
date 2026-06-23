package nortantis.test;

import nortantis.MapCreator;
import nortantis.MapSettings;
import nortantis.River;
import nortantis.SubMapCreator;
import nortantis.geom.Rectangle;
import nortantis.swing.SubMapDialog;
import nortantis.graph.voronoi.Corner;
import nortantis.graph.voronoi.Edge;
import nortantis.platform.Image;
import nortantis.platform.ImageHelper;
import nortantis.platform.PlatformFactory;
import nortantis.platform.awt.AwtFactory;
import nortantis.WorldGraph;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.File;
import java.io.IOException;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.apache.commons.io.FileUtils;

import static org.junit.jupiter.api.Assertions.*;

public class SubMapCreatorTest
{
	static final String failedMapsFolderName = "failed sub-maps";

	// Set any of these to true to force that test to write its result map to the failed sub-maps folder.
	private static final boolean forceWriteSubMapRiversFormConfluence = false;
	private static final boolean forceWriteSubMapRiversHaveNoFingers = false;
	private static final boolean forceWriteSubMapComplexRiversHaveNoFingersOrLoops = false;
	private static final boolean forceWriteSubMapRiversHaveNoLoops = false;
	private static final boolean forceWriteSubMapTShapedRiverHasThreeMouths = false;
	private static final boolean forceWriteSubMapRiversFormConfluence2 = false;

	@BeforeAll
	public static void setUpBeforeClass() throws IOException
	{
		PlatformFactory.setInstance(new AwtFactory());
		nortantis.swing.translation.Translation.initialize();
		FileUtils.deleteDirectory(new File(Paths.get("unit test files", failedMapsFolderName).toString()));
	}

	/**
	 * Verifies that when a sub-map contains two rivers that form a confluence in the source map, those rivers are still connected via a shared corner in the sub-map's edge edits. This is a
	 * regression test for a bug in SubMapCreator where the last edge of a tributary was incorrectly removed by simplifyToPath, preventing the confluence from being transferred.
	 */
	@Test
	public void subMapRiversFormConfluence() throws Exception
	{
		String originalSettingsPath = Paths.get("unit test files", "map settings", "riversForSubMaps.nort").toString();
		MapSettings originalSettings = new MapSettings(originalSettingsPath);
		originalSettings.resolution = 0.5;

		WorldGraph originalGraph = MapCreator.createGraphForUnitTests(originalSettings);

		// Sub-map selection bounds in RI (resolution-invariant) coordinates.
		Rectangle selectionBoundsRI = new Rectangle(1324, 999, 1307, 1307);

		int worldSize = SubMapDialog.computeDefaultWorldSize(originalSettings, selectionBoundsRI);

		long seed = 1962328436L;
		MapSettings subMapSettings = SubMapCreator.createSubMapSettings(originalSettings, originalGraph, selectionBoundsRI, worldSize,
				originalSettings.resolution, seed, true);

		// Recreate the same WorldGraph that createSubMapSettings built internally.
		WorldGraph newGraph = MapCreator.createGraphForUnitTests(subMapSettings);

		List<River> rivers = newGraph.findRivers();

		assertEquals(2, rivers.size(), "Sub-map should contain exactly 2 rivers (expected a main river and a tributary)");

		assertRiversAreAllConnected(rivers, subMapSettings, "subMapRiversFormConfluence");
		if (forceWriteSubMapRiversFormConfluence)
		{
			saveFailedMap(subMapSettings, "subMapRiversFormConfluence");
		}
	}

	/**
	 * Verifies that rivers in a sub-map contain no finger branches. A finger is a branch point — a corner with degree &gt; 2 in the combined river edge graph — that was not present in the original
	 * map. The original map for this test case has no such branches.
	 * <p>
	 * Note: {@link WorldGraph#findRivers()} separates branch arms into distinct {@link River} objects, so checking degree within a single river's edges would never detect fingers. Instead this test
	 * builds the degree map from the unique set of all edges across all rivers.
	 * </p>
	 */
	@Test
	public void subMapRiversHaveNoFingers() throws Exception
	{
		String originalSettingsPath = Paths.get("unit test files", "map settings", "riversForSubMaps.nort").toString();
		MapSettings originalSettings = new MapSettings(originalSettingsPath);
		originalSettings.resolution = 0.5;

		WorldGraph originalGraph = MapCreator.createGraphForUnitTests(originalSettings);

		// Sub-map selection bounds in RI (resolution-invariant) coordinates.
		Rectangle selectionBoundsRI = new Rectangle(0, 0, 1348, 4096);

		int worldSize = SubMapDialog.computeDefaultWorldSize(originalSettings, selectionBoundsRI);

		long seed = 983909832L;
		MapSettings subMapSettings = SubMapCreator.createSubMapSettings(originalSettings, originalGraph, selectionBoundsRI, worldSize,
				originalSettings.resolution, seed, true);

		WorldGraph newGraph = MapCreator.createGraphForUnitTests(subMapSettings);

		List<River> rivers = newGraph.findRivers();

		assertRiversHaveNoFingers(rivers, subMapSettings, "subMapRiversHaveNoFingers");
		if (forceWriteSubMapRiversHaveNoFingers)
		{
			saveFailedMap(subMapSettings, "subMapRiversHaveNoFingers");
		}
	}

	/**
	 * Verifies that rivers in a sub-map contain no fingers or loops for a sub-map with a complex river network.
	 */
	@Test
	public void subMapComplexRiversHaveNoFingersOrLoops() throws Exception
	{
		String originalSettingsPath = Paths.get("unit test files", "map settings", "riversForSubMaps.nort").toString();
		MapSettings originalSettings = new MapSettings(originalSettingsPath);
		originalSettings.resolution = 0.5;

		WorldGraph originalGraph = MapCreator.createGraphForUnitTests(originalSettings);

		// Sub-map selection bounds in RI (resolution-invariant) coordinates.
		Rectangle selectionBoundsRI = new Rectangle(2515, 1471, 1581, 2625);

		int worldSize = SubMapDialog.computeDefaultWorldSize(originalSettings, selectionBoundsRI);

		long seed = 1735631519L;
		MapSettings subMapSettings = SubMapCreator.createSubMapSettings(originalSettings, originalGraph, selectionBoundsRI, worldSize,
				originalSettings.resolution, seed, true);

		WorldGraph newGraph = MapCreator.createGraphForUnitTests(subMapSettings);

		List<River> rivers = newGraph.findRivers();

		assertRiversHaveNoLoops(rivers, subMapSettings, "subMapComplexRiversHaveNoFingersOrLoops");
		assertRiversHaveNoFingers(rivers, subMapSettings, "subMapComplexRiversHaveNoFingersOrLoops");
		if (forceWriteSubMapComplexRiversHaveNoFingersOrLoops)
		{
			saveFailedMap(subMapSettings, "subMapComplexRiversHaveNoFingersOrLoops");
		}
	}

	/**
	 * Verifies that rivers in a sub-map contain no loops. A loop exists when the river's edges form a cycle, detected by checking that the edge count exceeds corner count minus one (the invariant
	 * for a simple path).
	 */
	@Test
	public void subMapRiversHaveNoLoops() throws Exception
	{
		String originalSettingsPath = Paths.get("unit test files", "map settings", "riversForSubMaps.nort").toString();
		MapSettings originalSettings = new MapSettings(originalSettingsPath);
		originalSettings.resolution = 0.5;

		WorldGraph originalGraph = MapCreator.createGraphForUnitTests(originalSettings);

		// Sub-map selection bounds in RI (resolution-invariant) coordinates.
		Rectangle selectionBoundsRI = new Rectangle(0, 0, 1348, 4096);

		int worldSize = SubMapDialog.computeDefaultWorldSize(originalSettings, selectionBoundsRI);

		long seed = 1142346135L;
		MapSettings subMapSettings = SubMapCreator.createSubMapSettings(originalSettings, originalGraph, selectionBoundsRI, worldSize,
				originalSettings.resolution, seed, true);

		WorldGraph newGraph = MapCreator.createGraphForUnitTests(subMapSettings);

		List<River> rivers = newGraph.findRivers();

		assertRiversHaveNoLoops(rivers, subMapSettings, "subMapRiversHaveNoLoops");
		if (forceWriteSubMapRiversHaveNoLoops)
		{
			saveFailedMap(subMapSettings, "subMapRiversHaveNoLoops");
		}
	}

//	/**
//	 * Verifies that a T-shaped river in a sub-map has 3 mouths where it meets the ocean. Each mouth is counted when the first or last edge of a {@link River} has a coast or ocean corner endpoint.
//	 * This is a regression test for a bug where one arm of the T was incorrectly dropped, leaving only 2 mouths.
//	 */
//	@Test
//	public void subMapTShapedRiverHasThreeMouths() throws Exception
//	{
//		String originalSettingsPath = Paths.get("unit test files", "map settings", "riversForSubMaps.nort").toString();
//		MapSettings originalSettings = new MapSettings(originalSettingsPath);
//		originalSettings.resolution = 0.5;
//
//		WorldGraph originalGraph = MapCreator.createGraphForUnitTests(originalSettings);
//
//		// Sub-map selection bounds in RI (resolution-invariant) coordinates.
//		Rectangle selectionBoundsRI = new Rectangle(661, 18, 2013, 4078);
//
//		int worldSize = SubMapDialog.computeDefaultWorldSize(originalSettings, selectionBoundsRI);
//
//		long seed = 1670082139L;
//		MapSettings subMapSettings = SubMapCreator.createSubMapSettings(originalSettings, originalGraph, selectionBoundsRI, worldSize,
//				originalSettings.resolution, seed, true);
//
//		WorldGraph newGraph = MapCreator.createGraphForUnitTests(subMapSettings);
//
//		List<River> rivers = newGraph.findRivers();
//
//		System.err.println("DEBUG rivers=" + rivers.size());
//		for (int _i = 0; _i < rivers.size(); _i++) {
//			List<Edge> _re = rivers.get(_i).getEdges();
//			if (!_re.isEmpty()) {
//				Edge _f = _re.get(0), _l = _re.get(_re.size()-1);
//				System.err.println("  R"+_i+" edges="+_re.size()+" first.v0="+(_f.v0==null?"null":"C="+_f.v0.isCoast+",O="+_f.v0.isOcean+",W="+_f.v0.isWater)+" first.v1="+(_f.v1==null?"null":"C="+_f.v1.isCoast+",O="+_f.v1.isOcean+",W="+_f.v1.isWater)+" last.v0="+(_l.v0==null?"null":"C="+_l.v0.isCoast+",O="+_l.v0.isOcean+",W="+_l.v0.isWater)+" last.v1="+(_l.v1==null?"null":"C="+_l.v1.isCoast+",O="+_l.v1.isOcean+",W="+_l.v1.isWater));
//			}
//		}
//		long mouthCount = rivers.stream().filter(river ->
//		{
//			List<Edge> edges = river.getEdges();
//			if (edges.isEmpty())
//				return false;
//			return edgeTouchesCoastOrOcean(edges.get(0)) || edgeTouchesCoastOrOcean(edges.get(edges.size() - 1));
//		}).count();
//
//		if (mouthCount != 3)
//		{
//			String failedMapPath = saveFailedMap(subMapSettings, "subMapTShapedRiverHasThreeMouths");
//			fail("Expected the T-shaped river to have 3 mouths meeting the ocean, but found " + mouthCount
//					+ ".\nFailed map written to: " + failedMapPath);
//		}
//		else if (forceWriteSubMapTShapedRiverHasThreeMouths)
//		{
//			saveFailedMap(subMapSettings, "subMapTShapedRiverHasThreeMouths");
//		}
//	}
//
//	/**
//	 * Verifies that a T-shaped river in a sub-map has all its arms connected at the junction. This is a regression test for a bug where one arm of the T was disconnected from the others.
//	 */
//	@Test
//	public void subMapRiversFormConfluence2() throws Exception
//	{
//		String originalSettingsPath = Paths.get("unit test files", "map settings", "riversForSubMaps.nort").toString();
//		MapSettings originalSettings = new MapSettings(originalSettingsPath);
//		originalSettings.resolution = 0.5;
//
//		WorldGraph originalGraph = MapCreator.createGraphForUnitTests(originalSettings);
//
//		// Sub-map selection bounds in RI (resolution-invariant) coordinates.
//		Rectangle selectionBoundsRI = new Rectangle(661, 18, 2013, 4078);
//
//		int worldSize = SubMapDialog.computeDefaultWorldSize(originalSettings, selectionBoundsRI);
//
//		long seed = 1222331460L;
//		MapSettings subMapSettings = SubMapCreator.createSubMapSettings(originalSettings, originalGraph, selectionBoundsRI, worldSize,
//				originalSettings.resolution, seed, true);
//
//		WorldGraph newGraph = MapCreator.createGraphForUnitTests(subMapSettings);
//
//		List<River> rivers = newGraph.findRivers();
//
//		assertRiversAreAllConnected(rivers, subMapSettings, "subMapRiversFormConfluence2");
//		if (forceWriteSubMapRiversFormConfluence2)
//		{
//			saveFailedMap(subMapSettings, "subMapRiversFormConfluence2");
//		}
//	}

	/**
	 * Returns true if either endpoint of the edge is a coast or ocean corner, indicating the river reaches the ocean shore.
	 */
	private boolean edgeTouchesCoastOrOcean(Edge edge)
	{
		return (edge.v0 != null && (edge.v0.isCoast || edge.v0.isOcean))
				|| (edge.v1 != null && (edge.v1.isCoast || edge.v1.isOcean));
	}

	private String saveFailedMap(MapSettings subMapSettings, String testName) throws Exception
	{
		File failedMapsDir = Paths.get("unit test files", failedMapsFolderName).toFile();
		failedMapsDir.mkdirs();
		String failedMapPath = Paths.get("unit test files", failedMapsFolderName, testName + ".png").toString();
		Image map = new MapCreator().createMap(subMapSettings, null, null);
		ImageHelper.getInstance().write(map, failedMapPath);
		return failedMapPath;
	}

	/**
	 * Asserts that all rivers form a single connected component, i.e. every river shares at least one corner with some other river. Uses union-find to detect disconnected river arms.
	 */
	private void assertRiversAreAllConnected(List<River> rivers, MapSettings subMapSettings, String testName) throws Exception
	{
		if (rivers.size() <= 1)
			return;

		// Union-find: map each river index to its root.
		int[] parent = new int[rivers.size()];
		for (int i = 0; i < parent.length; i++)
			parent[i] = i;

		// Find with path compression.
		// Declared as a local array to allow use in lambda below.
		// We use a helper array trick since lambdas require effectively final variables.

		for (int i = 0; i < rivers.size(); i++)
		{
			Set<Corner> cornersI = rivers.get(i).getCorners();
			for (int j = i + 1; j < rivers.size(); j++)
			{
				Set<Corner> cornersJ = rivers.get(j).getCorners();
				Set<Corner> intersection = new HashSet<>(cornersI);
				intersection.retainAll(cornersJ);
				if (!intersection.isEmpty())
				{
					// Union i and j.
					int rootI = i, rootJ = j;
					while (parent[rootI] != rootI)
						rootI = parent[rootI];
					while (parent[rootJ] != rootJ)
						rootJ = parent[rootJ];
					if (rootI != rootJ)
						parent[rootI] = rootJ;
				}
			}
		}

		// Count distinct roots.
		Set<Integer> roots = new HashSet<>();
		for (int i = 0; i < parent.length; i++)
		{
			int root = i;
			while (parent[root] != root)
				root = parent[root];
			roots.add(root);
		}

		if (roots.size() > 1)
		{
			String failedMapPath = saveFailedMap(subMapSettings, testName);
			fail("Rivers are not all connected: found " + roots.size() + " disconnected components among " + rivers.size()
					+ " rivers.\nFailed map written to: " + failedMapPath);
		}
	}

	private void assertRiversHaveNoLoops(List<River> rivers, MapSettings subMapSettings, String testName) throws Exception
	{
		for (River river : rivers)
		{
			int edgeCount = river.getEdges().size();
			int cornerCount = river.getCorners().size();
			if (edgeCount > cornerCount - 1)
			{
				StringBuilder cornerSeq = new StringBuilder();
				for (Corner c : river.getOrderedCorners())
				{
					if (cornerSeq.length() > 0)
						cornerSeq.append(" -> ");
					cornerSeq.append(c.index);
				}
				String failedMapPath = saveFailedMap(subMapSettings, testName);
				fail("River has a loop: " + edgeCount + " edges but only " + cornerCount + " corners.\nCorner sequence: " + cornerSeq
						+ "\nFailed map written to: " + failedMapPath);
			}
		}
	}

	/**
	 * Asserts that no corner in the combined river edge graph has degree &gt; 2. Such corners are branch points, indicating a finger was introduced. Note: {@link WorldGraph#findRivers()} may
	 * place the same edge in two different {@link River} objects, so edges are deduplicated before computing degrees.
	 */
	private void assertRiversHaveNoFingers(List<River> rivers, MapSettings subMapSettings, String testName) throws Exception
	{
		Set<Edge> allRiverEdges = new HashSet<>();
		for (River river : rivers)
		{
			allRiverEdges.addAll(river.getEdges());
		}

		Map<Corner, Integer> cornerDegree = new HashMap<>();
		for (Edge e : allRiverEdges)
		{
			if (e.v0 != null)
				cornerDegree.merge(e.v0, 1, Integer::sum);
			if (e.v1 != null)
				cornerDegree.merge(e.v1, 1, Integer::sum);
		}

		for (Map.Entry<Corner, Integer> entry : cornerDegree.entrySet())
		{
			if (entry.getValue() > 2)
			{
				int badCornerIndex = entry.getKey().index;
				StringBuilder riverInfo = new StringBuilder();
				for (int ri = 0; ri < rivers.size(); ri++)
				{
					River river = rivers.get(ri);
					Set<Corner> corners = river.getCorners();
					boolean hasBadCorner = corners.stream().anyMatch(c -> c.index == badCornerIndex);
					if (hasBadCorner)
					{
						riverInfo.append("\n  River ").append(ri).append(" (").append(river.getEdges().size()).append(" edges, ").append(corners.size()).append(" corners): ");
						StringBuilder cornerSeq = new StringBuilder();
						for (Corner c : river.getOrderedCorners())
						{
							if (cornerSeq.length() > 0)
								cornerSeq.append(" -> ");
							cornerSeq.append(c.index);
						}
						riverInfo.append(cornerSeq);
					}
				}
				String failedMapPath = saveFailedMap(subMapSettings, testName);
				fail("River has a finger: corner " + badCornerIndex + " has degree " + entry.getValue()
						+ ".\nRivers containing corner " + badCornerIndex + ":" + riverInfo
						+ "\nFailed map written to: " + failedMapPath);
			}
		}
	}

}
