from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import uvicorn

from .api import create_app
from .ai_research import AIResearchService
from .backtest_engine import BacktestService
from .cash_instrument_validation import run_cash_instrument_readiness
from .chan_research import run_persisted_chan_validation
from .config import PlatformConfig
from .course49_diagnostics import diagnose_backtest
from .feedback import FeedbackService
from .historical_security_master import (
    HistoricalSecurityMasterBlockedError,
    publish_historical_security_master,
)
from .pairs_arbitrage_research import run_persisted_pairs_arbitrage_validation
from .service import PlatformService
from .security_master_observation import capture_current_security_master_observation
from .storage import Database
from .tq_intraday_snapshot import capture_tq_watchlist_file
from .validation import validate_course49
from .v9_repo_forward_shadow import V9RepoForwardShadowService
from .weekly_triangle_research import run_persisted_weekly_triangle_setup_stability
from .us_paper import USMomentumPaperService, USPaperConfig
from .us_pit import (
    ISharesIVVHistoricalReconciliationAdapter,
    ISharesIVVObservedSnapshotAdapter,
    lifecycle_evidence_adapter,
    LicenseClass,
    OverrideProposal,
    ReviewedEvidenceSpec,
    ReviewedLocalEvidenceAdapter,
    RegulatoryFeeEvidenceAdapter,
    SECCompanyIdentityIndexAdapter,
    SECCompanySubmissionsAdapter,
    SECFilingDocumentsAdapter,
    captured_filing_accessions,
    rebind_existing_filing_documents,
    approve_action_review,
    prepare_action_review,
    propose_action_review,
    SPGlobalSP500MembershipEventAdapter,
    SECNPortIVVAdapter,
    SourceRole,
    SyncRequest,
    USPITMarketPreparer,
    USPITForwardCaptureService,
    FORWARD_CAPTURE_TASK_NAME,
    forward_capture_task_spec,
    forward_capture_task_status,
    install_forward_capture_task,
    remove_forward_capture_task,
    USPITReviewWorkspaceAssembler,
    USPITService,
)
from .us_program import USMomentumProgram
from .us_qualification import (
    HistoricalQualificationService,
    run_strict_qualification_backtest,
)
from .strategies.us_momentum import USMomentumParameters
from .us_tdx import check_tq_preflight


