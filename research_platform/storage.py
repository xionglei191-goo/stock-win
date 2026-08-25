from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

from .config import PlatformConfig
from .composition import (
    CompositionMode,
    ConflictPolicy,
    StrategyGroupDefinition,
    StrategyGroupMember,
)
from .models import (
    OrderGroupIntent,
    PlatformSignal,
    RunStatus,
    RuntimeAdapter,
    SignalStatus,
    StrategyCategory,
    StrategyMetadata,
)


SCHEMA_VERSION = 13


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    frequency TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    asset_classes TEXT NOT NULL DEFAULT '["A_STOCK"]',
    execution_model TEXT NOT NULL DEFAULT 'SINGLE_LEG',
    supports_short INTEGER NOT NULL DEFAULT 0,
    data_requirements_json TEXT NOT NULL DEFAULT '[]',
    strategy_family TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT 'BUILT_IN',
    scan_enabled INTEGER NOT NULL DEFAULT 1,
    backtest_enabled INTEGER NOT NULL DEFAULT 1,
    runtime_adapter TEXT NOT NULL DEFAULT 'generic_daily',
    plugin_api_version TEXT NOT NULL DEFAULT '1',
    plugin_origin TEXT NOT NULL DEFAULT 'builtin',
    framework_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'independent',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_groups (
    group_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    composition_mode TEXT NOT NULL,
    conflict_policy TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    built_in INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_group_members (
    group_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    weight REAL NOT NULL,
    role TEXT NOT NULL DEFAULT 'alpha',
    priority INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY(group_id, strategy_id),
    FOREIGN KEY(group_id) REFERENCES strategy_groups(group_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategies_json TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    snapshot_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    strength REAL NOT NULL,
    target_weight REAL NOT NULL,
    horizon TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    stop_price REAL,
    status TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    evidence TEXT NOT NULL,
    framework_id TEXT NOT NULL DEFAULT '',
    playbook_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, strategy_id);
CREATE TABLE IF NOT EXISTS signal_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    reason_tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    max_acceptable_loss REAL,
    ai_review_id TEXT,
    ai_alignment TEXT NOT NULL DEFAULT 'NOT_AVAILABLE',
    decided_at TEXT NOT NULL,
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
CREATE TABLE IF NOT EXISTS order_group_intents (
    intent_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    group_key TEXT NOT NULL,
    action TEXT NOT NULL,
    strength REAL NOT NULL,
    gross_target_weight REAL NOT NULL,
    status TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    filled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_group_intents_status
ON order_group_intents(status, strategy_id, generated_at DESC);
CREATE TABLE IF NOT EXISTS order_group_legs (
    leg_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    ratio REAL NOT NULL,
    target_weight REAL NOT NULL,
    FOREIGN KEY(intent_id) REFERENCES order_group_intents(intent_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS order_group_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    FOREIGN KEY(intent_id) REFERENCES order_group_intents(intent_id)
);
CREATE TABLE IF NOT EXISTS paper_accounts (
    strategy_id TEXT PRIMARY KEY,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    updated_at TEXT NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_positions (
    strategy_id TEXT NOT NULL,
    code TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    stop_price REAL NOT NULL,
    last_price REAL NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(strategy_id, code)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    target_weight REAL NOT NULL,
    reason TEXT NOT NULL,
    block_reason TEXT NOT NULL DEFAULT '',
    filled_at TEXT,
    fill_price REAL,
    quantity INTEGER
);
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    pnl REAL
);
CREATE TABLE IF NOT EXISTS paper_group_positions (
    strategy_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    last_price REAL NOT NULL,
    ratio REAL NOT NULL,
    target_weight REAL NOT NULL,
    entry_fees REAL NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(strategy_id, group_key, code)
);
CREATE TABLE IF NOT EXISTS paper_group_fills (
    fill_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    leg_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    pnl REAL
);
CREATE TABLE IF NOT EXISTS paper_equity (
    strategy_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions INTEGER NOT NULL,
    PRIMARY KEY(strategy_id, timestamp)
);
CREATE TABLE IF NOT EXISTS backtests (
    backtest_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    start_date TEXT,
    end_date TEXT,
    snapshot_id TEXT,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS backtest_equity (
    backtest_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    backtest_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    pnl REAL,
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    group_key TEXT NOT NULL DEFAULT '',
    leg_id TEXT NOT NULL DEFAULT '',
    framework_id TEXT NOT NULL DEFAULT '',
    playbook_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS backtest_states (
    backtest_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    market_phase TEXT NOT NULL,
    market_style TEXT NOT NULL,
    suitability REAL NOT NULL,
    trade_mode TEXT NOT NULL,
    entry_allowed INTEGER NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(backtest_id, strategy_id, timestamp)
);
CREATE TABLE IF NOT EXISTS strategy_runtime_states (
    strategy_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    asof TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(strategy_id, scope)
);
CREATE TABLE IF NOT EXISTS strategy_frameworks (
    framework_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_playbooks (
    playbook_id TEXT PRIMARY KEY,
    framework_id TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    base_weight REAL NOT NULL DEFAULT 0,
    market_phase TEXT NOT NULL DEFAULT '',
    data_requirements_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(framework_id) REFERENCES strategy_frameworks(framework_id)
);
CREATE TABLE IF NOT EXISTS backtest_playbook_states (
    backtest_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    playbook_id TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    admitted INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    routed_count INTEGER NOT NULL DEFAULT 0,
    budget REAL NOT NULL DEFAULT 0,
    blocked_reasons TEXT NOT NULL DEFAULT '[]',
    funnel_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(backtest_id, strategy_id, timestamp, playbook_id)
);
CREATE TABLE IF NOT EXISTS snapshot_dependencies (
    snapshot_id TEXT NOT NULL,
    dependency_type TEXT NOT NULL,
    dependency_id TEXT NOT NULL,
    coverage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, dependency_type, dependency_id)
);
CREATE TABLE IF NOT EXISTS data_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    query_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_cache_entries (
    cache_key TEXT PRIMARY KEY,
    entry_type TEXT NOT NULL,
    snapshot_id TEXT NOT NULL DEFAULT '',
    data_asof TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    query_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_data_cache_lookup
ON data_cache_entries(entry_type, status, data_asof, last_accessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_cache_snapshot ON data_cache_entries(snapshot_id);
CREATE TABLE IF NOT EXISTS research_briefs (
    brief_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    generated_at TEXT,
    model TEXT NOT NULL DEFAULT '',
    response_id TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    content_json TEXT NOT NULL DEFAULT '{}',
    usage_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    snapshot_id TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_research_briefs_run ON research_briefs(run_id, created_at DESC);
CREATE TABLE IF NOT EXISTS ai_signal_reviews (
    review_id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL NOT NULL,
    summary TEXT NOT NULL,
    supporting_json TEXT NOT NULL DEFAULT '[]',
    opposing_json TEXT NOT NULL DEFAULT '[]',
    missing_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(brief_id) REFERENCES research_briefs(brief_id) ON DELETE CASCADE,
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_signal_reviews_signal ON ai_signal_reviews(signal_id, brief_id);
CREATE TABLE IF NOT EXISTS decision_outcomes (
    outcome_id TEXT PRIMARY KEY,
    decision_id INTEGER NOT NULL UNIQUE,
    signal_id TEXT NOT NULL,
    basis TEXT NOT NULL,
    status TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    snapshot_id TEXT,
    executable INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT NOT NULL DEFAULT '',
    entry_time TEXT,
    entry_price REAL,
    return_1d REAL,
    return_3d REAL,
    return_5d REAL,
    mae REAL,
    mfe REAL,
    realized_pnl REAL,
    details_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(decision_id) REFERENCES signal_decisions(decision_id),
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_signal ON decision_outcomes(signal_id, evaluated_at DESC);
CREATE TABLE IF NOT EXISTS strategy_observations (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    signal_asof TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    score REAL NOT NULL,
    entry_allowed INTEGER NOT NULL DEFAULT 0,
    target_weight REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    executable INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT NOT NULL DEFAULT '',
    entry_time TEXT,
    entry_price REAL,
    return_5d REAL,
    return_20d REAL,
    mae_20d REAL,
    mfe_20d REAL,
    candidate_json TEXT NOT NULL DEFAULT '{}',
    evaluation_json TEXT NOT NULL DEFAULT '{}',
    hypothesis_id TEXT NOT NULL DEFAULT '',
    hypothesis_rank INTEGER,
    hypothesis_selected INTEGER NOT NULL DEFAULT 0,
    conversion_status TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
    converted_at TEXT,
    conversion_days INTEGER,
    conversion_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(strategy_id, strategy_version, code, stage, signal_asof),
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_strategy_observations_status
ON strategy_observations(strategy_id, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_observations_cohort
ON strategy_observations(strategy_id, stage, signal_asof DESC);
CREATE TABLE IF NOT EXISTS v9_repo_shadow_protocols (
    protocol_hash TEXT PRIMARY KEY,
    observer_version TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v9_repo_shadow_events (
    event_id TEXT PRIMARY KEY,
    protocol_hash TEXT NOT NULL,
    session_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    data_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(protocol_hash, session_date, event_type),
    FOREIGN KEY(protocol_hash) REFERENCES v9_repo_shadow_protocols(protocol_hash)
);
CREATE INDEX IF NOT EXISTS idx_v9_repo_shadow_events_session
ON v9_repo_shadow_events(protocol_hash, session_date, event_type);
CREATE TABLE IF NOT EXISTS strategy_experiments (
    experiment_id TEXT PRIMARY KEY,
    base_strategy_id TEXT NOT NULL,
    baseline_backtest_id TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    model TEXT NOT NULL DEFAULT '',
    response_id TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    validation_json TEXT NOT NULL DEFAULT '{}',
    baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
    candidate_metrics_json TEXT NOT NULL DEFAULT '{}',
    stress_metrics_json TEXT NOT NULL DEFAULT '{}',
    candidate_backtest_id TEXT,
    stress_backtest_id TEXT,
    promoted_strategy_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(baseline_backtest_id) REFERENCES backtests(backtest_id)
);
CREATE INDEX IF NOT EXISTS idx_strategy_experiments_created ON strategy_experiments(created_at DESC);
CREATE TABLE IF NOT EXISTS experiment_artifacts (
    artifact_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(experiment_id) REFERENCES strategy_experiments(experiment_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS research_projects (
    project_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'research_project',
    lifecycle TEXT NOT NULL DEFAULT 'RESEARCH_ONLY',
    status TEXT NOT NULL DEFAULT 'DATA_BUILDING',
    data_asof TEXT,
    data_gates_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_data_batches (
    batch_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    published_start TEXT,
    published_end TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    schema_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(project_id) REFERENCES research_projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_research_data_batches_project
ON research_data_batches(project_id, fetched_at DESC);
CREATE TABLE IF NOT EXISTS research_candidates (
    candidate_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    method TEXT NOT NULL,
    asof TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    probability REAL,
    factor_json TEXT NOT NULL DEFAULT '{}',
    gate_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    snapshot_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(project_id, method, asof, code),
    FOREIGN KEY(project_id) REFERENCES research_projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_research_candidates_lookup
ON research_candidates(project_id, method, asof DESC, rank);
CREATE TABLE IF NOT EXISTS research_models (
    model_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    artifact_path TEXT NOT NULL DEFAULT '',
    artifact_hash TEXT NOT NULL DEFAULT '',
    feature_schema_hash TEXT NOT NULL DEFAULT '',
    training_start TEXT,
    training_end TEXT,
    validation_start TEXT,
    validation_end TEXT,
    test_start TEXT,
    test_end TEXT,
    random_seed INTEGER NOT NULL DEFAULT 49,
    library_version TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(project_id) REFERENCES research_projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_research_models_project
ON research_models(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS research_validations (
    validation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    snapshot_id TEXT NOT NULL DEFAULT '',
    rule_metrics_json TEXT NOT NULL DEFAULT '{}',
    ml_metrics_json TEXT NOT NULL DEFAULT '{}',
    baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
    stress_metrics_json TEXT NOT NULL DEFAULT '{}',
    gates_json TEXT NOT NULL DEFAULT '{}',
    champion_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(project_id) REFERENCES research_projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_research_validations_project
ON research_validations(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS early_winner_history_builds (
    build_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    start_year INTEGER NOT NULL,
    end_year INTEGER NOT NULL,
    status TEXT NOT NULL,
    expected_shards INTEGER NOT NULL DEFAULT 0,
    completed_shards INTEGER NOT NULL DEFAULT 0,
    last_completed_year INTEGER,
    calendar_hash TEXT NOT NULL DEFAULT '',
    manifest_path TEXT NOT NULL DEFAULT '',
    manifest_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(project_id) REFERENCES research_projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_early_winner_history_status
ON early_winner_history_builds(project_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS early_winner_history_shards (
    build_id TEXT NOT NULL,
    shard_year INTEGER NOT NULL,
    status TEXT NOT NULL,
    batch_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(build_id, shard_year),
    FOREIGN KEY(build_id) REFERENCES early_winner_history_builds(build_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS trading_deployments (
    deployment_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    state TEXT NOT NULL,
    champion_json TEXT NOT NULL DEFAULT '{}',
    validation_id TEXT NOT NULL DEFAULT '',
    snapshot_id TEXT NOT NULL DEFAULT '',
    account_alias TEXT NOT NULL DEFAULT '',
    max_capital_cny REAL,
    max_account_fraction REAL,
    shadow_started_at TEXT,
    pilot_started_at TEXT,
    live_started_at TEXT,
    high_water_equity REAL,
    last_equity REAL,
    last_equity_date TEXT,
    last_halt_at TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES research_projects(project_id)
);
CREATE TABLE IF NOT EXISTS trading_order_batches (
    batch_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    rebalance_date TEXT NOT NULL,
    execution_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    confirmation_code TEXT NOT NULL,
    champion_hash TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    approval_deadline TEXT NOT NULL,
    approved_at TEXT,
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    FOREIGN KEY(deployment_id) REFERENCES trading_deployments(deployment_id)
);
CREATE INDEX IF NOT EXISTS idx_trading_batches_status
ON trading_order_batches(deployment_id, status, execution_date DESC);
CREATE TABLE IF NOT EXISTS trading_order_intents (
    intent_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    reason TEXT NOT NULL,
    target_weight REAL NOT NULL,
    requested_quantity INTEGER NOT NULL DEFAULT 0,
    limit_price REAL,
    adv20 REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    automatic_risk_exit INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(batch_id) REFERENCES trading_order_batches(batch_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_trading_intents_batch
ON trading_order_intents(batch_id, status, side, code);
CREATE TABLE IF NOT EXISTS trading_broker_orders (
    broker_order_row_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    broker_order_id TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    order_quantity INTEGER NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    limit_price REAL NOT NULL,
    average_fill_price REAL,
    response_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(intent_id) REFERENCES trading_order_intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_trading_broker_orders_intent
ON trading_broker_orders(intent_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS trading_broker_fills (
    fill_id TEXT PRIMARY KEY,
    broker_order_row_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    arrival_price REAL NOT NULL,
    slippage REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    FOREIGN KEY(broker_order_row_id) REFERENCES trading_broker_orders(broker_order_row_id),
    FOREIGN KEY(intent_id) REFERENCES trading_order_intents(intent_id)
);
CREATE TABLE IF NOT EXISTS trading_position_snapshots (
    snapshot_row_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL,
    asset_json TEXT NOT NULL DEFAULT '{}',
    positions_json TEXT NOT NULL DEFAULT '[]',
    orders_json TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    FOREIGN KEY(deployment_id) REFERENCES trading_deployments(deployment_id)
);
CREATE TABLE IF NOT EXISTS trading_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot_row_id TEXT NOT NULL DEFAULT '',
    differences_json TEXT NOT NULL DEFAULT '[]',
    resolved_at TEXT,
    resolution_note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(deployment_id) REFERENCES trading_deployments(deployment_id)
);
CREATE INDEX IF NOT EXISTS idx_trading_reconciliations_status
ON trading_reconciliations(deployment_id, status, captured_at DESC);
CREATE TABLE IF NOT EXISTS trading_risk_events (
    risk_event_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    resolved_at TEXT,
    resolution_note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(deployment_id) REFERENCES trading_deployments(deployment_id)
);
CREATE TABLE IF NOT EXISTS trading_scheduler_heartbeats (
    scheduler_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(deployment_id) REFERENCES trading_deployments(deployment_id)
);
"""


class Database:
    _migration_lock = threading.Lock()

    def __init__(self, config: PlatformConfig):
        self.config = config

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.config.ensure_runtime_dirs()
        connection = sqlite3.connect(self.config.database_path, timeout=30, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            for attempt in range(20):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 19:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._migration_lock, self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            # Serialize schema inspection and ALTER TABLE across CLI/server processes.
            connection.execute("BEGIN IMMEDIATE")
            backtest_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(backtests)").fetchall()
            }
            if "parameters_json" not in backtest_columns:
                connection.execute(
                    "ALTER TABLE backtests ADD COLUMN parameters_json TEXT NOT NULL DEFAULT '{}'"
                )
            trade_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(backtest_trades)").fetchall()
            }
            if "evidence" not in trade_columns:
                connection.execute(
                    "ALTER TABLE backtest_trades ADD COLUMN evidence TEXT NOT NULL DEFAULT '{}'"
                )
            if "group_key" not in trade_columns:
                connection.execute(
                    "ALTER TABLE backtest_trades ADD COLUMN group_key TEXT NOT NULL DEFAULT ''"
                )
            if "leg_id" not in trade_columns:
                connection.execute(
                    "ALTER TABLE backtest_trades ADD COLUMN leg_id TEXT NOT NULL DEFAULT ''"
                )
            for column in ("framework_id", "playbook_id", "policy_version"):
                if column not in trade_columns:
                    connection.execute(
                        f"ALTER TABLE backtest_trades ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            signal_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(signals)").fetchall()
            }
            for column in ("framework_id", "playbook_id", "policy_version"):
                if column not in signal_columns:
                    connection.execute(
                        f"ALTER TABLE signals ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            order_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(paper_orders)")
            }
            if "block_reason" not in order_columns:
                connection.execute(
                    "ALTER TABLE paper_orders ADD COLUMN block_reason TEXT NOT NULL DEFAULT ''"
                )
            account_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(paper_accounts)").fetchall()
            }
            if "frozen" not in account_columns:
                connection.execute(
                    "ALTER TABLE paper_accounts ADD COLUMN frozen INTEGER NOT NULL DEFAULT 0"
                )
            strategy_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(strategies)").fetchall()
            }
            strategy_migrations = {
                "asset_classes": "TEXT NOT NULL DEFAULT '[\"A_STOCK\"]'",
                "execution_model": "TEXT NOT NULL DEFAULT 'SINGLE_LEG'",
                "supports_short": "INTEGER NOT NULL DEFAULT 0",
                "data_requirements_json": "TEXT NOT NULL DEFAULT '[]'",
                "strategy_family": "TEXT NOT NULL DEFAULT ''",
                "lifecycle": "TEXT NOT NULL DEFAULT 'BUILT_IN'",
                "scan_enabled": "INTEGER NOT NULL DEFAULT 1",
                "backtest_enabled": "INTEGER NOT NULL DEFAULT 1",
                "runtime_adapter": "TEXT NOT NULL DEFAULT 'generic_daily'",
                "plugin_api_version": "TEXT NOT NULL DEFAULT '1'",
                "plugin_origin": "TEXT NOT NULL DEFAULT 'builtin'",
                "framework_id": "TEXT NOT NULL DEFAULT ''",
                "policy_version": "TEXT NOT NULL DEFAULT ''",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "category": "TEXT NOT NULL DEFAULT 'independent'",
            }
            for column, definition in strategy_migrations.items():
                if column not in strategy_columns:
                    connection.execute(f"ALTER TABLE strategies ADD COLUMN {column} {definition}")
            connection.execute(
                """UPDATE strategies SET category='research_project'
                WHERE lifecycle='RESEARCH_ONLY'"""
            )
            decision_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(signal_decisions)")
            }
            decision_migrations = {
                "reason_tags": "TEXT NOT NULL DEFAULT '[]'",
                "confidence": "REAL",
                "max_acceptable_loss": "REAL",
                "ai_review_id": "TEXT",
                "ai_alignment": "TEXT NOT NULL DEFAULT 'NOT_AVAILABLE'",
            }
            for column, definition in decision_migrations.items():
                if column not in decision_columns:
                    connection.execute(
                        f"ALTER TABLE signal_decisions ADD COLUMN {column} {definition}"
                    )
            observation_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(strategy_observations)").fetchall()
            }
            observation_migrations = {
                "hypothesis_id": "TEXT NOT NULL DEFAULT ''",
                "hypothesis_rank": "INTEGER",
                "hypothesis_selected": "INTEGER NOT NULL DEFAULT 0",
                "conversion_status": "TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'",
                "converted_at": "TEXT",
                "conversion_days": "INTEGER",
                "conversion_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, definition in observation_migrations.items():
                if column not in observation_columns:
                    connection.execute(
                        f"ALTER TABLE strategy_observations ADD COLUMN {column} {definition}"
                    )
            group_position_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(paper_group_positions)").fetchall()
            }
            if "entry_fees" not in group_position_columns:
                connection.execute(
                    "ALTER TABLE paper_group_positions ADD COLUMN entry_fees REAL NOT NULL DEFAULT 0"
                )
            validation_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(research_validations)").fetchall()
            }
            if "champion_json" not in validation_columns:
                connection.execute(
                    "ALTER TABLE research_validations ADD COLUMN champion_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now().astimezone().isoformat()),
            )
            now = datetime.now().astimezone().isoformat()
            initial = self.config.portfolio.initial_cash * self.config.portfolio.strategy_budget_weight
            for strategy_id in (
                "chan_v1",
                "course49_v1",
                "course49_v2",
                "course49_v3",
                "course49_system",
                "pairs_arbitrage_v1",
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO paper_accounts(strategy_id, initial_cash, cash, updated_at) VALUES (?, ?, ?, ?)",
                    (strategy_id, initial, initial, now),
                )
            connection.execute(
                """UPDATE paper_accounts SET frozen=1
                WHERE strategy_id GLOB 'course49_v[0-9]*'"""
            )
            connection.execute(
                """UPDATE paper_accounts SET frozen=1
                WHERE strategy_id IN (
                    SELECT strategy_id FROM strategies WHERE category='research_project'
                )"""
            )

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.connect() as connection:
            connection.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def register_strategy(
        self,
        metadata: StrategyMetadata,
        plugin_origin: str = "builtin",
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO strategies
                   (strategy_id, version, name, description, frequency, requires_approval, enabled,
                    asset_classes, execution_model, supports_short, data_requirements_json,
                   strategy_family, lifecycle, scan_enabled, backtest_enabled, runtime_adapter,
                    plugin_api_version, plugin_origin, framework_id, policy_version, archived,
                    category, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(strategy_id) DO UPDATE SET version=excluded.version, name=excluded.name,
                   description=excluded.description, frequency=excluded.frequency,
                   requires_approval=excluded.requires_approval, enabled=excluded.enabled,
                   asset_classes=excluded.asset_classes, execution_model=excluded.execution_model,
                   supports_short=excluded.supports_short,
                   data_requirements_json=excluded.data_requirements_json,
                   strategy_family=excluded.strategy_family, lifecycle=excluded.lifecycle,
                   scan_enabled=excluded.scan_enabled, backtest_enabled=excluded.backtest_enabled,
                   runtime_adapter=excluded.runtime_adapter,
                   plugin_api_version=excluded.plugin_api_version,
                   plugin_origin=excluded.plugin_origin,
                   framework_id=excluded.framework_id,
                   policy_version=excluded.policy_version,
                   archived=excluded.archived,
                   category=excluded.category,
                   updated_at=excluded.updated_at""",
                (
                    metadata.strategy_id,
                    metadata.version,
                    metadata.name,
                    metadata.description,
                    metadata.frequency,
                    int(metadata.requires_approval),
                    int(metadata.enabled),
                    json.dumps(metadata.asset_classes, ensure_ascii=False),
                    metadata.execution_model.value,
                    int(metadata.supports_short),
                    json.dumps(
                        [requirement.__dict__ for requirement in metadata.data_requirements],
                        ensure_ascii=False,
                    ),
                    metadata.strategy_family or metadata.strategy_id,
                    metadata.lifecycle,
                    int(metadata.scan_enabled),
                    int(metadata.backtest_enabled),
                    RuntimeAdapter(metadata.runtime_adapter).value,
                    metadata.plugin_api_version,
                    plugin_origin,
                    metadata.framework_id,
                    metadata.policy_version,
                    int(metadata.archived),
                    StrategyCategory(metadata.category).value,
                    now,
                ),
            )
            category = StrategyCategory(metadata.category)
            if category != StrategyCategory.RESEARCH_PROJECT:
                initial = (
                    self.config.portfolio.initial_cash
                    * self.config.portfolio.strategy_budget_weight
                )
                connection.execute(
                    "INSERT OR IGNORE INTO paper_accounts(strategy_id, initial_cash, cash, updated_at) VALUES (?, ?, ?, ?)",
                    (metadata.strategy_id, initial, initial, now),
                )
            if metadata.archived or category == StrategyCategory.RESEARCH_PROJECT:
                connection.execute(
                    "UPDATE paper_accounts SET frozen=1 WHERE strategy_id=?",
                    (metadata.strategy_id,),
                )

    def upsert_research_project(
        self,
        *,
        project_id: str,
        version: str,
        name: str,
        description: str,
        status: str = "DATA_BUILDING",
        data_asof: str | None = None,
        data_gates: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM research_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing is not None else now
            connection.execute(
                """INSERT INTO research_projects
                (project_id, version, name, description, category, lifecycle, status,
                 data_asof, data_gates_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'research_project', 'RESEARCH_ONLY', ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    version=excluded.version,
                    name=excluded.name,
                    description=excluded.description,
                    status=excluded.status,
                    data_asof=excluded.data_asof,
                    data_gates_json=excluded.data_gates_json,
                    updated_at=excluded.updated_at""",
                (
                    project_id,
                    version,
                    name,
                    description,
                    status,
                    data_asof,
                    json.dumps(data_gates or {}, ensure_ascii=False),
                    created_at,
                    now,
                ),
            )

    def update_research_project(
        self,
        project_id: str,
        *,
        status: str,
        data_asof: str | None = None,
        data_gates: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        fields = ["status=?", "updated_at=?"]
        values: list[Any] = [status, now]
        if data_asof is not None:
            fields.append("data_asof=?")
            values.append(data_asof)
        if data_gates is not None:
            fields.append("data_gates_json=?")
            values.append(json.dumps(data_gates, ensure_ascii=False))
        values.append(project_id)
        self.execute(
            f"UPDATE research_projects SET {', '.join(fields)} WHERE project_id=?",
            values,
        )

    def save_research_data_batch(self, record: dict[str, Any]) -> None:
        columns = (
            "batch_id",
            "project_id",
            "dataset",
            "source",
            "status",
            "fetched_at",
            "published_start",
            "published_end",
            "row_count",
            "path",
            "content_hash",
            "schema_hash",
            "metadata_json",
            "error",
        )
        values = dict(record)
        values["metadata_json"] = json.dumps(values.pop("metadata", {}), ensure_ascii=False)
        with self.connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO research_data_batches ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                [values.get(column) for column in columns],
            )

    def replace_research_candidates(
        self,
        project_id: str,
        method: str,
        asof: str,
        candidates: Iterable[dict[str, Any]],
    ) -> None:
        rows = list(candidates)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM research_candidates WHERE project_id=? AND method=? AND asof=?",
                (project_id, method, asof),
            )
            if not rows:
                return
            connection.executemany(
                """INSERT INTO research_candidates
                (candidate_id, project_id, run_id, strategy_id, method, asof, code,
                 name, industry, rank, score, probability, factor_json, gate_json,
                 evidence_refs_json, snapshot_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["candidate_id"],
                        project_id,
                        row["run_id"],
                        row["strategy_id"],
                        method,
                        asof,
                        row["code"],
                        row.get("name", ""),
                        row.get("industry", ""),
                        int(row["rank"]),
                        float(row["score"]),
                        row.get("probability"),
                        json.dumps(row.get("factors", {}), ensure_ascii=False),
                        json.dumps(row.get("gates", {}), ensure_ascii=False),
                        json.dumps(row.get("evidence_refs", []), ensure_ascii=False),
                        row.get("snapshot_id", ""),
                        row["created_at"],
                    )
                    for row in rows
                ],
            )

    def save_research_model(self, record: dict[str, Any]) -> None:
        values = dict(record)
        values["metrics_json"] = json.dumps(values.pop("metrics", {}), ensure_ascii=False)
        values["metadata_json"] = json.dumps(values.pop("metadata", {}), ensure_ascii=False)
        columns = tuple(values)
        with self.connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO research_models ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )

    def save_research_validation(self, record: dict[str, Any]) -> None:
        values = dict(record)
        for key in (
            "rule_metrics",
            "ml_metrics",
            "baseline_metrics",
            "stress_metrics",
            "gates",
            "champion",
        ):
            values[f"{key}_json"] = json.dumps(values.pop(key, {}), ensure_ascii=False)
        columns = tuple(values)
        with self.connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO research_validations ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )

    def upsert_strategy_group(self, group: StrategyGroupDefinition) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at, built_in FROM strategy_groups WHERE group_id=?",
                (group.group_id,),
            ).fetchone()
            if existing is not None and bool(existing["built_in"]) and not group.built_in:
                raise ValueError("Built-in strategy groups cannot be overwritten")
            created_at = str(existing["created_at"]) if existing is not None else now
            connection.execute(
                """INSERT OR REPLACE INTO strategy_groups
                (group_id, version, name, description, composition_mode, conflict_policy,
                 enabled, built_in, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    group.group_id,
                    group.version,
                    group.name,
                    group.description,
                    group.composition_mode.value,
                    group.conflict_policy.value,
                    int(group.enabled),
                    int(group.built_in),
                    created_at,
                    now,
                ),
            )
            connection.execute("DELETE FROM strategy_group_members WHERE group_id=?", (group.group_id,))
            connection.executemany(
                """INSERT INTO strategy_group_members
                (group_id, strategy_id, weight, role, priority) VALUES (?, ?, ?, ?, ?)""",
                [
                    (group.group_id, member.strategy_id, member.weight, member.role, member.priority)
                    for member in group.members
                ],
            )
            if group.composition_mode not in {
                CompositionMode.CAPITAL_SLEEVES,
                CompositionMode.COMPARISON,
            }:
                connection.execute(
                    """INSERT OR IGNORE INTO paper_accounts
                    (strategy_id, initial_cash, cash, updated_at) VALUES (?, ?, ?, ?)""",
                    (
                        group.group_id,
                        self.config.portfolio.initial_cash,
                        self.config.portfolio.initial_cash,
                        now,
                    ),
                )

    def delete_strategy_group(self, group_id: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT built_in FROM strategy_groups WHERE group_id=?", (group_id,)
            ).fetchone()
            if row is None:
                raise KeyError(group_id)
            if bool(row["built_in"]):
                raise ValueError("Built-in strategy groups cannot be deleted")
            connection.execute("DELETE FROM strategy_groups WHERE group_id=?", (group_id,))

    def load_strategy_groups(self) -> list[StrategyGroupDefinition]:
        groups = self.query("SELECT * FROM strategy_groups ORDER BY group_id")
        members = self.query(
            "SELECT * FROM strategy_group_members ORDER BY group_id, priority, strategy_id"
        )
        by_group: dict[str, list[StrategyGroupMember]] = {}
        for row in members:
            by_group.setdefault(str(row["group_id"]), []).append(
                StrategyGroupMember(
                    strategy_id=str(row["strategy_id"]),
                    weight=float(row["weight"]),
                    role=str(row["role"]),
                    priority=int(row["priority"]),
                )
            )
        return [
            StrategyGroupDefinition(
                group_id=str(row["group_id"]),
                version=str(row["version"]),
                name=str(row["name"]),
                description=str(row["description"]),
                composition_mode=CompositionMode(str(row["composition_mode"])),
                conflict_policy=ConflictPolicy(str(row["conflict_policy"])),
                members=tuple(by_group.get(str(row["group_id"]), [])),
                enabled=bool(row["enabled"]),
                built_in=bool(row["built_in"]),
            )
            for row in groups
        ]

    def create_run(self, run_id: str, run_type: str, mode: str, strategies: list[str]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, run_type, status, mode, strategies_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, run_type, RunStatus.QUEUED.value, mode, json.dumps(strategies)),
            )

    def update_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error: str = "",
        snapshot_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        fields = ["status=?", "error=?", "metadata_json=?"]
        values: list[Any] = [status.value, error, json.dumps(metadata or {}, ensure_ascii=False)]
        if status == RunStatus.RUNNING:
            fields.append("started_at=?")
            values.append(now)
        if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.BLOCKED_DATA):
            fields.append("finished_at=?")
            values.append(now)
        if snapshot_id is not None:
            fields.append("snapshot_id=?")
            values.append(snapshot_id)
        values.append(run_id)
        self.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id=?", values)

    def save_signals(self, signals: Iterable[PlatformSignal]) -> None:
        rows = [signal.as_record() for signal in signals]
        if not rows:
            return
        columns = list(rows[0])
        placeholders = ",".join("?" for _ in columns)
        with self.connect() as connection:
            connection.executemany(
                f"INSERT OR REPLACE INTO signals({','.join(columns)}) VALUES ({placeholders})",
                [[row[column] for column in columns] for row in rows],
            )

    def save_order_groups(self, intents: Iterable[OrderGroupIntent]) -> None:
        items = list(intents)
        if not items:
            return
        with self.connect() as connection:
            for intent in items:
                row = intent.as_record()
                columns = list(row)
                connection.execute(
                    f"INSERT OR REPLACE INTO order_group_intents({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
                connection.execute("DELETE FROM order_group_legs WHERE intent_id=?", (intent.intent_id,))
                connection.executemany(
                    """INSERT INTO order_group_legs
                    (leg_id, intent_id, code, side, ratio, target_weight)
                    VALUES (:leg_id, :intent_id, :code, :side, :ratio, :target_weight)""",
                    intent.leg_records(),
                )

    def decide_order_group(
        self,
        intent_id: str,
        decision: SignalStatus,
        note: str = "",
    ) -> dict[str, Any]:
        if decision not in (SignalStatus.APPROVED, SignalStatus.REJECTED):
            raise ValueError("Decision must be APPROVED or REJECTED")
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            intent = connection.execute(
                "SELECT * FROM order_group_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise KeyError(intent_id)
            if intent["status"] != SignalStatus.PROPOSED.value:
                raise ValueError(f"Order group is not pending approval: {intent['status']}")
            if pd.Timestamp(now).timestamp() > pd.Timestamp(intent["valid_until"]).timestamp():
                connection.execute(
                    "UPDATE order_group_intents SET status=? WHERE intent_id=?",
                    (SignalStatus.EXPIRED.value, intent_id),
                )
                raise ValueError("Order group has expired")
            connection.execute(
                "UPDATE order_group_intents SET status=? WHERE intent_id=?",
                (decision.value, intent_id),
            )
            connection.execute(
                """INSERT INTO order_group_decisions(intent_id, decision, note, decided_at)
                VALUES (?, ?, ?, ?)""",
                (intent_id, decision.value, note, now),
            )
        return self.get_order_group(intent_id)

    def get_order_group(self, intent_id: str) -> dict[str, Any]:
        rows = self.query("SELECT * FROM order_group_intents WHERE intent_id=?", (intent_id,))
        if not rows:
            raise KeyError(intent_id)
        row = rows[0]
        row["legs"] = self.query(
            "SELECT * FROM order_group_legs WHERE intent_id=? ORDER BY leg_id", (intent_id,)
        )
        return row

    def list_order_groups(self, limit: int = 200, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.query(
                """SELECT * FROM order_group_intents WHERE status=?
                ORDER BY generated_at DESC LIMIT ?""",
                (status, limit),
            )
        else:
            rows = self.query(
                "SELECT * FROM order_group_intents ORDER BY generated_at DESC LIMIT ?", (limit,)
            )
        for row in rows:
            row["legs"] = self.query(
                "SELECT * FROM order_group_legs WHERE intent_id=? ORDER BY leg_id",
                (row["intent_id"],),
            )
        return rows

    def group_positions(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        if strategy_id:
            rows = self.query(
                """SELECT * FROM paper_group_positions WHERE strategy_id=?
                ORDER BY group_key, code""",
                (strategy_id,),
            )
        else:
            rows = self.query(
                "SELECT * FROM paper_group_positions ORDER BY strategy_id, group_key, code"
            )
        return rows

    def grouped_positions(self, strategy_id: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.group_positions(strategy_id):
            item = grouped.setdefault(
                str(row["group_key"]),
                {"group_key": row["group_key"], "strategy_id": strategy_id, "legs": []},
            )
            item["legs"].append(row)
        return list(grouped.values())

    def decide_signal(
        self,
        signal_id: str,
        decision: SignalStatus,
        note: str = "",
        *,
        reason_tags: Iterable[str] = (),
        confidence: float | None = None,
        max_acceptable_loss: float | None = None,
        ai_review_id: str | None = None,
    ) -> dict[str, Any]:
        if decision not in (SignalStatus.APPROVED, SignalStatus.REJECTED):
            raise ValueError("Decision must be APPROVED or REJECTED")
        now = datetime.now().astimezone().isoformat()
        tags = tuple(dict.fromkeys(str(item).strip() for item in reason_tags if str(item).strip()))
        if confidence is not None and not 0 <= confidence <= 100:
            raise ValueError("Confidence must be between 0 and 100")
        if max_acceptable_loss is not None and not 0 <= max_acceptable_loss <= 1:
            raise ValueError("Maximum acceptable loss must be between 0 and 1")
        with self.connect() as connection:
            signal = connection.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            if signal is None:
                raise KeyError(signal_id)
            if signal["status"] != SignalStatus.PROPOSED.value:
                raise ValueError(f"Signal is not pending approval: {signal['status']}")
            if decision == SignalStatus.APPROVED:
                account = connection.execute(
                    "SELECT frozen FROM paper_accounts WHERE strategy_id=?",
                    (signal["strategy_id"],),
                ).fetchone()
                if account is not None and bool(account["frozen"]):
                    raise ValueError("Archived strategy account is frozen")
            if pd.Timestamp(now).timestamp() > pd.Timestamp(signal["valid_until"]).timestamp():
                connection.execute(
                    "UPDATE signals SET status=? WHERE signal_id=?",
                    (SignalStatus.EXPIRED.value, signal_id),
                )
                raise ValueError("Signal has expired")
            connection.execute("UPDATE signals SET status=? WHERE signal_id=?", (decision.value, signal_id))
            alignment = "NOT_AVAILABLE"
            if ai_review_id:
                review = connection.execute(
                    "SELECT recommendation FROM ai_signal_reviews WHERE review_id=? AND signal_id=?",
                    (ai_review_id, signal_id),
                ).fetchone()
                if review is None:
                    raise ValueError("AI review does not belong to this signal")
                recommendation = str(review["recommendation"])
                aligned = (
                    decision == SignalStatus.APPROVED and recommendation == "SUPPORT"
                ) or (
                    decision == SignalStatus.REJECTED and recommendation == "OPPOSE"
                )
                alignment = "AGREE" if aligned else "DISAGREE"
            connection.execute(
                """INSERT INTO signal_decisions
                (signal_id, decision, note, reason_tags, confidence, max_acceptable_loss,
                 ai_review_id, ai_alignment, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_id,
                    decision.value,
                    note,
                    json.dumps(tags, ensure_ascii=False),
                    confidence,
                    max_acceptable_loss,
                    ai_review_id,
                    alignment,
                    now,
                ),
            )
            if decision == SignalStatus.APPROVED:
                connection.execute(
                    """INSERT OR IGNORE INTO paper_orders
                    (order_id, signal_id, strategy_id, code, side, status, signal_time, target_weight, reason)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
                    (
                        f"ord_{signal_id}", signal_id, signal["strategy_id"], signal["code"], signal["side"],
                        signal["generated_at"], signal["target_weight"], signal["reason_codes"],
                    ),
                )
        return self.query("SELECT * FROM signals WHERE signal_id=?", (signal_id,))[0]

    def expire_signals(self, now: datetime) -> int:
        now_epoch = pd.Timestamp(now).timestamp()
        candidates = self.query(
            "SELECT signal_id, valid_until FROM signals WHERE status=?",
            (SignalStatus.PROPOSED.value,),
        )
        expired = [
            str(item["signal_id"])
            for item in candidates
            if pd.Timestamp(item["valid_until"]).timestamp() < now_epoch
        ]
        if not expired:
            return 0
        with self.connect() as connection:
            connection.executemany(
                "UPDATE signals SET status=? WHERE signal_id=?",
                [(SignalStatus.EXPIRED.value, signal_id) for signal_id in expired],
            )
        return len(expired)

    def list_signals(self, limit: int = 200, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.query(
                "SELECT * FROM signals WHERE status=? ORDER BY generated_at DESC LIMIT ?", (status, limit)
            )
        return self.query("SELECT * FROM signals ORDER BY generated_at DESC LIMIT ?", (limit,))

    def portfolio_snapshot(self) -> dict[str, Any]:
        accounts = self.query("SELECT * FROM paper_accounts ORDER BY strategy_id")
        positions = self.query("SELECT * FROM paper_positions ORDER BY strategy_id, code")
        orders = self.query("SELECT * FROM paper_orders ORDER BY signal_time DESC LIMIT 200")
        fills = self.query("SELECT * FROM paper_fills ORDER BY timestamp DESC LIMIT 200")
        return {
            "accounts": accounts,
            "positions": positions,
            "orders": orders,
            "fills": fills,
            "order_groups": self.list_order_groups(),
            "group_positions": self.group_positions(),
            "group_fills": self.query(
                "SELECT * FROM paper_group_fills ORDER BY timestamp DESC LIMIT 200"
            ),
        }

    def load_runtime_states(self, strategy_id: str) -> dict[str, dict[str, Any]]:
        rows = self.query(
            "SELECT scope, state_json FROM strategy_runtime_states WHERE strategy_id=?",
            (strategy_id,),
        )
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                value = json.loads(str(row["state_json"]))
            except (json.JSONDecodeError, TypeError):
                value = {}
            result[str(row["scope"])] = value if isinstance(value, dict) else {}
        return result

    def replace_runtime_states(
        self,
        strategy_id: str,
        states: dict[str, dict[str, Any]],
        asof: str,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM strategy_runtime_states WHERE strategy_id=?",
                (strategy_id,),
            )
            connection.executemany(
                """INSERT INTO strategy_runtime_states
                (strategy_id, scope, asof, state_json, updated_at) VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        strategy_id,
                        scope,
                        asof,
                        json.dumps(state, ensure_ascii=False),
                        now,
                    )
                    for scope, state in states.items()
                ],
            )

    def save_backtest_state(
        self,
        backtest_id: str,
        strategy_id: str,
        timestamp: str,
        state: dict[str, Any],
    ) -> None:
        modes = state.get("trade_modes") or []
        trade_mode = ",".join(str(item) for item in modes) if isinstance(modes, list) else str(modes)
        self.execute(
            """INSERT OR REPLACE INTO backtest_states
            (backtest_id, strategy_id, timestamp, market_phase, market_style, suitability,
             trade_mode, entry_allowed, state_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                backtest_id,
                strategy_id,
                timestamp,
                str(state.get("market_phase", "")),
                str(state.get("market_style", "")),
                float(state.get("style_suitability", 0.0) or 0.0),
                trade_mode,
                int(bool(state.get("entry_allowed", False))),
                json.dumps(state, ensure_ascii=False),
            ),
        )
        playbook_states = state.get("playbook_states")
        if isinstance(playbook_states, list):
            with self.connect() as connection:
                for item in playbook_states:
                    if not isinstance(item, dict) or not item.get("playbook_id"):
                        continue
                    connection.execute(
                        """INSERT OR REPLACE INTO backtest_playbook_states
                        (backtest_id, strategy_id, timestamp, playbook_id, lifecycle,
                         admitted, candidate_count, routed_count, budget, blocked_reasons,
                         funnel_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            backtest_id,
                            strategy_id,
                            timestamp,
                            str(item["playbook_id"]),
                            str(item.get("lifecycle", "")),
                            int(bool(item.get("admitted", False))),
                            int(item.get("candidate_count", 0) or 0),
                            int(item.get("routed_count", 0) or 0),
                            float(item.get("budget", 0.0) or 0.0),
                            json.dumps(item.get("blocked_reasons") or [], ensure_ascii=False),
                            json.dumps(state.get("funnel") or {}, ensure_ascii=False),
                        ),
                    )

    def register_framework(
        self,
        framework: dict[str, Any],
        playbooks: Iterable[dict[str, Any]],
        *,
        policy_version: str,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            connection.execute(
                "UPDATE strategy_playbooks SET enabled=0 WHERE framework_id=?",
                (str(framework["framework_id"]),),
            )
            connection.execute(
                """INSERT INTO strategy_frameworks
                (framework_id, version, name, description, strategy_id, policy_version,
                 enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(framework_id) DO UPDATE SET
                version=excluded.version, name=excluded.name,
                description=excluded.description, strategy_id=excluded.strategy_id,
                policy_version=excluded.policy_version, enabled=1,
                updated_at=excluded.updated_at""",
                (
                    str(framework["framework_id"]),
                    str(framework["version"]),
                    str(framework["name"]),
                    str(framework["description"]),
                    str(framework["strategy_id"]),
                    policy_version,
                    now,
                    now,
                ),
            )
            for playbook in playbooks:
                connection.execute(
                    """INSERT INTO strategy_playbooks
                    (playbook_id, framework_id, version, name, description, lifecycle,
                     base_weight, market_phase, data_requirements_json, enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(playbook_id) DO UPDATE SET
                    framework_id=excluded.framework_id, version=excluded.version,
                    name=excluded.name, description=excluded.description,
                    lifecycle=excluded.lifecycle, base_weight=excluded.base_weight,
                    market_phase=excluded.market_phase,
                    data_requirements_json=excluded.data_requirements_json,
                    enabled=excluded.enabled, updated_at=excluded.updated_at""",
                    (
                        str(playbook["playbook_id"]),
                        str(playbook["framework_id"]),
                        str(playbook["version"]),
                        str(playbook["name"]),
                        str(playbook["description"]),
                        str(playbook["lifecycle"]),
                        float(playbook.get("base_weight", 0.0) or 0.0),
                        str(playbook.get("market_phase", "")),
                        json.dumps(playbook.get("data_requirements") or [], ensure_ascii=False),
                        now,
                    ),
                )

    def add_snapshot_dependency(
        self,
        snapshot_id: str,
        dependency_type: str,
        dependency_id: str,
        coverage: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            """INSERT OR REPLACE INTO snapshot_dependencies
            (snapshot_id, dependency_type, dependency_id, coverage_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                dependency_type,
                dependency_id,
                json.dumps(coverage or {}, ensure_ascii=False),
                datetime.now().astimezone().isoformat(),
            ),
        )


class ParquetSnapshotStore:
    def __init__(self, config: PlatformConfig, database: Database):
        self.config = config
        self.database = database

    def write_bars(
        self,
        snapshot_id: str,
        dataset: str,
        bars: dict[str, pd.DataFrame],
        query: dict[str, Any],
    ) -> Path:
        target_dir = self.config.snapshot_dir / snapshot_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{dataset}.parquet"
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        import pyarrow as pa
        import pyarrow.parquet as pq

        columns = sorted(
            {str(column) for frame in bars.values() for column in frame.columns},
            key=lambda value: (
                ("Open", "High", "Low", "Close", "Volume", "Amount").index(value)
                if value in ("Open", "High", "Low", "Close", "Volume", "Amount")
                else 999,
                value,
            ),
        )
        writer = None
        schema = None
        row_count = 0
        pending: list[pd.DataFrame] = []
        try:
            for code, frame in bars.items():
                item = frame.reindex(columns=columns).copy()
                item.index = pd.to_datetime(item.index)
                item.index.name = "timestamp"
                item = item.reset_index()
                item.insert(0, "code", code)
                pending.append(item)
                if len(pending) < 100:
                    continue
                table = pa.Table.from_pandas(pd.concat(pending, ignore_index=True), preserve_index=False)
                if writer is None:
                    schema = table.schema
                    writer = pq.ParquetWriter(str(temporary), schema, compression="snappy")
                writer.write_table(table.cast(schema))
                row_count += table.num_rows
                pending.clear()
            if pending:
                table = pa.Table.from_pandas(pd.concat(pending, ignore_index=True), preserve_index=False)
                if writer is None:
                    schema = table.schema
                    writer = pq.ParquetWriter(str(temporary), schema, compression="snappy")
                writer.write_table(table.cast(schema))
                row_count += table.num_rows
            if writer is not None:
                writer.close()
                writer = None
            else:
                pd.DataFrame(columns=["code", "timestamp", *columns]).to_parquet(
                    temporary, index=False
                )
            temporary.replace(path)
        finally:
            if writer is not None:
                writer.close()
            if temporary.exists():
                temporary.unlink()
        digest = _file_sha256(path)
        self.database.execute(
            """INSERT OR REPLACE INTO data_snapshots
            (snapshot_id, dataset, source, created_at, path, row_count, content_hash, query_json)
            VALUES (?, ?, 'tdx', ?, ?, ?, ?, ?)""",
            (
                f"{snapshot_id}:{dataset}", dataset, datetime.now().astimezone().isoformat(), str(path),
                row_count, digest, json.dumps(query, ensure_ascii=False),
            ),
        )
        return path

    def write_records(
        self,
        snapshot_id: str,
        dataset: str,
        records: list[dict[str, Any]],
        query: dict[str, Any],
    ) -> Path:
        target_dir = self.config.snapshot_dir / snapshot_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{dataset}.parquet"
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        pd.DataFrame(records).to_parquet(temporary, index=False)
        temporary.replace(path)
        digest = _file_sha256(path)
        self.database.execute(
            """INSERT OR REPLACE INTO data_snapshots
            (snapshot_id, dataset, source, created_at, path, row_count, content_hash, query_json)
            VALUES (?, ?, 'tdx', ?, ?, ?, ?, ?)""",
            (
                f"{snapshot_id}:{dataset}",
                dataset,
                datetime.now().astimezone().isoformat(),
                str(path),
                len(records),
                digest,
                json.dumps(query, ensure_ascii=False),
            ),
        )
        return path

    def open_record_writer(
        self,
        snapshot_id: str,
        dataset: str,
        query: dict[str, Any],
        *,
        schema: Any | None = None,
    ) -> "ParquetRecordStreamWriter":
        return ParquetRecordStreamWriter(
            self.config,
            self.database,
            snapshot_id,
            dataset,
            query,
            schema=schema,
        )

    def load_bars(self, snapshot_id: str, dataset: str) -> dict[str, pd.DataFrame]:
        frame, query = self._load_dataset(snapshot_id, dataset)
        required = {"code", "timestamp"}
        if not required.issubset(frame.columns):
            raise ValueError(f"Snapshot dataset '{dataset}' is not a bar dataset")
        result: dict[str, pd.DataFrame] = {}
        for code, rows in frame.groupby("code", sort=False):
            item = rows.drop(columns=["code"]).copy()
            item["timestamp"] = pd.to_datetime(item["timestamp"], errors="coerce")
            item = item.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
            if "Amount" in item.columns:
                item.attrs["amount_unit"] = "CNY"
            item.attrs["volume_unit"] = "share"
            item.attrs["timezone"] = self.config.timezone
            item.attrs["source"] = "snapshot"
            item.attrs["adjustment"] = str(query.get("adjustment", ""))
            result[str(code)] = item
        return result

    def load_records(self, snapshot_id: str, dataset: str) -> pd.DataFrame:
        frame, _ = self._load_dataset(snapshot_id, dataset)
        return frame

    def has_dataset(self, snapshot_id: str, dataset: str) -> bool:
        rows = self.database.query(
            "SELECT path FROM data_snapshots WHERE snapshot_id=?",
            (f"{snapshot_id}:{dataset}",),
        )
        return bool(rows and Path(str(rows[0].get("path", ""))).exists())

    def dataset_query(self, snapshot_id: str, dataset: str) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT query_json FROM data_snapshots WHERE snapshot_id=?",
            (f"{snapshot_id}:{dataset}",),
        )
        if not rows:
            return {}
        try:
            value = json.loads(str(rows[0].get("query_json") or "{}"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _load_dataset(
        self,
        snapshot_id: str,
        dataset: str,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM data_snapshots WHERE snapshot_id=?",
            (f"{snapshot_id}:{dataset}",),
        )
        if not rows:
            raise ValueError(f"Snapshot dataset not found: {snapshot_id}/{dataset}")
        row = rows[0]
        path = Path(str(row.get("path", "")))
        if not path.exists():
            raise ValueError(f"Snapshot file is missing: {path}")
        digest = _file_sha256(path)
        if digest != str(row.get("content_hash", "")):
            raise ValueError(f"Snapshot hash mismatch: {snapshot_id}/{dataset}")
        try:
            query = json.loads(str(row.get("query_json") or "{}"))
        except json.JSONDecodeError:
            query = {}
        return pd.read_parquet(path), query if isinstance(query, dict) else {}

    def load_sector_membership(
        self,
        asof: str | pd.Timestamp,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]] | None:
        cutoff = pd.Timestamp(asof).normalize()
        candidates: list[tuple[pd.Timestamp, dict[str, Any], dict[str, Any]]] = []
        for row in self.database.query(
            "SELECT * FROM data_snapshots WHERE dataset='sector_membership' ORDER BY created_at DESC"
        ):
            try:
                query = json.loads(str(row.get("query_json") or "{}"))
            except json.JSONDecodeError:
                query = {}
            effective = query.get("asof") or query.get("effective_asof")
            if not effective:
                continue
            timestamp = pd.Timestamp(effective).normalize()
            path = Path(str(row.get("path", "")))
            if timestamp <= cutoff and path.exists():
                candidates.append((timestamp, row, query))
        if not candidates:
            return None
        effective, row, query = max(candidates, key=lambda item: item[0])
        frame = pd.read_parquet(str(row["path"]))
        required = {"sector_code", "sector_name", "member_code"}
        if frame.empty or not required.issubset(frame.columns):
            return None
        sectors: dict[str, dict[str, Any]] = {}
        for sector_code, rows in frame.groupby("sector_code"):
            sectors[str(sector_code)] = {
                "name": str(rows["sector_name"].iloc[0]),
                "members": sorted(set(rows["member_code"].astype(str))),
            }
        root_query = self._sector_membership_root_query(query)
        root_quality = str(root_query.get("quality", "CURRENT"))
        root_source = str(root_query.get("source", ""))
        limited = root_quality == "LIMITED" or root_source == "current_fallback"
        metadata = {
            "quality": "LIMITED" if limited else "HISTORICAL_SNAPSHOT",
            "source": "current_fallback"
            if limited
            else str(row["snapshot_id"]).split(":", 1)[0],
            "effective_asof": str(root_query.get("asof") or effective.date().isoformat())
            if limited
            else effective.date().isoformat(),
            "content_hash": str(row["content_hash"]),
            "original_quality": root_quality,
        }
        return sectors, metadata

    def _sector_membership_root_query(self, query: dict[str, Any]) -> dict[str, Any]:
        current = dict(query)
        seen: set[str] = set()
        for _ in range(32):
            source = str(current.get("source", ""))
            if not source or source == "current_fallback" or source in seen:
                break
            seen.add(source)
            rows = self.database.query(
                "SELECT query_json FROM data_snapshots WHERE snapshot_id=?",
                (f"{source}:sector_membership",),
            )
            if not rows:
                break
            try:
                parent = json.loads(str(rows[0].get("query_json") or "{}"))
            except json.JSONDecodeError:
                break
            if not isinstance(parent, dict):
                break
            current = parent
        return current


class ParquetRecordStreamWriter:
    def __init__(
        self,
        config: PlatformConfig,
        database: Database,
        snapshot_id: str,
        dataset: str,
        query: dict[str, Any],
        *,
        schema: Any | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.snapshot_id = snapshot_id
        self.dataset = dataset
        self.query = query
        self.schema = schema
        self.row_count = 0
        self._writer: Any | None = None
        target_dir = config.snapshot_dir / snapshot_id
        target_dir.mkdir(parents=True, exist_ok=True)
        self.path = target_dir / f"{dataset}.parquet"
        self.temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")

    def __enter__(self) -> "ParquetRecordStreamWriter":
        return self

    def append(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pylist(records, schema=self.schema)
        if self._writer is None:
            self.schema = table.schema
            self._writer = pq.ParquetWriter(str(self.temporary_path), self.schema, compression="snappy")
        self._writer.write_table(table)
        self.row_count += len(records)

    def close(self) -> Path:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        elif self.schema is not None:
            import pyarrow as pa
            import pyarrow.parquet as pq

            pq.write_table(pa.Table.from_pylist([], schema=self.schema), str(self.temporary_path))
        else:
            pd.DataFrame().to_parquet(self.temporary_path, index=False)
        self.temporary_path.replace(self.path)
        digest = _file_sha256(self.path)
        self.database.execute(
            """INSERT OR REPLACE INTO data_snapshots
            (snapshot_id, dataset, source, created_at, path, row_count, content_hash, query_json)
            VALUES (?, ?, 'tdx', ?, ?, ?, ?, ?)""",
            (
                f"{self.snapshot_id}:{self.dataset}",
                self.dataset,
                datetime.now().astimezone().isoformat(),
                str(self.path),
                self.row_count,
                digest,
                json.dumps(self.query, ensure_ascii=False),
            ),
        )
        return self.path

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if exc_type is None:
            self.close()
            return
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self.temporary_path.exists():
            self.temporary_path.unlink()
