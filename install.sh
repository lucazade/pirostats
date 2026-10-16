#!/usr/bin/env bash
# System-wide install of PiroStats, mirroring the distro-package layout (PREFIX defaults
# to /usr, the same paths as the AUR package):
#   $PREFIX/lib/pirostats/             code + shipped defaults (read-only)
#   $PREFIX/bin/pirostats              the CLI (relative link; resolves the tree beside it)
#   $PREFIX/lib/systemd/user/          the --user service unit
#   $PREFIX/share/icons/...            the applet icon
#   $PREFIX/share/pirostats/           .commit (the commit it was installed from) and a copy
#                                      of uninstall.sh (so it can be removed without a clone)
#   the applet, global, through kpackagetool6 (Plasma decides where it goes)
# Run it as yourself or as root (sudo ./install.sh). One flow: the files go in
# as root (sudo is used for them when you are not root); enabling the service and
# refreshing the widget is per-user and is done for the user who ran the script
# (SUDO_USER when it runs under sudo). If that user has no graphical session
# (ssh, a plain root login) the commands to run are printed instead. Your customization
# lives in ~/.config/pirostats and is never touched here. Re-run to upgrade.
# --enable is accepted and does nothing: the service is always enabled.
# DESTDIR (a staging folder, for packages and tests) is put in front of every path, needs
# no root and touches neither systemd, Plasma nor kpackagetool6.
#
# This is the manual, non-AUR channel; the AUR PKGBUILD produces the same layout.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLET_ID="com.github.lucazade.pirostats"
PREFIX="${PREFIX:-/usr}"
DESTDIR="${DESTDIR:-}"
LIB_DIR="$DESTDIR$PREFIX/lib/pirostats"
BIN_DIR="$DESTDIR$PREFIX/bin"
SHARE_DIR="$DESTDIR$PREFIX/share/pirostats"
UNIT_DIR="$DESTDIR$PREFIX/lib/systemd/user"
ICON_DIR="$DESTDIR$PREFIX/share/icons/hicolor/scalable/apps"

for arg in "$@"; do
	case "$arg" in
		--enable) ;;
		*) echo "usage: install.sh [--enable]" >&2; exit 2 ;;
	esac
done

SUDO=""
if [ -z "$DESTDIR" ] && [ "$(id -u)" -ne 0 ]; then
	command -v sudo >/dev/null || { echo "[error] not root and sudo not found: run this as root." >&2; exit 1; }
	SUDO="sudo"
fi

if [ -z "$DESTDIR" ] && ! command -v kpackagetool6 >/dev/null; then
	echo "[error] kpackagetool6 not found — Plasma 6 is required." >&2
	exit 1
fi

echo "Installing PiroStats to $DESTDIR$PREFIX"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Copy to a new name, then rename over the old file.
put() { # put <mode> <source> <destination>
	$SUDO install -m "$1" "$2" "$3.new"
	$SUDO mv -f "$3.new" "$3"
}

# ── code tree + CLI (root) ────────────────────────────────────────────────────
$SUDO mkdir -p "$BIN_DIR" "$SHARE_DIR" "$UNIT_DIR" "$ICON_DIR" "$(dirname "$LIB_DIR")"
$SUDO chmod 755 "$SHARE_DIR"

