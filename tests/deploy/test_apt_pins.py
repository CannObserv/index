"""Tailscale's apt origin is scoped by pins (#9).

The patching knob follows ``pkgs.tailscale.com`` in the maintenance lane.
Named by its site, a followed origin offers every package it serves, at apt's
default 500, which ties Ubuntu's. So, per ``patching-hosts``' policy.md:

- a ``Package: *`` pin below 500 for the site, so it never replaces an
  Ubuntu package;
- a package pin above 500 for each package it was added for: ``tailscale``,
  and the keyring package that signs it.

The structure tests run everywhere. The effect tests evaluate the tracked file
with apt itself, through ``Dir::Etc::PreferencesParts``, so they prove it on
co-index before it is installed; they skip where apt doesn't know the origin
(CI). Installed parity is ``test_installed.py``'s.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PINS = REPO_ROOT / "deploy" / "apt-preferences.d" / "tailscale"
SITE = "pkgs.tailscale.com"
PACKAGES = {"tailscale", "tailscale-archive-keyring"}


def _stanzas() -> list[dict[str, str]]:
    body = "\n".join(ln for ln in PINS.read_text().splitlines() if not ln.lstrip().startswith("#"))
    return [
        dict(ln.split(": ", 1) for ln in block.strip().splitlines())
        for block in body.split("\n\n")
        if block.strip()
    ]


def test_catch_all_holds_the_site_below_ubuntu() -> None:
    catch_all = [s for s in _stanzas() if s["Package"] == "*"]
    assert catch_all == [{"Package": "*", "Pin": f"origin {SITE}", "Pin-Priority": "100"}]


def test_each_package_it_was_added_for_is_pinned_above_ubuntu() -> None:
    named = [s for s in _stanzas() if s["Package"] != "*"]
    assert len(named) == 1
    (stanza,) = named
    assert set(stanza["Package"].split()) == PACKAGES
    assert stanza["Pin"] == f"origin {SITE}"
    assert int(stanza["Pin-Priority"]) > 500


def _policy(tmp_path: Path, *packages: str) -> subprocess.CompletedProcess[str]:
    apt_cache = shutil.which("apt-cache")
    if apt_cache is None:
        pytest.skip("apt-cache not available on this host")
    parts = tmp_path / "preferences.d"
    parts.mkdir()
    shutil.copy(PINS, parts / "tailscale")
    return subprocess.run(
        [
            apt_cache,
            "-o",
            "Dir::Etc::Preferences=/dev/null",
            "-o",
            f"Dir::Etc::PreferencesParts={parts}",
            "policy",
            *packages,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_apt_parses_it_cleanly(tmp_path: Path) -> None:
    assert _policy(tmp_path).stderr == ""


def _knows_site(policy: str) -> None:
    if SITE not in policy:
        pytest.skip(f"apt has no lists from {SITE} on this host")


def test_apt_holds_the_site_at_100(tmp_path: Path) -> None:
    policy = _policy(tmp_path).stdout
    _knows_site(policy)
    priorities = {ln.split()[0] for ln in policy.splitlines() if f"https://{SITE}/" in ln}
    assert priorities == {"100"}


@pytest.mark.parametrize("package", sorted(PACKAGES))
def test_apt_takes_each_package_from_the_site(tmp_path: Path, package: str) -> None:
    """apt prints a package pin on each version line, and the site's own
    priority on the source lines beneath it."""
    policy = _policy(tmp_path, package).stdout
    _knows_site(policy)
    lines = [ln.split() for ln in policy.splitlines()]
    (candidate,) = [ln[1] for ln in lines if ln[:1] == ["Candidate:"]]
    # A version line is "<version> <pin>", or "*** <version> <pin>" when installed.
    pins = [ln[-1] for ln in lines if ln[-2:-1] == [candidate] and len(ln) <= 3]
    assert pins == ["600"], policy
