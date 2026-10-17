#!/usr/bin/python3 -I
"""#11: the embedding engine's fingerprint, for the check-in.

A new Ollama can change nomic-embed-text's vectors with the model, prefixes
and dimensions unchanged (0.40.5 case-folds input; 0.21.2 did not), and
SocratiCode records none of what changed. So the check-in embeds a few fixed
probes and compares them with what the pinned engine gave.

  engine-fingerprint.py capture <ollama-url> <out.json>
  engine-fingerprint.py check   <ollama-url> <fingerprint.json>

check exits 0 when every probe matches, 1 on drift with one line on stdout
naming the probes that moved (the ollama:engine finding's message, spliced
into hand-built JSON, so it holds no quote or backslash), and 2 when the
probes couldn't be embedded: a timeout behind a client's batch is not a
changed engine, and an unreachable Ollama is already ollama:tags.

Requests are SocratiCode 1.14.0's (provider-ollama.js): POST /api/embed with
the model's configured name and truncate:true, documents and queries
prefixed. Stdlib only.
"""

import json
import math
import re
import sys
import urllib.request

MODEL = "nomic-embed-text"
# One engine is bit-identical run to run (#11: 76/76), and Qdrant's float32
# copy of the same vector still reads 1.000000. #11's smallest real drift was
# 0.88 for a query.
THRESHOLD = 0.99999
TIMEOUT = 90

# Upper and lower case both: case-folding switched on or off moves at least
# one. Code, prose, an artifact label and uppercase-heavy XML, as stored.
PROBES = [
    (
        "code",
        "search_document: src/Parser.py\nclass ParseAddress:\n"
        "    def __init__(self, StreetName: str) -> None:\n"
        "        self.MAIN = StreetName.upper()",
    ),
    (
        "artifact",
        "search_document: context:Design-Plans:docs/plans/\n"
        "## D7: One embedding backend, pinned explicitly cohort-wide",
    ),
    (
        "xml",
        "search_document: data/train.xml\n<AddressNumber>123</AddressNumber> "
        "<StreetName>MAIN</StreetName> <StreetNamePostType>ST</StreetNamePostType>",
    ),
    ("query-mixed", "search_query: Where is the API Key checked on Requests"),
    ("query-lower", "search_query: where is the api key checked on requests"),
]


class Unknown(Exception):
    """The probes couldn't be embedded, so drift is unknown."""


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.load(r)


def _embed(base: str, texts: list[str]) -> list[list[float]]:
    req = urllib.request.Request(
        f"{base}/api/embed",
        data=json.dumps({"model": MODEL, "input": texts, "truncate": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            vectors = json.load(r)["embeddings"]
    except (OSError, ValueError, KeyError) as e:
        raise Unknown(f"embed failed: {e}") from e
    if len(vectors) != len(texts):
        raise Unknown(f"embed returned {len(vectors)} vectors for {len(texts)} texts")
    return vectors


def _version(base: str) -> str:
    try:
        return str(_get(f"{base}/api/version")["version"])
    except (OSError, ValueError, KeyError):
        return "unknown"


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


def _safe(line: str) -> str:
    return re.sub(r"[^\w .,:;<>=()/+-]", "?", line)


def capture(base: str, out: str) -> int:
    vectors = _embed(base, [text for _, text in PROBES])
    probes = [
        {"name": name, "text": text, "vector": v}
        for (name, text), v in zip(PROBES, vectors, strict=True)
    ]
    data = {"ollama_version": _version(base), "model": MODEL, "probes": probes}
    with open(out, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    return 0


def check(base: str, fingerprint: str) -> int:
    with open(fingerprint) as f:
        data = json.load(f)
    probes = data["probes"]
    vectors = _embed(base, [p["text"] for p in probes])
    moved = []
    for p, v in zip(probes, vectors, strict=True):
        if len(v) != len(p["vector"]):
            moved.append(f"{p['name']} dimension {len(v)} not {len(p['vector'])}")
        elif (c := _cos(p["vector"], v)) < THRESHOLD:
            moved.append(f"{p['name']} {c:.4f}")
    if not moved:
        return 0
    print(
        _safe(
            f"engine {_version(base)} drifted from the {data['ollama_version']} fingerprint"
            f" (cosine < {THRESHOLD}): {', '.join(moved)}"
        )
    )
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("capture", "check"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 64
    cmd, base, path = argv[1], argv[2].rstrip("/"), argv[3]
    try:
        return capture(base, path) if cmd == "capture" else check(base, path)
    except Unknown as e:
        print(f"engine-fingerprint: {e}, so drift is unknown", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
