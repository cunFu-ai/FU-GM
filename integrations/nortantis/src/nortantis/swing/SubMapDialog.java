package nortantis.swing;

import nortantis.*;
import nortantis.editor.MapUpdater;
import nortantis.geom.IntRectangle;
import nortantis.geom.Rectangle;
import nortantis.platform.Image;
import nortantis.platform.awt.AwtBridge;
import nortantis.swing.translation.Translation;

import nortantis.util.Helper;

import javax.swing.*;
import javax.swing.event.ChangeListener;
import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import java.awt.*;
import java.awt.event.ComponentAdapter;
import java.awt.event.ComponentEvent;
import java.awt.event.WindowAdapter;
import java.awt.event.WindowEvent;
import java.awt.image.BufferedImage;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;

/**
 * A two-step dialog for creating a higher-detail sub-map from a region of the current map.
 *
 * Step 1 (non-modal): User drags on the map to select a region. Step 2 (modal): User chooses detail level and previews the sub-map before creating it.
 */
public class SubMapDialog
{
	private final MainWindow mainWindow;

	// Captured state from the current map at construction time.
	private final MapSettings origSettings;
	private final WorldGraph origGraph;
	/** The display-quality resolution scale at which origGraph was created. */
	private final double origResolution;

	// Shared state between steps.
	private Rectangle selBoundsRI;

	// Step 1 dialog state.
	private JDialog step1Dialog;
	private JSpinner xSpinner, ySpinner, widthSpinner, heightSpinner;
	private JLabel step1ErrorLabel;
	private JButton step1NextButton;
	/** Guards against infinite update loops between spinners and the selection box. */
	private boolean updatingSpinnersFromBox = false;
	/** Currently selected aspect ratio (width / height). 0 = no lock. */
	private double selectedAspectRatio = 0.0;

	// Step 2 dialog / preview state.
	private JDialog step2Dialog;
	private MapUpdater previewUpdater;
	private MapEditingPanel previewPanel;
	private JPanel previewContainer;
	private volatile MapSettings lastSubMapSettings;
	private SliderWithDisplayedValue detailSliderWithValue;
	private JButton createButton;
	private JSlider detailSlider;
	private JProgressBar previewProgressBar;
	private Timer progressBarTimer;
	/** Stable seed for the sub-map graph; generated once per step-2 session so re-draws produce the same Voronoi layout. */
	private long subMapSeed;
	private JTextField seedTextField;
	/** Set to true in windowOpened; guards componentResized from firing the first preview draw before the dialog is fully shown. */
	private boolean step2DialogOpened = false;
	/** The 1× polygon count for the current selection (computed when step 2 opens). */
	private double oneXWorldSize;
	/** Hides/shows the custom polygon count slider row. */
	private RowHider sliderRowHider;
	/** Hides/shows the amber warning shown in Custom detail mode. */
	private RowHider customWarningRowHider;
	/** Radio button for custom polygon count; instance field so createPreviewUpdater can read its state. */
	private JRadioButton customRadio;
	private int clampedOneXWorldSize;
	static final int minPolygonsInSubMap = 1000;
	private static final Color warningMessageColor = new java.awt.Color(160, 90, 0);

	/**
	 * Computes the clamped 1× world size (polygon count) for a sub-map selection. This matches the default "Match source detail" value shown in the sub-map dialog.
	 */
	public static int computeDefaultWorldSize(MapSettings origSettings, Rectangle selectionBoundsRI)
	{
		double origMapArea = origSettings.generatedWidth * (double) origSettings.generatedHeight;
		double selArea = selectionBoundsRI.width * selectionBoundsRI.height;
		double oneXWorldSize = origSettings.worldSize * selArea / origMapArea;
		return (int) Math.round(Math.clamp(oneXWorldSize, minPolygonsInSubMap, SettingsGenerator.maxWorldSize));
	}

	public SubMapDialog(MainWindow mainWindow)
	{
		this.mainWindow = mainWindow;
		this.origSettings = mainWindow.getSettingsFromGUI(false);
		this.origGraph = mainWindow.updater.mapParts.graph;
		this.origResolution = mainWindow.displayQualityScale;
	}

	// -------------------------------------------------------------------------
	// Step 1: Selection box
	// -------------------------------------------------------------------------

