#!/usr/bin/env python3
"""#11: re-embed the store from its own payloads, for an engine change.

A new Ollama can change nomic-embed-text's vectors with the model unchanged,
and SocratiCode re-embeds only files whose hash changed, so an engine change
leaves every collection silently mixed. Every point carries the text it was
embedded from; this rebuilds that text exactly as SocratiCode 1.14.0 built it
(prepareDocumentText, provider-ollama's pre-truncation) and embeds it again.

  sudo cat /etc/socraticode/qdrant.key | python3 -I scripts/reembed.py --dry-run [--sample N]
  ... --dry-run --gate-out gate.json     # every point, on the engine that built the store
  ... --apply --gate gate.json           # on the new engine, in the window

--dry-run writes nothing and reports each collection's minimum cosine
against the stored vectors: on the engine that built the store, every point
must reach the threshold (the gate), since a text rebuilt slightly wrong
reads 0.999, not an error. On a candidate engine it measures drift.
--apply overwrites the dense vector only, in a collection the gate passed
under the profile it still has; payload and BM25 are untouched. A collection
still in-progress is refused either way. The Qdrant key is read from stdin,
never argv. Exits 0 when every collection passed or was written, 1 when any
failed or was refused, 64 on a usage error. Stdlib only.
"""

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request

QDRANT = "https://index.taild0fb76.ts.net:6333"
OLLAMA = "http://index.taild0fb76.ts.net:11434"
METADATA = "socraticode_metadata"
# One engine reproduces its vectors to 1.000000 through Qdrant's float32 copy.
# A text rebuilt with one character wrong read 0.997-0.9999 (2026-10-16).
THRESHOLD = 0.999999
# SocratiCode's EMBEDDING_BATCH_SIZE default. Ollama serves one request at a
# time (OLLAMA_NUM_PARALLEL=1), so a client's query waits behind the running
# batch: about 25 s for 32 chunks on 2 vCPU. A long run outside a window takes
# --batch 8.
BATCH = 32
# provider-ollama.js CHARS_PER_TOKEN_ESTIMATE.
CHARS_PER_TOKEN = 1.0


class Usage(Exception):
    pass


class Fatal(Exception):
    pass


def node_normalize(path: str) -> str:
    """Node's ``path.posix.normalize``: unlike ``os.path.normpath``, it keeps a
    trailing ``/``, which a directory artifact's label holds."""
    if path == "":
        return "."
    absolute = path.startswith("/")
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append("..")
            continue
        parts.append(seg)
    out = "/".join(parts)
    if not out and not absolute:
        out = "."
    if out and path.endswith("/"):
        out += "/"
    return ("/" if absolute else "") + out


def js_substring(text: str, n: int) -> str:
    """JS ``text.substring(0, n)``: ``n`` UTF-16 code units, which may end
    between a surrogate pair's halves."""
    units = text.encode("utf-16-le", "surrogatepass")
    if len(units) <= 2 * n:
        return text
    return units[: 2 * n].decode("utf-16-le", "surrogatepass")


def document_text(payload: dict, profile: dict) -> str:
    """The text SocratiCode embedded for this point: indexer.js passes the
    chunk's relativePath, context-artifacts.js its artifact label."""
    if payload.get("type") == "artifact":
        head = f"context:{payload['artifactName']}:{node_normalize(payload['relativePath'])}"
    else:
        head = payload["relativePath"]
    text = profile.get("documentPrefix", "")
    if profile.get("documentIncludesPath"):
        text += f"{head}\n"
    text += payload["content"]
    context = profile.get("embedding", {}).get("contextLength", 2048)
    return js_substring(text, math.floor(context * CHARS_PER_TOKEN))


