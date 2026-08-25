# US momentum PIT and paper-only operating contract

This project treats `us_momentum_v1` as a US-only strategy. It never uses the
A-share security master, A-share lot sizes, price limits, stamp duty, or the
generic A-share paper account. There is no real-broker order entry point.

## Lifecycle

The enforced lifecycle is:

`DATA_BLOCKED -> DATA_READY -> BACKTEST_QUALIFIED -> PAPER_COLLECTING -> PAPER_QUALIFIED`

`HISTORICAL_FAILED` and `PAPER_BLOCKED` are fail-closed outcomes. Each
transition is stored transactionally in `data/us_momentum_program.db`. Paper
periods bind to explicitly admitted, append-only PIT releases; admission
verifies the immutable historical prefix before a newly certified month can be
used. Manual SQLite state edits are detected against the append-only audit log.

At initial installation the honest state is `DATA_BLOCKED`. A working TDX HTTP
service is necessary but does not make the strategy data-ready or paper-ready.

`us-pit doctor` reports these separately: `infrastructure_status` covers the
local TDX/17709 runtime, SEC contact configuration, and disk space;
`pit_data_status` covers the latest immutable PIT release. Infrastructure can
be `READY` while the top-level status and PIT remain `DATA_BLOCKED`; only
`formal_run_allowed=true` permits strict historical backtests.

## Data product

The only accepted universe is `sp500_ivv_proxy_v1`. A release is stored under
`data/us_pit/releases/<release_id>/` and contains immutable Parquet artifacts,
a quality report, source lineage, and a content-addressed manifest. The SQLite
catalog contains metadata only.

Official/free source roles are deliberately narrow:

- SEC EDGAR N-PORT for IVV Series `S000004310` is a late-publication validation
  anchor. It is not backdated into a signal input.
- An iShares IVV snapshot captured today is eligible only from its real
  `observed_at`; historical `asOfDate` responses are reconciliation evidence.
- TDX is the primary raw/vendor-front market source.
- Pinned AKShare output is cross-check-only and may not overwrite TDX.
- Unlicensed community archives cannot be dependencies of a READY release.

The release gate requires all normalized tables, 60 consecutive true XNYS
month ends, 282 warm-up sessions, stable security identities, full signal/raw
and next-open coverage, effective-dated actions and fees, lifecycle/delisting
reconciliation, official anchors, and zero Critical/High findings.

## Local commands

SEC contact identity is read from the process environment and then the Windows
user environment. `us-pit doctor` shows only a masked value; do not place the
contact string in source files, command transcripts, or logs.

```powershell
py -3 -m pip install -e ".[us-data]"
py -3 -m research_platform us-pit doctor
py -3 -m research_platform us-pit sync `
  --start 2019-10-01 --end 2026-07-31
```

An append-only forward evidence worker can be installed now. It runs every
five minutes but captures only on an actual XNYS session after the regular
close plus 15 minutes. During the session and on market holidays it exits
successfully without writing evidence; a missed session is never backfilled.
Each admitted session freezes the current iShares payload, the complete TDX
market=103 response, official normalization, and current alias cross-check:

```powershell
py -3 -m research_platform us-pit capture-current
py -3 -m research_platform us-pit forward-status
py -3 -m research_platform us-pit worker install
py -3 -m research_platform us-pit worker status
py -3 -m research_platform us-pit gaps --release <RELEASE_SHA256>
```

These captures are current-only evidence. They improve future PIT coverage but
cannot establish historical membership or aliases before `observed_at`.

`sync` freezes raw official evidence only. It does not infer missing historical
membership or actions. Normalize and review all required Parquet tables in a
local directory. The intended fail-closed sequence is:

```powershell
py -3 -m research_platform us-pit normalize-official `
  --source-batch <SEC_BATCH_SHA256> `
  --source-batch <ISHARES_BATCH_SHA256>

py -3 -m research_platform us-pit sync-sp500-events `
  --start 2019-10-01 --end 2026-07-31

py -3 -m research_platform us-pit propose-membership-events `
  --source-batch <SPGLOBAL_BATCH_SHA256> `
  --normalization-dir D:\private\us-pit-normalized `
  --output-dir D:\private\us-pit-membership-candidates