	public void showStep1()
	{
		// Disable editing tools and theme controls for the duration of the sub-map workflow.
		// Call onSwitchingAway() first so each tool clears its internal selection state
		// (e.g. selected icon in IconsTool, selected text in TextTool) before we clear visuals.
		mainWindow.toolsPanel.currentTool.onSwitchingAway();
		mainWindow.mapEditingPanel.clearAllToolSpecificSelectionsAndHighlights();
		mainWindow.enableOrDisableFieldsThatRequireMap(false, null, true);

		step1Dialog = new JDialog(mainWindow, Translation.get("subMapDialog.step1.title"), false);
		step1Dialog.setDefaultCloseOperation(JDialog.DO_NOTHING_ON_CLOSE);

		GridBagOrganizer organizer = new GridBagOrganizer();
		final int topInset = 2;

		// Instructions
		JLabel instructionsLabel = new JLabel(Translation.get("subMapDialog.step1.instructions"));
		organizer.addLeftAlignedComponent(instructionsLabel);

		// Aspect ratio buttons
		GeneratedDimension[] dims = GeneratedDimension.presets();
		int numButtons = dims.length + 1; // +1 for Custom
		double[] ratios = new double[numButtons];
		String[] ratioLabels = new String[numButtons];
		ratios[0] = 0.0;
		ratioLabels[0] = GeneratedDimension.Custom.displayName();
		for (int i = 0; i < dims.length; i++)
		{
			ratios[i + 1] = dims[i].aspectRatio();
			ratioLabels[i + 1] = dims[i].displayName();
		}

		ButtonGroup ratioGroup = new ButtonGroup();
		List<JToggleButton> aspectRatioButtons = new ArrayList<>();
		for (int i = 0; i < numButtons; i++)
		{
			final double ratio = ratios[i];
			JToggleButton btn = new JToggleButton(ratioLabels[i]);
			btn.setSelected(Math.abs(ratio - selectedAspectRatio) < 0.001);
			btn.addActionListener(e ->
			{
				selectedAspectRatio = ratio;
				mainWindow.mapEditingPanel.setSelectionBoxLockedAspectRatio(ratio);
				mainWindow.mapEditingPanel.setSelectionBoxMaxAspectRatio(ratio == 0.0 ? GeneratedDimension.MAX_ASPECT_RATIO : 0.0);
				if (ratio > 0 && selBoundsRI != null)
				{
					selBoundsRI = adjustSelectionBoxToAspectRatio(selBoundsRI, ratio);
					mainWindow.mapEditingPanel.setSelectionBoxRI(selBoundsRI);
					updateStep1SpinnersFromBox();
				}
			});
			ratioGroup.add(btn);
			aspectRatioButtons.add(btn);
		}
		SegmentedButtonWidget segmentedButtonWidget = new SegmentedButtonWidget(aspectRatioButtons);
		segmentedButtonWidget.addToOrganizer(organizer, Translation.get("subMapDialog.step1.aspectRatio.label"),
				Translation.get("subMapDialog.step1.aspectRatio.help", GeneratedDimension.Custom.displayName(), GeneratedDimension.MAX_ASPECT_RATIO), topInset);

		// Position and size spinners (use display dimensions, which are rotated relative to generatedWidth/Height for 90°/270°)
		int mapDisplayW = getMapDisplayWidth();
		int mapDisplayH = getMapDisplayHeight();
		xSpinner = new JSpinner(new SpinnerNumberModel(0, 0, mapDisplayW, 1));
		ySpinner = new JSpinner(new SpinnerNumberModel(0, 0, mapDisplayH, 1));
		widthSpinner = new JSpinner(new SpinnerNumberModel(Math.min(100, mapDisplayW), 1, mapDisplayW, 1));
		heightSpinner = new JSpinner(new SpinnerNumberModel(Math.min(100, mapDisplayH), 1, mapDisplayH, 1));

		// TODO See if I need to set the preferred sizes of the spinners like the code had here before.
		Dimension spinnerSize = new Dimension(75, xSpinner.getPreferredSize().height);
		xSpinner.setPreferredSize(spinnerSize);
		ySpinner.setPreferredSize(spinnerSize);
		widthSpinner.setPreferredSize(spinnerSize);
		heightSpinner.setPreferredSize(spinnerSize);

		organizer.addLabelAndComponentsHorizontalWithTopInset(Translation.get("subMapDialog.step1.position.label"), "",
				Arrays.asList(new JLabel(Translation.get("subMapDialog.step1.x")), xSpinner, new JLabel(Translation.get("subMapDialog.step1.y")), ySpinner, new JLabel(Translation.get("subMapDialog.step1.width")), widthSpinner, new JLabel(Translation.get("subMapDialog.step1.height")), heightSpinner), topInset);

		organizer.addVerticalFillerRow();

		// Inline error label for spinner validation
		step1ErrorLabel = new JLabel(" ");
		step1ErrorLabel.setForeground(java.awt.Color.RED);

		// Buttons row
		JPanel buttonsPanel = new JPanel(new FlowLayout(FlowLayout.RIGHT, 5, 0));

		JButton cancelButton = new JButton(Translation.get("common.cancel"));
		cancelButton.addActionListener(e -> cancelStep1());

		step1NextButton = new JButton(Translation.get("subMapDialog.step1.next"));
		step1NextButton.setEnabled(false);
		step1NextButton.addActionListener(e ->
		{
			if (selBoundsRI != null && validateStep1Spinners() == null)
			{
				disposeStep1();
				showStep2();
			}
		});

		buttonsPanel.add(step1NextButton);
		buttonsPanel.add(Box.createHorizontalStrut(5));
		buttonsPanel.add(cancelButton);

		JPanel bottomRow = new JPanel(new BorderLayout());
		bottomRow.add(step1ErrorLabel, BorderLayout.LINE_START);
		bottomRow.add(buttonsPanel, BorderLayout.LINE_END);
		organizer.addLeftAlignedComponent(bottomRow, topInset, GridBagOrganizer.rowVerticalInset, false);

		step1Dialog.add(organizer.panel);
		step1Dialog.setPreferredSize(new Dimension(step1Dialog.getPreferredSize().width + 35, step1Dialog.getPreferredSize().height + 15));
		step1Dialog.pack();
		step1Dialog.setMinimumSize(step1Dialog.getSize());
		java.awt.Point parentLocation = mainWindow.getLocation();
		Dimension parentSize = mainWindow.getSize();
		Dimension dialogSize = step1Dialog.getSize();
		step1Dialog.setLocation(parentLocation.x + parentSize.width / 2 - dialogSize.width / 2, parentLocation.y + parentSize.height - dialogSize.height - 18);

		// Constrain the selection box to the displayed map bounds (accounts for rotation).
		mainWindow.mapEditingPanel.setSelectionBoxConstraints(new Rectangle(0, 0, getMapDisplayWidth(), getMapDisplayHeight()));
		mainWindow.mapEditingPanel.setSelectionBoxLockedAspectRatio(selectedAspectRatio);
		mainWindow.mapEditingPanel.setSelectionBoxMaxAspectRatio(selectedAspectRatio == 0.0 ? GeneratedDimension.MAX_ASPECT_RATIO : 0.0);

		// Register the selection box handler on the main map panel.
		mainWindow.mapEditingPanel.enableSelectionBox(() ->
		{
			selBoundsRI = mainWindow.mapEditingPanel.getSelectionBoxRI();
			updateStep1SpinnersFromBox();
			updateStep1NextButton();
		});

		// Wire spinners: when edited by user, update the selection box.
		ChangeListener xYListener = e ->
		{
			if (!updatingSpinnersFromBox)
			{
				applySpinnersToSelectionBox();
			}
		};
		xSpinner.addChangeListener(xYListener);
		ySpinner.addChangeListener(xYListener);

		widthSpinner.addChangeListener(e ->
		{
			if (!updatingSpinnersFromBox)
			{
				if (selectedAspectRatio != 0.0)
				{
					int w = ((Number) widthSpinner.getValue()).intValue();
					int h = (int) Math.round(w / selectedAspectRatio);
					h = Math.max(1, Math.min(getMapDisplayHeight(), h));
					updatingSpinnersFromBox = true;
					try
					{
						heightSpinner.setValue(h);
					}
					finally
					{
						updatingSpinnersFromBox = false;
					}
				}
				applySpinnersToSelectionBox();
			}
		});

		heightSpinner.addChangeListener(e ->
		{
			if (!updatingSpinnersFromBox)
			{
				if (selectedAspectRatio != 0.0)
				{
					int h = ((Number) heightSpinner.getValue()).intValue();
					int w = (int) Math.round(h * selectedAspectRatio);
					w = Math.max(1, Math.min(getMapDisplayWidth(), w));
					updatingSpinnersFromBox = true;
					try
					{
						widthSpinner.setValue(w);
					}
					finally
					{
						updatingSpinnersFromBox = false;
					}
				}
				applySpinnersToSelectionBox();
			}
		});

		step1Dialog.addWindowListener(new WindowAdapter()
		{
			@Override
			public void windowClosing(WindowEvent e)
			{
				cancelStep1();
			}
		});

		// If we already have a selection (e.g. when going Back from step 2), sync the spinners.
		if (selBoundsRI != null)
		{
			updateStep1SpinnersFromBox();
			updateStep1NextButton();
		}

		step1Dialog.setVisible(true);
	}