# The new tree is built beside the old one and swapped in: nothing from an older
# version stays behind.
$SUDO rm -rf "$LIB_DIR.new" "$LIB_DIR.old"
$SUDO mkdir -m 755 "$LIB_DIR.new"
$SUDO cp -r "$REPO_DIR/src" "$REPO_DIR/style" "$REPO_DIR/lang" "$REPO_DIR/config" "$LIB_DIR.new/"
$SUDO install -m755 "$REPO_DIR/pirostats" "$LIB_DIR.new/pirostats"
$SUDO find "$LIB_DIR.new" -name __pycache__ -type d -prune -exec rm -rf {} +
# whatever the umask or the clone had: directories 755, files 644, executables stay
$SUDO chmod -R u=rwX,go=rX "$LIB_DIR.new"
if [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; then
	$SUDO chown -R root:root "$LIB_DIR.new"
fi
if [ -e "$LIB_DIR" ]; then
	$SUDO mv "$LIB_DIR" "$LIB_DIR.old"
fi
$SUDO mv "$LIB_DIR.new" "$LIB_DIR"
$SUDO rm -rf "$LIB_DIR.old"

# A relative link, so that it also works under another PREFIX or a DESTDIR. It replaces
# the absolute link of an earlier install instead of writing through it.
$SUDO ln -sfn "../lib/pirostats/pirostats" "$BIN_DIR/pirostats.new"
$SUDO mv -fT "$BIN_DIR/pirostats.new" "$BIN_DIR/pirostats"

# ── systemd --user unit + icon (root) ─────────────────────────────────────────
# The unit names the CLI by its full path, which follows PREFIX.
sed "s|/usr/bin/pirostats|$PREFIX/bin/pirostats|g" "$REPO_DIR/service/pirostats.service" >"$TMP/pirostats.service"
put 644 "$TMP/pirostats.service" "$UNIT_DIR/pirostats.service"
put 644 "$REPO_DIR/plasmoid/package/contents/icons/pirostats.svg" "$ICON_DIR/pirostats.svg"

# ── .commit + uninstall.sh (root) ─────────────────────────────────────────────
# root runs git on a clone that may belong to someone else: safe.directory on the
# command line says it is fine for this one call.
git_here() { git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" "$@" 2>/dev/null; }
commit="$(git_here rev-parse HEAD || echo unknown)"
if [ "$commit" != unknown ] && [ -n "$(git_here status --porcelain || true)" ]; then
	commit="$commit+dirty"
fi
printf '%s\n' "$commit" >"$TMP/commit"
put 644 "$TMP/commit" "$SHARE_DIR/.commit"
put 755 "$REPO_DIR/uninstall.sh" "$SHARE_DIR/uninstall.sh"

# ── applet, global (root) ─────────────────────────────────────────────────────
# kpackagetool6 puts it where Plasma looks, whatever PREFIX is, and copies the files with
# the modes they have in the clone. So it installs from a copy with the modes set (a clone
# made under umask 077, or on a shared folder, would leave the applet unreadable for the
# user and Plasma could not load it). Under another PREFIX the three commands of the
# applet are pointed at that PREFIX on the same copy.
applet_upgraded=false
applet_done=false
if [ -z "$DESTDIR" ]; then
	cp -r "$REPO_DIR/plasmoid/package" "$TMP/package"
	chmod -R u=rwX,go=rX "$TMP/package"
	if [ "$PREFIX" != /usr ]; then
		sed -i "s|/usr/bin/pirostats|$PREFIX/bin/pirostats|g" "$TMP/package/contents/config/main.xml"
	fi
	applet_src="$TMP/package"
	if kpackagetool6 --type Plasma/Applet --global --show "$APPLET_ID" >/dev/null 2>&1; then
		$SUDO kpackagetool6 --type Plasma/Applet --global --upgrade "$applet_src"
		applet_upgraded=true
	elif ! $SUDO kpackagetool6 --type Plasma/Applet --global --install "$applet_src"; then
		# --show could not see it, yet it is there: an earlier install left it unreadable.
		$SUDO kpackagetool6 --type Plasma/Applet --global --upgrade "$applet_src"
		applet_upgraded=true
	fi
	applet_done=true
else
	echo "[note] the applet is not installed under DESTDIR: kpackagetool6 is not run"
fi

# ── the user who gets the service and the refreshed widget ────────────────────
# Yourself when you are not root. Under sudo, SUDO_USER, if that user has a
# session bus to talk to. Otherwise nobody: the commands are printed at the end.
USER_CMD=()
user_steps=no
direct=no
if [ -n "$DESTDIR" ]; then
	:
elif [ "$(id -u)" -ne 0 ]; then
	user_steps=yes
	direct=yes
elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && command -v runuser >/dev/null \
	&& uid="$(id -u "$SUDO_USER" 2>/dev/null)" && [ -S "/run/user/$uid/bus" ]; then
	home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
	USER_CMD=(runuser -u "$SUDO_USER" -- env "HOME=$home" "XDG_RUNTIME_DIR=/run/user/$uid"
		"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus")
	user_steps=yes
fi

# A step of the user that fails is a warning, not the end of the install: the files are
# already in place.
as_user() {
	if ! ${USER_CMD[@]+"${USER_CMD[@]}"} "$@"; then
		echo "[warning] failed: $*" >&2
		return 1
	fi
}

# ── enable the service + refresh the widget (per-user) ────────────────────────
if [ "$user_steps" = yes ]; then
	command -v kbuildsycoca6 >/dev/null && ${USER_CMD[@]+"${USER_CMD[@]}"} kbuildsycoca6 >/dev/null 2>&1 || true
	as_user systemctl --user daemon-reload || true
	as_user systemctl --user enable pirostats || true
	# restart (not enable --now): if a previous version is already running, --now
	# wouldn't restart it and the old code would stay in RAM.
	as_user systemctl --user restart pirostats || true

	# The shell must be restarted to load a new or upgraded applet: a new one is not in
	# "Add Widgets" until then. When systemd runs it,
	# restart its unit: that is the supported way, and it also works when this script runs
	# as root. A plasmashell started by hand (killall + kstart) is not the unit's, and
	# restarting the unit then does nothing: for that one there is no way to reach it from
	# root, so the command is printed.
	if [ "$applet_done" = true ]; then
		if ${USER_CMD[@]+"${USER_CMD[@]}"} systemctl --user is-active --quiet plasma-plasmashell.service 2>/dev/null; then
			as_user systemctl --user restart plasma-plasmashell.service || true
		elif [ "$direct" = yes ] && command -v kstart >/dev/null; then
			killall plasmashell 2>/dev/null || true
			kstart plasmashell >/dev/null 2>&1 &
		else
			echo "[note] to reload the applet, restart the shell: kquitapp6 plasmashell && kstart plasmashell"
		fi
	fi
fi

echo
echo "PiroStats installed ($commit) in $DESTDIR$PREFIX."
if [ "$user_steps" = yes ]; then
	echo "Service status:"
	as_user systemctl --user status pirostats --no-pager || true
elif [ -z "$DESTDIR" ]; then
	echo "No graphical session to reach for the user who ran this: run these as that user,"
	echo "from their session:"
	echo "  systemctl --user daemon-reload"
	echo "  systemctl --user enable pirostats"
	echo "  systemctl --user restart pirostats"
	if [ "$applet_done" = true ]; then
		echo "  systemctl --user restart plasma-plasmashell   # loads the applet"
	fi
fi
if [ "$applet_done" = true ] && [ "$applet_upgraded" = false ]; then
	echo
	echo "First install: add the 'PiroStats' widget to a panel"
	echo "(right-click the panel → Add Widgets → search 'PiroStats')."
fi
