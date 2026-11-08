"""Behaviour tests for scripts/qdrant-ro-key.sh, Qdrant's read-only key (#40, D22).

The patching knob's ``recovery`` line, ``qdrant-recovery.sh query``, runs as
the knob user, never root (knob.md:69), and the admin key is root-only. So a
second key, Qdrant's ``service.read_only_api_key``, lives in a file group
``qdrant-ro`` reads, and in ``qdrant.env``, which Qdrant reads at its start.

- **Root only**, for a named user who isn't root, and only beside an admin key:
  Qdrant with a read-only key and no admin key would be a different store.
- **One script mints and rotates**: a rerun replaces the key in both places.
  ``qdrant.env`` keeps its other lines and mode 600.
- **``qdrant-ro.key`` is 0640, group ``qdrant-ro``**, and so is the directory's
  group: ``/etc/socraticode`` is 0750, and a file in a directory the user
  can't enter is no use. The group is made when missing; the user joins it.
- **The key reaches no argv and no output**: it's written by redirection.

The script runs from a copy whose host path points into ``tmp_path``, with
``id``, ``getent``, ``groupadd``, ``gpasswd`` and ``chgrp`` stubbed on ``PATH``.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "qdrant-ro-key.sh"
HOST_PATHS = re.compile(r"/etc/socraticode\b")
ADMIN = "QDRANT__SERVICE__API_KEY=admin-key-0123456789"
RO = "QDRANT__SERVICE__READ_ONLY_API_KEY="

# Each stub logs its argv to $STUB_LOG. id -u answers $ID_U; getent knows
# the users in $USERS and answers for group qdrant-ro only when $HAS_GROUP is 1.
STUB = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
case "$(basename "$0")" in
  id) [ "$1" = -u ] && echo "$ID_U" ;;
  getent)
    case "$1" in
      passwd) [[ " $USERS " == *" $2 "* ]] && echo "$2:x:1000:1000::/home/$2:/bin/bash" || exit 2 ;;
      group) [ "$HAS_GROUP" = 1 ] && echo "qdrant-ro:x:999:" || exit 2 ;;
    esac ;;
esac
exit 0
"""


@pytest.fixture
def host(tmp_path: Path) -> Path:
    etc = tmp_path / "etc" / "socraticode"
    etc.mkdir(parents=True)
    etc.chmod(0o750)
    (etc / "qdrant.env").write_text(f"{ADMIN}\n")
    (etc / "qdrant.env").chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("id", "getent", "groupadd", "gpasswd", "chgrp"):
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    copy = tmp_path / "qdrant-ro-key.sh"
    copy.write_text(HOST_PATHS.sub(lambda m: f"{tmp_path}{m.group(0)}", SCRIPT.read_text()))
    return tmp_path