	/**
	 * Applies the current spinner values as the selection box, validates, and updates the Next button and error label.
	 */
	private void applySpinnersToSelectionBox()
	{
		int x = ((Number) xSpinner.getValue()).intValue();
		int y = ((Number) ySpinner.getValue()).intValue();
		int w = ((Number) widthSpinner.getValue()).intValue();
		int h = ((Number) heightSpinner.getValue()).intValue();

		String error = validateSpinnerValues(x, y, w, h);
		step1ErrorLabel.setText(error != null ? error : " ");

		if (error == null)
		{
			selBoundsRI = new Rectangle(x, y, w, h);
			mainWindow.mapEditingPanel.setSelectionBoxRI(selBoundsRI);
		}
		updateStep1NextButton();
	}

	/**
	 * Validates spinner values. Returns an error message string, or null if valid.
	 */
	private String validateSpinnerValues(int x, int y, int w, int h)
	{
		if (w <= 0 || h <= 0)
		{
			return Translation.get("subMapDialog.error.widthHeightMin");
		}
		if (x < 0 || y < 0)
		{
			return Translation.get("subMapDialog.error.xyMin");
		}
		if (x + w > getMapDisplayWidth())
		{
			return Translation.get("subMapDialog.error.xWidthExceeds", getMapDisplayWidth());
		}
		if (y + h > getMapDisplayHeight())
		{
			return Translation.get("subMapDialog.error.yHeightExceeds", getMapDisplayHeight());
		}
		return null;
	}

