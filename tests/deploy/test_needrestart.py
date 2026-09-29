"""needrestart lists restarts and never performs them (#2).

apt's ``DPkg::Post-Invoke`` hook (``/etc/apt/apt.conf.d/99needrestart``) runs
``needrestart -m u`` after every dpkg run. Ubuntu's patch turns ``-m u`` into
**automatic** restarts when ``$nrconf{restart}`` is unset, which is the stock
state. A ``libc6`` security update would then restart ``containerd`` and
``docker`` mid-apply, and ``qdrant.service`` and ``ollama.service`` with them
through ``Requires=docker.service``, outside any approved window. The drop-in
makes the answer independent of whether ``NEEDRESTART_MODE=l`` survives every
process between the operator and the hook. Shape from CannObserv/notifier#91.

Tracked in ``deploy/``, installed as:

- ``needrestart.conf.d/index.conf`` -> ``/etc/needrestart/conf.d/``

Pure assertions on the tracked copy run everywhere; installed-parity and live
assertions skip where the node is not this one, CI included.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
DROPIN = DEPLOY / "needrestart.conf.d" / "index.conf"
INSTALLED = Path("/etc/needrestart/conf.d/index.conf")
MAIN_CONF = Path("/etc/needrestart/needrestart.conf")

# needrestart's config is Perl, eval'd into `%nrconf`. Evaluating it the same
# way is the only honest parse: a syntax error makes needrestart die, and the
# apt hook swallows that with `|| true`.
_EVAL = (
    "our %nrconf; our $LOGPREF = q(); "
    "eval do { local(@ARGV, $/) = $ARGV[0]; <> }; die $@ if $@; "
    "print defined $nrconf{restart} ? $nrconf{restart} : q(undef);"
)


def _restart_mode(conf: Path) -> str:
    """Evaluate ``conf`` as needrestart does and return ``$nrconf{restart}``."""
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl not available on this host")
    return subprocess.run(
        [perl, "-e", _EVAL, str(conf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_dropin_sets_list_only_restart_mode() -> None:
    assert _restart_mode(DROPIN) == "l"


def test_dropin_sets_nothing_else() -> None:
    """One key, so the drop-in cannot quietly change needrestart's other
    behaviour, and ``$nrconf{ui}`` stays unset: setting it would force
    interactive mode instead."""
    lines = [
        ln.strip()
        for ln in DROPIN.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines == ["$nrconf{restart} = 'l';"]


def test_installed_copy_matches_tracked() -> None:
    try:
        installed = INSTALLED.read_text()
    except FileNotFoundError:
        pytest.skip(f"{INSTALLED} not installed on this host")
    assert installed == DROPIN.read_text()


def test_live_config_chain_resolves_to_list_only() -> None:
    """The main config globs ``conf.d/*.conf`` in sort order, so a later file
    could override this one. Evaluate the chain needrestart itself reads, but
    only once the drop-in is installed: before that, the stock chain leaves the
    key unset, which is the state this file exists to change."""
    if not MAIN_CONF.exists():
        pytest.skip("needrestart not installed on this host")
    if not INSTALLED.exists():
        pytest.skip(f"{INSTALLED} not installed on this host")
    assert _restart_mode(MAIN_CONF) == "l"
