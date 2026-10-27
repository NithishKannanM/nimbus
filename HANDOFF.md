# Handoff, 2026-10-12

Where Nimbus stands at the end of the day, what changed, and what is next.
Nothing from today is committed yet. This replaces the 2026-10-05 handoff
(still readable with `git show HEAD:HANDOFF.md`); everything from it that is
still open is carried into the lists below.

## Run the demo

```bash
scripts/run_demo.sh            # API + job stream with CPU pressure on worker-1 + dashboard
scripts/run_demo.sh --no-load  # API + dashboard only
scripts/run_demo.sh --built    # build the dashboard and serve it from the API on :8000
```

Open http://localhost:5173. Ctrl+C stops everything, including the CPU stressor.
Logs go to `.nimbus/demo-logs/`. The script finds the working venv at
`~/.venvs/nimbus` by itself (the repo `.venv` is broken on NTFS); override with
`NIMBUS_PYTHON=...`. If a port is taken it says so and stops: `fuser -k 8000/tcp`.

The demo: worker-1 sits at 100% CPU from load Nimbus did not place. On `fifo`
jobs keep landing on it; click `adaptive` and the Live strategy comparison shows
fewer jobs sent to the busy worker and shorter execution. Measured today:

| | fifo | adaptive |
|---|---|---|
| Sent to a busy worker (>=80% CPU) | 23% | 7-8% |
| Mean execution time | 1.75 s | 1.1-1.26 s |
| p95 execution time | 7.7 s | 2.5-2.7 s |

This is live and uncontrolled (the strategies ran one after another). The
controlled evidence is `docs/results.md`: under CPU pressure adaptive cut
makespan 68% vs fifo (p=0.0013) and 74% vs binpack, classed descriptive, not
primary, because the load-regime gate misfires on pressure profiles. Under no
pressure there is no difference, which was the predicted outcome.

## What changed today

**Dashboard redesign.** The brutalist theme read as a toy project; the user
picked the Modernize admin template as the reference. Now: 270px sidebar with
scroll-spy, sticky status bar, stat tiles, white cards, light and dark mode
(toggle in the top bar, remembered per browser). The declared / reserved /
observed colour meanings carried over unchanged. `DESIGN.md` is the contract.

**Less text.** Long explanations moved behind an "i" (`InfoTip.tsx`, hover,
focus or tap). Cards keep a subtitle of a few words. The user asked for this;
keep it that way.

**New panels.**
- *Active algorithm*: running strategy, three compact steps (order, filter,
  place) with detail in tooltips, and a bold Last decision panel.
- *Live strategy comparison*: one row per strategy used since the API started,
  each job counted under the strategy that placed it.
  Backend: `GET /scheduler/comparison`, `backend/services/comparison.py`; the
  engine records when each strategy was switched on. Covered by a new test in
  `tests/integration/test_api.py`.

**Cleanup pass** (taste skill, applied only where it fits a dashboard): no
em-dashes on screen, icons from `@tabler/icons-react` 3.31.0 instead of hand-drawn
SVG, no decorative dots, at most one middle dot per line.

**Fixes.** Expanding a fifo row in the Decision ledger would have crashed the
page (null `weights`). The `.overline` CSS class collided with Tailwind's
`overline` utility and drew a line above every label; renamed to `.eyebrow`.

**New:** `scripts/run_demo.sh`.

Verified: `637 passed, 1 skipped` (pytest), `tsc --noEmit` and `npm run build`
pass, checked in Chrome in both themes.

Run the tests with the ext4 venv:
`~/.venvs/nimbus/bin/python -m pytest -q -p no:cacheprovider`. To recreate it:
`python3 -m venv ~/.venvs/nimbus && ~/.venvs/nimbus/bin/pip install -r requirements-dev.txt -r requirements-aws.txt`.

## Next steps

1. **Commit today's work.** `git status` also lists `infra/*.tf`,
   `backend/services/factory.py`, `worker/api.py` and `tests/unit/fakes_aws.py`;
   those are file-mode changes from the NTFS drive (644 to 755), not edits.
   Run `git config core.fileMode false` before committing to drop them.
2. **AWS code delivery: fixed 2026-10-23, verified on real AWS.** The old
   `deploy.sh` rsynced code to the control plane only, so workers (no public
   ingress) never got any. Now `deploy.sh` uploads the working tree to
   `s3://<bucket>/release/nimbus.tar.gz` (SSE-AES256, as the bucket policy
   requires) and every instance runs `/usr/local/bin/nimbus-fetch-release`
   (`infra/user_data/fetch_release.sh`) as root in `ExecStartPre`: it unpacks
   only when the S3 ETag changed, keeps `.nimbus/`, and swaps directories
   atomically. Units have `StartLimitIntervalSec=0`, so fresh instances retry
   until the upload lands. Redeploy = re-run `deploy.sh`; it restarts running
   units over SSM (best effort) and then polls `/healthz` until every worker is
   HEALTHY (15 minute limit). New output: `instance_ids`.
   Verified: `terraform fmt -check` and `terraform validate` (1.16.5, from a
   scratch dir, nothing left in `infra/`), both templates render to valid bash,
   and a local simulation with a stand-in `aws` CLI covered no release yet,
   first install, same release again and a new release. The package is about
   1 MB and contains no `.git`, Terraform state, `.env` or `node_modules`.
   Only a real `terraform apply` proves it end to end.