def _latest_release_summary(
    releases: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select the newest catalog row by creation time, never by release hash."""

    if not releases:
        return None
    return max(
        releases,
        key=lambda item: (
            datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00")),
            str(item.get("release_id") or ""),
        ),
    )


def _paper_corporate_action_records(dataset: Any, session_date: Any) -> list[dict[str, Any]]:
    """Return the verified release actions that must be applied for one session.

    Keeping this conversion next to the CLI wiring makes the production worker
    consume the exact corporate-action frame from its admitted decision release;
    the paper runtime remains responsible for evidence and timing validation.
    """

    frame = dataset.actions_on(session_date)
    if frame.empty:
        return []
    result: list[dict[str, Any]] = []
    for source in frame.to_dict(orient="records"):
        row = dict(source)
        action_type = str(row.get("action_type") or "").strip().upper()
        successor = str(row.get("successor_security_id") or "").strip()
        if successor and action_type in {
            "SPLIT", "STOCK_DIVIDEND", "STOCK_MERGER", "SPINOFF"
        }:
            code = dataset.vendor_code(successor, session_date)
            if action_type in {"SPLIT", "STOCK_DIVIDEND"}:
                row.setdefault("successor_code", code)
            elif action_type == "STOCK_MERGER":
                row.setdefault("target_security_id", successor)
                row.setdefault("target_code", code)
            else:
                row.setdefault("child_security_id", successor)
                row.setdefault("child_code", code)
        result.append(row)
    return result


def _paper_non_session_result(
    runtime: Any, session_date: date
) -> tuple[int, dict[str, Any]] | None:
    """Turn expected calendar closures into a clean worker result.

    A date beyond the immutable schedule is different from a weekend/holiday:
    the former needs an operator-visible schedule extension and must fail closed.
    """

    if runtime.schedule.contains(session_date):
        return None
    sessions = tuple(runtime.schedule.sessions)
    exhausted = bool(sessions and session_date > sessions[-1])
    return (
        2 if exhausted else 0,
        {
            "status": "PAPER_BLOCKED" if exhausted else "MARKET_CLOSED",
            "reason": (
                "FROZEN_XNYS_SCHEDULE_EXHAUSTED"
                if exhausted
                else "NOT_AN_XNYS_SESSION"
            ),
            "session_date": session_date.isoformat(),
            "runtime": runtime.status(),
            "broker_writes_enabled": False,
        },
    )


def _latest_completed_xnys_month_end(observed_at: datetime) -> date:
    """Return the last XNYS month-end whose regular session has completed."""

    import exchange_calendars as xcals
    import pandas as pd

    current = observed_at.astimezone(ZoneInfo("America/New_York"))
    xnys = xcals.get_calendar("XNYS")
    sessions = xnys.sessions_in_range(
        pd.Timestamp(date(current.year - 1, 1, 1)),
        pd.Timestamp(current.date() + timedelta(days=45)),
    )
    month_ends: list[date] = []
    for value, following_value in zip(sessions, sessions[1:]):
        session = pd.Timestamp(value)
        following = pd.Timestamp(following_value)
        close = xnys.session_close(session).to_pydatetime().astimezone(
            ZoneInfo("America/New_York")
        )
        day = session.tz_localize(None).date()
        next_day = following.tz_localize(None).date()
        if close <= current and next_day.month != day.month:
            month_ends.append(day)
    if not month_ends:
        raise ValueError("no completed XNYS month-end is available")
    return month_ends[-1]


def _xnys_month_ends(start: date, end: date) -> tuple[date, ...]:
    import exchange_calendars as xcals
    import pandas as pd

    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    values = [pd.Timestamp(item).tz_localize(None).date() for item in sessions]
    return tuple(
        day
        for index, day in enumerate(values)
        if index == len(values) - 1 or values[index + 1].month != day.month
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TongdaXin multi-strategy research platform")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("doctor", help="Check local runtime and TDX connectivity")
    subcommands.add_parser(
        "validation-gates",
        help="Run independent calendar/factor/corporate-action validation gates",
    )
    subcommands.add_parser(
        "snapshot-coverage",
        help="Audit point-in-time sector membership snapshot accumulation",
    )
    lhb_seat = subcommands.add_parser(
        "lhb-seat-store",
        help="Append-only dragon-tiger seat detail library (import or coverage)",
    )
    lhb_seat_sub = lhb_seat.add_subparsers(dest="lhb_seat_command", required=True)
    lhb_seat_import = lhb_seat_sub.add_parser("import")
    lhb_seat_import.add_argument("--input", required=True, help="JSON file with a list of seat detail rows")
    lhb_seat_import.add_argument(
        "--database",
        default=str(PlatformConfig().repository_root / "data" / "lhb_seat_details.db"),
    )
    lhb_seat_sub.add_parser("coverage").add_argument(
        "--database",
        default=str(PlatformConfig().repository_root / "data" / "lhb_seat_details.db"),
    )
    subcommands.add_parser("catalog", help="List strategy plugins and configured groups")

    sync = subcommands.add_parser("sync", help="Create a daily-data research snapshot")
    sync.add_argument("--daily-bars", type=int, default=120)
    sync.add_argument("--refresh-sectors", action="store_true")
    sync.add_argument("--refresh-data", action="store_true")

    scan = subcommands.add_parser("scan", help="Run research or paper scan")
    scan.add_argument("--strategies", default="course49_system")
    scan.add_argument("--mode", choices=("research", "paper"), default="research")
    scan.add_argument("--push-tdx", action="store_true")
    scan.add_argument("--refresh-sectors", action="store_true")
    scan.add_argument("--max-stocks", type=int)
    scan.add_argument("--sampling-mode", choices=("full", "stratified"), default="full")
    scan.add_argument("--sample-seed", type=int, default=49)
    scan.add_argument("--refresh-data", action="store_true")

    daily = subcommands.add_parser("daily-research", help="Run a scan and generate an AI research brief")
    daily.add_argument("--strategies", default="course49_system")
    daily.add_argument("--refresh-sectors", action="store_true")
    daily.add_argument("--max-stocks", type=int)
    daily.add_argument("--sampling-mode", choices=("full", "stratified"), default="full")
    daily.add_argument("--sample-seed", type=int, default=49)
    daily.add_argument("--refresh-data", action="store_true")

    brief = subcommands.add_parser("generate-brief", help="Generate a brief for an existing scan")
    brief.add_argument("--run-id", required=True)

    subcommands.add_parser("refresh-feedback", help="Refresh decision outcomes")
    subcommands.add_parser(
        "refresh-weekly-observations",
        help="Refresh observation-only weekly triangle outcomes",
    )

    setup_study = subcommands.add_parser(
        "weekly-triangle-setup-study",
        help="Recompute the frozen weekly-triangle SETUP stability study",
    )
    setup_study.add_argument(
        "--directory",
        default=str(PlatformConfig().repository_root / "data" / "research" / "weekly_triangle_v1"),
    )
    setup_study.add_argument(
        "--development-windows",
        default="bt_4bec5474e50b44bdb53aff39bb4075ca,bt_e40fe0fd8a2546729bbfe591b768c27a",
    )
    setup_study.add_argument(
        "--validation-windows",
        default="bt_1f2378fe2c984617911770ccb742a05e,bt_6b96520a77fb4ef68726988f55ef57c1",
    )

    pairs_study = subcommands.add_parser(
        "pairs-arbitrage-study",
        help="Recompute the frozen pairs-arbitrage historical audit",
    )
    pairs_study.add_argument(
        "--directory",
        default=str(
            PlatformConfig().repository_root
            / "data"
            / "research"
            / "pairs_arbitrage_v1"
        ),
    )

    chan_study = subcommands.add_parser(
        "chan-study",
        help="Run or resume the frozen five-window Chan historical audit",
    )
    chan_study.add_argument(
        "--directory",
        default=str(
            PlatformConfig().repository_root / "data" / "research" / "chan_v1"
        ),
    )

    tq_snapshot = subcommands.add_parser(
        "tq-minute-snapshot",
        help="Capture a validated immutable TQ snapshot for a frozen research watchlist",
    )
    tq_snapshot.add_argument(
        "--tdx-root",
        required=True,
        help="Root of a TdxQuant-enabled TongdaXin installation",
    )
    tq_snapshot.add_argument(
        "--watchlist",
        required=True,
        help="Frozen Parquet or CSV watchlist containing code/session_date pairs",
    )
    tq_snapshot.add_argument(
        "--output-dir",
        default=str(PlatformConfig().repository_root / "data" / "tq_intraday_research" / "snapshots"),
    )
    tq_snapshot.add_argument(
        "--checkpoint-dir",
        default=str(PlatformConfig().repository_root / "data" / "tq_intraday_research" / "checkpoints"),
    )
    tq_snapshot.add_argument("--period", choices=("5m",), default="5m")

    cash_status = subcommands.add_parser(
        "cash-instrument-status",
        help="Validate frozen money-ETF sources without opening result windows",
    )
    cash_status.add_argument(
        "--directory",
        default=str(
            PlatformConfig().repository_root / "data" / "cash_instrument_validation"
        ),
    )
    cash_status.add_argument(
        "--tdx-root",
        default=str(PlatformConfig().repository_root / "tdx-mock"),
    )

    security_master_observe = subcommands.add_parser(
        "security-master-observe",
        help=(
            "Seal read-only official/TDX current-master evidence; audit only, "
            "with no master publication, training, or trading"
        ),
    )
    security_master_observe.add_argument(
        "--runtime-dir",
        default=str(PlatformConfig().runtime_dir),
        help="Runtime data root for immutable evidence CAS objects",
    )
    security_master_observe.add_argument(
        "--tdx-timeout-seconds",
        type=float,
        default=120.0,
        help="Hard timeout for the single read-only TDX get_stock_list request",
    )

    security_master_publish = subcommands.add_parser(
        "security-master-publish",
        help=(
            "Publish the policy-bound historical master from one fresh immutable "
            "observation digest; no caller paths, code sets, or child digests"
        ),
    )
    security_master_publish.add_argument(
        "--current-observation-manifest",
        required=True,
        metavar="SHA256",
        help="SHA-256 digest of a freshly sealed current-observation manifest",
    )

    sse_delisted_capture = subcommands.add_parser(
        "early-winner-capture-sse-delisted-bars",
        help=(
            "Resume a small read-only batch of frozen 2018-2023 SSE delisted "
            "raw-bar evidence; audit only, with no training or trading"
        ),
    )
    sse_delisted_capture.add_argument(
        "--max-new-captures",
        type=int,
        default=5,
        help="Maximum new pre-2024 targets to attempt in this resumable batch",
    )
    sse_delisted_capture.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Read-only official-source request timeout",
    )

    cninfo_capture = subcommands.add_parser(
        "early-winner-capture-cninfo-announcements",
        help=(
            "Resume a small read-only batch of frozen 2018-2023 CNINFO "
            "announcement evidence; audit only, with no training or trading"
        ),
    )
    cninfo_capture.add_argument(
        "--max-new-targets",
        type=int,
        default=1,
        help="Maximum new authoritative targets to attempt in this resumable batch",
    )
    cninfo_capture.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Read-only official-source request timeout",
    )

    delisted_history_audit = subcommands.add_parser(
        "early-winner-audit-delisted-history",
        help=(
            "Cold-replay the frozen delisted-history source indexes and publish "
            "an audit-only gate; no training or trading"
        ),
    )
    delisted_history_audit.add_argument(
        "--source-index",
        action="append",
        metavar="DATASET=SHA256",
        help=(
            "Repeat for an explicit source set; omitted uses the frozen current "
            "partial-source example"
        ),
    )

    shadow = subcommands.add_parser(
        "v9-repo-shadow",
        help="Append one observation-only V9 plus one-day repo shadow session",
    )
    shadow.add_argument(
        "--tdx-root",
        help="Optional TongdaXin root; defaults to TDX_ROOT or platform configuration",
    )
    shadow.add_argument("--refresh-sectors", action="store_true")
    shadow.add_argument("--refresh-data", action="store_true")
    shadow.add_argument(
        "--tdx-probe-timeout",
        type=float,
        default=0.0,
        help="Optional fast TDX probe timeout; 0 skips the duplicate probe",
    )
    shadow.add_argument(
        "--tdx-run-timeout",
        type=float,
        default=300.0,
        help="Maximum seconds for the disposable full-market capture worker",
    )
    subcommands.add_parser(
        "v9-repo-shadow-status",
        help="Read the isolated V9/repo shadow gate and latest observation",
    )

    backtest = subcommands.add_parser("backtest", help="Run the Python primary backtest")
    backtest.add_argument("--strategy", default="course49_system")
    backtest.add_argument("--start")
    backtest.add_argument("--end")
    backtest.add_argument("--daily-bars", type=int, default=180)
    backtest.add_argument("--max-stocks", type=int)
    backtest.add_argument("--sampling-mode", choices=("full", "stratified"), default="full")
    backtest.add_argument("--sample-seed", type=int, default=49)
    backtest.add_argument(
        "--universe",
        choices=(
            "all_a", "main_board", "growth", "star", "beijing",
            "sp500_ivv_proxy_v1", "custom",
        ),
        default="all_a",
    )
    backtest.add_argument("--pit-release-id")
    backtest.add_argument("--codes", default="", help="Comma-separated codes for the custom universe")
    backtest.add_argument("--refresh-sectors", action="store_true")
    backtest.add_argument("--refresh-data", action="store_true")
    backtest.add_argument(
        "--playbooks",
        default="",
        help="Comma-separated Course49 playbook ids for research backtests",
    )

    us_pit = subcommands.add_parser("us-pit", help="Build and verify immutable US PIT releases")
    us_pit_sub = us_pit.add_subparsers(dest="us_pit_command", required=True)
    us_pit_sync = us_pit_sub.add_parser("sync")
    us_pit_sync.add_argument("--universe", default="sp500_ivv_proxy_v1")
    us_pit_sync.add_argument("--start", default="2019-10-01")
    us_pit_sync.add_argument("--end")
    us_pit_sync.add_argument(
        "--years",
        type=int,
        help="Deprecated compatibility override: start at January 1 N years ago",
    )
    us_pit_reconcile = us_pit_sub.add_parser(
        "sync-ishares-reconciliation",
        help=(
            "Freeze exact XNYS month-end IVV product-data JSON as late-observed "
            "validation anchors; never historical signal input"
        ),
    )
    us_pit_reconcile.add_argument("--start", default="2021-08-01")
    us_pit_reconcile.add_argument("--end", default="2026-07-31")
    us_pit_sub.add_parser(
        "doctor",
        help="Check SEC contact configuration, local PIT catalog, and read-only TDX runtime",
    )
    us_pit_fees = us_pit_sub.add_parser(
        "sync-fees",
        help="Freeze the official SEC and FINRA regulatory-fee source objects",
    )
    us_pit_fees.add_argument("--start", default="2018-05-22")
    us_pit_fees.add_argument("--end")
    us_pit_sub.add_parser(
        "sync-sec-company-index",
        help="Freeze SEC's current company index as a review-only CIK search aid",
    )
    us_pit_sub.add_parser(
        "sync-tdx-master",
        help="Freeze the current TDX market=103 US code table as cross-check evidence",
    )
    us_pit_sp_events = us_pit_sub.add_parser(
        "sync-sp500-events",
        help="Freeze official S&P press archive pages and explicit S&P 500 ADD/REMOVE announcements",
    )
    us_pit_sp_events.add_argument("--start", default="2021-08-01")
    us_pit_sp_events.add_argument("--end", default="2026-07-31")
    us_pit_sp_reparse = us_pit_sub.add_parser(
        "reparse-sp500-events",
        help="Derive v2 table/narrative events from immutable captured S&P probes",
    )
    us_pit_sp_reparse.add_argument("--source-batch", action="append", required=True)
    us_pit_sp_reparse.add_argument("--start", default="2019-10-01")
    us_pit_sp_reparse.add_argument("--end", default="2026-07-31")
    us_pit_sub.add_parser(
        "capture-current",
        help="Freeze today's post-close iShares and TDX evidence once, without backfill",
    )
    us_pit_sub.add_parser(
        "forward-status",
        help="Show the append-only current PIT capture status",
    )
    us_pit_gaps = us_pit_sub.add_parser(
        "gaps", help="Show structured blockers from one PIT release"
    )
    us_pit_gaps.add_argument("--release")
    us_pit_worker = us_pit_sub.add_parser(
        "worker", help="Manage the read-only current PIT capture task"
    )
    us_pit_worker_sub = us_pit_worker.add_subparsers(
        dest="us_pit_worker_command", required=True
    )
    for worker_command in ("install", "status", "remove"):
        worker_parser = us_pit_worker_sub.add_parser(worker_command)
        worker_parser.add_argument("--task-name", default=FORWARD_CAPTURE_TASK_NAME)
    us_pit_alias = us_pit_sub.add_parser(
        "crosscheck-current-aliases",
        help="Bind current observed IVV tickers to a frozen TDX market=103 master",
    )
    us_pit_alias.add_argument("--normalization-dir", required=True)
    us_pit_alias.add_argument("--tdx-source-batch", required=True)
    us_pit_alias.add_argument("--output-dir", required=True)
    us_pit_import = us_pit_sub.add_parser(
        "import-evidence",
        help="Freeze one locally reviewed public source file without interpreting it",
    )
    us_pit_import.add_argument("--file", required=True)
    us_pit_import.add_argument("--dataset", required=True)
    us_pit_import.add_argument("--source-id", required=True)
    us_pit_import.add_argument("--source-version", required=True)
    us_pit_import.add_argument("--url", required=True)
    us_pit_import.add_argument("--published-at", required=True)
    us_pit_import.add_argument("--as-of-date")
    us_pit_import.add_argument(
        "--role",
        choices=tuple(item.value for item in SourceRole),
        default=SourceRole.SIGNAL_INPUT.value,
    )
    us_pit_import.add_argument(
        "--license-class",
        choices=tuple(item.value for item in LicenseClass),
        default=LicenseClass.OFFICIAL_PUBLIC.value,
    )
    us_pit_import.add_argument("--media-type")
    us_pit_lifecycle = us_pit_sub.add_parser(
        "import-lifecycle",
        help="Validate and freeze one structured official lifecycle surveillance document",
    )
    us_pit_lifecycle.add_argument("--file", required=True)
    us_pit_lifecycle.add_argument("--source-id", required=True)
    us_pit_lifecycle.add_argument("--source-version", required=True)
    us_pit_lifecycle.add_argument("--url", required=True)
    us_pit_lifecycle.add_argument("--published-at", required=True)
    us_pit_lifecycle.add_argument(
        "--source-batch",
        action="append",
        required=True,
        help="Captured official source batch referenced by the lifecycle v3 document",
    )
    us_pit_normalize = us_pit_sub.add_parser(
        "normalize-official",
        help="Parse captured SEC/iShares evidence into review-only candidate Parquet",
    )
    us_pit_normalize.add_argument("--source-batch", action="append", required=True)
    us_pit_bridge = us_pit_sub.add_parser(
        "propose-identity-bridges",
        help="Create review-only ticker-to-stable-ID identity suggestions",
    )
    us_pit_bridge.add_argument("--normalization-dir", required=True)
    us_pit_bridge.add_argument("--output-dir", required=True)
    us_pit_bridge.add_argument("--as-of-date")
    us_pit_event_candidates = us_pit_sub.add_parser(
        "propose-membership-events",
        help="Create review-only stable-ID suggestions from captured S&P 500 announcements",
    )
    us_pit_event_candidates.add_argument("--source-batch", action="append", required=True)
    us_pit_event_candidates.add_argument("--normalization-dir", required=True)
    us_pit_event_candidates.add_argument("--output-dir", required=True)
    us_pit_event_review = us_pit_sub.add_parser(
        "prepare-membership-review",
        help="Create an unapproved membership-event review file from frozen candidates",
    )
    us_pit_event_review.add_argument("--candidate-dir", required=True)
    us_pit_event_review.add_argument("--output-dir", required=True)
    us_pit_event_direct_review = us_pit_sub.add_parser(
        "review-membership-events",
        help="Directly verify frozen official membership evidence and approve exact ticker identities",
    )
    us_pit_event_direct_review.add_argument("--source-batch", action="append", required=True)
    us_pit_event_direct_review.add_argument("--candidate-dir", required=True)
    us_pit_event_direct_review.add_argument("--normalization-dir", required=True)
    us_pit_event_direct_review.add_argument("--output-dir", required=True)
    us_pit_event_direct_review.add_argument(
        "--reviewer", default="codex-evidence-review"
    )
    us_pit_event_direct_review.add_argument(
        "--identity-crosscheck-dir",
        help=(
            "V2: frozen SEC-filed identity crosscheck package enabling "
            "dual-official-source identity admission for name-matched anchors"
        ),
    )
    us_pit_membership_audit = us_pit_sub.add_parser(
        "audit-membership",
        help="Replay unapproved official candidates into a non-buildable gap audit",
    )
    us_pit_membership_audit.add_argument("--normalization-dir", required=True)
    us_pit_membership_audit.add_argument("--candidate-dir", required=True)
    us_pit_membership_audit.add_argument("--source-batch", action="append", required=True)
    us_pit_membership_audit.add_argument("--output-dir", required=True)
    us_pit_membership_audit.add_argument("--start", default="2021-08-01")
    us_pit_membership_audit.add_argument("--end", default="2026-07-31")
    us_pit_evidence_requests = us_pit_sub.add_parser(
        "evidence-requests",
        help="Create a non-buildable official corporate-action evidence queue",
    )
    us_pit_evidence_requests.add_argument("--membership-audit-dir", required=False)
    us_pit_evidence_requests.add_argument(
        "--operator-transitions",
        help="JSON file with operator-proposed transitions (anchor_date + both security ids)",
    )
    us_pit_evidence_requests.add_argument("--proposed-by", default="local-user")
    us_pit_evidence_requests.add_argument("--normalization-dir", help="Official normalization for identity metadata resolution")
    us_pit_evidence_requests.add_argument("--output-dir", required=True)
    us_pit_sec_cik = us_pit_sub.add_parser(
        "propose-sec-cik",
        help="Create review-only CIK candidates for corporate-action evidence requests",
    )
    us_pit_sec_cik.add_argument("--source-batch", action="append", required=True)
    us_pit_sec_cik.add_argument("--evidence-request-dir", required=True)
    us_pit_sec_cik.add_argument("--output-dir", required=True)
    us_pit_sec_cik.add_argument("--normalization-dir")
    us_pit_sec_submissions = us_pit_sub.add_parser(
        "sync-sec-submissions",
        help="Freeze SEC filing indexes for unique review-only CIK candidates",
    )
    us_pit_sec_submissions.add_argument("--cik-candidate-dir", required=True)
    us_pit_sec_submissions.add_argument(
        "--only-new-against",
        help="Only capture CIKs absent from an earlier immutable candidate package",
    )
    us_pit_sec_filings = us_pit_sub.add_parser(
        "propose-sec-filings",
        help="Create review-only filing candidates near identity transition anchors",
    )
    us_pit_sec_filings.add_argument("--source-batch", action="append", required=True)
    us_pit_sec_filings.add_argument("--cik-candidate-dir", required=True)
    us_pit_sec_filings.add_argument("--output-dir", required=True)
    us_pit_sec_filings.add_argument("--before-days", type=int, default=365)
    us_pit_sec_filings.add_argument("--after-days", type=int, default=93)
    us_pit_sec_documents = us_pit_sub.add_parser(
        "sync-sec-filing-documents",
        help="Freeze deduplicated SEC complete submissions from a review-only filing queue",
    )
    us_pit_sec_documents.add_argument("--filing-candidate-dir", required=True)
    us_pit_sec_documents.add_argument("--chunk-size", type=int, default=25)
    us_pit_sec_screen = us_pit_sub.add_parser(
        "screen-sec-filings",
        help="Screen frozen SEC submissions into an unapproved action-evidence review queue",
    )
    us_pit_sec_screen.add_argument(
        "--source-batch",
        action="append",
        default=[],
        help="Explicit document batches; omitted resolves the candidate-bound catalog set",
    )
    us_pit_sec_screen.add_argument("--filing-candidate-dir", required=True)
    us_pit_sec_screen.add_argument("--evidence-request-dir", required=True)
    us_pit_sec_screen.add_argument("--output-dir", required=True)
    us_pit_sec_rank = us_pit_sub.add_parser(
        "rank-sec-filings",
        help="Rank frozen SEC filing candidates into a bounded unapproved review queue",
    )
    us_pit_sec_rank.add_argument("--screen-dir", required=True)
    us_pit_sec_rank.add_argument("--filing-candidate-dir", required=True)
    us_pit_sec_rank.add_argument("--evidence-request-dir", required=True)
    us_pit_sec_rank.add_argument("--output-dir", required=True)
    us_pit_sec_rank.add_argument("--per-request", type=int, default=10)
    us_pit_action_review = us_pit_sub.add_parser(
        "prepare-action-review",
        help="Create a one-row-per-transition SEC action review draft",
    )
    us_pit_action_review.add_argument("--evidence-request-dir", required=True)
    us_pit_action_review.add_argument("--ranked-review-dir", required=True)
    us_pit_action_review.add_argument("--output-dir", required=True)
    us_pit_action_propose = us_pit_sub.add_parser(
        "propose-action-review",
        help="Validate and freeze a completed SEC action review without approval",
    )
    us_pit_action_propose.add_argument("--template-dir", required=True)
    us_pit_action_propose.add_argument("--completed-csv", required=True)
    us_pit_action_propose.add_argument("--output-dir", required=True)
    us_pit_action_propose.add_argument("--proposed-by", default="local-user")
    us_pit_action_approve = us_pit_sub.add_parser(
        "approve-action-review",
        help="Approve an immutable SEC action proposal by its expected SHA256",
    )
    us_pit_action_approve.add_argument("--proposal-dir", required=True)
    us_pit_action_approve.add_argument("--output-dir", required=True)
    us_pit_action_approve.add_argument("--expected-sha256", required=True)
    us_pit_action_approve.add_argument("--approved-by", default="local-user")
    us_pit_action_approve.add_argument(
        "--acknowledgement",
        default="I verified every cited frozen SEC document and action term.",
    )
    us_pit_review_template = us_pit_sub.add_parser(
        "prepare-review",
        help="Create an unapproved review template and machine-readable PIT gap report",
    )
    us_pit_review_template.add_argument("--normalization-dir", required=True)
    us_pit_review_template.add_argument("--output-dir", required=True)
    us_pit_review_template.add_argument("--start", default="2021-08-01")
    us_pit_review_template.add_argument("--end", default="2026-07-31")
    us_pit_review_template.add_argument("--membership-review-dir")
    us_pit_review_template.add_argument("--membership-audit-dir")
    us_pit_review_template.add_argument("--action-review-dir")
    us_pit_assemble = us_pit_sub.add_parser(
        "assemble-reviewed",
        help="Resolve reviewed identities and emit a fail-closed PIT workspace",
    )
    us_pit_assemble.add_argument("--normalization-dir", required=True)
    us_pit_assemble.add_argument("--review-dir", required=True)
    us_pit_assemble.add_argument("--output-dir", required=True)
    us_pit_assemble.add_argument("--start", required=True)
    us_pit_assemble.add_argument("--end", required=True)
    us_pit_assemble.add_argument("--source-batch", action="append", required=True)
    us_pit_assemble.add_argument("--reviewer", default="local-user")
    us_pit_assemble.add_argument("--approved-at")
    us_pit_market = us_pit_sub.add_parser(
        "prepare-market",
        help="Complete a reviewed PIT workspace with read-only TDX market data",
    )
    us_pit_market.add_argument("--input-dir", required=True)
    us_pit_market.add_argument("--output-dir", required=True)
    us_pit_market.add_argument("--start", required=True)
    us_pit_market.add_argument("--end", required=True)
    us_pit_market.add_argument("--universe", default="sp500_ivv_proxy_v1")
    us_pit_market.add_argument("--tdx-source-version")
    us_pit_market.add_argument("--commission-rate", type=float, default=0.0005)
    us_pit_market.add_argument("--slippage-rate", type=float, default=0.0005)
    us_pit_build = us_pit_sub.add_parser("build")
    us_pit_build.add_argument("--universe", default="sp500_ivv_proxy_v1")
    us_pit_build.add_argument("--years", type=int, default=5)
    us_pit_build.add_argument("--input-dir", required=True)
    us_pit_build.add_argument("--source-batch", action="append", default=[])
    us_pit_build.add_argument("--override", action="append", default=[])
    us_pit_validate = us_pit_sub.add_parser("validate")
    us_pit_validate.add_argument("--release", required=True)
    us_pit_qualify = us_pit_sub.add_parser("qualify")
    us_pit_qualify.add_argument("--release", required=True)
    us_pit_list = us_pit_sub.add_parser("list")
    us_pit_override = us_pit_sub.add_parser("override")
    us_pit_override_sub = us_pit_override.add_subparsers(
        dest="us_pit_override_command", required=True
    )
    us_pit_propose = us_pit_override_sub.add_parser("propose")
    us_pit_propose.add_argument("--file", required=True)
    us_pit_approve = us_pit_override_sub.add_parser("approve")
    us_pit_approve.add_argument("--draft", required=True)
    us_pit_approve.add_argument("--expected-sha256", required=True)
    us_pit_approve.add_argument("--approved-by", default="local-user")
    us_pit_approve.add_argument(
        "--acknowledgement",
        default="I verified the cited evidence and accept this local repair.",
    )
    us_pit_override_sub.add_parser("list")

    us_paper = subcommands.add_parser("us-paper", help="Operate isolated paper-only US runtime")
    us_paper_sub = us_paper.add_subparsers(dest="us_paper_command", required=True)
    us_paper_sub.add_parser("status")
    us_paper_start = us_paper_sub.add_parser("start")
    us_paper_start.add_argument(
        "--sessions",
        type=int,
        default=420,
        help="Number of future XNYS sessions to freeze for the paper worker",
    )
    us_paper_admit = us_paper_sub.add_parser(
        "admit-release",
        help="Verify and append a DATA_READY PIT release for future paper decisions",
    )
    us_paper_admit.add_argument("--release", required=True)
    us_paper_sub.add_parser("tick")
    us_paper_sub.add_parser("evaluate")
    us_paper_sub.add_parser("tdx-shadow-status")
    us_paper_shadow_start = us_paper_sub.add_parser("tdx-shadow-start")
    us_paper_shadow_start.add_argument("--release", required=True)
    us_paper_sub.add_parser("tdx-shadow-tick")
    us_paper_shadow_reconcile = us_paper_sub.add_parser("tdx-shadow-reconcile")
    us_paper_shadow_reconcile.add_argument("--session", required=True)
    us_paper_sub.add_parser("tdx-shadow-evaluate")
    us_paper_kill = us_paper_sub.add_parser("kill")
    us_paper_kill.add_argument("--note", required=True)
    us_paper_resume = us_paper_sub.add_parser("resume")
    us_paper_resume.add_argument("--note", required=True)
    us_paper_worker = us_paper_sub.add_parser(
        "worker", help="Manage the local 60-second paper-only Windows worker"
    )
    us_paper_worker_sub = us_paper_worker.add_subparsers(
        dest="us_paper_worker_command", required=True
    )
    for worker_command in ("install", "status", "remove"):
        worker_parser = us_paper_worker_sub.add_parser(worker_command)
        worker_parser.add_argument(
            "--task-name", default="ResearchPlatform-USMomentum-Paper"
        )
    backtest.add_argument(
        "--execution-cost-multiplier",
        type=float,
        default=1.0,
        help="Scale commission, tax, and slippage for supported strategy backtests",
    )

    replay = subcommands.add_parser(
        "backtest-replay",
        help="Replay a supported strategy from an immutable saved snapshot",
    )
    replay.add_argument("--source-backtest-id", required=True)
    replay.add_argument("--strategy")
    replay.add_argument("--start")
    replay.add_argument("--end")
    replay.add_argument("--execution-cost-multiplier", type=float, default=1.0)

    for command in ("validate-course49", "validate-course49-v3"):
        validate = subcommands.add_parser(
            command,
            help="Evaluate the versioned long-window, forward, and cost-stress gate",
        )
        validate.add_argument("--baseline-backtest-id", required=True)
        validate.add_argument("--stress-backtest-id")
        validate.add_argument("--historical-holdout-backtest-id")
        validate.add_argument("--policy-freeze-date", default="2026-08-09")

    diagnose = subcommands.add_parser(
        "diagnose-course49",
        help="Run an execution-aware reward diagnostic from a saved backtest snapshot",
    )
    diagnose.add_argument("--backtest-id", required=True)
    diagnose.add_argument("--state-strategy")
    diagnose.add_argument("--scope", choices=("snapshot", "backtest"), default="snapshot")
    diagnose.add_argument("--output-dir")

    serve = subcommands.add_parser("serve", help="Serve the API and built React dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    subcommands.add_parser("cache-status", help="Show memory and disk cache usage")
    subcommands.add_parser("cache-prune", help="Apply the configured disk cache limit")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = PlatformConfig()
    if args.command == "us-pit":
        pit = USPITService(config.us_pit_dir)
        program = USMomentumProgram(config.us_program_database_path)
        if args.us_pit_command == "sync":
            if args.universe != "sp500_ivv_proxy_v1":
                raise ValueError("US PIT sync only supports sp500_ivv_proxy_v1")
            observed_at = datetime.now(timezone.utc)
            if args.years is not None:
                start = date(observed_at.year - max(1, args.years), 1, 1)
            else:
                start = date.fromisoformat(args.start)
            end = (
                date.fromisoformat(args.end)
                if args.end
                else _latest_completed_xnys_month_end(observed_at)
            )
            if end < start:
                raise ValueError("US PIT sync end date precedes start date")
            request = SyncRequest(start, end, observed_at)
            sec_batch, observed_batch = pit.capture_official_evidence(request)
            _print(
                {
                    "status": "SOURCE_EVIDENCE_CAPTURED",
                    "batch_ids": [sec_batch.batch_id, observed_batch.batch_id],
                    "dependencies": [
                        item.to_dict()
                        for batch in (sec_batch, observed_batch)
                        for item in batch.dependencies
                    ],
                    "next_state": "DATA_BLOCKED",
                    "detail": (
                        "SEC anchors and the current actually-observed iShares snapshot "
                        "were frozen; normalized membership, identity, corporate actions "
                        "and TDX bars are still required before build."
                    ),
                }
            )
        elif args.us_pit_command == "sync-ishares-reconciliation":
            observed_at = datetime.now(timezone.utc)
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
            if end < start:
                raise ValueError("iShares reconciliation end date precedes start date")
            month_ends = _xnys_month_ends(start, end)
            if not month_ends:
                raise ValueError("iShares reconciliation window has no XNYS month-end")
            batch = pit.sync(
                ISharesIVVHistoricalReconciliationAdapter(month_ends),
                SyncRequest(start, end, observed_at),
            )
            _print(
                {
                    "status": "VALIDATION_ANCHORS_CAPTURED",
                    "batch_id": batch.batch_id,
                    "snapshot_count": len(batch.dependencies),
                    "first_as_of_date": month_ends[0].isoformat(),
                    "last_as_of_date": month_ends[-1].isoformat(),
                    "historical_signal_eligible": False,
                    "next_state": "DATA_BLOCKED",
                    "detail": (
                        "Official exact-date JSON was frozen at its actual observation time. "
                        "It may reconcile identities and holdings sets but cannot backfill "
                        "decision-time availability."
                    ),
                }
            )
        elif args.us_pit_command == "doctor":
            user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
            if not user_agent:
                try:
                    import winreg

                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                        user_agent = str(
                            winreg.QueryValueEx(key, "SEC_USER_AGENT")[0] or ""
                        ).strip()
                except (ImportError, OSError):
                    user_agent = ""
            preflight = check_tq_preflight(portable_root=config.tdx_root).as_dict()
            try:
                akshare_version = importlib.metadata.version("akshare")
            except importlib.metadata.PackageNotFoundError:
                akshare_version = None
            akshare_expected = "1.18.84"
            disk = shutil.disk_usage(config.us_pit_dir.parent)
            catalog = pit.list_releases()
            batches = pit.list_source_batches()
            forward_service = USPITForwardCaptureService(pit)
            forward_capture = forward_service.status()
            current_aliases = forward_service.latest_alias_status()
            ready = bool(user_agent and preflight["ready"] and disk.free >= 20 * 1024**3)
            latest_release = _latest_release_summary(catalog)
            pit_status = (
                str(latest_release.get("status"))
                if latest_release is not None
                else "DATA_BLOCKED"
            )
            formal_run_allowed = bool(ready and pit_status == "DATA_READY")
            _print(
                {
                    "status": "DATA_READY" if formal_run_allowed else "DATA_BLOCKED",
                    "infrastructure_status": "READY" if ready else "DATA_BLOCKED",
                    "pit_data_status": pit_status,
                    "formal_run_allowed": formal_run_allowed,
                    "sec_user_agent_configured": bool(user_agent),
                    "sec_user_agent_masked": (
                        "configured (contact hidden)" if user_agent else "missing"
                    ),
                    "tdx_root": str(config.tdx_root),
                    "tdx": preflight,
                    "akshare": {
                        "installed": akshare_version is not None,
                        "version": akshare_version,
                        "expected_version": akshare_expected,
                        "pinned_version_matches": akshare_version == akshare_expected,
                        "authority": "CROSS_CHECK_ONLY",
                        "may_override_tdx": False,
                    },
                    "pit_root": str(config.us_pit_dir),
                    "source_batch_count": len(batches),
                    "release_count": len(catalog),
                    "forward_capture": forward_capture,
                    "current_alias_crosscheck": current_aliases,
                    "free_disk_bytes": disk.free,
                    "minimum_free_disk_bytes": 20 * 1024**3,
                    "broker_writes_enabled": False,
                }
            )
            return 0 if ready else 2
        elif args.us_pit_command == "sync-fees":
            observed_at = datetime.now(timezone.utc)
            batch = pit.sync(
                RegulatoryFeeEvidenceAdapter(),
                SyncRequest(
                    start_date=date.fromisoformat(args.start),
                    end_date=(
                        observed_at.date()
                        if args.end is None
                        else date.fromisoformat(args.end)
                    ),
                    observed_at=observed_at,
                ),
            )
            _print(
                {
                    "status": "REGULATORY_FEE_EVIDENCE_CAPTURED",
                    "batch_id": batch.batch_id,
                    "datasets": [item.dataset for item in batch.dependencies],
                    "sec_user_agent_configured": True,
                }
            )
        elif args.us_pit_command == "sync-sec-company-index":
            observed_at = datetime.now(timezone.utc)
            batch = pit.sync(
                SECCompanyIdentityIndexAdapter(),
                SyncRequest(
                    start_date=observed_at.date(),
                    end_date=observed_at.date(),
                    observed_at=observed_at,
                ),
            )
            dependency = batch.dependencies[0]
            _print(
                {
                    "status": "SEC_COMPANY_INDEX_CAPTURED",
                    "batch_id": batch.batch_id,
                    "object_sha256": dependency.object_sha256,
                    "company_count": dict(dependency.metadata).get("company_count"),
                    "current_snapshot_only": True,
                    "historical_identity_authority": False,
                    "corporate_action_evidence": False,
                    "broker_writes_enabled": False,
                }
            )
        elif args.us_pit_command == "sync-sec-submissions":
            from .us_pit.sec_filing_candidates import load_unique_candidate_ciks

            observed_at = datetime.now(timezone.utc)
            ciks = set(load_unique_candidate_ciks(args.cik_candidate_dir))
            if args.only_new_against:
                ciks -= set(load_unique_candidate_ciks(args.only_new_against))
            if not ciks:
                raise ValueError("SEC submissions candidate difference is empty")
            ordered_ciks = tuple(sorted(ciks))
            batch = pit.sync(
                SECCompanySubmissionsAdapter(ordered_ciks),
                SyncRequest(
                    start_date=date(2019, 10, 1),
                    end_date=observed_at.date(),
                    observed_at=observed_at,
                ),
            )
            _print(
                {
                    "status": "SEC_SUBMISSIONS_INDEXES_CAPTURED",
                    "batch_id": batch.batch_id,
                    "cik_count": len(ordered_ciks),
                    "object_count": len(batch.dependencies),
                    "discovery_only": True,
                    "corporate_action_terms_verified": False,
                    "broker_writes_enabled": False,
                }
            )
        elif args.us_pit_command == "sync-sec-filing-documents":
            observed_at = datetime.now(timezone.utc)
            if args.chunk_size <= 0 or args.chunk_size > 100:
                raise ValueError("SEC filing document chunk size must be between 1 and 100")
            base = SECFilingDocumentsAdapter(args.filing_candidate_dir)
            rebound, rebound_batch_ids = rebind_existing_filing_documents(
                pit.store,
                base,
                chunk_size=args.chunk_size,
            )
            captured, existing_batch_ids = captured_filing_accessions(
                pit.store,
                candidate_set_id=base.candidate_set_id,
                candidate_manifest_sha256=base.candidate_manifest_sha256,
            )
            pending = [
                record["accession_number"]
                for record in base.records
                if record["accession_number"] not in captured
            ]
            new_batch_ids: list[str] = []
            for offset in range(0, len(pending), args.chunk_size):
                chunk = pending[offset : offset + args.chunk_size]
                chunk_observed_at = datetime.now(timezone.utc)
                adapter = SECFilingDocumentsAdapter(
                    args.filing_candidate_dir,
                    accessions=chunk,
                )
                batch = pit.sync(
                    adapter,
                    SyncRequest(
                        start_date=date(2019, 10, 1),
                        end_date=chunk_observed_at.date(),
                        observed_at=chunk_observed_at,
                    ),
                )
                new_batch_ids.append(batch.batch_id)
                captured.update(chunk)
            _print(
                {
                    "status": "SEC_FILING_DOCUMENTS_CAPTURED",
                    "candidate_set_id": base.candidate_set_id,
                    "document_count": len(base.records),
                    "captured_document_count": len(captured),
                    "new_batch_ids": new_batch_ids,
                    "rebound_batch_ids": list(rebound_batch_ids),
                    "rebound_document_count": len(rebound),
                    "existing_batch_ids": sorted(existing_batch_ids),
                    "chunk_size": args.chunk_size,
                    "discovery_only": True,
                    "corporate_action_terms_verified": False,
                    "broker_writes_enabled": False,
                }
            )
        elif args.us_pit_command == "sync-sp500-events":
            observed_at = datetime.now(timezone.utc)
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
            if end < start:
                raise ValueError("S&P event sync end date precedes start date")
            batch = pit.sync(
                SPGlobalSP500MembershipEventAdapter(),
                SyncRequest(start, end, observed_at),
            )
            event_dependencies = [
                item for item in batch.dependencies if item.dataset == "membership_events"
            ]
            _print(
                {
                    "status": "OFFICIAL_MEMBERSHIP_EVENTS_CAPTURED",
                    "batch_id": batch.batch_id,
                    "announcement_count": len(event_dependencies),
                    "event_count": sum(
                        int(dict(item.metadata).get("event_count") or 0)
                        for item in event_dependencies
                    ),
                    "candidate_only": False,
                    "stable_identity_resolved": False,
                    "next_state": "DATA_BLOCKED",
                    "broker_writes_enabled": False,
                }
            )
        elif args.us_pit_command == "reparse-sp500-events":
            from .us_pit.spglobal_events import reparse_spglobal_event_probes

            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
            batch = reparse_spglobal_event_probes(
                pit.store,
                args.source_batch,
                start_date=start,
                end_date=end,
            )
            event_dependencies = [
                item for item in batch.dependencies if item.dataset == "membership_events"
            ]
            _print(
                {
                    "status": "OFFICIAL_MEMBERSHIP_EVENTS_REPARSED",
                    "batch_id": batch.batch_id,
                    "announcement_count": len(event_dependencies),
                    "event_count": sum(
                        int(dict(item.metadata).get("event_count") or 0)
                        for item in event_dependencies
                    ),
                    "source_batch_ids": sorted(set(args.source_batch)),
                    "network_accessed": False,
                    "candidate_only": False,
                    "stable_identity_resolved": False,
                    "next_state": "DATA_BLOCKED",
                    "broker_writes_enabled": False,
                }
            )
        elif args.us_pit_command == "sync-tdx-master":
            from .us_pit.tdx_current_master import TDXCurrentUSMasterAdapter

            observed_at = datetime.now(timezone.utc)
            batch = pit.sync(
                TDXCurrentUSMasterAdapter(),
                SyncRequest(
                    start_date=observed_at.date(),
                    end_date=observed_at.date(),
                    observed_at=observed_at,
                ),
            )
            dependency = batch.dependencies[0]
            _print(
                {
                    "status": "TDX_CURRENT_US_MASTER_CAPTURED",
                    "batch_id": batch.batch_id,
                    "row_count": dependency.metadata.get("row_count"),
                    "role": dependency.role.value,
                    "membership_authority": False,
                    "broker_writes_enabled": False,
                }
            )
        elif args.us_pit_command == "capture-current":
            result = USPITForwardCaptureService(pit).capture()
            _print(result.to_dict())
            if result.status in {"MARKET_CLOSED", "WAITING_FOR_POST_CLOSE"}:
                return 0
        elif args.us_pit_command == "forward-status":
            service = USPITForwardCaptureService(pit)
            _print(
                {
                    **service.status(),
                    "current_alias_crosscheck": service.latest_alias_status(),
                }
            )
        elif args.us_pit_command == "gaps":
            releases = pit.list_releases()
            release_id = args.release or (
                None if not releases else str(releases[0]["release_id"])
            )
            if release_id is None:
                _print(
                    {
                        "status": "DATA_BLOCKED",
                        "release_id": None,
                        "issues": [
                            {
                                "code": "NO_PIT_RELEASE",
                                "severity": "CRITICAL",
                                "dataset": "release",
                                "message": "No PIT release has been built.",
                                "evidence": {},
                            }
                        ],
                        "broker_writes_enabled": False,
                    }
                )
            else:
                report = pit.validate_release(release_id)
                release_detail = pit.release_detail(release_id)
                upstream_review_gaps = dict(
                    release_detail.get("metadata", {}).get(
                        "upstream_review_gaps", {}
                    )
                )
                _print(
                    {
                        "status": report.status.value,
                        "release_id": release_id,
                        "quality_contract_revision": report.metrics.get(
                            "quality_contract_revision"
                        ),
                        "critical_count": sum(
                            item.severity.value == "CRITICAL" for item in report.issues
                        ),
                        "high_count": sum(
                            item.severity.value == "HIGH" for item in report.issues
                        ),
                        "issues": [item.to_dict() for item in report.issues],
                        "upstream_review_gaps": upstream_review_gaps,
                        "broker_writes_enabled": False,
                    }
                )
        elif args.us_pit_command == "worker":
            task_name = args.task_name
            if args.us_pit_worker_command == "status":
                _print(forward_capture_task_status(task_name))
            elif args.us_pit_worker_command == "remove":
                _print(remove_forward_capture_task(task_name))
            else:
                spec = forward_capture_task_spec(
                    python_executable=sys.executable,
                    project_root=config.repository_root,
                    task_name=task_name,
                )
                _print(install_forward_capture_task(spec))
        elif args.us_pit_command == "crosscheck-current-aliases":
            from .us_pit.alias_crosscheck import crosscheck_current_aliases

            result = crosscheck_current_aliases(
                pit.store,
                args.normalization_dir,
                args.tdx_source_batch,
                args.output_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "crosscheck_id": result.manifest["crosscheck_id"],
                    "path": str(result.path),
                    "row_count": result.manifest["row_count"],
                    "verified_count": result.manifest["verified_count"],
                    "current_only": True,
                    "historical_membership_authority": False,
                }
            )
        elif args.us_pit_command == "import-evidence":
            observed_at = datetime.now(timezone.utc)
            published_at = datetime.fromisoformat(
                args.published_at.replace("Z", "+00:00")
            )
            as_of_date = (
                None
                if args.as_of_date is None
                else date.fromisoformat(args.as_of_date)
            )
            start_date = as_of_date or published_at.date()
            batch = pit.sync(
                ReviewedLocalEvidenceAdapter(
                    ReviewedEvidenceSpec(
                        path=Path(args.file),
                        dataset=args.dataset,
                        source_id=args.source_id,
                        source_version=args.source_version,
                        public_url=args.url,
                        role=SourceRole(args.role),
                        license_class=LicenseClass(args.license_class),
                        published_at=published_at,
                        as_of_date=as_of_date,
                        media_type=args.media_type,
                    )
                ),
                SyncRequest(start_date, observed_at.date(), observed_at),
            )
            _print(
                {
                    "batch_id": batch.batch_id,
                    "dependencies": [
                        dependency.to_dict() for dependency in batch.dependencies
                    ],
                }
            )
        elif args.us_pit_command == "import-lifecycle":
            observed_at = datetime.now(timezone.utc)
            published_at = datetime.fromisoformat(
                args.published_at.replace("Z", "+00:00")
            )
            adapter = lifecycle_evidence_adapter(
                path=Path(args.file),
                source_id=args.source_id,
                source_version=args.source_version,
                public_url=args.url,
                published_at=published_at,
                store=pit.store,
                source_batch_ids=args.source_batch,
            )
            batch = pit.sync(
                adapter,
                SyncRequest(
                    start_date=min(
                        published_at.date(),
                        adapter.spec.as_of_date or published_at.date(),
                    ),
                    end_date=observed_at.date(),
                    observed_at=observed_at,
                    universe_id="sp500_ivv_proxy_v1",
                ),
            )
            _print(
                {
                    "status": "CAPTURED",
                    "dataset": "lifecycle_status",
                    "batch_id": batch.batch_id,
                    "dependencies": [item.to_dict() for item in batch.dependencies],
                }
            )
        elif args.us_pit_command == "normalize-official":
            result = pit.normalize_official_evidence(args.source_batch)
            _print(
                {
                    "normalization_id": result.normalization_id,
                    "path": str(result.path),
                    "status": result.status,
                    "release_status": result.manifest["release_status"],
                    "candidate_only": result.manifest["candidate_only"],
                    "direct_build_allowed": result.manifest[
                        "direct_build_allowed"
                    ],
                    "counts": result.manifest["counts"],
                    "artifacts": result.manifest["artifacts"],
                    "review_requirements": result.manifest[
                        "review_requirements"
                    ],
                }
            )
        elif args.us_pit_command == "propose-identity-bridges":
            from .us_pit.identity_bridge import propose_identity_bridges

            result = propose_identity_bridges(
                args.normalization_dir,
                args.output_dir,
                as_of_date=args.as_of_date,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "bridge_id": result.manifest["bridge_id"],
                    "path": str(result.path),
                    "row_count": result.manifest["row_count"],
                    "matched_exact_name": result.manifest["matched_exact_name"],
                    "matched_official_ticker": result.manifest.get(
                        "matched_official_ticker", 0
                    ),
                    "matched_total": result.manifest.get(
                        "matched_total", result.manifest["matched_exact_name"]
                    ),
                    "ambiguous": result.manifest["ambiguous"],
                    "unresolved": result.manifest["unresolved"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "propose-membership-events":
            from .us_pit.spglobal_events import build_spglobal_event_candidates

            result = build_spglobal_event_candidates(
                pit.store,
                args.source_batch,
                args.normalization_dir,
                args.output_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "candidate_set_id": result.manifest["candidate_set_id"],
                    "path": str(result.path),
                    "row_count": result.manifest["row_count"],
                    "matched": result.manifest["matched"],
                    "ambiguous": result.manifest["ambiguous"],
                    "unresolved": result.manifest["unresolved"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "prepare-membership-review":
            from .us_pit.spglobal_events import prepare_spglobal_event_review

            result = prepare_spglobal_event_review(
                args.candidate_dir,
                args.output_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "review_template_id": result.manifest["review_template_id"],
                    "path": str(result.path),
                    "row_count": result.manifest["row_count"],
                    "approved_rows": 0,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "review-membership-events":
            from .us_pit.spglobal_events import review_spglobal_event_evidence

            result = review_spglobal_event_evidence(
                pit.store,
                args.source_batch,
                args.candidate_dir,
                args.normalization_dir,
                args.output_dir,
                reviewer=args.reviewer,
                identity_crosscheck_dir=args.identity_crosscheck_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "review_id": result.manifest["review_id"],
                    "path": str(result.path),
                    "candidate_rows": result.manifest["candidate_rows"],
                    "approved_rows": result.manifest["approved_rows"],
                    "blocked_rows": result.manifest["blocked_rows"],
                    "direct_build_allowed": result.manifest[
                        "direct_build_allowed"
                    ],
                }
            )
        elif args.us_pit_command == "audit-membership":
            from .us_pit.membership_audit import audit_membership_candidates

            result = audit_membership_candidates(
                pit.store,
                args.normalization_dir,
                args.candidate_dir,
                args.source_batch,
                args.output_dir,
                decision_start=args.start,
                decision_end=args.end,
            )
            _print(
                {
                    "status": result.report["status"],
                    "audit_id": result.report["audit_id"],
                    "path": str(result.path),
                    "decision_months": result.report["decision_months"],
                    "replayed_months": result.report["replayed_months"],
                    "reconciled_anchor_intervals": result.report[
                        "reconciled_anchor_intervals"
                    ],
                    "unresolved_identity_events": result.report[
                        "unresolved_identity_events"
                    ],
                    "identity_transition_candidates": result.report[
                        "identity_transition_candidates"
                    ],
                    "residual_membership_event_requests": result.report[
                        "residual_membership_event_requests"
                    ],
                    "residual_membership_event_counts": result.report[
                        "residual_membership_event_counts"
                    ],
                    "membership_event_conflict_root_count": result.report[
                        "membership_event_conflict_root_count"
                    ],
                    "gap_counts": result.report["gap_counts"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "prepare-review":
            from .us_pit.review_template import prepare_review_template

            result = prepare_review_template(
                args.normalization_dir,
                args.output_dir,
                decision_start=date.fromisoformat(args.start),
                decision_end=date.fromisoformat(args.end),
                membership_review_dir=args.membership_review_dir,
                membership_audit_dir=args.membership_audit_dir,
                action_review_dir=args.action_review_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "review_template_id": result.manifest["review_template_id"],
                    "path": str(result.path),
                    "decision_months": result.manifest["decision_months"],
                    "approved": False,
                    "gap_report": str(result.path / "review_gaps.json"),
                }
            )
        elif args.us_pit_command == "evidence-requests":
            from .us_pit.evidence_requests import (
                build_operator_transition_evidence_requests,
                build_transition_evidence_requests,
            )

            if args.operator_transitions:
                import json as _json
                from pathlib import Path as _Path

                transitions = _json.loads(
                    _Path(args.operator_transitions).read_text(encoding="utf-8")
                )
                result = build_operator_transition_evidence_requests(
                    pit.store,
                    args.normalization_dir,
                    transitions,
                    args.output_dir,
                    proposed_by=args.proposed_by,
                )
            else:
                if not args.membership_audit_dir:
                    raise SystemExit(
                        "evidence-requests requires --membership-audit-dir or --operator-transitions"
                    )
                result = build_transition_evidence_requests(
                    args.membership_audit_dir,
                    args.output_dir,
                )
            _print(
                {
                    "status": result.manifest["status"],
                    "request_set_id": result.manifest["request_set_id"],
                    "path": str(result.path),
                    "request_count": result.manifest["request_count"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "propose-sec-cik":
            from .us_pit.sec_identity_candidates import build_sec_cik_candidates

            result = build_sec_cik_candidates(
                pit.store,
                args.source_batch,
                args.evidence_request_dir,
                args.output_dir,
                normalization_dir=args.normalization_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "candidate_set_id": result.manifest["candidate_set_id"],
                    "path": str(result.path),
                    "row_count": result.manifest["row_count"],
                    "match_counts": result.manifest["match_counts"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "propose-sec-filings":
            from .us_pit.sec_filing_candidates import build_sec_filing_candidates

            result = build_sec_filing_candidates(
                pit.store,
                args.source_batch,
                args.cik_candidate_dir,
                args.output_dir,
                before_days=args.before_days,
                after_days=args.after_days,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "candidate_set_id": result.manifest["candidate_set_id"],
                    "path": str(result.path),
                    "unique_cik_count": result.manifest["unique_cik_count"],
                    "filing_candidate_count": result.manifest["filing_candidate_count"],
                    "unresolved_side_count": result.manifest["unresolved_side_count"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "screen-sec-filings":
            from .us_pit.sec_filing_screen import screen_sec_filing_candidates

            source_batch_ids = list(args.source_batch)
            if not source_batch_ids:
                base = SECFilingDocumentsAdapter(args.filing_candidate_dir)
                captured, catalog_batches = captured_filing_accessions(
                    pit.store,
                    candidate_set_id=base.candidate_set_id,
                    candidate_manifest_sha256=base.candidate_manifest_sha256,
                )
                expected = {record["accession_number"] for record in base.records}
                if captured != expected:
                    raise ValueError(
                        "candidate-bound SEC filing catalog is incomplete; sync documents first"
                    )
                source_batch_ids = sorted(catalog_batches)
            result = screen_sec_filing_candidates(
                pit.store,
                source_batch_ids,
                args.filing_candidate_dir,
                args.evidence_request_dir,
                args.output_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "screen_set_id": result.manifest["screen_set_id"],
                    "path": str(result.path),
                    "source_document_count": result.manifest["source_document_count"],
                    "relevance_counts": result.manifest["relevance_counts"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "rank-sec-filings":
            from .us_pit.sec_filing_screen import rank_sec_filing_screen

            result = rank_sec_filing_screen(
                args.screen_dir,
                args.filing_candidate_dir,
                args.evidence_request_dir,
                args.output_dir,
                per_request=args.per_request,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "review_set_id": result.manifest["review_set_id"],
                    "path": str(result.path),
                    "request_count": result.manifest["request_count"],
                    "covered_request_count": result.manifest["covered_request_count"],
                    "row_count": result.manifest["row_count"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "prepare-action-review":
            result = prepare_action_review(
                args.evidence_request_dir,
                args.ranked_review_dir,
                args.output_dir,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "template_id": result.manifest["template_id"],
                    "path": str(result.path),
                    "request_count": result.manifest["request_count"],
                    "candidate_count": result.manifest["candidate_count"],
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "propose-action-review":
            result = propose_action_review(
                pit.store,
                args.template_dir,
                args.completed_csv,
                args.output_dir,
                proposed_by=args.proposed_by,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "proposal_sha256": result.manifest["proposal_sha256"],
                    "path": str(result.path),
                    "review_row_count": result.manifest["review_row_count"],
                    "approved": False,
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "approve-action-review":
            result = approve_action_review(
                pit.store,
                args.proposal_dir,
                args.output_dir,
                expected_sha256=args.expected_sha256,
                approved_by=args.approved_by,
                acknowledgement=args.acknowledgement,
            )
            _print(
                {
                    "status": result.manifest["status"],
                    "approval_id": result.manifest["approval_id"],
                    "path": str(result.path),
                    "source_batch_id": result.source_batch.batch_id,
                    "action_count": result.manifest["action_count"],
                    "direct_build_allowed": False,
                }
            )
        elif args.us_pit_command == "assemble-reviewed":
            result = USPITReviewWorkspaceAssembler(pit.store).assemble(
                args.normalization_dir,
                args.review_dir,
                args.output_dir,
                decision_start=date.fromisoformat(args.start),
                decision_end=date.fromisoformat(args.end),
                source_batch_ids=args.source_batch,
                reviewer=args.reviewer,
                approved_at=(
                    None
                    if args.approved_at is None
                    else datetime.fromisoformat(args.approved_at.replace("Z", "+00:00"))
                ),
            )
            _print(
                {
                    "workspace_id": result.workspace_id,
                    "path": str(result.path),
                    "status": result.status,
                    "direct_build_allowed": result.manifest[
                        "direct_build_allowed"
                    ],
                    "gap_counts": result.manifest["gap_counts"],
                    "gap_report": str(result.path / "gap_report.json"),
                }
            )
        elif args.us_pit_command == "prepare-market":
            if args.universe != "sp500_ivv_proxy_v1":
                raise ValueError(
                    "US PIT market preparation only supports sp500_ivv_proxy_v1"
                )
            tqcenter_path = config.tq_user_dir / "tqcenter.py"
            source_version = args.tdx_source_version or "tdx-local-reviewed-gate"
            preparer = USPITMarketPreparer(
                pit.store,
                None,
                tdx_source_version=source_version,
                commission_rate=args.commission_rate,
                slippage_rate=args.slippage_rate,
            )
            gate_gaps = preparer.inspect_reviewed_workspace(args.input_dir)
            if gate_gaps:
                result = preparer.prepare(
                    args.input_dir,
                    args.output_dir,
                    start_date=date.fromisoformat(args.start),
                    end_date=date.fromisoformat(args.end),
                    universe_id=args.universe,
                )
                _print(
                    {
                        **result.to_dict(),
                        "preflight": {
                            "ready": False,
                            "skipped": True,
                            "reason": "REVIEW_WORKSPACE_BLOCKED_BEFORE_TDX",
                        },
                    }
                )
                return 2
            preflight = check_tq_preflight(portable_root=config.tdx_root).as_dict()
            if not preflight["ready"] or not tqcenter_path.is_file():
                _print(
                    {
                        "status": "DATA_BLOCKED",
                        "reason": (
                            "TDX_PREFLIGHT_FAILED"
                            if not preflight["ready"]
                            else "TQCENTER_NOT_FOUND"
                        ),
                        "preflight": preflight,
                        "tqcenter_path": str(tqcenter_path),
                        "broker_writes_enabled": False,
                    }
                )
                return 2
            from .data import TdxProvider

            tqcenter_sha256 = hashlib.sha256(tqcenter_path.read_bytes()).hexdigest()
            source_version = (
                args.tdx_source_version
                or f"tdx-local-tqcenter-{tqcenter_sha256[:16]}"
            )
            with TdxProvider(config, __file__, cache_reads=False) as provider:
                result = USPITMarketPreparer(
                    pit.store,
                    provider,
                    tdx_source_version=source_version,
                    commission_rate=args.commission_rate,
                    slippage_rate=args.slippage_rate,
                ).prepare(
                    args.input_dir,
                    args.output_dir,
                    start_date=date.fromisoformat(args.start),
                    end_date=date.fromisoformat(args.end),
                    universe_id=args.universe,
                )
            _print({**result.to_dict(), "preflight": preflight})
            return 0 if result.ready else 2
        elif args.us_pit_command == "build":
            release = pit.build_from_directory(
                args.input_dir,
                source_batch_ids=args.source_batch,
                approved_overrides=args.override,
                universe_id=args.universe,
            )
            state = program.register_data_release(release)
            _print({"release": pit.release_detail(release.release_id), "program": state})
        elif args.us_pit_command == "validate":
            release = pit.store.load_release(args.release)
            report = pit.validate_release(args.release)
            state = program.register_data_release(release)
            _print({"quality_report": report.to_dict(), "program": state})
        elif args.us_pit_command == "qualify":
            release = pit.store.load_release(args.release)
            if release.status.value != "DATA_READY":
                _print(
                    {
                        "status": "DATA_BLOCKED",
                        "reason": "PIT_RELEASE_NOT_DATA_READY",
                        "release_id": release.release_id,
                        "release_status": release.status.value,
                        "quality_report": release.quality_report.to_dict(),
                        "broker_writes_enabled": False,
                    }
                )
                return 2
            dataset = release.to_backtest_dataset()
            code_path = config.repository_root / "research_platform" / "strategies" / "us_momentum.py"
            code_sha256 = hashlib.sha256(code_path.read_bytes()).hexdigest()
            qualification = HistoricalQualificationService(
                config.runtime_dir / "us_momentum_sealed.sqlite3"
            ).qualify(
                dataset,
                run_strict_qualification_backtest,
                parameters=USMomentumParameters(),
                strategy_code_sha256=code_sha256,
            )
            evidence_hash = hashlib.sha256(
                json.dumps(
                    asdict(qualification),
                    default=str,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            manifest_sha256 = hashlib.sha256(
                (release.path / "manifest.json").read_bytes()
            ).hexdigest()
            state = program.register_historical(
                qualification,
                evidence_hash,
                release_id=release.release_id,
                manifest_sha256=manifest_sha256,
            )
            _print(
                {
                    "decision": asdict(qualification.decision),
                    "freeze_sha256": qualification.freeze_sha256,
                    "run_sha256": dict(qualification.run_sha256),
                    "program": state,
                }
            )
        elif args.us_pit_command == "list":
            _print(
                {
                    "releases": pit.list_releases(),
                    "source_batches": pit.list_source_batches(),
                    "program": program.status(),
                }
            )
        elif args.us_pit_command == "override":
            if args.us_pit_override_command == "propose":
                value = json.loads(Path(args.file).read_text(encoding="utf-8"))
                state = pit.store.propose_override(OverrideProposal.from_dict(value))
                _print(
                    {
                        "override_id": state.proposal.override_id,
                        "draft_sha256": state.draft_sha256,
                        "approved": state.approved,
                    }
                )
            elif args.us_pit_override_command == "approve":
                state = pit.store.approve_override(
                    args.draft,
                    expected_sha256=args.expected_sha256,
                    approved_at=datetime.now(timezone.utc).isoformat(),
                    approved_by=args.approved_by,
                    acknowledgement=args.acknowledgement,
                )
                _print(
                    {
                        "override_id": state.proposal.override_id,
                        "draft_sha256": state.draft_sha256,
                        "approved": state.approved,
                    }
                )
            else:
                _print(
                    [
                        {
                            "override_id": item.proposal.override_id,
                            "draft_sha256": item.draft_sha256,
                            "approved": item.approved,
                        }
                        for item in pit.store.list_overrides()
                    ]
                )
        return 0
    if args.command == "us-paper":
        paper = USMomentumPaperService(
            USPaperConfig(database_path=config.us_paper_database_path)
        )
        program = USMomentumProgram(config.us_program_database_path)
        if args.us_paper_command == "worker":
            from .us_paper_runtime import (
                install_windows_task,
                remove_windows_task,
                windows_task_scheduler_spec,
                windows_task_status,
            )

            task_name = str(args.task_name)
            if args.us_paper_worker_command == "status":
                _print(windows_task_status(task_name))
                return 0
            if args.us_paper_worker_command == "remove":
                _print(remove_windows_task(task_name))
                return 0
            state = program.status()
            if state["state"] != "PAPER_COLLECTING":
                _print(
                    {
                        "status": "PAPER_BLOCKED",
                        "reason": "WORKER_INSTALL_REQUIRES_PAPER_COLLECTING",
                        "program": state,
                        "broker_writes_enabled": False,
                    }
                )
                return 2
            if not config.us_paper_runtime_database_path.is_file():
                _print(
                    {
                        "status": "PAPER_BLOCKED",
                        "reason": "PAPER_RUNTIME_NOT_INITIALIZED",
                        "broker_writes_enabled": False,
                    }
                )
                return 2
            spec = windows_task_scheduler_spec(
                python_executable=sys.executable,
                project_root=Path(__file__).resolve().parent.parent,
                task_name=task_name,
            )
            _print(install_windows_task(spec))
            return 0
        if args.us_paper_command == "status":
            payload = paper.status()
            preflight = check_tq_preflight(portable_root=config.tdx_root).as_dict()
            payload["deployment_gate"] = preflight
            payload["broker_writes_enabled"] = False
            payload["program"] = program.status()
            payload["qualification"] = program.status()["state"]
            payload["qualification_detail"] = (
                "The isolated executor is admitted only in PAPER_COLLECTING; "
                "historical and 20-session TDX evidence remain mandatory."
                if preflight["ready"]
                else "TDX deployment preflight is not ready."
            )
            _print(payload)
        elif args.us_paper_command == "admit-release":
            state = program.status()
            if state["state"] not in {"PAPER_COLLECTING", "PAPER_QUALIFIED"}:
                _print(
                    {
                        "status": "PAPER_BLOCKED",
                        "reason": "PROGRAM_NOT_PAPER_ACTIVE",
                        "program": state,
                        "broker_writes_enabled": False,
                    }
                )
                return 2
            pit_service = USPITService(config.us_pit_dir)
            candidate = pit_service.store.load_release(str(args.release))
            program_state = program.admit_paper_release(candidate)
            admission = program_state.get("paper_release_admission")
            if not isinstance(admission, Mapping):
                raise ValueError("program did not persist the PIT release admission")
            from .us_paper_runtime import USPaperRuntime, USPaperRuntimeConfig

            runtime_config = USPaperRuntimeConfig(
                state_database_path=config.us_paper_runtime_database_path,
                release_id=str(program_state["release_id"]),
                manifest_sha256=str(program_state["manifest_sha256"]),
                worker_id="paper-release-admission",
            )
            runtime = USPaperRuntime.open_existing(runtime_config, paper)
            runtime_state = runtime.admit_paper_release(admission)
            _print(
                {
                    "status": "PAPER_RELEASE_ADMITTED",
                    "program": program_state,
                    "runtime": runtime_state,
                    "broker_writes_enabled": False,
                }
            )
        elif args.us_paper_command in {"start", "tick"}:
            state = program.status()
            if state["state"] != "PAPER_COLLECTING":
                _print(
                    {
                        "status": "PAPER_BLOCKED",
                        "reason": "PROGRAM_NOT_PAPER_COLLECTING",
                        "program": state,
                        "broker_writes_enabled": False,
                    }
                )
                return 2
            from .us_paper_runtime import (
                DAILY_SOURCE_FREQUENCY,
                DAILY_SOURCE_SCHEMA,
                FrozenXNYSSchedule,
                USPaperRuntime,
                USPaperRuntimeConfig,
                USPaperRuntimeError,
                canonical_daily_source_sha256,
            )

            release_id = str(state["release_id"])
            manifest_sha256 = str(state["manifest_sha256"])
            pit_service = USPITService(config.us_pit_dir)
            release = pit_service.store.load_release(release_id)
            actual_manifest_sha256 = hashlib.sha256(
                (release.path / "manifest.json").read_bytes()
            ).hexdigest()
            if actual_manifest_sha256 != manifest_sha256:
                raise ValueError("active program manifest hash no longer matches the PIT release")
            runtime_config = USPaperRuntimeConfig(
                state_database_path=config.us_paper_runtime_database_path,
                release_id=release_id,
                manifest_sha256=manifest_sha256,
                worker_id="windows-task-scheduler",
            )
            if args.us_paper_command == "start":
                if args.sessions < 252:
                    raise ValueError("paper runtime must freeze at least 252 future XNYS sessions")
                if config.us_paper_runtime_database_path.exists():
                    raise USPaperRuntimeError(
                        "paper runtime is already initialized; recurring work must use us-paper tick"
                    )
                import exchange_calendars as xcals
                import pandas as pd

                today = datetime.now(ZoneInfo("America/New_York")).date()
                xnys = xcals.get_calendar("XNYS")
                first = xnys.date_to_session(pd.Timestamp(today), direction="next")
                window = xnys.sessions_window(first, args.sessions - 1)
                sessions = tuple(
                    pd.Timestamp(value).tz_localize(None).date() for value in window
                )
                runtime = USPaperRuntime(
                    runtime_config,
                    schedule=FrozenXNYSSchedule(sessions),
                    paper=paper,
                )
                _print(runtime.status())
            else:
                runtime = USPaperRuntime.open_existing(runtime_config, paper)
                now_ny = datetime.now(ZoneInfo("America/New_York"))
                decision: dict[str, Any] | None = None
                program_binding = (
                    str(state.get("paper_decision_release_id") or ""),
                    str(state.get("paper_decision_manifest_sha256") or ""),
                )
                runtime_binding = runtime.current_decision_binding()
                if program_binding != (
                    str(runtime_binding["release_id"]),
                    str(runtime_binding["manifest_sha256"]),
                ):
                    _print(
                        {
                            "status": "PAPER_BLOCKED",
                            "reason": "ADMITTED_RELEASE_LEDGER_OUT_OF_SYNC",
                            "program_binding": program_binding,
                            "runtime_binding": runtime_binding,
                            "broker_writes_enabled": False,
                        }
                    )
                    return 2
                non_session = _paper_non_session_result(runtime, now_ny.date())
                if non_session is not None:
                    exit_code, payload = non_session
                    _print(payload)
                    return exit_code
                decision_release = pit_service.store.load_release(
                    str(runtime_binding["release_id"])
                )
                decision_manifest_sha256 = hashlib.sha256(
                    (decision_release.path / "manifest.json").read_bytes()
                ).hexdigest()
                if decision_manifest_sha256 != str(
                    runtime_binding["manifest_sha256"]
                ):
                    raise ValueError(
                        "admitted decision release manifest no longer matches runtime audit"
                    )
                try:
                    decision_dataset = decision_release.to_backtest_dataset()
                    corporate_actions = _paper_corporate_action_records(
                        decision_dataset, now_ny.date()
                    )
                    calendar_dates = {
                        value.date()
                        for value in __import__("pandas").to_datetime(
                            decision_dataset.calendar["session_date"], errors="raise"
                        )
                    }
                    same_month = [
                        item
                        for item in calendar_dates
                        if item.year == now_ny.year and item.month == now_ny.month
                    ]
                    if same_month and now_ny.date() == max(same_month):
                        from .data import TdxProvider
                        from .us_paper_decision import (
                            TDXCurrentUSBarSource,
                            USPaperDecisionAuditStore,
                            USPaperDecisionCoordinator,
                        )

                        with TdxProvider(config, __file__, cache_reads=False) as provider:
                            decision = USPaperDecisionCoordinator(
                                dataset=decision_dataset,
                                paper=paper,
                                bar_source=TDXCurrentUSBarSource(provider),
                                manifest_sha256=decision_manifest_sha256,
                                audit_store=USPaperDecisionAuditStore(
                                    config.us_paper_decision_archive_dir
                                ),
                            ).decide(now_ny.date(), now=now_ny)
                except Exception as exc:
                    _print({
                        "status": "PAPER_BLOCKED",
                        "reason": (
                            "ADMITTED_RELEASE_INPUT_ERROR:"
                            f"{type(exc).__name__}:{exc}"
                        ),
                        "paper_only": True,
                        "broker_writes_enabled": False,
                    })
                    return 2
                daily_bars: list[dict[str, Any]] = []
                if now_ny.time() >= runtime.config.market_close:
                    position_codes = [
                        str(item["code"])
                        for item in paper.status().get("positions", [])
                    ]
                    raw_codes = sorted(set(position_codes) | {"BIL.US"})
                    if raw_codes:
                        from .data import TdxProvider

                        session = now_ny.date().isoformat()
                        with TdxProvider(config, __file__, cache_reads=False) as provider:
                            frames = provider.fetch_bars(
                                raw_codes,
                                "1d",
                                5,
                                fields=("Open", "High", "Low", "Close"),
                                dividend_type="none",
                                start_time=session,
                                end_time=session,
                            )
                            bil_front = provider.fetch_bars(
                                ["BIL.US"],
                                "1d",
                                5,
                                fields=("Close",),
                                dividend_type="front",
                                end_time=session + " 23:59:59",
                            )
                        import pandas as pd

                        for code in raw_codes:
                            frame = frames.get(code)
                            if frame is None or frame.empty:
                                continue
                            normalized = pd.to_datetime(
                                frame.index, errors="raise"
                            ).normalize()
                            selected = frame.loc[normalized == pd.Timestamp(session)]
                            if len(selected) != 1:
                                continue
                            row = selected.iloc[0]
                            source_rows = [
                                {
                                    "session_date": session,
                                    "Open": float(row["Open"]),
                                    "High": float(row["High"]),
                                    "Low": float(row["Low"]),
                                    "Close": float(row["Close"]),
                                }
                            ]
                            daily_bars.append(
                                {
                                    "code": code,
                                    "session_date": session,
                                    "open": source_rows[0]["Open"],
                                    "high": source_rows[0]["High"],
                                    "low": source_rows[0]["Low"],
                                    "close": source_rows[0]["Close"],
                                    "observed_at": now_ny,
                                    "source_schema": DAILY_SOURCE_SCHEMA,
                                    "source": "TDX",
                                    "source_code": code,
                                    "frequency": DAILY_SOURCE_FREQUENCY,
                                    "adjustment": "none",
                                    "source_rows": source_rows,
                                    "source_sha256": canonical_daily_source_sha256(
                                        source="TDX",
                                        source_code=code,
                                        adjustment="none",
                                        source_rows=source_rows,
                                    ),
                                }
                            )
                        bil_frame = bil_front.get("BIL.US")
                        if bil_frame is not None and len(bil_frame) >= 2:
                            bil_frame = bil_frame.sort_index()
                            bil_dates = pd.to_datetime(
                                bil_frame.index, errors="raise"
                            ).normalize()
                            current_rows = bil_frame.loc[
                                bil_dates == pd.Timestamp(session)
                            ]
                            previous_rows = bil_frame.loc[
                                bil_dates < pd.Timestamp(session)
                            ]
                            if len(current_rows) == 1 and not previous_rows.empty:
                                prior_adjusted = float(previous_rows.iloc[-1]["Close"])
                                current_adjusted = float(current_rows.iloc[0]["Close"])
                                if prior_adjusted > 0 and current_adjusted > 0:
                                    previous_session = pd.Timestamp(
                                        previous_rows.index[-1]
                                    ).date().isoformat()
                                    source_rows = [
                                        {
                                            "session_date": previous_session,
                                            "Close": prior_adjusted,
                                        },
                                        {
                                            "session_date": session,
                                            "Close": current_adjusted,
                                        },
                                    ]
                                    daily_bars.append(
                                        {
                                            "code": "BILTR.US",
                                            "session_date": session,
                                            "open": prior_adjusted,
                                            "high": max(prior_adjusted, current_adjusted),
                                            "low": min(prior_adjusted, current_adjusted),
                                            "close": current_adjusted,
                                            "observed_at": now_ny,
                                            "source_schema": DAILY_SOURCE_SCHEMA,
                                            "source": "TDX",
                                            "source_code": "BIL.US",
                                            "frequency": DAILY_SOURCE_FREQUENCY,
                                            "adjustment": "front",
                                            "source_rows": source_rows,
                                            "source_sha256": canonical_daily_source_sha256(
                                                source="TDX",
                                                source_code="BIL.US",
                                                adjustment="front",
                                                source_rows=source_rows,
                                            ),
                                        }
                                    )
                runtime_status = runtime.tick(
                    now=now_ny,
                    daily_bars=daily_bars,
                    corporate_actions=corporate_actions,
                )
                _print({"runtime": runtime_status, "month_end_decision": decision})
        elif args.us_paper_command == "evaluate":
            state = program.status()
            if state["state"] != "PAPER_COLLECTING":
                _print(
                    {
                        "status": "PAPER_BLOCKED",
                        "reason": "PROGRAM_NOT_PAPER_COLLECTING",
                        "program": state,
                        "broker_writes_enabled": False,
                    }
                )
                return 2
            from .us_paper_qualification import USPaperQualificationEvidenceBuilder
            from .us_paper_runtime import USPaperRuntime, USPaperRuntimeConfig

            release_id = str(state["release_id"])
            manifest_sha256 = str(state["manifest_sha256"])
            runtime_config = USPaperRuntimeConfig(
                state_database_path=config.us_paper_runtime_database_path,
                release_id=release_id,
                manifest_sha256=manifest_sha256,
                worker_id="paper-qualification-readonly",
            )
            runtime = USPaperRuntime.open_existing(runtime_config, paper)
            evidence = USPaperQualificationEvidenceBuilder(
                paper_database_path=config.us_paper_database_path,
                runtime_database_path=config.us_paper_runtime_database_path,
                frozen_xnys_sessions=runtime.schedule.sessions,
                us_pit_root=config.us_pit_dir,
                decision_archive_root=config.us_paper_decision_archive_dir,
            ).build()
            program_state = evidence.register(program)
            _print({"evidence": evidence.as_dict(), "program": program_state})
        elif args.us_paper_command in {
            "tdx-shadow-start",
            "tdx-shadow-status",
            "tdx-shadow-tick",
            "tdx-shadow-reconcile",
            "tdx-shadow-evaluate",
        }:
            import exchange_calendars as xcals
            import pandas as pd
            from .us_tdx_shadow import (
                TDXShadowConfig,
                TDXShadowQualificationCollector,
            )

            if args.us_paper_command == "tdx-shadow-start":
                release_id = args.release
                active = program.status()
                if active.get("state") != "BACKTEST_QUALIFIED":
                    _print(
                        {
                            "status": "PAPER_BLOCKED",
                            "reason": "TDX_SHADOW_REQUIRES_BACKTEST_QUALIFIED",
                            "program": active,
                        }
                    )
                    return 2
                if str(active.get("release_id") or "") != str(release_id):
                    raise ValueError(
                        "TDX shadow release must match the active qualified program release"
                    )
            else:
                release_id = program.status().get("release_id")
                if not release_id:
                    _print(
                        {
                            "status": "PAPER_BLOCKED",
                            "reason": "NO_ACTIVE_DATA_READY_RELEASE",
                            "program": program.status(),
                        }
                    )
                    return 2
            release = USPITService(config.us_pit_dir).store.load_release(str(release_id))
            manifest_sha256 = hashlib.sha256(
                (release.path / "manifest.json").read_bytes()
            ).hexdigest()
            shadow_config = TDXShadowConfig(
                config.us_tdx_shadow_database_path,
                release_id=str(release_id),
                manifest_sha256=manifest_sha256,
            )
            if args.us_paper_command == "tdx-shadow-start":
                today = datetime.now(ZoneInfo("America/New_York")).date()
                xnys = xcals.get_calendar("XNYS")
                start_session = xnys.date_to_session(pd.Timestamp(today), direction="next")
                calendar_index = xnys.sessions_window(start_session, 39)
                calendar = tuple(
                    pd.Timestamp(value).tz_localize(None).date() for value in calendar_index
                )
                collector = TDXShadowQualificationCollector(
                    shadow_config,
                    frozen_xnys_sessions=calendar,
                    qualification_sessions=calendar[:20],
                )
            else:
                collector = TDXShadowQualificationCollector.open_existing(shadow_config)
            if args.us_paper_command in {"tdx-shadow-start", "tdx-shadow-status"}:
                _print(collector.status())
            elif args.us_paper_command == "tdx-shadow-tick":
                _print(collector.tick(now=datetime.now(ZoneInfo("America/New_York"))))
            elif args.us_paper_command == "tdx-shadow-reconcile":
                from .data import TdxProvider
                from .us_qualification import TDX_QUALIFICATION_SAMPLE

                codes = [item.symbol for item in TDX_QUALIFICATION_SAMPLE]
                with TdxProvider(config, __file__, cache_reads=False) as provider:
                    frames = provider.fetch_bars(
                        codes,
                        "1d",
                        5,
                        fields=("Open",),
                        dividend_type="none",
                        start_time=args.session,
                        end_time=args.session,
                    )
                rows: list[dict[str, Any]] = []
                raw_hash = hashlib.sha256()
                for item in TDX_QUALIFICATION_SAMPLE:
                    frame = frames.get(item.symbol)
                    if frame is None or frame.empty or "Open" not in frame.columns:
                        raise ValueError(f"TDX raw Open is missing: {item.symbol}/{args.session}")
                    index = __import__("pandas").to_datetime(frame.index, errors="raise").normalize()
                    selected = frame.loc[index == __import__("pandas").Timestamp(args.session)]
                    if len(selected) != 1:
                        raise ValueError(
                            f"TDX raw Open must have exactly one row: {item.symbol}/{args.session}"
                        )
                    opening = float(selected.iloc[0]["Open"])
                    rows.append(
                        {
                            "symbol": item.symbol,
                            "exchange": item.exchange,
                            "session_date": args.session,
                            "open": opening,
                        }
                    )
                    raw_hash.update(
                        f"{item.symbol}|{args.session}|{opening:.12g}\n".encode("ascii")
                    )
                _print(
                    collector.reconcile_raw_opens(
                        args.session,
                        rows,
                        observed_at=datetime.now(ZoneInfo("America/New_York")),
                        source_sha256=raw_hash.hexdigest(),
                    )
                )
            else:
                decision = collector.evaluate()
                status = collector.status()
                evidence_hash = hashlib.sha256(
                    json.dumps(status, default=str, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                state = program.register_tdx(
                    decision,
                    evidence_hash,
                    release_id=str(release_id),
                    manifest_sha256=manifest_sha256,
                )
                if decision.qualified:
                    state = program.start_paper_collection(
                        release_id=str(release_id),
                        manifest_sha256=manifest_sha256,
                    )
                _print({"decision": asdict(decision), "collector": status, "program": state})
        elif args.us_paper_command == "kill":
            _print(paper.kill(reason=args.note, now=datetime.now(ZoneInfo("America/New_York"))))
        elif args.us_paper_command == "resume":
            # A free-form operator note is not evidence that delayed quotes,
            # missing Opens, or unresolved corporate actions are healthy.  A
            # subsequent verified worker tick owns recovery; this command is
            # retained only as an explicit fail-closed compatibility surface.
            _print(
                {
                    "status": "PAPER_BLOCKED",
                    "reason": "MANUAL_RESUME_DISABLED_USE_EVIDENCE_DRIVEN_TICK",
                    "paper": paper.status(),
                    "broker_writes_enabled": False,
                }
            )
            return 2
        return 0
    if args.command == "tq-minute-snapshot":
        _print(
            capture_tq_watchlist_file(
                Path(args.tdx_root),
                Path(args.watchlist),
                Path(args.output_dir),
                checkpoint_dir=Path(args.checkpoint_dir),
                period=args.period,
            )
        )
        return 0
    if args.command == "cash-instrument-status":
        _print(
            run_cash_instrument_readiness(
                validation_dir=Path(args.directory),
                tdx_root=Path(args.tdx_root),
            )
        )
        return 0
    if args.command == "security-master-observe":
        _print(
            capture_current_security_master_observation(
                Path(args.runtime_dir),
                tdx_timeout_seconds=args.tdx_timeout_seconds,
            )
        )
        return 0
    if args.command == "security-master-publish":
        try:
            result = publish_historical_security_master(
                args.current_observation_manifest
            )
        except HistoricalSecurityMasterBlockedError as exc:
            _print(
                {
                    "status": "BLOCKED_DATA",
                    "reason": "SECURITY_MASTER_PUBLICATION_FAILED_CLOSED",
                    "detail": str(exc),
                    "published": False,
                    "training_started": False,
                    "trading_accessed": False,
                }
            )
            return 2
        _print(result)
        return 0
    if args.command in {
        "early-winner-capture-sse-delisted-bars",
        "early-winner-capture-cninfo-announcements",
    }:
        research_root = config.runtime_dir / "research" / "early_winner_v4"
        safety = {
            "status": "BLOCKED_DATA",
            "audit_only": True,
            "no_training": True,
            "no_trading": True,
            "training_started": False,
            "trading_accessed": False,
            "promotion_blocked": True,
            "global_complete": False,
            "ready": False,
        }
        try:
            if args.command == "early-winner-capture-sse-delisted-bars":
                from .sse_delisted_raw_bars import (
                    capture_current_sse_delisted_raw_bars,
                )

                result = capture_current_sse_delisted_raw_bars(
                    security_master_root=(
                        config.runtime_dir / "security_master"
                    ),
                    cas_root=(research_root / "sse_delisted_raw_bars" / "cas"),
                    max_new_captures=args.max_new_captures,
                    timeout_seconds=args.timeout_seconds,
                )
                payload = result.to_dict()
                payload.update(safety)
                payload.update(
                    {
                        "reason": "DELISTED_HISTORY_SOURCE_INCOMPLETE",
                        "complete": False,
                        "deferred_count": len(result.deferred_codes),
                    }
                )
            else:
                from .cninfo_announcement_capture import (
                    CninfoAnnouncementCaptureCoordinator,
                )

                coordinator = CninfoAnnouncementCaptureCoordinator(
                    cas_root=(
                        research_root / "cninfo_delisted_disclosures" / "cas"
                    ),
                    checkpoint_root=(
                        research_root
                        / "cninfo_delisted_disclosures"
                        / "checkpoints_v1"
                    ),
                    master_store_root=(
                        config.runtime_dir / "security_master"
                    ),
                    timeout_seconds=args.timeout_seconds,
                )
                progress = coordinator.capture(
                    max_new_targets=args.max_new_targets
                )
                payload = progress.to_dict()
                payload.update(safety)
                payload.update(
                    {
                        "reason": "DELISTED_HISTORY_SOURCE_INCOMPLETE",
                        "capture_complete": bool(progress.complete),
                        "complete": False,
                    }
                )
        except Exception as exc:
            source = (
                "SSE_DELISTED_RAW_BARS"
                if args.command == "early-winner-capture-sse-delisted-bars"
                else "CNINFO_ANNOUNCEMENTS"
            )
            _print(
                {
                    **safety,
                    "reason": f"{source}_CAPTURE_FAILED_CLOSED",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                    "complete": False,
                }
            )
            return 2
        _print(payload)
        return 0
    if args.command == "early-winner-audit-delisted-history":
        safety = {
            "audit_only": True,
            "no_training": True,
            "no_trading": True,
            "training_started": False,
            "trading_accessed": False,
            "caller_ready_accepted": False,
        }
        try:
            from .delisted_history_audit_runner import (
                run_current_partial_source_example,
                run_delisted_history_audit,
            )

            if args.source_index is None:
                result = run_current_partial_source_example(
                    runtime_dir=config.runtime_dir
                )
            else:
                source_index_digests: dict[str, str] = {}
                for value in args.source_index:
                    if (
                        not isinstance(value, str)
                        or value.count("=") != 1
                    ):
                        raise ValueError(
                            "source index must be exactly DATASET=64hex"
                        )
                    dataset, digest = value.split("=", 1)
                    if (
                        not dataset
                        or not dataset[0].islower()
                        or not all(
                            character.islower()
                            or character.isdigit()
                            or character == "_"
                            for character in dataset
                        )
                        or len(digest) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in digest
                        )
                    ):
                        raise ValueError(
                            "source index must be exactly DATASET=64hex"
                        )
                    if dataset in source_index_digests:
                        raise ValueError(
                            f"duplicate source-index dataset: {dataset}"
                        )
                    source_index_digests[dataset] = digest
                result = run_delisted_history_audit(
                    runtime_dir=config.runtime_dir,
                    source_index_digests=source_index_digests,
                )
            payload = dict(result)
            payload.update(safety)
        except Exception as exc:
            _print(
                {
                    "status": "BLOCKED_DATA",
                    "reason": "DELISTED_HISTORY_AUDIT_FAILED_CLOSED",
                    "ready": False,
                    "promotion_blocked": True,
                    **safety,
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                }
            )
            return 2
        _print(payload)
        return 0
    if args.command in {"v9-repo-shadow", "v9-repo-shadow-status"}:
        if args.command == "v9-repo-shadow" and args.tdx_root:
            config = replace(config, tdx_root=Path(args.tdx_root))
        database = Database(config)
        database.initialize()
        shadow_service = V9RepoForwardShadowService(config, database)
        if args.command == "v9-repo-shadow-status":
            _print(shadow_service.status())
        else:
            _print(
                shadow_service.capture_live(
                    refresh_sectors=args.refresh_sectors,
                    refresh_data=args.refresh_data,
                    tdx_probe_timeout_seconds=args.tdx_probe_timeout,
                    tdx_run_timeout_seconds=args.tdx_run_timeout,
                )
            )
        return 0
    service = PlatformService(config)
    if args.command == "doctor":
        _print(service.doctor())
    elif args.command == "catalog":
        _print(service.strategy_catalog())
    elif args.command == "sync":
        _print(
            service.sync_data(
                daily_bars=args.daily_bars,
                refresh_sectors=args.refresh_sectors,
                refresh_data=args.refresh_data,
            )
        )
    elif args.command == "scan":
        report = service.run_scan(
            [item.strip() for item in args.strategies.split(",") if item.strip()],
            mode=args.mode,
            push_tdx=args.push_tdx,
            refresh_sectors=args.refresh_sectors,
            max_stocks=args.max_stocks,
            sampling_mode=args.sampling_mode,
            sample_seed=args.sample_seed,
            refresh_data=args.refresh_data,
        )
        _print(asdict(report))
        return 0 if report.status in ("SUCCEEDED", "BLOCKED_DATA") else 1
    elif args.command == "validation-gates":
        from .validation_gates import ValidationGateBlockedError, run_validation_gates

        _print(run_validation_gates(config.repository_root))
    elif args.command == "snapshot-coverage":
        from .data_coverage import sector_membership_coverage

        _print(sector_membership_coverage(config.database_path))
    elif args.command == "lhb-seat-store":
        from .lhb_seat_detail_store import LhbSeatDetailStore

        store = LhbSeatDetailStore(args.database)
        if args.lhb_seat_command == "import":
            import json as _json
            from pathlib import Path as _Path

            rows = _json.loads(_Path(args.input).read_text(encoding="utf-8"))
            _print(store.record_rows(rows))
        else:
            _print(store.coverage())
    elif args.command == "backtest":
        from .validation_gates import ValidationGateBlockedError, ensure_backtest_allowed

        try:
            ensure_backtest_allowed(config.repository_root)
        except ValidationGateBlockedError as exc:
            _print({"status": "BLOCKED", "reason": str(exc)})
            raise SystemExit(2) from exc
        backtests = BacktestService(config, service.database)
        _print(
            backtests.run(
                args.strategy,
                start_date=args.start,
                end_date=args.end,
                daily_bars=args.daily_bars,
                max_stocks=args.max_stocks,
                universe=args.universe,
                stock_codes=[code.strip() for code in args.codes.split(",") if code.strip()],
                refresh_sectors=args.refresh_sectors,
                sampling_mode=args.sampling_mode,
                sample_seed=args.sample_seed,
                execution_cost_multiplier=args.execution_cost_multiplier,
                refresh_data=args.refresh_data,
                playbook_ids=[
                    item.strip() for item in args.playbooks.split(",") if item.strip()
                ],
                pit_release_id=args.pit_release_id,
            )
        )
    elif args.command == "daily-research":
        _print(
            service.run_daily_research(
                [item.strip() for item in args.strategies.split(",") if item.strip()],
                refresh_sectors=args.refresh_sectors,
                max_stocks=args.max_stocks,
                sampling_mode=args.sampling_mode,
                sample_seed=args.sample_seed,
                refresh_data=args.refresh_data,
            )
        )
    elif args.command == "generate-brief":
        _print(AIResearchService(config, service.database).generate_brief(args.run_id))
    elif args.command == "refresh-feedback":
        _print(FeedbackService(config, service.database).refresh())
    elif args.command == "refresh-weekly-observations":
        _print(service.weekly_triangle_observations.refresh())
    elif args.command == "weekly-triangle-setup-study":
        _print(
            run_persisted_weekly_triangle_setup_stability(
                args.directory,
                development_windows=tuple(
                    item.strip()
                    for item in args.development_windows.split(",")
                    if item.strip()
                ),
                validation_windows=tuple(
                    item.strip()
                    for item in args.validation_windows.split(",")
                    if item.strip()
                ),
            )
        )
    elif args.command == "pairs-arbitrage-study":
        _print(
            run_persisted_pairs_arbitrage_validation(
                service.database,
                args.directory,
            )
        )
    elif args.command == "chan-study":
        _print(
            run_persisted_chan_validation(
                config,
                service.database,
                args.directory,
            )
        )
    elif args.command == "backtest-replay":
        backtests = BacktestService(config, service.database)
        _print(
            backtests.replay_backtest(
                args.source_backtest_id,
                strategy_id=args.strategy,
                start_date=args.start,
                end_date=args.end,
                execution_cost_multiplier=args.execution_cost_multiplier,
            )
        )
    elif args.command in {"validate-course49", "validate-course49-v3"}:
        _print(
            validate_course49(
                service.database,
                args.baseline_backtest_id,
                stress_backtest_id=args.stress_backtest_id,
                historical_holdout_backtest_id=args.historical_holdout_backtest_id,
                policy_freeze_date=args.policy_freeze_date,
            )
        )
    elif args.command == "diagnose-course49":
        _print(
            diagnose_backtest(
                config,
                service.database,
                args.backtest_id,
                state_strategy_id=args.state_strategy,
                scope=args.scope,
                output_dir=Path(args.output_dir) if args.output_dir else None,
            )
        )
    elif args.command == "serve":
        uvicorn.run(create_app(config), host=args.host, port=args.port, reload=args.reload)
    elif args.command == "cache-status":
        _print(service.data_cache.status())
    elif args.command == "cache-prune":
        _print(service.data_cache.prune())
    return 0


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
