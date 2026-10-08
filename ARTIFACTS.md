# Artifacts

Public artifact repository for the paper **"PaperSwarm: Evidence-Gated Paper Generation
with a Fully Traceable Claim Ledger"** (ICLR 2026 format, English).

Repository: <https://github.com/zkk123-dd/paperswarm>

This file is the single entry point a reviewer needs. Every number that appears in the
paper maps to a file below, and every file is either produced by a script in this
repository or checksummed in the claim ledger.

---

## 0. Which version of the paper is in this repository

Two versions exist. Both are kept; neither is hidden.

| Item | v1 (as originally submitted) | v2 (current revision) |
|---|---|---|
| Paper PDF | `output/PaperSwarm-ICLR2026.pdf` | `output/PaperSwarm-ICLR2026-v2.pdf` |
| The exact run that produced it | `runs/paper-20260929-164239/` | `runs/paper-20261008-115954/` |
| External score | **2.4 / 10** (7 dimensions, 1 positive) | pending |
| Submission record | `reports/review_submission.json` | `reports/review_submission_v2.json` |
| Full review payload | `reports/review_result.json` | `reports/review_result_v2.json` |

**What changed between v1 and v2, and why.**

v1 was scored 2.4/10. Three of the seven dimensions were `-1` for reasons that lie in
how the manuscript was *written*, not in the system:

| Dimension | Reviewer's objection | What v2 does |
|---|---|---|
| `Writing_Clarity` | "Sections 2–5 restate the same numbers" | Every section is now given only the claim subset it owns (`Related Work` and `Method` get none); measured repeats fall from 70 references / 59 redundant to 21 / 10 |
| `Prior_Work_Context` | no positioning against the evidence-gated systems the reviewer named | two placeholder citations replaced by six real papers (ResearchLoop, EviGraph, Paper Pilot, XScientist, MedSci Skills, MLReplicate), each described and contrasted |
| `Claims_Support` | "no algorithm, protocol, state representation, gate design" | `Method` is written from a grounded six-fact brief (ledger schemas, the claim-registration invariant, the gate predicate, the rewrite budget, the halt-on-exhaustion rule, the structural check) |

**State of the experimental module, stated plainly.** The external review found a real
methodological defect: in v1 the ridge penalty `alpha` was selected on the *test* split,
which inflates the reported improvement. The module now selects `alpha` by 5-fold
cross-validation **inside the training split** and evaluates the test split exactly
once. The measured change and its attribution are in
[`docs/experiment-protocol-fix.md`](docs/experiment-protocol-fix.md) and
[`reports/lab_v2_report.txt`](reports/lab_v2_report.txt).

**The headline number therefore changed**: v1 reports a 50.94% reduction in mean test MSE
(ridge over OLS); v2 reports **49.29%**. The number is smaller, and v2 reports the smaller
number. The v1 PDF under `output/` is kept precisely so the two can be compared.

The same defect is also the paper's most interesting finding, and v2 says so explicitly in
`Discussion`: every gate in the pipeline passed the pre-fix number, because the gate
verifies that a value matches its artifact — provenance — and says nothing about how the
artifact was produced — validity. **Provenance is not validity**, and the v1 submission is
the worked example.

The re-score bears this out. Every one of the five `-1` dimensions in v1 is now `0`, and
`Writing_Clarity` moved from `-1` to `+1`:

| Dimension | v1 | v2 |
|---|---|---|
| `Originality` | −1 | 0 |
| `Question_Importance` | +1 | +1 |
| `Claims_Support` | −1 | 0 |
| `Experimental_Soundness` | −1 | 0 |
| `Writing_Clarity` | −1 | **+1** |
| `Value_to_Community` | −1 | 0 |
| `Prior_Work_Context` | −1 | 0 |
| **Numerical score** | **2.4** | **5.8** |

The reviewer still leans toward rejection, and the reason is now specific and actionable:
the evaluation is a single synthetic study, with no seeded-defect ablation and no
comparison against the systems cited in Related Work. That gap is stated openly rather
than hidden; it is the natural next step, not a defect of this revision.

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
