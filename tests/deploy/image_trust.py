"""The roots an image's CA bundle trusts, for the images co-index runs (#13).

A host store fix never reaches a bundle built into an image, and a Go binary
before 1.27.3 trusts its bundle as given. So each image's bundle is checked
for itself: parsed as certificates, never grepped for a label.
"""

import socket
import ssl
import subprocess

import pytest

HOST = "co-index"
BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
# Subject CNs. CannObserv/processor#66; https://exe.dev/security/emudhra-compromise.
DISTRUSTED = {"emSign ECC Root CA - G3"}


def trusted_cns(image: str) -> list[str]:
    """The subject CNs of the roots in ``image``'s bundle. Runs on co-index
    only, where a missing image is a failure, not a skip."""
    if socket.gethostname() != HOST:
        pytest.skip(f"not {HOST}")
    pem = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/cat", image, BUNDLE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=pem)
    return [
        v for ca in ctx.get_ca_certs() for rdn in ca["subject"] for k, v in rdn if k == "commonName"
    ]