class Client:
    def __init__(self, qdrant: str, ollama: str, key: str) -> None:
        self.qdrant, self.ollama, self.key = qdrant.rstrip("/"), ollama.rstrip("/"), key

    def _call(self, method: str, url: str, body: object, headers: dict) -> dict:
        req = urllib.request.Request(
            url,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **headers},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise Fatal(f"{method} {url.split('?')[0]}: HTTP {e.code}") from e
        except (OSError, ValueError) as e:
            raise Fatal(f"{method} {url.split('?')[0]}: {e}") from e

    def q(self, method: str, path: str, body: object = None) -> dict:
        return self._call(method, self.qdrant + path, body, {"api-key": self.key})

    def scroll(self, name: str, vectors: bool, limit: int, offset=None) -> tuple[list, object]:
        body = {
            "limit": limit,
            "with_payload": True,
            "with_vector": ["dense"] if vectors else False,
        }
        if offset is not None:
            body["offset"] = offset
        result = self.q("POST", f"/collections/{name}/points/scroll", body)["result"]
        return result["points"], result.get("next_page_offset")

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        body = {"model": model, "input": texts, "truncate": True}
        vectors = self._call("POST", f"{self.ollama}/api/embed", body, {})["embeddings"]
        if len(vectors) != len(texts):
            raise Fatal(f"ollama returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors

    def version(self) -> str:
        try:
            return str(self._call("GET", f"{self.ollama}/api/version", None, {})["version"])
        except (Fatal, KeyError):
            return "unknown"


def _profiles(client: Client) -> dict[str, tuple[dict, dict]]:
    """Each profiled collection's (profile, metadata). The symgraph and
    codegraph collections hold no embeddings and have no profile."""
    found: dict[str, tuple[dict, dict]] = {}
    offset = None
    while True:
        points, offset = client.scroll(METADATA, False, 256, offset)
        for p in points:
            meta = p["payload"]
            profile = meta.get("effectiveIndexProfile")
            if isinstance(profile, str):
                profile = json.loads(profile)
            if profile and meta.get("collectionName"):
                found[meta["collectionName"]] = (profile, meta)
        if offset is None:
            return found


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


def _collection(client: Client, name: str, profile: dict, args) -> dict:
    model = profile.get("embedding", {}).get("model", "nomic-embed-text")
    n, low, below, offset = 0, 1.0, 0, None
    started = time.monotonic()
    while True:
        limit = args.batch if args.sample is None else min(args.batch, args.sample - n)
        points, offset = client.scroll(name, True, limit, offset)
        if not points:
            break
        vectors = client.embed(model, [document_text(p["payload"], profile) for p in points])
        if args.apply:
            body = {
                "points": [{"id": p["id"], "vector": {"dense": v}} for p, v in zip(points, vectors)]
            }
            client.q("PUT", f"/collections/{name}/points/vectors?wait=true", body)
        else:
            for p, v in zip(points, vectors, strict=True):
                stored = p["vector"]["dense"] if isinstance(p["vector"], dict) else p["vector"]
                c = _cos(stored, v)
                low = min(low, c)
                below += c < args.threshold
        n += len(points)
        if offset is None or (args.sample is not None and n >= args.sample):
            break
    return {"points": n, "min": low, "below": below, "seconds": time.monotonic() - started}


def run(args, key: str) -> int:
    client = Client(args.qdrant, args.ollama, key)
    gate = None
    if args.apply:
        with open(args.gate) as f:
            gate = json.load(f)
    version = client.version()
    mode = (
        "apply"
        if args.apply
        else f"dry run{'' if args.sample is None else f', sample {args.sample}'}"
    )
    print(f"engine {version} at {args.ollama}; {mode}", flush=True)
    profiles = _profiles(client)
    names = sorted(n for n in profiles if not args.collection or n in args.collection)
    verdicts: dict[str, dict] = {}
    failed, total, started = False, 0, time.monotonic()
    for name in names:
        profile, meta = profiles[name]
        refusal = None
        if meta.get("indexingStatus") == "in-progress":
            refusal = "in-progress: its client is indexing it"
        elif gate is not None:
            entry = gate["collections"].get(name)
            if not entry or not entry["passed"]:
                refusal = "the gate did not pass it"
            elif entry["profile"] != profile:
                refusal = "its profile changed since the gate"
        if refusal:
            print(f"{name:28} REFUSED  {refusal}", flush=True)
            verdicts[name] = {"passed": False, "points": 0, "min": None, "profile": profile}
            failed = True
            continue
        r = _collection(client, name, profile, args)
        total += r["points"]
        if args.apply:
            print(f"{name:28} WROTE  points {r['points']}  {r['seconds']:.0f}s", flush=True)
            continue
        passed = r["below"] == 0
        failed |= not passed
        verdicts[name] = {
            "passed": passed,
            "points": r["points"],
            "min": r["min"],
            "profile": profile,
        }
        print(
            f"{name:28} {'PASS' if passed else 'FAIL'}  points {r['points']}  min {r['min']:.6f}"
            f"  below {r['below']}  {r['seconds']:.0f}s",
            flush=True,
        )
    print(f"total points {total} in {time.monotonic() - started:.0f}s")
    if args.gate_out:
        with open(args.gate_out, "w") as f:
            json.dump(
                {"ollama_version": version, "threshold": args.threshold, "collections": verdicts},
                f,
                indent=1,
            )
            f.write("\n")
    return 1 if failed else 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Usage(message)


def main(argv: list[str]) -> int:
    parser = _Parser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--qdrant", default=QDRANT)
    parser.add_argument("--ollama", default=OLLAMA)
    parser.add_argument("--collection", action="append", help="only this one; repeatable")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sample", type=int, help="dry run: each collection's first N points")
    parser.add_argument("--gate-out", help="dry run: record the verdicts, for --apply")
    parser.add_argument("--gate", help="apply: a full dry run's --gate-out")
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    parser.add_argument("--batch", type=int, default=BATCH, help="texts per embed request")
    try:
        args = parser.parse_args(argv)
        if args.dry_run == args.apply:
            raise Usage("give one of --dry-run and --apply")
        if args.apply and not args.gate:
            raise Usage(
                "--apply needs --gate, from a full dry run on the engine that built the store"
            )
        if args.apply and (args.sample is not None or args.gate_out):
            raise Usage("--sample and --gate-out are for a dry run")
        if args.gate_out and args.sample is not None:
            raise Usage("a gate is every point: --gate-out refuses --sample")
        if args.batch < 1:
            raise Usage("--batch must be at least 1")
        if args.sample is not None and args.sample < 1:
            raise Usage("--sample must be at least 1")
        key = sys.stdin.readline().strip()
        if not key:
            raise Usage("no Qdrant key on stdin")
    except Usage as e:
        print(f"reembed: {e}", file=sys.stderr)
        return 64
    try:
        return run(args, key)
    except Fatal as e:
        print(f"reembed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
