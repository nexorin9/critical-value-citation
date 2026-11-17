# Critical Value Handling-Basis Retrieval System

> A critical-value result arrives from the LIS — the resident daemon judges it against the hospital's threshold table, recalls the reporting-policy clause, looks up the person on duty, and hands the on-duty staff one **disposition card that carries its citations** (threshold row / who to notify / reporting deadline / where the record goes); once the on-duty person confirms it in the console, the receipt is written back idempotently to the hospital critical-value registry.

## What it is / who it is for

`critical-value-citation` is an **event-driven resident service** (daemon + built-in console page + operations CLI) for hospital laboratories. It listens for HL7 v2 `ORU^R01` critical-value pushes over MLLP (with an HTTP callback channel alongside), judges criticality against the in-house threshold table, recalls the applicable clauses from the critical-value reporting policy, resolves the on-duty person from the duty roster, and pushes a four-element disposition card to the duty chat group. Only after the on-duty person confirms in the console does the receipt go back to the hospital critical-value registry / nursing execution platform — idempotently, keyed by message control number; every state change lands in the ledger and in an append-only audit trail.

Intended users: on-duty laboratory technicians, on-duty head nurses, laboratory section chiefs and QC officers, the medical administration office, and the hospital IT department that maintains the interfaces and the credentials.

It produces **no diagnosis and no treatment advice, and it signs nothing**: it only lays out the four facts — the basis and its source, the recipient, the deadline, the record destination — while the disposition decision stays with the human. Patient names, ID numbers and admission numbers are never stored and never enter the card or the push payload.

## Core capabilities

- **The in-house threshold table is the single authority** — not the LIS abnormal flag: when LIS flags a result the table does not, the disagreement is recorded and the table wins; when the table is exceeded but LIS failed to flag it, the result is still judged critical.
- **Every conclusion carries its source** — a threshold row names file, line and version; a policy clause names file fingerprint, paragraph number and section path, so a reviewer can flip back to the original material.
- **An unknown unit means no judgement** — a result whose conversion rule is unknown is marked "unit unknown" and escalated to a human instead of being guessed from experience; reference ranges are selected by sex and age band.
- **"Why is this critical?" is recomputable** — the judgement is recorded in seven stages (raw value → conversion → rule matched → which file and line → does it agree with the LIS flag → conclusion) and can be replayed by event ID and compared field by field with what was persisted.
- **A missing element is marked missing, never invented** — no threshold row, no recalled clause, roster unavailable: the card states what is missing and why, and which degradation level was applied.
- **A human confirmation separates advice from fact** — only after the on-duty person confirms does the receipt become an effective record in the registry; unconfirmed cards stay "pushed, awaiting receipt" and are re-checked and re-pushed when they expire.
- **Conflicting readings are laid out for arbitration** — when the threshold row and the policy clause disagree on the deadline, the recipient or the critical threshold, the system does not pick one; both values and both citations go to the console arbitration area.
- **A threshold-table revision has a computable blast radius** — added and removed items, changed thresholds, the value ranges whose conclusion would flip, and a recomputed list of historical events; the historical ledger itself is **never rewritten**.

## Quick Start

**Requirements** — Python ≥ 3.10 (3.11+ recommended); macOS / Linux (use WSL on Windows). **Zero external middleware**: no Elasticsearch, MySQL, Redis, MinIO or model service; persistence is SQLite on a single duty workstation. Only four parsing dependencies: `openpyxl` (threshold table xlsx), `python-docx` (policy docx), `pypdf` (policy pdf, optional) and `pytest` (tests).

**Install**

```bash
cd critical-value-citation && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Configure** — copy `.env.example` to `.env` and fill in the real in-house values; **every credential is read from environment variables**, no secret ever appears in the code:

```bash
export CRITVAL_WECOM_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
export CRITVAL_BASE_URL="https://his.example-hospital.local"          # critical-value registry
export CRITVAL_TOKEN="<registry token issued by the hospital>"
export NURSE_ROSTER_BASE_URL="https://roster.example-hospital.local"  # duty roster
export ROSTER_TOKEN="<roster token issued by the hospital>"
export LIS_CRITICAL_TOKEN="<shared token agreed with the LIS>"
export CONSOLE_TOKEN="<console access token>"
```

**Run and verify**

```bash
python -m critval.daemon          # resident daemon: listen → judge → build card → push → await confirmation
#   then open http://127.0.0.1:8760/events in a browser — the first screen is the event ledger
pytest -q                         # test suite
bash scripts/demo.sh              # one command: install self-check → deliver → judge → card → persist → export
python3 scripts/e2e.py            # end-to-end: ER7 frame → judgement → card → SQLite ledger
python3 scripts/check_install.py  # offline install self-check: dependencies / config slots / directory permissions
```

## Repository layout

| Path | Contents |
|---|---|
| `critval/` | Product package: `daemon.py` (resident loop: MLLP listener + directory delivery + timer → bounded queue → concurrent processing → heartbeat), `hl7/` (inbound protocol), `kb/` `index/` `retrieve/` (material parsing and versioning, index, hybrid retrieval), `judge/` (criticality judgement), `card/` (disposition card assembly), `roster/` `notify/` `writeback/` `ledger/` (on-duty resolution, push reliability, write-back gate and reconciliation, ledger state machine and audit), `flow/` `console/` `runtime/` (disposition chain, console pages and states, heartbeat lock and log rotation), plus `config.py` `models.py` `cli.py` |
| `scripts/` | Demo and verification: `demo.sh`, `check_install.py`, `e2e.py`, `capture_screenshots.py`, synthetic sample generation, baselines and benchmarks |
| `samples/` | Small committed samples: HL7 frames, dialect configuration, threshold table v1/v2, policy, duty roster, version binding table |
| `tests/` | Test suite (de-identified fixtures and local protocol stand-ins live in the test data layer only) |
| `docs/` | `cli.md` (full CLI and API), `examples.md` (output samples), `architecture.md` (data flow and directory tree), `concepts/` (visual target), `screenshots/` (real run screenshots) |
| `data/` | Regenerable corpus and runtime artifacts (gitignored) |
| `README.md` `LICENSE` `requirements.txt` `pyproject.toml` `.env.example` | Product docs, MIT license, dependency list, credential-slot samples |

## License

MIT

Full documentation: see README.md (Chinese)

---

## Follow Us

Scan the QR code to follow our official account for updates and community access:

![Follow Us](qrcode.jpg)