py -3 -m research_platform us-pit prepare-membership-review `
  --candidate-dir D:\private\us-pit-membership-candidates `
  --output-dir D:\private\us-pit-membership-review

py -3 -m research_platform us-pit audit-membership `
  --normalization-dir D:\private\us-pit-normalized `
  --candidate-dir D:\private\us-pit-membership-candidates `
  --source-batch <SEC_BATCH_SHA256> `
  --source-batch <SPGLOBAL_BATCH_SHA256> `
  --output-dir D:\private\us-pit-membership-audit

py -3 -m research_platform us-pit evidence-requests `
  --membership-audit-dir D:\private\us-pit-membership-audit `
  --output-dir D:\private\us-pit-corporate-action-evidence-requests

py -3 -m research_platform us-pit sync-sec-company-index

py -3 -m research_platform us-pit propose-sec-cik `
  --source-batch <SEC_COMPANY_INDEX_BATCH_SHA256> `
  --evidence-request-dir D:\private\us-pit-corporate-action-evidence-requests `
  --normalization-dir D:\private\us-pit-normalized `
  --output-dir D:\private\us-pit-sec-cik-candidates

py -3 -m research_platform us-pit sync-sec-submissions `
  --cik-candidate-dir D:\private\us-pit-sec-cik-candidates

py -3 -m research_platform us-pit propose-sec-filings `
  --source-batch <SEC_SUBMISSIONS_BATCH_SHA256> `
  --cik-candidate-dir D:\private\us-pit-sec-cik-candidates `
  --output-dir D:\private\us-pit-sec-filing-candidates

py -3 -m research_platform us-pit sync-sec-filing-documents `
  --filing-candidate-dir D:\private\us-pit-sec-filing-candidates

py -3 -m research_platform us-pit screen-sec-filings `
  --filing-candidate-dir D:\private\us-pit-sec-filing-candidates `
  --evidence-request-dir D:\private\us-pit-corporate-action-evidence-requests `
  --output-dir D:\private\us-pit-sec-filing-screen

py -3 -m research_platform us-pit rank-sec-filings `
  --screen-dir D:\private\us-pit-sec-filing-screen `
  --filing-candidate-dir D:\private\us-pit-sec-filing-candidates `
  --evidence-request-dir D:\private\us-pit-corporate-action-evidence-requests `
  --output-dir D:\private\us-pit-sec-filing-review

py -3 -m research_platform us-pit prepare-action-review `
  --evidence-request-dir D:\private\us-pit-corporate-action-evidence-requests `
  --ranked-review-dir D:\private\us-pit-sec-filing-review `
  --output-dir D:\private\us-pit-action-review-template

# Edit only action_review.csv. Cite an exact excerpt from one frozen CAS
# document for every decision; do not infer ratios, dates, or settlements.
py -3 -m research_platform us-pit propose-action-review `
  --template-dir D:\private\us-pit-action-review-template `
  --completed-csv D:\private\us-pit-action-review-template\action_review.csv `
  --output-dir D:\private\us-pit-action-review-proposal `
  --proposed-by <REVIEWER_ID>

py -3 -m research_platform us-pit approve-action-review `
  --proposal-dir D:\private\us-pit-action-review-proposal `
  --output-dir D:\private\us-pit-action-review-approved `
  --expected-sha256 <PROPOSAL_SHA256> `
  --approved-by <INDEPENDENT_APPROVER_ID>

py -3 -m research_platform us-pit prepare-review `
  --normalization-dir D:\private\us-pit-normalized `
  --membership-review-dir D:\private\us-pit-membership-review `
  --membership-audit-dir D:\private\us-pit-membership-audit `
  --action-review-dir D:\private\us-pit-action-review-approved `
  --output-dir D:\private\us-pit-review `
  --start 2021-08-01 --end 2026-07-31

py -3 -m research_platform us-pit propose-identity-bridges `
  --normalization-dir D:\private\us-pit-normalized `
  --output-dir D:\private\us-pit-identity-bridges

