"""install.sh / uninstall.sh, run against a DESTDIR staging folder.

Nothing here touches the real system: DESTDIR is a temp folder, HOME is a temp folder,
and the commands an install must NOT call under DESTDIR (sudo, systemctl, kpackagetool6,
runuser) are replaced by fakes that write a line to a log, so a call shows up as a
failure. The root case is run in a user namespace (`unshare -Ur`, uid 0, nothing real).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN = ("sudo", "systemctl", "kpackagetool6", "runuser", "kbuildsycoca6", "killall", "kstart")
UNSHARE = shutil.which("unshare")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not found")


@pytest.fixture
def sandbox(tmp_path):
    """A DESTDIR, a HOME, and a PATH whose forbidden commands only log themselves."""
    fake = tmp_path / "fakebin"
    fake.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("")
    for name in FORBIDDEN:
        f = fake / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 1\n')
        f.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    env = {
        "PATH": f"{fake}:{os.environ['PATH']}",
        "HOME": str(home),
        "DESTDIR": str(dest),
    }
    return {"dest": dest, "home": home, "log": log, "env": env, "tmp": tmp_path}


def run(sb, script, *args, extra_env=None, prefix=None, root=False):
    env = dict(sb["env"], **(extra_env or {}))
    if prefix:
        env["PREFIX"] = prefix
    cmd = ["bash", str(REPO / script), *args]
    if root:
        cmd = [UNSHARE, "-Ur", *cmd]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def mode(path):
    return stat.S_IMODE(path.lstat().st_mode)


def calls(sb):
    return sb["log"].read_text()


@needs_bash
def test_install_layout_and_modes(sandbox):
    r = run(sandbox, "install.sh")
    assert r.returncode == 0, r.stderr
    d = sandbox["dest"] / "usr"
    lib = d / "lib/pirostats"
    assert (lib / "src/daemon.py").is_file()
    assert (lib / "style").is_dir() and (lib / "lang").is_dir() and (lib / "config/config.toml").is_file()
    assert mode(lib / "pirostats") == 0o755
    assert mode(lib / "config/config.toml") == 0o644
    assert mode(lib) == 0o755 and mode(lib / "src") == 0o755
    assert not list(lib.rglob("__pycache__"))
    assert mode(d / "lib/systemd/user/pirostats.service") == 0o644
    assert mode(d / "share/icons/hicolor/scalable/apps/pirostats.svg") == 0o644
    assert mode(d / "share/pirostats/.commit") == 0o644
    assert mode(d / "share/pirostats/uninstall.sh") == 0o755
    assert (d / "share/pirostats/uninstall.sh").read_bytes() == (REPO / "uninstall.sh").read_bytes()
    assert not list(d.rglob("*.new")) and not list(d.rglob("*.old"))


@needs_bash
def test_executable_bits_are_kept(sandbox):
    """chmod -R u=rwX,go=rX must keep the exec bit the clone has, and add none."""
    run(sandbox, "install.sh")
    lib = sandbox["dest"] / "usr/lib/pirostats"
    for src in (REPO / "src").rglob("*"):
        if src.is_file() and "__pycache__" not in src.parts:
            got = mode(lib / src.relative_to(REPO))
            assert got == (0o755 if os.access(src, os.X_OK) else 0o644), src


@needs_bash
def test_link_is_relative_and_resolves(sandbox):
    run(sandbox, "install.sh")
    link = sandbox["dest"] / "usr/bin/pirostats"
    assert link.is_symlink()
    assert os.readlink(link) == "../lib/pirostats/pirostats"
    assert link.resolve() == (sandbox["dest"] / "usr/lib/pirostats/pirostats").resolve()
    assert (link.resolve().parent / "src").is_dir()


@needs_bash
def test_link_of_an_older_install_is_replaced_not_written_through(sandbox):
    """An older install left an absolute link; the new one replaces it, and the target
    it pointed to keeps its content."""
    bin_dir = sandbox["dest"] / "usr/bin"
    bin_dir.mkdir(parents=True)
    old_target = sandbox["tmp"] / "old-pirostats"
    old_target.write_text("old\n")
    (bin_dir / "pirostats").symlink_to(old_target)
    run(sandbox, "install.sh")
    assert os.readlink(bin_dir / "pirostats") == "../lib/pirostats/pirostats"
    assert old_target.read_text() == "old\n"


@needs_bash
def test_service_follows_prefix(sandbox):
    run(sandbox, "install.sh")
    text = (sandbox["dest"] / "usr/lib/systemd/user/pirostats.service").read_text()
    assert "ExecStart=/usr/bin/pirostats daemon" in text
    run(sandbox, "install.sh", prefix="/opt/ps")
    unit = sandbox["dest"] / "opt/ps/lib/systemd/user/pirostats.service"
    assert "ExecStart=/opt/ps/bin/pirostats daemon" in unit.read_text()
    assert "/usr/bin" not in unit.read_text()


@needs_bash
def test_commit_file(sandbox):
    run(sandbox, "install.sh")
    commit = (sandbox["dest"] / "usr/share/pirostats/.commit").read_text().strip()
    assert commit == "unknown" or commit.split("+")[0].isalnum()


@needs_bash
def test_commit_unknown_without_git(sandbox, tmp_path):
    """A copy of the tree with no .git: .commit says unknown."""
    clone = tmp_path / "clone"
    shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(".git", "__pycache__", ".*_cache"))
    r = subprocess.run(["bash", str(clone / "install.sh")], env=sandbox["env"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (sandbox["dest"] / "usr/share/pirostats/.commit").read_text() == "unknown\n"


@needs_bash
def test_stale_files_do_not_survive_a_reinstall(sandbox):
    run(sandbox, "install.sh")
    stale = sandbox["dest"] / "usr/lib/pirostats/src/gone_in_new_version.py"
    stale.write_text("x")
    r = run(sandbox, "install.sh")
    assert r.returncode == 0
    assert not stale.exists()


@needs_bash
def test_enable_is_accepted_and_unknown_argument_is_refused(sandbox):
    assert run(sandbox, "install.sh", "--enable").returncode == 0
    r = run(sandbox, "install.sh", "--bogus")
    assert r.returncode == 2 and "usage" in r.stderr
    r = run(sandbox, "uninstall.sh", "--bogus")
    assert r.returncode == 0  # uninstall has never taken arguments; it ignores them


@needs_bash
def test_destdir_calls_nothing_external(sandbox):
    """No sudo, no systemctl, no kpackagetool6 under DESTDIR, for install and uninstall."""
    assert run(sandbox, "install.sh").returncode == 0
    assert run(sandbox, "uninstall.sh").returncode == 0
    assert calls(sandbox) == ""


@needs_bash
def test_uninstall_removes_everything_and_keeps_the_config(sandbox):
    run(sandbox, "install.sh")
    cfg = sandbox["home"] / ".config/pirostats"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("mine\n")
    r = run(sandbox, "uninstall.sh")
    assert r.returncode == 0, r.stderr
    d = sandbox["dest"] / "usr"
    leftovers = [p for p in d.rglob("*") if not p.is_dir()]
    assert leftovers == [] and not (d / "bin/pirostats").is_symlink()
    assert not (d / "share/pirostats").exists()
    assert (cfg / "config.toml").read_text() == "mine\n"


@needs_bash
def test_uninstall_from_the_installed_copy(sandbox):
    """The copy left in share/pirostats removes the install with no clone around."""
    run(sandbox, "install.sh")
    copy = sandbox["dest"] / "usr/share/pirostats/uninstall.sh"
    r = subprocess.run(["bash", str(copy)], env=sandbox["env"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not (sandbox["dest"] / "usr/lib/pirostats").exists()


@needs_bash
def test_uninstall_with_nothing_installed(sandbox):
    r = run(sandbox, "uninstall.sh")
    assert r.returncode == 0, r.stderr


@needs_bash
def test_a_failed_copy_leaves_the_old_install_alone(sandbox):
    """The tree is built as .new first. A source that is missing stops the install before
    the old tree is touched."""
    run(sandbox, "install.sh")
    lib = sandbox["dest"] / "usr/lib/pirostats"
    (lib / "marker").write_text("old")
    bad = sandbox["tmp"] / "badclone"
    shutil.copytree(REPO, bad, ignore=shutil.ignore_patterns(".git", "__pycache__", ".*_cache"))
    shutil.rmtree(bad / "lang")
    r = subprocess.run(["bash", str(bad / "install.sh")], env=sandbox["env"], capture_output=True, text=True)
    assert r.returncode != 0
    assert (lib / "marker").read_text() == "old"


@needs_bash
@pytest.mark.skipif(UNSHARE is None, reason="unshare not found")
def test_root_without_a_session_prints_the_commands(sandbox):
    """Root (here: a user namespace) whose SUDO_USER has no session bus: the files and the
    applet go in, nothing of the user is touched, the commands are printed, exit 0.
    No DESTDIR (that would skip the user part), so PREFIX points inside the sandbox;
    kpackagetool6 is a fake that says "not installed yet" and then succeeds."""
    probe = subprocess.run([UNSHARE, "-Ur", "id", "-u"], capture_output=True, text=True)
    if probe.stdout.strip() != "0":
        pytest.skip("user namespaces are not available")
    kpt = Path(sandbox["env"]["PATH"].split(":")[0]) / "kpackagetool6"
    kpt.write_text(f'#!/bin/sh\necho "kpackagetool6 $*" >> "{sandbox["log"]}"\n'
                   'case "$*" in *--show*) exit 1;; esac\nexit 0\n')
    sandbox["env"].pop("DESTDIR")
    prefix = str(sandbox["tmp"] / "rootprefix")
    r = run(sandbox, "install.sh", prefix=prefix, extra_env={"SUDO_USER": "nobody"}, root=True)
    assert r.returncode == 0, r.stderr
    assert "No graphical session" in r.stdout
    assert "systemctl --user enable pirostats" in r.stdout
    assert "First install: add the 'PiroStats' widget" in r.stdout
    log = calls(sandbox)
    assert "kpackagetool6 --type Plasma/Applet --global --install" in log
    assert "systemctl" not in log and "runuser" not in log and "killall" not in log
    assert (Path(prefix) / "lib/pirostats/src/daemon.py").is_file()
    r = run(sandbox, "uninstall.sh", prefix=prefix, extra_env={"SUDO_USER": "nobody"}, root=True)
    assert r.returncode == 0, r.stderr
    assert "systemctl --user disable --now pirostats" in r.stdout
    assert not (Path(prefix) / "lib/pirostats").exists()
    assert "kpackagetool6 --type Plasma/Applet --global --remove" in calls(sandbox)


@needs_bash
@pytest.mark.skipif(UNSHARE is None, reason="unshare not found")
def test_other_prefix_points_the_applet_commands_at_it(sandbox):
    """With a PREFIX other than /usr the applet is installed from a copy whose three
    commands name that PREFIX; the repo file itself is not touched."""
    probe = subprocess.run([UNSHARE, "-Ur", "id", "-u"], capture_output=True, text=True)
    if probe.stdout.strip() != "0":
        pytest.skip("user namespaces are not available")
    kpt = Path(sandbox["env"]["PATH"].split(":")[0]) / "kpackagetool6"
    seen = sandbox["tmp"] / "main.xml.seen"
    kpt.write_text('#!/bin/sh\ncase "$*" in *--show*) exit 1;; esac\n'
                   'for a in "$@"; do last="$a"; done\n'
                   f'cp "$last/contents/config/main.xml" "{seen}"\nexit 0\n')
    sandbox["env"].pop("DESTDIR")
    before = (REPO / "plasmoid/package/contents/config/main.xml").read_bytes()
    prefix = str(sandbox["tmp"] / "pfx")
    r = run(sandbox, "install.sh", prefix=prefix, extra_env={"SUDO_USER": "nobody"}, root=True)
    assert r.returncode == 0, r.stderr
    text = seen.read_text()
    assert f"{prefix}/bin/pirostats click" in text
    assert f"{prefix}/bin/pirostats page next" in text
    assert "/usr/bin/pirostats" not in text
    assert (REPO / "plasmoid/package/contents/config/main.xml").read_bytes() == before


@needs_bash
def test_leftover_new_tree_of_an_interrupted_install_is_cleared(sandbox):
    """A .new left by an install that was cut short must not end up inside the new tree."""
    leftover = sandbox["dest"] / "usr/lib/pirostats.new"
    leftover.mkdir(parents=True)
    (leftover / "junk").write_text("x")
    assert run(sandbox, "install.sh").returncode == 0
    lib = sandbox["dest"] / "usr/lib/pirostats"
    assert not (lib / "junk").exists() and not leftover.exists()


@needs_bash
@pytest.mark.skipif(UNSHARE is None, reason="unshare not found")
def test_applet_is_installed_from_a_copy_with_sane_modes(sandbox):
    """kpackagetool6 copies the modes of its source. A clone made under umask 077 (or on a
    shared folder showing rwxrwx---) must not leave the applet unreadable for the user."""
    probe = subprocess.run([UNSHARE, "-Ur", "id", "-u"], capture_output=True, text=True)
    if probe.stdout.strip() != "0":
        pytest.skip("user namespaces are not available")
    clone = sandbox["tmp"] / "clone"
    shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(".git", "__pycache__", ".*_cache"))
    for p in (clone / "plasmoid/package").rglob("*"):
        p.chmod(0o770 if p.is_file() else 0o750)
    modes = sandbox["tmp"] / "modes.txt"
    kpt = Path(sandbox["env"]["PATH"].split(":")[0]) / "kpackagetool6"
    kpt.write_text('#!/bin/sh\ncase "$*" in *--show*) exit 1;; esac\n'
                   'for a in "$@"; do last="$a"; done\n'
                   f'find "$last" -printf "%m %P\\n" > "{modes}"\nexit 0\n')
    sandbox["env"].pop("DESTDIR")
    env = dict(sandbox["env"], PREFIX=str(sandbox["tmp"] / "pfx"), SUDO_USER="nobody")
    r = subprocess.run([UNSHARE, "-Ur", "bash", str(clone / "install.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    lines = [ln.split(" ", 1) for ln in modes.read_text().splitlines()]
    assert lines
    for m, name in lines:
        assert m in ("755", "644"), (m, name)
    assert (clone / "plasmoid/package/contents/config/main.xml").stat().st_mode & 0o777 == 0o770


@needs_bash
@pytest.mark.skipif(UNSHARE is None, reason="unshare not found")
def test_install_falls_back_to_upgrade_when_show_cannot_see_the_applet(sandbox):
    """--show says no, --install says "already there": the applet is upgraded instead."""
    probe = subprocess.run([UNSHARE, "-Ur", "id", "-u"], capture_output=True, text=True)
    if probe.stdout.strip() != "0":
        pytest.skip("user namespaces are not available")
    kpt = Path(sandbox["env"]["PATH"].split(":")[0]) / "kpackagetool6"
    kpt.write_text(f'#!/bin/sh\necho "kpackagetool6 $*" >> "{sandbox["log"]}"\n'
                   'case "$*" in *--show*|*--install*) exit 1;; esac\nexit 0\n')
    sandbox["env"].pop("DESTDIR")
    r = run(sandbox, "install.sh", prefix=str(sandbox["tmp"] / "pfx"),
            extra_env={"SUDO_USER": "nobody"}, root=True)
    assert r.returncode == 0, r.stderr
    log = calls(sandbox)
    assert "--install" in log and "--upgrade" in log
    assert "First install" not in r.stdout


def _direct_user_setup(sb, plasma_active, applet_there=True):
    """Not root, no DESTDIR: sudo is a pass-through (chown skipped), kpackagetool6 says the
    applet is there, systemctl answers is-active for plasma-plasmashell as asked."""
    fake = Path(sb["env"]["PATH"].split(":")[0])
    log = sb["log"]
    (fake / "sudo").write_text('#!/bin/sh\n[ "$1" = chown ] && exit 0\nexec "$@"\n')
    (fake / "kpackagetool6").write_text(
        f'#!/bin/sh\necho "kpackagetool6 $*" >> "{log}"\n'
        + ('' if applet_there else 'case "$*" in *--show*) exit 1;; esac\n') + 'exit 0\n')
    (fake / "systemctl").write_text(
        f'#!/bin/sh\necho "systemctl $*" >> "{log}"\n'
        f'case "$*" in *is-active*plasma-plasmashell.service*) exit {0 if plasma_active else 3};;\n'
        '*is-active*) exit 3;; esac\nexit 0\n')
    for name in ("killall", "kstart"):
        (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    sb["env"].pop("DESTDIR")
    sb["env"]["PREFIX"] = str(sb["tmp"] / "pfx")


@needs_bash
def test_upgrade_restarts_plasma_through_its_unit_when_systemd_runs_it(sandbox):
    """A shell started by systemd is restarted through its unit, not killed and respawned:
    a shell respawned by hand is no longer the unit's, and a later restart of the unit
    would silently do nothing (seen in the VM)."""
    _direct_user_setup(sandbox, plasma_active=True)
    r = run(sandbox, "install.sh")
    assert r.returncode == 0, r.stderr
    log = calls(sandbox)
    assert "systemctl --user restart plasma-plasmashell.service" in log
    assert "killall" not in log and "kstart" not in log
    assert "systemctl --user restart pirostats" in log


@needs_bash
def test_upgrade_falls_back_to_killing_the_shell_when_systemd_does_not_run_it(sandbox):
    _direct_user_setup(sandbox, plasma_active=False)
    r = run(sandbox, "install.sh")
    assert r.returncode == 0, r.stderr
    log = calls(sandbox)
    assert "restart plasma-plasmashell" not in log
    assert "killall plasmashell" in log and "kstart plasmashell" in log


@needs_bash
def test_first_install_restarts_plasma_too(sandbox):
    """A new applet is not in "Add Widgets" until the shell restarts, so the first install
    restarts it like an upgrade does."""
    _direct_user_setup(sandbox, plasma_active=True, applet_there=False)
    r = run(sandbox, "install.sh")
    assert r.returncode == 0, r.stderr
    log = calls(sandbox)
    assert "--install" in log and "--upgrade" not in log
    assert "systemctl --user restart plasma-plasmashell.service" in log
    assert "First install: add the 'PiroStats' widget" in r.stdout


@needs_bash
def test_uninstall_is_quiet_when_the_applet_is_not_installed(sandbox):
    fake = Path(sandbox["env"]["PATH"].split(":")[0])
    (fake / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (fake / "kpackagetool6").write_text('#!/bin/sh\necho "Errore: not installed"\nexit 1\n')
    (fake / "systemctl").write_text('#!/bin/sh\nexit 0\n')
    for f in (fake / "sudo", fake / "kpackagetool6", fake / "systemctl"):
        f.chmod(0o755)
    sandbox["env"].pop("DESTDIR")
    r = run(sandbox, "uninstall.sh", prefix=str(sandbox["tmp"] / "pfx"))
    assert r.returncode == 0
    assert "Errore" not in r.stdout
