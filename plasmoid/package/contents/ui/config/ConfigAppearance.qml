import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore

import "../libconfig" as LibConfig

LibConfig.FormKCM {
	id: root

	// The Desktop section below only means anything for a widget placed on the
	// desktop (Planar); in a panel the panel owns the background and the widget
	// never shows its full representation there, so hide it.
	readonly property bool onDesktop: plasmoid.formFactor === PlasmaCore.Types.Planar


	// ── Font: shared by both surfaces ─────────────────────────
	LibConfig.Heading {
		text: i18n("Font")
	}
	LibConfig.FontFamily {
		Kirigami.FormData.label: i18n("Font Family:")
		configKey: 'fontFamily'
		monospaceOnly: true
	}
	LibConfig.TextFormat {
		boldConfigKey: 'bold'
		italicConfigKey: 'italic'
		underlineConfigKey: 'underline'
		alignConfigKey: 'textAlign'
		vertAlignConfigKey: 'vertAlign'
	}

	// ── Panel ─────────────────────────────────────────────────
	LibConfig.Heading {
		text: i18n("Panel")
	}
	LibConfig.SpinBox {
		Kirigami.FormData.label: i18n("Font Size:")
		configKey: 'fontSize'
		suffix: i18n("px")
	}
	LibConfig.SpinBox {
		Kirigami.FormData.label: i18n("Line Height:")
		configKey: 'panelLineHeight'
		decimals: 2
		minimumValue: 0.5
		maximumValue: 3.0
		stepSize: 5
	}

	// ── Tooltip ───────────────────────────────────────────────
	LibConfig.Heading {
		text: i18n("Tooltip")
	}
	RowLayout {
		Kirigami.FormData.label: i18n("Font Size:")
		spacing: 0
		LibConfig.CheckBox {
			configKey: 'useTooltipFontSize'
		}
		LibConfig.SpinBox {
			configKey: 'tooltipFontSize'
			suffix: i18n("px")
		}
	}
	LibConfig.SpinBox {
		Kirigami.FormData.label: i18n("Line Height:")
		configKey: 'tooltipLineHeight'
		decimals: 2
		minimumValue: 0.5
		maximumValue: 3.0
		stepSize: 5
	}

	// ── Desktop ───────────────────────────────────────────────
	// Two looks for the widget on the desktop (ignored in a panel, which has its
	// own background). With a background it's a solid panel; without, it's
	// transparent on the wallpaper and the text is forced to one flat color for
	// legibility — white or black — with threshold colors kept.
	LibConfig.Heading {
		visible: root.onDesktop
		text: i18n("Desktop")
	}
	LibConfig.CheckBox {
		visible: root.onDesktop
		Kirigami.FormData.label: i18n("Background:")
		configKey: 'showBackground'
		text: i18n("Solid panel behind the widget")
	}
	LibConfig.ColorField {
		visible: root.onDesktop
		Kirigami.FormData.label: i18n("Text color (no background):")
		configKey: 'desktopTextColor'
		defaultColor: '#ffffff'
		showAlphaChannel: false
	}
	LibConfig.CheckBox {
		visible: root.onDesktop
		Kirigami.FormData.label: i18n("Outline:")
		configKey: 'desktopOutline'
		text: i18n("Halo around the text")
	}
	LibConfig.ColorField {
		visible: root.onDesktop
		Kirigami.FormData.label: i18n("Outline color:")
		configKey: 'desktopOutlineColor'
		defaultColor: '#000000'
		showAlphaChannel: false
	}
	LibConfig.CheckBox {
		visible: root.onDesktop
		Kirigami.FormData.label: i18n("Width:")
		configKey: 'desktopFit'
		text: i18n("Fill the widget width")
	}
	LibConfig.SpinBox {
		visible: root.onDesktop && plasmoid.configuration.desktopFit
		Kirigami.FormData.label: i18n("Width, px (0 = fit text):")
		configKey: 'desktopWidth'
		minimumValue: 0
		maximumValue: 2000
	}

}
