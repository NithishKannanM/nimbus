# Handoff -- 2026-10-05 (Linux session)

The previous Windows handoff (NEXT.md items 1, 3, 4) is finished and its
plan file is deleted. This file is the current state.

## State

- **Tests: 637 passed** on Linux, Python 3.14 (498 old + 139 new).
  Run with the ext4 venv, because the repo `.venv` is dead on NTFS:
  `~/.venvs/nimbus/bin/python -m pytest -q -p no:cacheprovider`
  (recreate: `python3 -m venv ~/.venvs/nimbus && ~/.venvs/nimbus/bin/pip install -r requirements-dev.txt -r requirements-aws.txt`).
- **Terraform:** `terraform fmt -check` and `terraform validate` pass
  (1.16.5, aws provider 5.100.0, `-backend=false`). Terraform is not installed
  system-wide. Validate caught an older bug: `infra/batch.tf` used `name` on
  `aws_batch_compute_environment`, but provider 5.x needs
  `compute_environment_name`. Fixed.
- **AWS: nothing has run on AWS.** The HTTP data plane, batch entrypoint and
  CloudWatch metrics are code-complete and tested locally only. Phase 5's exit
  criterion is still unmet.

## Done this session

- **HTTP data plane (item 1):** `backend/services/remote.py`, `NIMBUS_WORKER_TRANSPORT=http`.
  Unit tests, `tests/unit/test_internal_api.py`, and
  `tests/integration/test_http_dataplane.py` (end to end through the real
  FastAPI apps).
- **Batch entrypoint (item 3):** `worker/batch_entrypoint.py`, plus
  `evaluate_on_exit` in `infra/batch.tf`.
- **CloudWatch Channel 1 (item 4):** `backend/services/metrics.py`,
  `factory.build_metrics`, and engine emission per docs/architecture.md s10.
- **Docs:** README, NEXT.md, architecture, aws-batch-baseline, cost-model and
  limitations are all updated, and they say "tested locally, unrun on AWS".
- **Frontend:**
  - Repo-root `DESIGN.md` is the design contract (plotter paper; see its Known Gaps).
  - A taste-skill audit fixed contrast, ARIA, type floor, responsive layout and
    loading/error states.
  - Graphite DECLARED ink is now `#5E645C` (was `#6B7169`, under AA contrast).
    **Not yet confirmed by the user.**
- **Checked live in Chrome:**
  - Fixed: the Finding sentence said "agree" while a worker was reserved 100%
    but observed 8%. It now judges the absolute gap.
  - Fixed: the empty fleet grid cell, the jobs-log scrollbar overlap, page
    overflow on mobile, and the strip now opens at its live edge.
- **Phase 1 demo:** `scripts/demo_phase1.py --jobs 24`: 24/24 completed, 0
  failed, 0.80 s makespan.

## How to see the working dashboard

```bash
~/.venvs/nimbus/bin/python -m uvicorn backend.api.app:app --port 8000   # http://127.0.0.1:8000/
~/.venvs/nimbus/bin/python scripts/demo_dashboard.py --duration 900     # sample jobs + CPU pressure on worker-1
```
If the frontend changes, rebuild with `cd frontend && npm run build`.

## Next

1. NEXT.md items 2, 5, 6, 7 are untouched. They need AWS spend or deliberate
   decisions.
2. An AWS run of items 1, 3 and 4 (`terraform apply`, then the suite under
   `NIMBUS_BACKEND=aws`).
3. Check the CloudWatch cost. `docs/cost-model.md` estimates about $9.30/month
   for 31 series, which is over the budget line. Whether AWS prorates hourly is
   unverified.
4. Confirm the graphite colour change with the user.
5. Mobile: the strip's worker labels are off-screen at the live edge (DESIGN.md
   Known Gaps).

## Gotchas

- The repo is on NTFS. Files can pick up an executable bit from Windows.
  This commit reset 6 such files to 644 in the git index.
- Taste-skill skills are installed globally in `~/.claude/skills`. Use them as
  an audit lens only, and don't let them replace the plotter-paper direction.
