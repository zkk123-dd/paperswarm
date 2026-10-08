# Artifacts

Public artifact repository for the paper **"PaperSwarm: Evidence-Gated Paper Generation
with a Fully Traceable Claim Ledger"** (ICLR 2026 format, English).

Repository: <https://github.com/zkk123-dd/paperswarm>

This file is the single entry point a reviewer needs. Every number that appears in the
paper maps to a file below, and every file is either produced by a script in this
repository or checksummed in the claim ledger.

---

## 0. Which version of the paper is in this repository

| Item | Value |
|---|---|
| Paper PDF | [`output/PaperSwarm-ICLR2026.pdf`](output/PaperSwarm-ICLR2026.pdf) |
| The exact run that produced it | [`runs/paper-20260929-164239/`](runs/paper-20260929-164239/) |
| Externally scored by | paperreview.ai (Stanford Agentic Reviewer), venue = ICLR |
| External score | **2.4 / 10** (7 dimensions, 1 positive) |
| Submission record | [`reports/review_submission.json`](reports/review_submission.json) |
| Full review payload | [`reports/review_result.json`](reports/review_result.json) |

**State of the repository, stated plainly.** The experimental module
(`src/paperswarm/lab.py`) has been rewritten after the external review to remove a
real methodological defect: in the submitted version the ridge penalty `alpha` was
selected on the *test* split, which inflates the reported improvement. The rewritten
module selects `alpha` by 5-fold cross-validation **inside the training split** and
evaluates the test split exactly once. The measured change and its attribution are in
[`docs/experiment-protocol-fix.md`](docs/experiment-protocol-fix.md) and
[`reports/lab_v2_report.txt`](reports/lab_v2_report.txt).

The PDF under `output/` is the **submitted** version (drawn from run
`paper-20260929-164239`) and therefore still reports the pre-fix number. Both numbers
are documented side by side; neither is hidden.

---

## 1. Paper → artifact map

| Paper location | Claim | Artifact |
|---|---|---|
| Abstract, §4, §5 | every `\result{}` value | `runs/paper-20260929-164239/ledger/claims.jsonl` (claim id → value → SHA-256 of the source artifact) |
| §4 Experiments | OLS / ridge test MSE and standard deviations | `cl-ols-mean`, `cl-ols-std`, `cl-ridge-mean`, `cl-ridge-std` |
| §4 Experiments | ridge-vs-OLS reduction | `cl-improve` |
| §4 Experiments | selected penalty, noise floor, dataset shape | `cl-alpha`, `cl-noise`, `cl-seeds`, `cl-features`, `cl-train`, `cl-test` |
| §3 Method (system) | gate predicate behaviour | `src/paperswarm/tex_gate.py`, `src/paperswarm/evidence.py` |
| — | tokens, latency, cost | `runs/paper-20260929-164239/run_summary.json`, `llm_calls.jsonl` |

All 11 registered claims are listed above. The submitted paper presents these values as
running prose: **it contains no table and no figure** (verified by searching every
`.tex` file in `runs/paper-20260929-164239/` for `tabular` / `begin{table}` — zero
matches). That is a defect of the submitted version, not a property of the system, and
is addressed in the revised version.

Numbers are never typed into the LaTeX source. The body may only contain
`\result{claim_id}`; the value is resolved from the ledger at assemble time and the
source artifact's SHA-256 is re-checked. If an artifact is modified after registration,
assembly fails.

V1 reproduction was verified before the rewrite: the rewritten module reproduces the
submitted numbers exactly (`0.6198 / 0.3041 / 50.94 / alpha=0.1`), which is what makes
the difference attributable rather than confounded.

---

## 2. Traceability protocol

1. `scripts/full_paper.py` runs the experiment, writes `exp/metrics.json`, registers it
   in the ledger with its SHA-256.
2. The model writes LaTeX sections that may only reference `\result{claim_id}`.
3. `tex_gate` rejects the write if a result-context number is hand-written, if a
   referenced claim does not exist, or if the referenced claim fails verification.
4. Assembly resolves the values and compiles with `tectonic`.
5. Every LLM call is appended to `llm_calls.jsonl` (tokens, latency, retries, role).

To verify that the ledger genuinely binds the numbers, tamper with an artifact under a
run directory and re-run assembly — all claims are rejected and the process exits
non-zero.

---

## 3. Deliverables for the technical package

| # | Deliverable | Path |
|---|---|---|
| 1 | Generated paper (PDF, English, ICLR template) | `output/PaperSwarm-ICLR2026.pdf` |
| 2 | Agent source on the openJiuwen framework (commented) | `src/paperswarm/`, `docs/architecture-and-innovation.md` |
| 3 | Run configuration, environment, reproduction guide | `configs/model.yaml`, `docs/reproduction-guide.md` |
| 4 | Technical documentation (architecture, module calls, contributions) | `docs/architecture-and-innovation.md` |
| 5 | Resource report (tokens, runtime, traceable) | `docs/token-cost-report.md`, `reports/token_report.json` |
| 6 | Framework contribution note (+ PR) | `docs/upstream-contribution.md` |
| 7 | Reviewer access token and score | `reports/review_submission.json`, `reports/review_result.json` |

Supporting material: `docs/DELIVERABLES.md` is the full delivery checklist including
the items that are **not** finished and what each one is missing; `docs/env-recon.md`
records the environment limits (no Linux sandbox available on the build machine);
`runs/` keeps every run's full trace, not just the final one.

---

## 4. Reproduction

Offline checks need no network, no model and no LaTeX:

```bash
export PYTHONPATH="$PWD/src"
python tests/test_evidence.py                 # ledger + tamper detection
python tests/test_kernel_offline.py           # retry bounds, loop bounds, gate rejections
python scripts/verify_vendored_kernel.py
python -m pytest integration/test_paper_providers_contract.py -q
python scripts/verify_swarmflow_dryrun.py
```

Full pipeline (needs a local OpenAI-compatible endpoint or a cloud profile):

```bash
python scripts/full_paper.py --skip-llm-check   # experiment + ledger + assemble, no LLM
python scripts/full_paper.py --profile cloud-deepseek
python scripts/token_report.py                  # aggregate tokens / latency / cost
python scripts/review_readiness.py              # pre-submission self-check
```

Exact dependency versions, the three reproduction paths and the acceptance checklist are
in [`docs/reproduction-guide.md`](docs/reproduction-guide.md).

---

## 5. License

Apache-2.0, matching the upstream JiuwenSwarm license. See [`LICENSE`](LICENSE).