	/**
	 * Returns the current validation error for the spinners, or null if valid.
	 */
	private String validateStep1Spinners()
	{
		return validateSpinnerValues(((Number) xSpinner.getValue()).intValue(), ((Number) ySpinner.getValue()).intValue(), ((Number) widthSpinner.getValue()).intValue(),
				((Number) heightSpinner.getValue()).intValue());
	}

	/**
	 * Updates the spinner values to reflect the current selBoundsRI. Guards against recursive updates.
	 */
	private void updateStep1SpinnersFromBox()
	{
		if (xSpinner == null || selBoundsRI == null)
		{
			return;
		}
		updatingSpinnersFromBox = true;
		try
		{
			xSpinner.setValue((int) Math.round(selBoundsRI.x));
			ySpinner.setValue((int) Math.round(selBoundsRI.y));
			widthSpinner.setValue(Math.max(1, (int) Math.round(selBoundsRI.width)));
			heightSpinner.setValue(Math.max(1, (int) Math.round(selBoundsRI.height)));
			step1ErrorLabel.setText(" ");
		}
		finally
		{
			updatingSpinnersFromBox = false;
		}
		updateStep1NextButton();
	}

	private void updateStep1NextButton()
	{
		if (step1NextButton == null)
		{
			return;
		}
		step1NextButton.setEnabled(selBoundsRI != null && validateStep1Spinners() == null);
	}

	/**
	 * Adjusts the selection box to match the given aspect ratio (width / height), keeping the top-left corner fixed and clamping to the map bounds.
	 */
	private Rectangle adjustSelectionBoxToAspectRatio(Rectangle box, double ratio)
	{
		double newHeight = box.width / ratio;
		// Clamp height to map bounds.
		newHeight = Math.min(newHeight, getMapDisplayHeight() - box.y);
		newHeight = Math.max(1, newHeight);
		// If height was clamped, back-compute width to maintain ratio.
		double newWidth = newHeight * ratio;
		newWidth = Math.min(newWidth, getMapDisplayWidth() - box.x);
		newWidth = Math.max(1, newWidth);
		return new Rectangle(box.x, box.y, newWidth, newHeight);
	}

	private void cancelStep1()
	{
		mainWindow.mapEditingPanel.clearSelectionBox();
		if (step1Dialog != null)
		{
			step1Dialog.dispose();
			step1Dialog = null;
		}
		mainWindow.enableOrDisableFieldsThatRequireMap(true, mainWindow.getSettingsFromGUI(false), true);
	}

	private void disposeStep1()
	{
		if (step1Dialog != null)
		{
			step1Dialog.dispose();
			step1Dialog = null;
		}
	}

	// -------------------------------------------------------------------------
	// Step 2: Detail level + preview
	// -------------------------------------------------------------------------