3. **AWS account (set up 2026-10-23).** Project "Stack of Dreams", account
   `245177345538`, free plan ($100 credit, ends 2027-04-23), region
   **ap-southeast-2**. AWS CLI 2.38.5 is in `~/.local/bin`; profile `nimbus`
   signs in with `aws login --profile nimbus` (12 h sessions, renewable for 90
   days). The AWS Agent Toolkit is installed: 24 AWS skills, and an `aws-mcp`
   server in Claude Code, Codex, Gemini and OpenCode, each pointed at the
   `nimbus` profile via `AWS_MCP_PROXY_PROFILES`. The Terraform default region
   is us-east-1, so deploys need `TF_VAR_region=ap-southeast-2`; Sydney prices
   are about 20-25% higher (roughly $0.10/hour for the fleet).
4. **DEPLOYED to AWS on 2026-10-23; all 4 EC2 instances STOPPED the same day
   (stack not destroyed).** Stopped instances cost nothing for compute, but the
   EBS volumes, the SQS interface endpoint and the other resources still bill a
   little. To resume: `AWS_PROFILE=nimbus-tf aws ec2 start-instances
   --instance-ids i-0b8fa97b9628fe519 i-072ef4c87183250e7 i-0cfa070eaa63d8f0a
   i-09599985c7f0b38a3`, then re-run `terraform apply` with your current IP
   (the control plane gets a NEW public IP; read it with
   `terraform refresh && terraform output api_url`). To remove everything:
   teardown.sh. Original deploy notes:
   Dashboard http://13.239.38.43:8000 (reachable only from 122.187.117.179;
   re-run deploy.sh after an IP change). backend=aws, 3/3 workers HEALTHY, and
   a test job ran end to end (SQS -> worker-1 EC2 -> DynamoDB, with live
   observed CPU from all three workers in the decision trace). Two bugs only
   real AWS could show were fixed on the way: `httpx` was missing from
   `requirements.txt` (only dev requirements had it), and `aws_region` ignored
   `AWS_REGION`, so every boto3 client had no region (`NoRegionError`); it now
   also reads `AWS_REGION` and `AWS_DEFAULT_REGION`, with a regression test.
   Suite: 641 passed. Use profile `nimbus-tf` (in `~/.aws/config`) for
   Terraform; plain `aws login` sessions are not understood by the Terraform
   AWS provider. Ship code changes with
   `AWS_PROFILE=nimbus-tf scripts/demo/deploy.sh --code-only`. Tear down with
   `AWS_PROFILE=nimbus-tf TF_VAR_region=ap-southeast-2 TF_VAR_admin_cidr=<ip>/32 scripts/demo/teardown.sh -auto-approve`.
   Note: `~/.aws/config` also has a `[default]` profile on a different account
   (649437299771); never deploy without an explicit profile.
   Simulation on AWS: `AWS_PROFILE=nimbus-tf scripts/demo/simulate.sh`
   (job stream + a CPU hog on worker-1's EC2 host via SSM Run Command, which
   auto-stops on the host after the run or 1 h). Tested 2026-10-23: worker-1
   read 100% observed CPU; fifo sent 30/80 jobs (37%) to it, mean exec 3.7 s.
5. **How the first deploy was done:** install `terraform` (not on this machine yet), `aws login --profile nimbus`,
   `export AWS_PROFILE=nimbus TF_VAR_region=ap-southeast-2`,
   `export TF_VAR_admin_cidr="$(curl -s https://checkip.amazonaws.com)/32"`,
   `scripts/demo/deploy.sh` (it builds the dashboard itself), open
   `http://<control-plane-ip>:8000`, and always finish with
   `scripts/demo/teardown.sh`. About $0.10/hour in Sydney. Nothing has ever been
   deployed. If workers stay unhealthy: `aws ssm start-session --target <id>`
   then `journalctl -u nimbus-worker -n 100`.
6. **Still open from before** (details in `NEXT.md`): the Phase 5 exit
   criterion (an AWS run of the HTTP data plane, the Batch entrypoint and the
   CloudWatch metrics), checking CloudWatch cost (`docs/cost-model.md` estimates
   about $9.30/month for 31 series, over the budget line), `priority` ordering
   (unimplemented on purpose), re-running the headline experiment on EC2
   (`docs/limitations.md` section 3), and `adaptive-v2`.
7. Optional: self-host the two fonts (Plus Jakarta Sans, IBM Plex Mono) if the
   demo might run without internet; they load from Google Fonts today.
8. Optional: a slide deck for the faculty presentation from the explanation
   given today (what Nimbus is, where it sits in a pipeline, the backup-job
   scenario, the live demo, the caveats).

## Things to say carefully to the faculty

- It now runs on AWS (ap-southeast-2, 4 EC2 instances, SQS, DynamoDB, S3). The
  benchmark evidence in docs/results.md is still from local runs; re-running
  the experiments on EC2 is open (docs/limitations.md section 3).
- Workloads are synthetic and seeded (9 job types, 5 profiles), not a real trace.
- The 68% result is descriptive, not primary. Say why before anyone asks.

## Gotchas

- After editing `frontend/tailwind.config.js`, restart Vite; it does not reload
  config changes.
- `pkill -f` / `pgrep -f` match the calling shell here; stop things by port
  (`fuser -k PORT/tcp`). Restarting uvicorn by port alone can orphan its
  forkserver children; `run_demo.sh` kills whole process groups to avoid that.
- Headless Chrome `--force-dark-mode` inverts the page; test dark mode with the
  toggle.
- Restarting the API clears the live comparison. Restart just before a demo.
- Start the API from the repo root (`run_demo.sh` does). `artifact_dir` is
  relative, so starting elsewhere shows an empty benchmark archive.
- The user picks the visual direction; ask before changing it. Taste skills
  are installed globally in `~/.claude/skills`.
