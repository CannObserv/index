# index

Deploy surface and operator tooling for **`co-index`**, the CannObserv cohort's shared [SocratiCode](https://github.com/giancarloerra/socraticode) store: Qdrant and Ollama on one tailnet-only VM.

- **Design record:** [docs/plans/2026-09-11-shared-qdrant-vm-design.md](docs/plans/2026-09-11-shared-qdrant-vm-design.md)
- **Host runbook:** [deploy/README.md](deploy/README.md)
- **Installing the store key into a client repo:** from a clone of this repo, `scripts/install_qdrant_key.sh <client-repo>`, with the key on stdin. See the script's header.

Moved from [CannObserv/notifier](https://github.com/CannObserv/notifier) with history ([notifier#90](https://github.com/CannObserv/notifier/issues/90)).
