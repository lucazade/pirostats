// Version 4

import QtQuick

import org.kde.plasma.plasma5support as Plasma5Support

import "." as LibConfig

LibConfig.ComboBox {
	id: configFontFamily

	property bool monospaceOnly: false

	populated: false

	// Qt doesn't expose QFontDatabase::isFixedPitch() to QML, so the monospace
	// families come from fontconfig, which knows the property (spacing=mono). Color
	// fonts are left out (emoji are flagged mono too). One async call when the page
	// opens; the first name on each line is the family, the rest are its aliases.
	readonly property string monospaceCommand: "fc-list ':spacing=mono:color=false' family"

	Plasma5Support.DataSource {
		id: fontconfig
		engine: "executable"
		onNewData: (source, data) => {
			disconnectSource(source)
			configFontFamily.monospaceFamilies(data["stdout"])
		}
	}

	// "Default" is the fixed-width font of the KDE font settings when only
	// monospace fonts are offered, otherwise the UI font.
	function defaultEntry() {
		return { text: monospaceOnly ? i18n("Default (system fixed-width font)")
		                             : i18nc("Use default font", "Default"), value: "" }
	}

	function monospaceFamilies(output) {
		var installed = {}
		var all = Qt.fontFamilies()
		for (var i = 0; i < all.length; i++)
			installed[all[i]] = true
		var seen = {}
		var names = []
		var lines = output.split("\n")
		for (var j = 0; j < lines.length; j++) {
			var name = lines[j].split(",")[0].trim()
			if (name && installed[name] && !seen[name]) {
				seen[name] = true
				names.push(name)
			}
		}
		names.sort(function(a, b) { return a.localeCompare(b) })
		var arr = [defaultEntry()]
		for (var k = 0; k < names.length; k++)
			arr.push({ text: names[k], value: names[k] })
		// Not populated while the model changes, or the reset of the current index
		// would write the empty default into the config.
		populated = false
		model = arr
		selectValue(configValue)
		populated = true
	}

	// Based on: org.kde.plasma.digitalclock
	onPopulate: {
		var arr = [defaultEntry()]
		if (monospaceOnly) {
			model = arr
			fontconfig.connectSource(monospaceCommand)
		} else {
			var fonts = Qt.fontFamilies()
			for (var i = 0; i < fonts.length; i++)
				arr.push({ text: fonts[i], value: fonts[i] })
			model = arr
		}
		populated = true
	}
}