py -3 -m research_platform us-pit assemble-reviewed `
  --normalization-dir D:\private\us-pit-normalized `
  --review-dir D:\private\us-pit-review `
  --output-dir D:\private\us-pit-reviewed `
  --start 2019-10-01 --end 2026-07-31 `
  --source-batch <SEC_BATCH_SHA256> `
  --source-batch <ISHARES_BATCH_SHA256>

py -3 -m research_platform us-pit prepare-market `
  --input-dir D:\private\us-pit-reviewed `
  --output-dir D:\private\us-pit-market `
  --start 2019-10-01 --end 2026-07-31
```

Identity bridges are name-based review suggestions only. They are hash-bound
to the observed iShares and SEC anchor objects, remain unapproved, and cannot
be consumed by `assemble-reviewed` until independent identity evidence is
supplied.

The SEC company index is a current, review-only CIK search aid. The submissions
index, complete filing capture, keyword screen, and ranking queue only locate
possible evidence. They do not establish historical identity, action type,
announcement time, effective time, exchange ratio, or settlement amount. Every
selected filing must be reviewed against its frozen CAS object and approved
through the two-stage hash workflow before it can produce a corporate-action
row. Approval dependencies preserve the original capture timestamp and bind
the proposal hash, approver, approval timestamp, acknowledgement hash, and all
review decisions. One filing may support multiple independently cited actions;
none may overwrite another.

The repository's current corporate-action review queue is
`data/us_pit/action_reviews/corporate_actions_201910_202607_v6`. It contains 20
unresolved transitions and 170 unique, decision-time-visible SEC candidates.
It is deliberately `REVIEW_REQUIRED`, has no selected candidates, and cannot
be passed directly to `build`.

Official S&P pages from 2019 and early 2020 often state changes in prose rather
than an HTML table. They are reparsed only from their immutable CAS objects;
the reparse command performs no network access and publishes a new derivation
batch without modifying the capture batch:

```powershell
py -3 -m research_platform us-pit reparse-sp500-events `
  --source-batch 8013e995ab7d822deaf7f0434a5420bbf4712d01218eaffa3b019c45715048e9 `
  --start 2019-10-01 --end 2026-07-31
```

The current v3 derivation batch is
`11334161d5564512f4c75c602f9b7d34a25f6d82c2959273c9e90c2e41544248`.
It contains 222 explicit ADD/REMOVE rows from 73 frozen announcements. The
parser accepts only explicit tickers, action terms, and effective dates; titles
alone never become events. Candidates remain unapproved.

The current membership audit remains blocked by 19 quarterly anchor
mismatches, one unique membership-event state conflict affecting two month-end
checks, and one anchor acceptance-window conflict. These are historical
evidence gaps, not TDX installation failures.
The current immutable diagnostic is
`data/us_pit/membership_audits/spglobal_sec_201910_202607_v11`; its CLI output
includes `membership_event_conflict_root_count` for review triage while the
full per-month failures remain in `membership_audit.json`.
This audit also emits
`residual_membership_event_requests.parquet`: after applying review-only
identity-transition suggestions, every remaining ADD/REMOVE discrepancy is a
separate, unapproved official-evidence request. It is diagnostic only and can
never be consumed as a membership event without a frozen S&P source and normal
review approval.
The current file contains 38 requests: 19 ADD and 19 REMOVE facts. Six event
rows still lack a stable identity and 20 anchor differences remain explicit
identity-transition review candidates. These diagnostics explain the part of
the 19 anchor mismatches that cannot yet be closed by approved evidence.
The corresponding immutable, unapproved membership review template is
`data/us_pit/review_templates/membership_events_201910_202607_v11` (221 rows,
zero approved). It is the only current review input; editing candidate Parquet
or a manifest cannot approve an event.

Lifecycle surveillance documents use `us-lifecycle-surveillance-v3`. Each
security observation must bind a stable ISIN/CUSIP, explicit status,
`observed_through`, optional terminal effective date, and a short excerpt that
is rechecked against the frozen CAS source. A caller-supplied coverage list or
boolean cannot establish delisting coverage.

