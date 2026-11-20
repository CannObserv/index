"""How the collector runs: its image, its box, its config's edges (D21, #33).

**The image is a version and a digest,** as Qdrant's is (#13): a tag alone
is whatever the registry serves that day, and this image parses what every
cohort VM sends.

**The container is boxed.** It listens to twelve VMs with no ingest auth
(D21, on D4's terms), so it gets no capability, a read-only root filesystem,
no new privileges and a memory limit: it shares 8 GB with Qdrant and Ollama
(D1). It runs as root so its files are root's, in a directory only root
opens. With every capability dropped that root can't read another user's
files either.

**Its config has no way out and one way in.** OTLP/HTTP on 4318 and nothing
else: no gRPC port, no extension, no metrics listener of its own. Every
exporter is a file under the one mounted directory, so nothing this host
collects is sent anywhere, and the image's CA bundle is never consulted.

**The exporter writes plain JSONL.** Its own ``compression: zstd`` frames
each batch behind a length prefix, which ``zstd -d`` refuses (measured on
0.165.0). ``otelcol-retention.sh`` compresses rotated files instead.

**A processor that errs loses the batch.** Under ``ignore``, a statement
that fails is skipped and the record goes on unfiltered: for a filter that
exists to keep content off the disk, that is the wrong way to fail.
"""

import re
import subprocess
from pathlib import Path

import yaml
from collector import (
    CONFIG,
    DATA_DIR,
    HOST_CONFIG,
    REPO_ROOT,
    RUN_SCRIPT,
    docker_run_words,
    options,
)

KNOB = REPO_ROOT / ".skills" / "patching-hosts"
RETENTION = REPO_ROOT / "deploy" / "otelcol-retention.sh"
IMAGE = re.compile(r"otel/opentelemetry-collector-contrib:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}")
MIB = 1024 * 1024


def _words() -> list[str]:
    return docker_run_words(RUN_SCRIPT)


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def test_the_image_is_a_version_and_a_digest() -> None:
    assert IMAGE.fullmatch(_words()[-1])