	private void showStep2()
	{
		// Generate a stable seed for this step-2 session so repeated redraws produce the same Voronoi graph.
		// Use nextInt so the seed fits in an integer and displays as a readable value in the seed field.
		subMapSeed = Helper.safeAbs(new Random().nextInt());

		step2Dialog = new JDialog(mainWindow, Translation.get("subMapDialog.step2.title"), true);
		step2Dialog.setDefaultCloseOperation(JDialog.DO_NOTHING_ON_CLOSE);
		step2Dialog.setResizable(true);
		step2Dialog.setSize(900, 775);
		step2Dialog.setMinimumSize(new Dimension(600, 500));

		JPanel mainPanel = new JPanel(new BorderLayout(5, 5));
		mainPanel.setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8));

		// -- Top control area (GridBagOrganizer for aligned labels and fields) --
		GridBagOrganizer controlOrganizer = new GridBagOrganizer();
		final int topInset = 2;

		// Compute the 1× polygon count for this selection to use as the default slider value.
		double origMapAreaForDefault = origSettings.generatedWidth * (double) origSettings.generatedHeight;
		double selAreaForDefault = selBoundsRI.width * selBoundsRI.height;
		oneXWorldSize = origSettings.worldSize * selAreaForDefault / origMapAreaForDefault;
		clampedOneXWorldSize = computeDefaultWorldSize(origSettings, selBoundsRI);
		boolean matchDetailPossible = oneXWorldSize >= minPolygonsInSubMap;

		// Advice label explaining key sub-map limitations.
		JLabel adviceLabel = new JLabel(Translation.get("subMapDialog.step2.advice"));
		controlOrganizer.addLeftAlignedComponent(adviceLabel, 0, 8, false);

		// Number of polygons: radio buttons to choose between matching source detail or a custom level.
		JRadioButton matchSourceRadio = new JRadioButton(Translation.get("subMapDialog.step2.matchSourceDetail", clampedOneXWorldSize));
		customRadio = new JRadioButton(Translation.get("subMapDialog.step2.choose"));
		ButtonGroup detailModeGroup = new ButtonGroup();
		detailModeGroup.add(matchSourceRadio);
		detailModeGroup.add(customRadio);
		// Default: match source detail, unless the selected area is too small to reach the minimum polygon count.
		if (matchDetailPossible)
		{
			matchSourceRadio.setSelected(true);
		}
		else
		{
			matchSourceRadio.setEnabled(false);
			customRadio.setSelected(true);
		}

		controlOrganizer.addLabelAndComponentsHorizontalWithTopInset(Translation.get("subMapDialog.step2.numberOfPolygons.label"), "", Arrays.asList(matchSourceRadio, customRadio), topInset);

		// Explanation shown when the selected area is too small to match the source detail level.
		if (!matchDetailPossible)
		{
			JLabel matchDetailDisabledLabel = new JLabel(Translation.get("subMapDialog.step2.matchDetailDisabled", minPolygonsInSubMap));
			matchDetailDisabledLabel.setForeground(warningMessageColor);
			controlOrganizer.addLeftAlignedComponent(matchDetailDisabledLabel, 0, 4, false);
		}

		// Slider row (shown only in Choose mode).
		JSlider rawSlider = new JSlider(minPolygonsInSubMap, SettingsGenerator.maxWorldSize, clampedOneXWorldSize);
		rawSlider.setMajorTickSpacing(8000);
		rawSlider.setMinorTickSpacing(1000);
		rawSlider.setPaintTicks(true);
		rawSlider.setPaintLabels(true);
		rawSlider.setSnapToTicks(true);

		detailSliderWithValue = new SliderWithDisplayedValue(rawSlider, value ->
		{
			double origMapArea = origSettings.generatedWidth * (double) origSettings.generatedHeight;
			double selArea = selBoundsRI.width * selBoundsRI.height;
			double oneX = origSettings.worldSize * selArea / origMapArea;
			double ratio = (oneX > 0) ? value / oneX : 1.0;
			return Translation.get("subMapDialog.step2.sliderDisplay", String.format("%.1f", ratio), value);
		}, () ->
		{
			triggerPreviewRedraw();
		}, null);
		detailSlider = detailSliderWithValue.slider;
		String polygonsTooltip = Translation.get("subMapDialog.step2.polygons.tooltip", minPolygonsInSubMap, SettingsGenerator.maxWorldSize);
		sliderRowHider = detailSliderWithValue.addToOrganizer(controlOrganizer, "", polygonsTooltip);
		sliderRowHider.setVisible(!matchDetailPossible);

		// Warning shown when Choose mode is selected (indented to match slider).
		JLabel customWarningLabel = new JLabel(Translation.get("subMapDialog.step2.customWarning"));
		customWarningLabel.setForeground(warningMessageColor);
		JPanel customWarningWrapper = new JPanel(new BorderLayout());
		customWarningWrapper.setBorder(BorderFactory.createEmptyBorder(0, 20, 0, 0));
		customWarningWrapper.add(customWarningLabel);
		customWarningRowHider = controlOrganizer.addLeftAlignedComponent(customWarningWrapper, 2, 8, false);
		customWarningRowHider.setVisible(!matchDetailPossible);

		// Wire radio button listeners.
		matchSourceRadio.addActionListener(e ->
		{
			if (previewUpdater != null)
			{
				previewUpdater.cancel();
			}
			previewPanel.setImage(null);
			sliderRowHider.setVisible(false);
			customWarningRowHider.setVisible(false);
			step2Dialog.validate();
			triggerPreviewRedraw();
		});
		customRadio.addActionListener(e ->
		{
			if (previewUpdater != null)
			{
				previewUpdater.cancel();
			}
			previewPanel.setImage(null);
			sliderRowHider.setVisible(true);
			customWarningRowHider.setVisible(true);
			step2Dialog.validate();
			triggerPreviewRedraw();
		});

		// Random seed.
		seedTextField = new JTextField(String.valueOf(subMapSeed), 10);
		seedTextField.setMaximumSize(new Dimension(seedTextField.getPreferredSize().width, seedTextField.getPreferredSize().height));
		seedTextField.getDocument().addDocumentListener(new DocumentListener()
		{
			private void handleChange()
			{
				try
				{
					subMapSeed = Long.parseLong(seedTextField.getText());
					triggerPreviewRedraw();
				}
				catch (NumberFormatException ex)
				{
					// Ignore invalid input; don't redraw.
				}
			}

			@Override
			public void insertUpdate(DocumentEvent e)
			{
				handleChange();
			}

			@Override
			public void removeUpdate(DocumentEvent e)
			{
				if (!seedTextField.getText().isEmpty())
				{
					handleChange();
				}
			}

			@Override
			public void changedUpdate(DocumentEvent e)
			{
				handleChange();
			}
		});
		JButton newSeedButton = new JButton(Translation.get("theme.newSeed"));
		newSeedButton.setToolTipText(Translation.get("theme.newSeed.tooltip"));
		newSeedButton.addActionListener(e -> seedTextField.setText(String.valueOf(Helper.safeAbs(new Random().nextInt()))));
		controlOrganizer.addLabelAndComponentsHorizontalWithTopInset(Translation.get("subMapDialog.step2.randomSeed.label"), "", Arrays.asList(seedTextField, newSeedButton), topInset);

		mainPanel.add(controlOrganizer.panel, BorderLayout.NORTH);

		// -- Preview area --
		JPanel previewWrapper = new JPanel(new BorderLayout(0, 4));

		JLabel previewLabel = new JLabel(Translation.get("subMapDialog.step2.preview.label"));
		previewLabel.setBorder(BorderFactory.createEmptyBorder(0, 0, 2, 0));
		previewWrapper.add(previewLabel, BorderLayout.NORTH);

		BufferedImage placeholder = AwtBridge.toBufferedImage(nortantis.platform.ImageHelper.getInstance()
				.createPlaceholderImage(new String[] { Translation.get("subMapDialog.step2.drawingPreview") }, AwtBridge.fromAwtColor(SwingHelper.getTextColorForPlaceholderImages())));
		previewPanel = new MapEditingPanel(placeholder);

		previewContainer = new JPanel(new FlowLayout(FlowLayout.CENTER, 0, 0));
		previewContainer.add(previewPanel);

		previewWrapper.add(previewContainer, BorderLayout.CENTER);

		mainPanel.add(previewWrapper, BorderLayout.CENTER);

		// -- Bottom: progress bar + buttons --
		JPanel bottomPanel = new JPanel();
		bottomPanel.setLayout(new BoxLayout(bottomPanel, BoxLayout.X_AXIS));

		previewProgressBar = new JProgressBar();
		previewProgressBar.setStringPainted(true);
		previewProgressBar.setString(Translation.get("newSettingsDialog.drawing"));
		previewProgressBar.setIndeterminate(true);
		previewProgressBar.setVisible(false);
		bottomPanel.add(previewProgressBar);

		progressBarTimer = new Timer(50, e -> previewProgressBar.setVisible(previewUpdater != null && previewUpdater.isMapBeingDrawn()));
		progressBarTimer.setInitialDelay(500);

		bottomPanel.add(Box.createHorizontalGlue());

		JPanel buttonRow = new JPanel(new FlowLayout(FlowLayout.RIGHT, 5, 2));

		JButton backButton = new JButton(Translation.get("subMapDialog.step2.back"));
		backButton.addActionListener(e ->
		{
			stopPreviewUpdater();
			step2Dialog.dispose();
			step2Dialog = null;
			// Restore constraints and aspect ratio when going back.
			mainWindow.mapEditingPanel.setSelectionBoxConstraints(new Rectangle(0, 0, getMapDisplayWidth(), getMapDisplayHeight()));
			mainWindow.mapEditingPanel.setSelectionBoxLockedAspectRatio(selectedAspectRatio);
			mainWindow.mapEditingPanel.setSelectionBoxRI(selBoundsRI);
			showStep1();
		});

		JButton cancelButton = new JButton(Translation.get("common.cancel"));
		cancelButton.addActionListener(e ->
		{
			stopPreviewUpdater();
			mainWindow.mapEditingPanel.clearSelectionBox();
			step2Dialog.dispose();
			step2Dialog = null;
			mainWindow.enableOrDisableFieldsThatRequireMap(true, mainWindow.getSettingsFromGUI(false), true);
		});

		createButton = new JButton(Translation.get("subMapDialog.step2.create"));
		createButton.addActionListener(e -> handleCreate());

		buttonRow.add(backButton);
		buttonRow.add(createButton);
		buttonRow.add(Box.createHorizontalStrut(5));
		buttonRow.add(cancelButton);
		bottomPanel.add(buttonRow);
		mainPanel.add(bottomPanel, BorderLayout.SOUTH);

		step2Dialog.add(mainPanel);

		step2DialogOpened = false;

		// Build the preview MapUpdater.
		createPreviewUpdater();

		// Wire dialog resize to re-trigger preview.
		// Guard against the spurious componentResized that fires during initial layout (before windowOpened).
		step2Dialog.addComponentListener(new ComponentAdapter()
		{
			@Override
			public void componentResized(ComponentEvent e)
			{
				if (!step2DialogOpened)
				{
					return;
				}
				triggerPreviewRedraw();
			}
		});

		step2Dialog.addWindowListener(new WindowAdapter()
		{
			@Override
			public void windowOpened(WindowEvent e)
			{
				step2DialogOpened = true;
				// Trigger the first draw here, after the dialog is visible and its container is sized.
				triggerPreviewRedraw();
			}

			@Override
			public void windowClosing(WindowEvent e)
			{
				stopPreviewUpdater();
				mainWindow.mapEditingPanel.clearSelectionBox();
				step2Dialog.dispose();
				step2Dialog = null;
				mainWindow.enableOrDisableFieldsThatRequireMap(true, mainWindow.getSettingsFromGUI(false), true);
			}
		});

		step2Dialog.setLocationRelativeTo(mainWindow);

		step2Dialog.setVisible(true);
	}

	private void createPreviewUpdater()
	{
		previewUpdater = new MapUpdater(false)
		{
			@Override
			protected void onBeginDraw()
			{
			}

			@Override
			public MapSettings getSettingsFromGUI()
			{
				// Called on background thread by MapUpdater.
				try
				{
					boolean redistributeIcons = customRadio != null && customRadio.isSelected();
					MapSettings settings = SubMapCreator.createSubMapSettings(origSettings, origGraph, selBoundsRI, getSubmapWorldSize(), origResolution, subMapSeed, redistributeIcons);
					// Set resolution to 1.0 as a baseline; MapCreator.createMap will override it via
					// Background.calcMapBoundsAndAdjustResolutionIfNeeded to fit the maxMapSize passed to the updater.
					settings.resolution = 1.0;
					lastSubMapSettings = settings;
					return settings;
				}
				catch (Exception e)
				{
					SwingHelper.handleException(e, step2Dialog, false);
					throw e;
				}
			}

			@Override
			protected void onFinishedDrawingFull(Image map, boolean anotherDrawIsQueued, int borderPaddingAsDrawn, List<String> warningMessages)
			{
				SwingUtilities.invokeLater(() ->
				{
					if (previewPanel.mapFromMapCreator != null && previewPanel.mapFromMapCreator != map)
					{
						previewPanel.mapFromMapCreator.close();
					}
					previewPanel.mapFromMapCreator = map;
					previewPanel.setImage(AwtBridge.toBufferedImage(map));
					previewPanel.setBorderPadding(borderPaddingAsDrawn);

					if (!anotherDrawIsQueued)
					{
						enableOrDisableProgressBar(false);
					}

					if (step2Dialog != null)
					{
						step2Dialog.revalidate();
						step2Dialog.repaint();
					}
				});
			}

			@Override
			protected void onFinishedDrawingIncremental(boolean anotherDrawIsQueued, int borderPaddingAsDrawn, IntRectangle incrementalChangeArea, List<String> warningMessages)
			{
				// Preview only does full redraws.
			}

			@Override
			protected void onFailedToDraw(Exception exception)
			{
				SwingUtilities.invokeLater(() -> enableOrDisableProgressBar(false));
				if (exception != null)
				{
					SwingHelper.handleException(exception, step2Dialog, false);
				}
			}

			@Override
			protected MapEdits getEdits()
			{
				MapSettings s = lastSubMapSettings;
				return s != null ? s.edits : null;
			}

			@Override
			protected Image getCurrentMapForIncrementalUpdate()
			{
				return previewPanel.mapFromMapCreator;
			}
		};
		previewUpdater.setEnabled(true);
	}

	private void triggerPreviewRedraw()
	{
		if (previewUpdater == null)
		{
			return;
		}
		// Defer to the next EDT cycle, then force a second validate() pass. HTML labels have a
		// two-pass layout problem: their preferred height depends on their width, which is only
		// known after the first layout pass. Callers (radio button listeners) already call
		// validate() once before reaching here; the second pass here settles the correct height.
		SwingUtilities.invokeLater(() ->
		{
			if (previewUpdater == null)
			{
				return;
			}
			if (step2Dialog != null)
			{
				step2Dialog.validate();
			}
			nortantis.geom.Dimension size = getPreviewContainerSize();
			if (size == null)
			{
				return;
			}
			previewUpdater.setMaxMapSize(size);
			enableOrDisableProgressBar(true);
			previewUpdater.createAndShowMapFull();
		});
	}

	private void enableOrDisableProgressBar(boolean enable)
	{
		if (progressBarTimer == null || previewProgressBar == null)
		{
			return;
		}
		if (enable)
		{
			progressBarTimer.start();
		}
		else
		{
			progressBarTimer.stop();
			previewProgressBar.setVisible(false);
		}
	}

	private nortantis.geom.Dimension getPreviewContainerSize()
	{
		if (previewContainer == null)
		{
			return null;
		}
		int width = previewContainer.getWidth();
		int height = previewContainer.getHeight();
		if (width <= 0 || height <= 0)
		{
			return null;
		}
		double scale = previewPanel != null ? previewPanel.osScale : 1.0;
		return new nortantis.geom.Dimension(width * scale, height * scale);
	}

	private int getSubmapWorldSize()
	{
		if (customRadio.isSelected())
		{
			return detailSlider.getValue();
		}
		return clampedOneXWorldSize;
	}

	/**
	 * Returns the displayed map width in RI units, accounting for rotation (90°/270° swaps generatedWidth and generatedHeight).
	 */
	private int getMapDisplayWidth()
	{
		return (origSettings.rightRotationCount == 1 || origSettings.rightRotationCount == 3) ? origSettings.generatedHeight : origSettings.generatedWidth;
	}

	/**
	 * Returns the displayed map height in RI units, accounting for rotation (90°/270° swaps generatedWidth and generatedHeight).
	 */
	private int getMapDisplayHeight()
	{
		return (origSettings.rightRotationCount == 1 || origSettings.rightRotationCount == 3) ? origSettings.generatedWidth : origSettings.generatedHeight;
	}

	private void handleCreate()
	{
		MapSettings settings = lastSubMapSettings;
		if (settings == null)
		{
			JOptionPane.showMessageDialog(step2Dialog, Translation.get("subMapDialog.step2.notReady"), Translation.get("subMapDialog.step2.notReady.title"), JOptionPane.INFORMATION_MESSAGE);
			return;
		}
		stopPreviewUpdater();
		mainWindow.mapEditingPanel.clearSelectionBox();
		step2Dialog.dispose();
		step2Dialog = null;
		mainWindow.enableOrDisableFieldsThatRequireMap(true, settings, true);
		mainWindow.clearOpenSettingsFilePath();
		mainWindow.loadSettingsIntoGUI(settings);
	}

	private void stopPreviewUpdater()
	{
		enableOrDisableProgressBar(false);
		if (previewUpdater != null)
		{
			previewUpdater.cancel();
			previewUpdater.setEnabled(false);
		}
	}
}