`assemble-reviewed` remains `DATA_BLOCKED` until stable identities, historical
membership events, lifecycle/termination evidence, and corporate actions have
been explicitly reviewed. `prepare-market` only reads TDX and writes a new
immutable directory; it never fills missing bars and never uses AKShare as a
fallback. It must be run after the review workspace exists, and its printed
market source batch hash must also be passed to `build`:

```powershell
py -3 -m research_platform us-pit build `
  --input-dir D:\private\us-pit-market `
  --source-batch <SEC_BATCH_SHA256> `
  --source-batch <ISHARES_BATCH_SHA256> `
  --source-batch <MARKET_BATCH_SHA256>

py -3 -m research_platform us-pit validate --release <RELEASE_SHA256>
py -3 -m research_platform us-pit qualify --release <RELEASE_SHA256>
```

Personal corrections use hash-bound two-stage approval:

```powershell
py -3 -m research_platform us-pit override propose --file proposal.json
py -3 -m research_platform us-pit override approve `
  --draft <OVERRIDE_ID> --expected-sha256 <DRAFT_SHA256>
```

The 20-session TDX shadow collector is read-only and fixed to SPY plus 30
cross-exchange names. Start it only after a DATA_READY release is registered:

```powershell
py -3 -m research_platform us-paper tdx-shadow-start --release <RELEASE_SHA256>
py -3 -m research_platform us-paper tdx-shadow-tick
py -3 -m research_platform us-paper tdx-shadow-reconcile --session YYYY-MM-DD
py -3 -m research_platform us-paper tdx-shadow-status
py -3 -m research_platform us-paper tdx-shadow-evaluate
```

The final raw Open for all 31 instruments must be reconciled after every close
through the collector API; missing or conflicting rows are permanent failures.
Only a qualified historical decision plus a qualified 20-session decision can
move the lifecycle into `PAPER_COLLECTING`.

```powershell
py -3 -m research_platform us-paper status
py -3 -m research_platform us-paper start --sessions 420
py -3 -m research_platform us-paper admit-release --release <RELEASE_SHA256>
py -3 -m research_platform us-paper tick
py -3 -m research_platform us-paper evaluate
py -3 -m research_platform us-paper worker install
py -3 -m research_platform us-paper worker status
```

`tick` exits fail-closed unless the lifecycle is `PAPER_COLLECTING`. The paper
database and runtime database are isolated from the platform's A-share account.
`start` is a one-time operation that freezes the forward XNYS sessions and
binds the runtime database to the active release and manifest. Recurring
workers reopen that exact binding with `tick`; they never rebuild the calendar
from the current date.
`evaluate` derives the 252-session/12-cycle/20-trade decision from the two
read-only SQLite ledgers. It requires raw `BIL.US`, a separately evidenced
`BILTR.US` total-return mark, and a complete 60-second held-position quote
ledger for every completed session; operators cannot type in qualification
totals. Every month-end decision freezes its complete input bundle, parameters,
release lineage, code hashes, and output in content-addressed storage. The
qualification process reruns that archive and reconciles signals, orders,
fills, corporate actions, cash, and positions.

All quote RPC methods are allow-listed read-only TQ methods. Company actions
are applied before orders each session. A fresh causal quote may create a paper
stop fill; a closing daily Low can only schedule a real next-open recovery exit.
The kill switch cancels BUY orders while reliable risk and ordinary SELL orders
continue. Recovery from degraded data is evidence-driven; the CLI cannot clear
the gate with an operator note.

## What cannot be claimed immediately

A five-year READY release cannot be produced until the historical PIT
membership/events, stable identifiers, company actions, delisted securities,
TDX bars, and fee schedule are actually complete and reconciled. Historical
qualification requires running the frozen 36/12/12-month protocol. TDX
qualification takes 20 consecutive real sessions, and paper qualification
requires 252 sessions, 12 full month-end cycles, and 20 closed trades.

If any free-source gap remains, the required result is `DATA_BLOCKED` with an
explicit quality issue, not a guessed value, current-survivor fallback, or
backfilled trade.