def test_the_container_has_no_capability_and_a_read_only_root() -> None:
    words = _words()
    assert options(words, "--user") == ["0:0"]
    assert options(words, "--cap-drop") == ["ALL"]
    assert options(words, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in words
    assert not {"--privileged", "--cap-add", "--env-file", "--device", "--pid"} & set(words)


def _mib(size: str) -> int:
    number, unit = re.fullmatch(r"(\d+)(m|MiB)", size).groups()
    return int(number)


def test_memory_is_limited_and_the_runtime_knows_it() -> None:
    """Without ``GOMEMLIMIT`` Go collects only by growth, and the kernel's
    limit kills the container before it would. No swap, so the limit is one."""
    words = _words()
    [limit] = options(words, "--memory")
    assert options(words, "--memory-swap") == [limit]
    envs = dict(e.partition("=")[::2] for e in options(words, "--env"))
    assert set(envs) == {"GOMEMLIMIT"}
    assert _mib(envs["GOMEMLIMIT"]) < _mib(limit) <= 512
    limiter = _config()["processors"]["memory_limiter"]
    assert limiter["limit_mib"] < _mib(envs["GOMEMLIMIT"])
    assert options(words, "--pids-limit")


def test_its_docker_log_is_bounded() -> None:
    """The unit's journal is capped by journald. Docker's own copy of the
    same lines isn't, unless told."""
    opts = dict(o.partition("=")[::2] for o in options(_words(), "--log-opt"))
    assert set(opts) == {"max-size", "max-file"}
    # Named, not the daemon's default: under another driver these options
    # are refused, and the container never starts.
    assert options(_words(), "--log-driver") == ["json-file"]


def test_it_mounts_its_config_read_only_and_one_data_directory() -> None:
    assert options(_words(), "--volume") == [
        f"{HOST_CONFIG}:/etc/otelcol-contrib/config.yaml:ro",
        f"{DATA_DIR}:{DATA_DIR}",
    ]


def test_the_data_directory_is_roots_alone_before_the_start(tmp_path: Path) -> None:
    """Docker would create a missing mount source 755. ``install -d`` also
    resets the mode and owner of one that exists."""
    from test_bound_address import _run

    done, log = _run(tmp_path, "otelcol-run.sh", "echo 100.64.0.9\n")
    assert done.returncode == 0, done.stderr
    calls = log.read_text().splitlines()
    made = f"install -d -m 700 -o root -g root {DATA_DIR}"
    assert made in calls
    [run] = [c for c in calls if c.startswith("docker run ")]
    assert calls.index(made) < calls.index(run)


def test_the_only_way_in_is_otlp_http_on_4318() -> None:
    config = _config()
    assert config["receivers"] == {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}
    assert "extensions" not in config
    assert "extensions" not in config["service"]
    assert config["service"]["telemetry"]["metrics"]["level"] == "none"


def test_every_exporter_is_a_plain_jsonl_file_in_the_data_directory() -> None:
    exporters = _config()["exporters"]
    assert set(exporters) == {"file/events", "file/metrics"}
    for name, exporter in exporters.items():
        signal = name.split("/")[1]
        assert exporter["path"] == f"{DATA_DIR}/{signal}.jsonl"
        assert "compression" not in exporter and "format" not in exporter
        assert set(exporter) == {"path", "rotation"}


def test_logs_and_metrics_are_the_only_pipelines() -> None:
    pipelines = _config()["service"]["pipelines"]
    assert set(pipelines) == {"logs", "metrics"}
    assert pipelines["logs"]["exporters"] == ["file/events"]
    assert pipelines["metrics"]["exporters"] == ["file/metrics"]
    for pipeline in pipelines.values():
        assert pipeline["receivers"] == ["otlp"]
        assert pipeline["processors"][0] == "memory_limiter"
        assert len(pipeline["processors"]) > 1


def test_every_processor_in_a_pipeline_is_defined_and_fails_closed() -> None:
    config = _config()
    used = {p for pipe in config["service"]["pipelines"].values() for p in pipe["processors"]}
    assert used == set(config["processors"])
    for name in used - {"memory_limiter"}:
        assert config["processors"][name]["error_mode"] == "propagate", name


def _bytes(script: Path, name: str) -> int:
    [expr] = re.findall(rf"^{name}=\$\(\(\s*([\d\s*]+)\)\)", script.read_text(), flags=re.M)
    total = 1
    for factor in expr.split("*"):
        total *= int(factor)
    return total


def test_the_exporter_cannot_outgrow_the_cap_on_its_own() -> None:
    """The retention run holds the cap once a day. Between runs, what bounds
    the directory is the exporter's own limit on rotated files, so a client
    that floods it loses the flood, not the disk."""
    cap = _bytes(RETENTION, "CAP_BYTES")
    uncompressed = 0
    for exporter in _config()["exporters"].values():
        rotation = exporter["rotation"]
        assert set(rotation) == {"max_megabytes", "max_backups"}
        uncompressed += rotation["max_megabytes"] * MIB * (rotation["max_backups"] + 1)
    assert uncompressed * 4 <= cap


def _knob(directive: str) -> list[str]:
    lines = KNOB.read_text().splitlines()
    return [ln.split(None, 1)[1] for ln in lines if ln.startswith(f"{directive} ")]


def test_the_knob_declares_the_unit_and_owns_the_image() -> None:
    """A patching run stops and checks declared services, and leaves an owned
    image alone: the pin moves by a commit here (the runbook's "Collector")."""
    assert "otelcol.service" in _knob("service")
    repository = _words()[-1].split(":")[0]
    assert f"{repository} self" in _knob("owner")


def test_the_knobs_health_line_is_an_empty_export_on_the_clients_port() -> None:
    """Every probe runs it, so it writes nothing (knob.md), which
    test_otelcol_pipeline.py proves of an empty export. It runs as the knob
    user under ``sh -c``, and must fail on a refusal: ``-f``."""
    [line] = [ln for ln in _knob("health") if ":4318" in ln]
    assert line.startswith("curl -sf --max-time 5 ")
    assert "http://index.taild0fb76.ts.net:4318/v1/logs" in line
    assert "-d '{}'" in line
    assert " #" not in line, "the knob would read the rest as a comment"
    assert subprocess.run(["sh", "-n", "-c", line]).returncode == 0
