#!/usr/bin/env bash
# Reverses install.sh: stops the --user service, removes the system files under PREFIX
# (default /usr; via sudo when you are not root), the applet and the icon, then clears
# the runtime files. Your ~/.config/pirostats is left alone. Remove the widget from your
# panel too (right-click → Remove).
# Run it from a clone or from $PREFIX/share/pirostats/uninstall.sh. The user steps are for
# the user who ran it (SUDO_USER under sudo).
# With DESTDIR set it works under it, needs no root and touches neither systemd nor
# kpackagetool6.
# No "set -e": a step that fails must not stop the rest of the cleanup.
set -uo pipefail

APPLET_ID="com.github.lucazade.pirostats"
PREFIX="${PREFIX:-/usr}"
DESTDIR="${DESTDIR:-}"
LIB_DIR="$DESTDIR$PREFIX/lib/pirostats"
LINK="$DESTDIR$PREFIX/bin/pirostats"
SHARE_DIR="$DESTDIR$PREFIX/share/pirostats"
UNIT="$DESTDIR$PREFIX/lib/systemd/user/pirostats.service"
ICON="$DESTDIR$PREFIX/share/icons/hicolor/scalable/apps/pirostats.svg"

SUDO=""
if [ -z "$DESTDIR" ] && [ "$(id -u)" -ne 0 ]; then
	command -v sudo >/dev/null || { echo "[error] not root and sudo not found: run this as root." >&2; exit 1; }
	SUDO="sudo"
fi

# The user whose service is stopped and whose runtime files go: yourself, or SUDO_USER.
USER_CMD=()
user_steps=no
rt_dir="" cache_dir=""
if [ -n "$DESTDIR" ]; then
	:
elif [ "$(id -u)" -ne 0 ]; then
	user_steps=yes
	# Mirrors runtime.py's _runtime_dir: $XDG_RUNTIME_DIR/pirostats, else /tmp/pirostats-$UID.
	rt_dir="${XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/pirostats}"
	rt_dir="${rt_dir:-/tmp/pirostats-$(id -u)}"
	cache_dir="$HOME/.cache/pirostats"
elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && uid="$(id -u "$SUDO_USER" 2>/dev/null)"; then
	home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
	rt_dir="/run/user/$uid/pirostats"
	cache_dir="$home/.cache/pirostats"
	if command -v runuser >/dev/null && [ -S "/run/user/$uid/bus" ]; then
		USER_CMD=(runuser -u "$SUDO_USER" -- env "HOME=$home" "XDG_RUNTIME_DIR=/run/user/$uid"
			"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus")
		user_steps=yes
	fi
fi

if [ "$user_steps" = yes ]; then
	${USER_CMD[@]+"${USER_CMD[@]}"} systemctl --user disable --now pirostats 2>/dev/null || true
fi

$SUDO rm -f "$UNIT"
$SUDO rm -f "$LINK"
$SUDO rm -rf "$LIB_DIR"
$SUDO rm -f "$ICON"
$SUDO rm -f "$SHARE_DIR/.commit" "$SHARE_DIR/uninstall.sh"
$SUDO rmdir "$SHARE_DIR" 2>/dev/null || true
if [ -z "$DESTDIR" ] && command -v kpackagetool6 >/dev/null; then
	# Its message only when it removed something ("not installed" is not worth printing).
	if out="$($SUDO kpackagetool6 --type Plasma/Applet --global --remove "$APPLET_ID" 2>&1)"; then
		echo "$out"
	fi
fi

if [ "$user_steps" = yes ]; then
	${USER_CMD[@]+"${USER_CMD[@]}"} systemctl --user daemon-reload 2>/dev/null || true
fi

# Runtime tree (panel/tooltip HTML, page counter, geometry), then the one-shot
# render/profile files, which stay in /tmp on purpose — see runtime.py.
if [ -n "$rt_dir" ]; then
	rm -rf "$rt_dir" 2>/dev/null || true
	rm -rf "$cache_dir" 2>/dev/null || true
fi
if [ -z "$DESTDIR" ]; then
	rm -f /tmp/pirostats_* 2>/dev/null || true
fi

if [ -n "$DESTDIR" ]; then
	echo "PiroStats uninstalled from $DESTDIR$PREFIX."
elif [ "$user_steps" = no ] && [ "$(id -u)" -eq 0 ]; then
	echo "PiroStats uninstalled. No graphical session to reach: stop the service as your user:"
	echo "  systemctl --user disable --now pirostats"
else
	echo "PiroStats uninstalled. (Remove the widget from your panel if it's still there.)"
fi
echo "Your config in ~/.config/pirostats was kept; delete it by hand if you want it gone."
