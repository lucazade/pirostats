import QtQuick
import QtQuick.Layouts
import QtCore
import Qt.labs.folderlistmodel

import org.kde.kirigami as Kirigami
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponent
import org.kde.plasma.plasma5support as Plasma5Support

PlasmoidItem {
	id: widget

	// https://github.com/KDE/plasma-workspace/blob/master/dataengines/executable/executable.h
	// https://github.com/KDE/plasma-workspace/blob/master/dataengines/executable/executable.cpp
	// https://github.com/KDE/plasma-framework/blob/master/src/declarativeimports/core/datasource.h
	// https://github.com/KDE/plasma-framework/blob/master/src/declarativeimports/core/datasource.cpp
	// https://github.com/KDE/plasma-framework/blob/master/src/plasma/scripting/dataenginescript.cpp
	Plasma5Support.DataSource {
		id: executable
		engine: "executable"
		connectedSources: []
		onNewData: (sourceName, data) => {
			var exitCode = data["exit code"]
			var exitStatus = data["exit status"]
			var stdout = data["stdout"]
			var stderr = data["stderr"]
			exited(sourceName, exitCode, exitStatus, stdout, stderr)
			disconnectSource(sourceName) // cmd finished
		}
		function exec(cmd) {
			if (cmd) {
				connectSource(cmd)
			}
		}
		signal exited(string cmd, int exitCode, int exitStatus, string stdout, string stderr)
	}

	// Force a unique source per invocation. The executable DataSource keys its
	// sources by the command string and silently drops connectSource() for a
	// string that is still connected; with a no-output command like
	// `pirostats page next` the engine can also suppress the (identical, empty)
	// data update, so the source never delivers newData, is never disconnected,
	// and every later same-direction scroll is a no-op — the "wheel goes dead
	// after a quick reverse" bug. A trailing shell comment (# n) makes each call
	// a distinct source that runs and disconnects cleanly. Safe: the engine runs
	// the command through a shell. Only fire-and-forget actions (click, paging)
	// use this; the panel/tooltip cat commands must keep their stable source
	// names, which onExited matches on.
	property int execNonce: 0
	function execOnce(cmd) {
		if (cmd)
			executable.exec(cmd + " # " + (++execNonce))
	}

	function performClick() {
		execOnce(plasmoid.configuration.clickCommand)
	}

	function performMouseWheelUp() {
		execOnce(plasmoid.configuration.mousewheelUpCommand)
	}

	function performMouseWheelDown() {
		execOnce(plasmoid.configuration.mousewheelDownCommand)
	}

	// One wheel gesture, one page. A movement of the wheel emits a burst of events
	// (this device sends several per flick); we turn a page on the FIRST event of
	// a gesture, then stay quiet for the rest of it. A gesture is "in progress"
	// until the events stop for wheelGestureGapMs — every event pushes that idle
	// deadline out — so a long/continuous scroll is still ONE page, and paging
	// again needs a fresh gesture (a brief pause, then scroll). This is the leading
	// edge + idle-reset, not the old fixed-window debounce, which re-fired every
	// window and paged continuously while you kept scrolling. Tune wheelGestureGapMs:
	// longer groups more into one gesture (and lengthens the pause needed to page
	// again); shorter splits a bursty flick sooner. Shared by panel + pinned popup.
	readonly property int wheelGestureGapMs: 200
	property bool wheelInGesture: false
	function wheelStep(delta) {
		if (delta === 0)
			return
		if (!wheelInGesture) {
			wheelInGesture = true
			if (delta > 0)
				performMouseWheelUp()
			else
				performMouseWheelDown()
			// No catch-up needed: the notch bumps the daemon's page counter, the daemon
			// rewrites the tooltip, and the watcher brings it in on its own.
		}
		wheelGestureEnd.restart()   // any event extends the current gesture
	}
	// The gesture ends once the wheel has been idle this long; the next event then
	// starts a new gesture (and turns the next page).
	Timer {
		id: wheelGestureEnd
		interval: widget.wheelGestureGapMs
		onTriggered: widget.wheelInGesture = false
	}

	// The family every text here is drawn with: the configured one if it is
	// installed, otherwise the fixed-width font of the KDE font settings. Never the
	// proportional UI font: the layout pads columns with spaces, so it only holds
	// in a monospace font.
	readonly property string textFamily: {
		var f = plasmoid.configuration.fontFamily
		return (f && Qt.fontFamilies().indexOf(f) >= 0) ? f : Kirigami.Theme.fixedWidthFont.family
	}

	Item {
		id: config
		// The daemon publishes into a per-user runtime directory, and we resolve the
		// very same one it does: QStandardPaths' RuntimeLocation IS $XDG_RUNTIME_DIR
		// on Linux, which is what the daemon reads (see src/runtime.py). So the path
		// is written down in neither the kcfg nor the daemon's config — the two sides
		// derive it and cannot drift apart. It also can't be a kcfg default: /run/user/
		// <uid> isn't knowable when the package is built and installed system-wide.
		// writableLocation returns a url ("file:///run/user/1000"); the cats need a
		// plain path, the watcher needs the url.
		readonly property string runtimeDir:
			StandardPaths.writableLocation(StandardPaths.RuntimeLocation)
				.toString().replace(/^file:\/\//, "") + "/pirostats"
		readonly property url runtimeUrl: "file://" + runtimeDir
		// Not knobs: these two cats and the geometry write below are the whole
		// contract with the daemon, not commands anyone is meant to swap out.
		readonly property string command: "cat " + runtimeDir + "/panel.html"
		readonly property string tooltipCommand: "cat " + runtimeDir + "/tooltip.html"
		readonly property string geomFile: runtimeDir + "/state/geom"
		readonly property bool clickEnabled: !!plasmoid.configuration.clickCommand
		readonly property bool mousewheelEnabled: (plasmoid.configuration.mousewheelUpCommand || plasmoid.configuration.mousewheelDownCommand)
		readonly property color textColor: plasmoid.configuration.textColor || Kirigami.Theme.textColor
		readonly property color outlineColor: plasmoid.configuration.outlineColor || Kirigami.Theme.backgroundColor
		readonly property bool showOutline: plasmoid.configuration.showOutline

	}

	// https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
	property var ansiColors: ({
		30: '#000000', // Black
		31: '#aa0000', // Red
		32: '#00aa00', // Green
		33: '#aa6500', // Yellow
		34: '#0000aa', // Blue
		35: '#aa00aa', // Magenta
		36: '#00aaaa', // Cyan
		37: '#aaaaaa', // White
		90: '#656565', // Bright Black
		91: '#ff6565', // Bright Red
		92: '#65ff65', // Bright Green
		93: '#ffff65', // Bright Yellow
		94: '#6565ff', // Bright Blue
		95: '#ff65ff', // Bright Magenta
		96: '#65ffff', // Bright Cyan
		97: '#ffffff', // Bright White
	})
	function resetState(state) {
		var out = state.closeTags.join(' ')
		state.bold = false
		state.closeTags = []
		return out
	}
	function parseAnsiCode(n, i, tokens, state) {
		if (n == 0) { // Reset
			return resetState(state)
		} else if (n == 1) {
			state.closeTags.push('</b>')
			state.bold = true
			return '<b>'
		} else if (30 <= n && n <= 37 || 90 <= n && n <= 97) {
			if (state.bold && 30 <= n && n <= 37) {
				// Bold also intensifies the colors to "Bright".
				// 30 => 90
				n += 60
			}
			var hexColor = ansiColors[n]
			state.closeTags.push('</font>')
			return '<font color="' + hexColor + '">'
		} else {
			return ''
		}
	}
	// https://stackoverflow.com/questions/4745317/converting-integers-to-hex-string-in-javascript
	function formatHexInt(n) {
		var num = Number(n)
		if (isNaN(num)) {
			return "00"
		}
		num = Math.max(0, Math.min(num, 255))
		var str = num.toString(16)
		return str.length == 1 ? '0' + str : str
	}
	function rgbToHex(r, g, b) {
		return '#' + formatHexInt(r) + formatHexInt(g) + formatHexInt(b)
	}
	function parseColorMode(i, tokens) {
		var colorMode = parseInt(tokens[++i], 10)
		if (colorMode == 2) { // RGB
			var r = parseInt(tokens[++i], 10)
			var g = parseInt(tokens[++i], 10)
			var b = parseInt(tokens[++i], 10)
			return rgbToHex(r, g, b)
		} else if (colorMode == 5) { // Preset of 256 colors
			// Logic taken from Konsole
			// https://invent.kde.org/utilities/konsole/-/blob/master/src/autotests/CharacterColorTest.cpp#L159
			var n = parseInt(tokens[++i], 10)
			if (0 <= n && n <= 7) { // Normal
				var u = n + 30
				return ansiColors[u]
			} else if (8 <= n && n <= 15) { // Bright
				var u = n - 8 + 90
				return ansiColors[u]
			} else if (16 <= n && n <= 231) { // 212
				var u = n - 16
				var r = Math.floor(((u / 36) % 6) != 0 ? (40 * ((u / 36) % 6) + 55) : 0)
				var g = Math.floor(((u / 6) % 6) != 0 ? (40 * ((u / 6) % 6) + 55) : 0)
				var b = Math.floor(((u / 1) % 6) != 0 ? (40 * ((u / 1) % 6) + 55) : 0)
				return rgbToHex(r, g, b)
			} else if (232 <= n && n <= 255) {
				var gray = Math.floor((n - 232) * 10 + 8)
				return rgbToHex(gray, gray, gray)
			}
		}
		return null
	}
	function parseAnsiEscape(codes, state) {
		var tokens = codes.split(';')
		var out = ''
		for (var i = 0; i < tokens.length; i++) {
			tokens[i] = parseInt(tokens[i], 10)
		}
		for (var i = 0; i < tokens.length; i++) {
			var token = tokens[i]
			if (token == 38) { // Set FG
				var hexColor = parseColorMode(i, tokens)
				if (hexColor) {
					state.closeTags.push('</font>')
					out += '<font color="' + hexColor + '">'
				}
			} else if (token == 48) { // Set BG
				var hexColor = parseColorMode(i, tokens)
				// Ignore
			} else {
				out += parseAnsiCode(token, i, tokens, state)
			}
		}
		return out
	}

	property string outputText: ''
	property string tooltipText: ''
	// Whether the panel widget is currently hovered (the hover tooltip is shown).
	// Set from the compact representation's HoverHandler (panelHover), which fires
	// reliably on the first hover. readOutputs reads it (with `expanded`) as the gate
	// on the tooltip's share of each watcher notification.
	property bool tooltipHovered: false

	// Desktop "no background" look: with the widget transparent on the wallpaper,
	// the daemon's own base colors (grey labels, cyan titles, dark "good"/"active"
	// values) lose contrast. Force those to one flat color — white or black, the
	// user's pick — by appending a CSS rule that overrides exactly those classes.
	// It lands last inside the daemon's <style>, so at equal specificity it wins.
	// Threshold classes (.warn/.crit/.deactive) and the colored bars are left out,
	// so alerts keep their meaning. Plain values carry no color class and inherit
	// the Text color instead (set on pinText), so this rule need not name them.
	function desktopRecolor(html, color) {
		// The inactive pager dots (pages you haven't scrolled to) stay dimmer than
		// the active one: the chosen text color blended halfway to mid-grey. Kept a
		// solid 6-digit color on purpose — Qt's RichText CSS may not parse the
		// 8-digit alpha form — and it works for any picked color, not a fixed grey.
		var c = Qt.color(color)
		var dim = Qt.rgba((c.r + 0.5) / 2, (c.g + 0.5) / 2, (c.b + 0.5) / 2, 1).toString()
		// Each selector must be at least as specific as the base rule it overrides,
		// or the base wins despite ours coming later: .active is set by the base as
		// ".tooltip .active" (two classes), so a bare ".active" here would lose and
		// e.g. the SMART "OK" value would keep its default color.
		var rule = ".tooltip .title,.tooltip .label,.tooltip .aux,.good,.tooltip .active,"
			+ ".tooltip .pager .on,.tooltip .page{color:" + color + ";}"
			+ ".tooltip .pager .off{color:" + dim + ";}"
			// The rule under each section title is a thin block tinted with
			// background-color, not color, so recolor it separately to match.
			+ ".tooltip .title-rule{background-color:" + color + ";}"
		return html.replace("</style>", rule + "</style>")
	}

	function formatOutputText(stdout) {
		var formattedText = stdout

		// Newlines
		if (plasmoid.configuration.replaceAllNewlines) {
			formattedText = formattedText.replace(/\n/g, ' ').trim()
		} else if (formattedText.length >= 1 && formattedText[formattedText.length-1] == '\n') {
			formattedText = formattedText.substr(0, formattedText.length-1)
		}

		// Terminal Colors (Issue #7)
		var state = {
			html: false,
			bold: false,
			closeTags: [],
		}
		formattedText = formattedText.replace(/\033\[(\d+(;\d+)*)?m/g, function(match, p1, p2){
			state.html = true
			if (typeof p1 === 'string') {
				return parseAnsiEscape(p1, state)
			} else { // \033[m is Reset
				return parseAnsiEscape('0', state)
			}
		})
		formattedText += resetState(state)

		// Output is always rendered as rich text (see textFormat: Text.RichText
		// below), so newlines must always be encoded, and bold must be applied
		// as an explicit <b> tag: font.weight on the Text item does not reliably
		// propagate into nested <font> spans once the content contains HTML
		// (the color tags above) — same mechanism already used for the ANSI
		// bold escape (\033[1m) in parseAnsiCode().
		formattedText = formattedText.replace(/\n/g, '<br>')
		if (plasmoid.configuration.bold) {
			formattedText = '<b>' + formattedText + '</b>'
		}
		return formattedText
	}

	Connections {
		target: executable
		function onExited(cmd, exitCode, exitStatus, stdout, stderr) {
			// Match by prefix: the tooltip/paging commands carry a " # <nonce>"
			// suffix (see execOnce) to dodge the DataSource's duplicate-source
			// dropping. The two base commands cat different files, so neither is a
			// prefix of the other.
			var isPanel = config.command !== "" && cmd.indexOf(config.command) === 0
			var isTooltip = config.tooltipCommand !== "" && cmd.indexOf(config.tooltipCommand) === 0
			if (isPanel || isTooltip) {
				var formattedText = formatOutputText(stdout)

				// console.log('[pirostats]', 'stdout', JSON.stringify(stdout))
				// console.log('[pirostats]', 'format', JSON.stringify(formattedText))

				if (isPanel) {
					widget.outputText = formattedText
				} else if (isTooltip) {
					widget.tooltipText = formattedText
				}
			}
		}
	}

	function runCommand() {
		// console.log('[pirostats]', Date.now(), 'runCommand', config.command)
		executable.exec(config.command)
	}

	function runTooltipCommand() {
		// Nonce it (execOnce) so a rapid re-cat is never dropped as a duplicate
		// source: catching up after a page change fires several cats within a few
		// hundred ms, and an identical-output cat could otherwise leave the source
		// connected and swallow the next one. onExited matches by prefix.
		execOnce(config.tooltipCommand)
	}

	// One clock, and it is not ours: the daemon's poll_interval. It writes panel.html
	// and tooltip.html back to back on every poll, and we read on the notification
	// rather than on a tick of our own — so a frame reaches the panel as soon as it
	// exists, instead of aging up to a poll first, and a poll_interval the applet
	// never hears about can no longer alias against our own rate.
	//
	// Watching the DIRECTORY, not the files, is load-bearing: the daemon publishes by
	// writing a tmp and renaming over the target, which swaps the inode out from under
	// any watch on the file itself. This is inotify (FolderListModel), not an mtime
	// poll — verified to catch every write at a 700ms cadence, faster than mtime's 1s
	// granularity could resolve. Nothing else may live in that directory; see
	// src/runtime.py.
	FolderListModel {
		id: outputWatcher
		folder: config.runtimeUrl
		nameFilters: ["*.html"]
		showDirs: false
		// nameFilters picks the model's ROWS, it does not filter notifications: every
		// write in the directory notifies every model watching it. That costs nothing
		// here — the daemon writes both files every poll anyway, so there is nothing
		// to tell apart. One poll emits a handful of signals (two files, each with a
		// rename-over tmp); the debounce collapses them into exactly one read.
		onDataChanged: readDebounce.restart()
		onRowsInserted: readDebounce.restart()
	}

	// Not a clock: no rate of its own, only a coalescing window. Restarted by every
	// notification, so it fires once the directory has been quiet for 50ms — i.e.
	// once per poll, after the last rename, with both files complete on disk.
	Timer {
		id: readDebounce
		interval: 50
		onTriggered: widget.readOutputs()
	}

	// Boot-race recovery. The daemon's systemd unit is ordered After=graphical-
	// session.target, i.e. after plasmashell, so at login the runtime directory
	// usually does not exist yet when this applet loads. A FolderListModel pointed
	// at a missing directory never attaches its inotify watch and never notices the
	// directory being created and filled later — so the panel stays blank until the
	// widget is removed and re-added (reported as "blank on every reboot"). Until the
	// first frame arrives, re-point the watcher (re-assigning folder forces a fresh
	// scan and a fresh watch now that the directory may exist, exactly what remove/
	// re-add does by hand) and re-cat. The `running` binding stops it on its own the
	// moment the first non-empty read lands, so it costs nothing once we're up.
	Timer {
		id: bootstrap
		interval: 1000
		repeat: true
		running: widget.outputText === ""
		onTriggered: {
			outputWatcher.folder = ""
			outputWatcher.folder = config.runtimeUrl
			widget.runCommand()
		}
	}

	function readOutputs() {
		widget.runCommand()
		// The tooltip read stays lazy — the panel's is not. Qt reparses and relays out
		// the tooltip's RichText on every text change, so re-reading it while nobody is
		// looking is work for no one. There is no catching up to do when a hover starts:
		// the daemon renders every poll regardless, so the file is always fresh.
		if (widget.tooltipHovered || widget.expanded)
			widget.runTooltipCommand()
	}

	Component.onCompleted: {
		// The watcher only speaks when the directory CHANGES, so without a read at load
		// the panel would stay blank until the daemon's next poll. The tooltip seed also
		// gives ToolTipArea.enabled something to be true about before the first hover —
		// see tooltipArea below.
		widget.runCommand()
		widget.runTooltipCommand()
	}

	Plasmoid.onActivated: widget.performClick()

	// Pinning (the full-representation popup) opens the tooltip gate: readOutputs
	// keeps it live from here on, the same lazy refresh the hover gets. The read
	// right now is what fills the popup before the first poll lands in it.
	onExpandedChanged: {
		if (widget.expanded)
			widget.runTooltipCommand()
	}

	// The hover tooltip: our HTML content fed to the shell's own tooltip area
	// (CompactApplet wraps the compact representation in a ToolTipArea bound to
	// Plasmoid.toolTipItem). This replaces the default name/description tooltip
	// and, being active only while !expanded, steps aside when the popup is
	// pinned. Content is fetched lazily — only while the panel is hovered
	// (panelHover), never on the panel's main tick.
	toolTipItem: Item {
		id: tooltipRoot
		// Width from the hidden NoWrap twin, not tooltipText.contentWidth: block
		// <div>s fill the given width, so contentWidth latches to the window and
		// never shrinks. Height shrinks fine, so it comes from contentHeight. The
		// padding is added back so the content's right/bottom edge isn't clipped;
		// pinning Layout min=max=implicit forces Plasma's shared ToolTipDialog to
		// follow the content size down as well as up.
		implicitWidth: plasmoid.configuration.useTooltipWidth ? plasmoid.configuration.tooltipWidth : (ttMeasure.contentWidth + ttText.leftPadding + ttText.rightPadding)
		implicitHeight: ttText.contentHeight + ttText.topPadding + ttText.bottomPadding
		Layout.minimumWidth: implicitWidth
		Layout.maximumWidth: implicitWidth
		Layout.preferredWidth: implicitWidth
		Layout.minimumHeight: implicitHeight
		Layout.maximumHeight: implicitHeight
		Layout.preferredHeight: implicitHeight

		Text {
			id: ttMeasure
			visible: false
			width: 1
			text: widget.tooltipText
			textFormat: Text.RichText
			wrapMode: Text.NoWrap
			elide: Text.ElideNone
			font.pointSize: plasmoid.configuration.useTooltipFontSize ? plasmoid.configuration.tooltipFontSize : plasmoid.configuration.fontSize
			font.family: widget.textFamily
			font.weight: plasmoid.configuration.bold ? Font.Bold : Font.Normal
			font.italic: plasmoid.configuration.italic
			font.underline: plasmoid.configuration.underline
			fontSizeMode: Text.FixedSize
		}

		Text {
			id: ttText
			anchors.fill: parent
			text: widget.tooltipText
			textFormat: Text.RichText
			color: config.textColor
			horizontalAlignment: Text.AlignLeft
			wrapMode: Text.NoWrap
			elide: Text.ElideNone
			font.pointSize: plasmoid.configuration.useTooltipFontSize ? plasmoid.configuration.tooltipFontSize : plasmoid.configuration.fontSize
			font.family: widget.textFamily
			font.weight: plasmoid.configuration.bold ? Font.Bold : Font.Normal
			font.italic: plasmoid.configuration.italic
			font.underline: plasmoid.configuration.underline
			fontSizeMode: Text.FixedSize
			leftPadding: 8
			rightPadding: 8
			topPadding: 8
			bottomPadding: 8
			lineHeight: plasmoid.configuration.tooltipLineHeight
			lineHeightMode: Text.ProportionalHeight
			linkColor: Kirigami.Theme.linkColor
			onLinkActivated: Qt.openUrlExternally(link)
		}
	}

	// Plasma's own widget background, on when showBackground is set (the desktop
	// "with background" look); off leaves the widget transparent on the wallpaper
	// (the "conky" look, where legibility comes from the forced text color in
	// pinText). In a panel the panel supplies the background regardless.
	Plasmoid.backgroundHints: plasmoid.configuration.showBackground ? PlasmaCore.Types.DefaultBackground : PlasmaCore.Types.NoBackground

	// The pinned popup (middle-click) is the only way this applet expands, and it
	// is dismissed the same way — middle-click again. Keep it open when it loses
	// focus (a click elsewhere) instead of Plasma's default auto-hide, so it can
	// stay parked while you work in another window and watch the live pages.
	hideOnWindowDeactivate: false

	compactRepresentation: Item {
		id: panelItem

		readonly property bool isHorizontal: plasmoid.formFactor == PlasmaCore.Types.Horizontal
		readonly property bool isVertical: plasmoid.formFactor == PlasmaCore.Types.Vertical
		readonly property bool isInPanel: isHorizontal || isVertical
		readonly property bool isOnDesktop: !isInPanel

		// pirostats: republish the geometry when the panel orientation changes.
		// output.width can change (and trigger publishGeometry) BEFORE isVertical
		// updates, leaving a stale orientation flag in the file; this guarantees a
		// republish with the correct value once the orientation settles.
		onIsVerticalChanged: output.publishGeometry()

		readonly property int itemWidth: {
			if (isOnDesktop) {
				return Math.ceil(output.contentWidth)
			} else if (isHorizontal && plasmoid.configuration.useFixedWidth) {
				return plasmoid.configuration.fixedWidth * Kirigami.Units.devicePixelRatio
			} else { // isHorizontal || isVertical
				return Math.ceil(output.implicitWidth)
			}
		}
		Layout.minimumWidth: isHorizontal ? itemWidth : -1
		Layout.fillWidth: isVertical
		Layout.preferredWidth: itemWidth // Panel widget default
		// width: itemWidth // Desktop widget default
		// onItemWidthChanged: console.log('itemWidth', itemWidth, 'implicitWidth', output.implicitWidth, 'contentWidth', output.contentWidth)

		readonly property int itemHeight: {
			if (isOnDesktop) {
				return Math.ceil(output.contentHeight)
			} else if (isVertical && plasmoid.configuration.useFixedHeight) {
				return plasmoid.configuration.fixedHeight * Kirigami.Units.devicePixelRatio
			} else { // isHorizontal || isVertical
				return Math.ceil(output.implicitHeight)
			}
		}
		Layout.minimumHeight: isVertical ? itemHeight : -1
		Layout.fillHeight: isHorizontal
		Layout.preferredHeight: itemHeight // Panel widget default
		// height: itemHeight // Desktop widget default
		// onItemHeightChanged: console.log('itemHeight', itemHeight, 'implicitHeight', output.implicitHeight, 'contentHeight', output.contentHeight)


		// Drive the tooltip's lazy refresh off the panel-widget hover. This is
		// the reliable "the hover tooltip is (about to be) shown" signal: the
		// shell shows its ToolTipArea while the pointer is over the compact
		// representation, and HoverHandler.hovered flips on the FIRST hover too.
		// Relying on toolTipItem.onVisibleChanged alone missed that first show
		// (the item is created visible=true, so the dialog displaying it produced
		// no visible-change edge) — the tooltip stayed frozen on the first hover
		// until you left and re-entered. See readOutputs for the why of lazy.
		HoverHandler {
			id: panelHover
			onHoveredChanged: {
				if (widget.expanded)
					return   // pinned: onExpandedChanged owns the refresh
				// tooltipHovered IS the gate readOutputs consults; setting it is what
				// opens and closes the tooltip's share of the watcher.
				widget.tooltipHovered = hovered
				if (hovered)
					widget.runTooltipCommand()   // show the current frame, don't wait for the next
			}
		}

		// Note MouseArea is below the Text so
		// that we don't eat the link clicks.
		MouseArea {
			id: mouseArea
			anchors.fill: parent
			hoverEnabled: config.clickEnabled

			cursorShape: output.hoveredLink ? Qt.PointingHandCursor : Qt.ArrowCursor

			// Middle-click pins the tooltip: it toggles the full representation,
			// a persistent popup showing the same tooltipText. Left-click still
			// runs the configured click command; the wheel still pages.
			acceptedButtons: Qt.LeftButton | Qt.MiddleButton
			onClicked: (mouse) => {
				if (mouse.button === Qt.MiddleButton)
					widget.expanded = !widget.expanded
				else
					widget.performClick()
			}

			// One page per notch — see widget.wheelStep.
			onWheel: (wheel) => {
				wheel.accepted = true
				widget.wheelStep(wheel.angleDelta.y || wheel.angleDelta.x)
			}
		}

		Text {
			id: output
			width: parent.width
			height: parent.height

			text: widget.outputText
			textFormat: Text.RichText

			// pirostats: publish the panel's real geometry (the Text's usable width
			// in px, the real advance of one mono glyph in px, orientation) to a
			// file the daemon reads to auto-fit the bar and width, without guessing
			// DPI or margins. advanceWidth via TextMetrics because with font.pointSize
			// the resolved pixelSize isn't readable. Reuses the widget's DataSource
			// for the shell-out; updates on a width change (panel resize) or font
			// change (glyph advance change).
			TextMetrics {
				id: glyphMetrics
				font: output.font
				text: "██████████"   // 10 mono blocks
				onAdvanceWidthChanged: output.publishGeometry()
			}
			// Same advance but at the tooltip font (separate pointSize): the daemon
			// uses it to size the graphs-page PNGs to the tooltip text's real width,
			// without guessing DPI.
			TextMetrics {
				id: tooltipGlyphMetrics
				font.family: output.font.family
				font.pointSize: plasmoid.configuration.useTooltipFontSize ? plasmoid.configuration.tooltipFontSize : output.font.pointSize
				text: "██████████"   // 10 mono blocks
				onAdvanceWidthChanged: output.publishGeometry()
			}
			function publishGeometry() {
				if (output.width > 0 && glyphMetrics.advanceWidth > 0) {
					// mkdir -p: we can publish before the daemon has ever run, and the
					// geometry lives in the runtime tree's state/ subdirectory — out of
					// the watched directory, since it churns on every panel resize and
					// has nothing to show. Already a shell (the redirect), so the mkdir
					// costs no extra process.
					executable.exec(
						"sh -c 'mkdir -p " + config.runtimeDir + "/state && "
						+ "printf \"%s %s %s %s\\n\" " + output.width + " "
						+ (glyphMetrics.advanceWidth / 10) + " "
						+ (panelItem.isVertical ? 1 : 0) + " "
						+ (tooltipGlyphMetrics.advanceWidth / 10)
						+ " > " + config.geomFile + "'")
				}
			}
			onWidthChanged: publishGeometry()
			Component.onCompleted: publishGeometry()

			color: config.textColor
			style: config.showOutline ? Text.Outline : Text.Normal
			styleColor: config.outlineColor

			linkColor: Kirigami.Theme.linkColor
			onLinkActivated: Qt.openUrlExternally(link)

			font.pointSize: plasmoid.configuration.fontSize
			font.family: widget.textFamily
			font.weight: plasmoid.configuration.bold ? Font.Bold : Font.Normal
			font.italic: plasmoid.configuration.italic
			font.underline: plasmoid.configuration.underline
			fontSizeMode: Text.FixedSize
			horizontalAlignment: plasmoid.configuration.textAlign
			verticalAlignment: plasmoid.configuration.vertAlign
			lineHeight: plasmoid.configuration.panelLineHeight
			lineHeightMode: Text.ProportionalHeight

			property bool isFixedWidth: {
				if (plasmoid.formFactor == PlasmaCore.Types.Planar) { // Desktop Widget
					return true
				} else if (plasmoid.formFactor == PlasmaCore.Types.Horizontal) {
					return plasmoid.configuration.useFixedWidth
				} else if (plasmoid.formFactor == PlasmaCore.Types.Vertical) {
					return true
				} else {
					return false
				}
			}
			elide: Text.ElideRight
			wrapMode: isFixedWidth ? Text.Wrap : Text.NoWrap
		}

	}

	// pirostats: the pinned tooltip. Middle-click on the panel toggles this
	// popup; it renders the same tooltipText as the hover tooltip but persists
	// (Plasma keeps a full representation up until you click away or toggle it),
	// so you can watch the graphs live. Page it by scrolling the PANEL while it's
	// up (the popup follows); scrolling the popup itself is intentionally inert.
	activationTogglesExpanded: false   // left-click runs the click command, not expand

	fullRepresentation: Item {
		id: pinItem
		// Size the popup to the tooltip content, same trick as the hover tooltip:
		// a hidden NoWrap twin gives the natural width (the visible RichText's
		// block <div>s otherwise fill and latch to the popup width). Plain
		// content-fit: paging is done on the PANEL (a stable surface), never on
		// the popup, so the popup never resizes out from under the cursor and can
		// simply follow the content of whatever page the panel selected.
		Layout.minimumWidth: pinMeasure.contentWidth + pinText.leftPadding + pinText.rightPadding
		Layout.maximumWidth: Layout.minimumWidth
		Layout.preferredWidth: Layout.minimumWidth
		Layout.minimumHeight: pinText.contentHeight + pinText.topPadding + pinText.bottomPadding
		Layout.maximumHeight: Layout.minimumHeight
		Layout.preferredHeight: Layout.minimumHeight

		// This same full representation serves two very different contexts: inline
		// on the desktop (the widget itself) and the pinned popup from a panel. The
		// desktop-only behaviors below (its own background, outlined text, wheel
		// paging) key off this — the popup keeps Plasma's dialog background and the
		// panel drives its paging.
		readonly property bool onDesktop: plasmoid.formFactor === PlasmaCore.Types.Planar

		MouseArea {
			anchors.fill: parent
			acceptedButtons: Qt.MiddleButton
			onClicked: widget.expanded = false   // middle-click again un-pins
			// Wheel paging is enabled ONLY on the desktop, where this full
			// representation IS the widget and there's no panel to page from. In
			// the pinned popup it stays inert: scrolling would resize the popup out
			// from under the cursor on a shorter page (a stick, glitchy on resize),
			// so there you page from the panel and the popup follows. On the desktop
			// the widget is inline — a resize just regrows it in place, no popup to
			// lose focus.
			onWheel: (wheel) => {
				if (!pinItem.onDesktop)
					return
				wheel.accepted = true
				widget.wheelStep(wheel.angleDelta.y || wheel.angleDelta.x)
			}
		}

		Text {
			id: pinMeasure
			visible: false
			width: 1
			text: widget.tooltipText
			textFormat: Text.RichText
			wrapMode: Text.NoWrap
			elide: Text.ElideNone
			font.pointSize: (onDesktop || plasmoid.configuration.useTooltipFontSize) ? plasmoid.configuration.tooltipFontSize : plasmoid.configuration.fontSize
			font.family: widget.textFamily
			font.weight: plasmoid.configuration.bold ? Font.Bold : Font.Normal
			font.italic: plasmoid.configuration.italic
			font.underline: plasmoid.configuration.underline
			fontSizeMode: Text.FixedSize
		}

		Text {
			id: pinText
			anchors.fill: parent
			// In the transparent desktop mode, force the base text color (plain
			// values inherit this Text color) and recolor the classed base text via
			// the injected CSS rule; thresholds stay untouched. Elsewhere (panel
			// popup, or desktop with a background) the daemon's own colors stand.
			readonly property bool conkyMode: pinItem.onDesktop && !plasmoid.configuration.showBackground
			// Empty config means "default" (ColorField stores "" for its default),
			// so fall back to the kcfg defaults here.
			readonly property string txtColor: plasmoid.configuration.desktopTextColor || "#ffffff"
			readonly property string outlineColor: plasmoid.configuration.desktopOutlineColor || "#000000"
			text: conkyMode ? widget.desktopRecolor(widget.tooltipText, txtColor)
			                : widget.tooltipText
			textFormat: Text.RichText
			color: conkyMode ? txtColor : config.textColor
			// Optional halo in conky mode so the text reads on a busy wallpaper; it
			// rings every glyph, threshold colors included. Color is the user's pick
			// (default black, which sits well under white text).
			style: (conkyMode && plasmoid.configuration.desktopOutline) ? Text.Outline : Text.Normal
			styleColor: outlineColor
			horizontalAlignment: Text.AlignLeft
			wrapMode: Text.NoWrap
			elide: Text.ElideNone
			font.pointSize: (onDesktop || plasmoid.configuration.useTooltipFontSize) ? plasmoid.configuration.tooltipFontSize : plasmoid.configuration.fontSize
			font.family: widget.textFamily
			font.weight: plasmoid.configuration.bold ? Font.Bold : Font.Normal
			font.italic: plasmoid.configuration.italic
			font.underline: plasmoid.configuration.underline
			fontSizeMode: Text.FixedSize
			leftPadding: 8
			rightPadding: 8
			topPadding: 8
			bottomPadding: 8
			lineHeight: plasmoid.configuration.tooltipLineHeight
			lineHeightMode: Text.ProportionalHeight
			linkColor: Kirigami.Theme.linkColor
			onLinkActivated: Qt.openUrlExternally(link)
		}
	}
}