def _run(
    host: Path, *args: str, uid: int = 0, has_group: bool = False
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{host / 'bin'}:{os.environ['PATH']}",
        "STUB_LOG": str(host / "stub.log"),
        "ID_U": str(uid),
        "USERS": "exedev root",
        "HAS_GROUP": "1" if has_group else "0",
    }
    return subprocess.run(
        ["bash", str(host / "qdrant-ro-key.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _etc(host: Path) -> Path:
    return host / "etc" / "socraticode"


def _key(host: Path) -> str:
    return (_etc(host) / "qdrant-ro.key").read_text()


def _log(host: Path) -> str:
    log = host / "stub.log"
    return log.read_text() if log.exists() else ""


def test_every_host_path_is_one_the_copy_rewrites() -> None:
    text = SCRIPT.read_text()
    assert "/etc/socraticode" in text
    assert not re.search(r"/etc/(?!socraticode\b)|/var/", text)


@pytest.mark.parametrize("args", [(), ("mint",), ("mint", "exedev", "extra"), ("rotate", "exedev")])
def test_usage(host: Path, args: tuple[str, ...]) -> None:
    done = _run(host, *args)
    assert done.returncode == 2
    assert "Usage" in done.stderr


def test_mint_needs_root(host: Path) -> None:
    done = _run(host, "mint", "exedev", uid=1000)
    assert done.returncode == 2
    assert "sudo" in done.stderr
    assert not (_etc(host) / "qdrant-ro.key").exists()


@pytest.mark.parametrize("user", ["nobody-here", "root"])
def test_mint_refuses_a_user_who_cant_hold_it(host: Path, user: str) -> None:
    """Knob commands never run as root, and an unknown user can't join a group."""
    done = _run(host, "mint", user)
    assert done.returncode == 2
    assert user in done.stderr
    assert not (_etc(host) / "qdrant-ro.key").exists()


def test_mint_refuses_without_an_admin_key(host: Path) -> None:
    (_etc(host) / "qdrant.env").write_text("OTHER=1\n")
    done = _run(host, "mint", "exedev")
    assert done.returncode == 2
    assert "QDRANT__SERVICE__API_KEY" in done.stderr
    assert not (_etc(host) / "qdrant-ro.key").exists()
    assert (_etc(host) / "qdrant.env").read_text() == "OTHER=1\n"


def test_mint_writes_one_key_to_both_places(host: Path) -> None:
    done = _run(host, "mint", "exedev")
    assert done.returncode == 0, done.stderr
    key = _key(host)
    assert re.fullmatch(r"[0-9a-f]{64}\n", key)
    assert (_etc(host) / "qdrant.env").read_text() == f"{ADMIN}\n{RO}{key}"


def test_the_files_modes(host: Path) -> None:
    done = _run(host, "mint", "exedev")
    assert done.returncode == 0, done.stderr
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    assert mode(_etc(host) / "qdrant-ro.key") == 0o640
    assert mode(_etc(host) / "qdrant.env") == 0o600
    assert mode(_etc(host)) == 0o750


def test_the_group_reads_the_key_and_enters_the_directory(host: Path) -> None:
    done = _run(host, "mint", "exedev")
    assert done.returncode == 0, done.stderr
    log = _log(host).splitlines()
    assert "groupadd --system qdrant-ro" in log
    assert "gpasswd -a exedev qdrant-ro" in log
    assert f"chgrp qdrant-ro {_etc(host)}" in log
    # The key file's group is set before it's moved into place, never after.
    assert any(re.fullmatch(rf"chgrp qdrant-ro {_etc(host)}/\.qdrant-ro\.key\.\w+", c) for c in log)


def test_an_existing_group_is_kept(host: Path) -> None:
    done = _run(host, "mint", "exedev", has_group=True)
    assert done.returncode == 0, done.stderr
    assert "groupadd" not in _log(host)


def test_a_rerun_rotates_the_key(host: Path) -> None:
    """Rotation is the same command: one read-only line, a new key."""
    (_etc(host) / "qdrant.env").write_text(f"{ADMIN}\n{RO}old\nOTHER=1\n")
    done = _run(host, "mint", "exedev")
    assert done.returncode == 0, done.stderr
    first = _key(host)
    assert (_etc(host) / "qdrant.env").read_text() == f"{ADMIN}\nOTHER=1\n{RO}{first}"
    done = _run(host, "mint", "exedev")
    assert done.returncode == 0, done.stderr
    assert _key(host) != first
    assert (_etc(host) / "qdrant.env").read_text() == f"{ADMIN}\nOTHER=1\n{RO}{_key(host)}"


def test_the_key_reaches_no_argv_and_no_output(host: Path) -> None:
    done = _run(host, "mint", "exedev")
    assert done.returncode == 0, done.stderr
    key = _key(host).strip()
    assert key not in done.stdout + done.stderr
    assert key not in _log(host)
    assert "restart" in done.stdout


def test_no_temporary_file_is_left(host: Path) -> None:
    done = _run(host, "mint", "exedev")
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in _etc(host).iterdir()) == ["qdrant-ro.key", "qdrant.env"]
