from __future__ import annotations

import json
import os
import platform
import subprocess
import urllib.error
import urllib.request
try:  # pragma: no cover - imported only on Windows deployments
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo


TQ_ENDPOINT = "http://127.0.0.1:17709/"
TQ_INSTALL_URL = "https://data.tdx.com.cn/level2/new_tdx64.exe"
SUPPORTED_UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信专业版",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(量化模拟)",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信iTendx研究终端",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(测试)",
)
READ_ONLY_METHODS = frozenset(
    {
        "get_match_stkinfo",
        "get_market_data",
        "get_market_snapshot",
        "get_stock_info",
        "get_stock_list",
        "get_trading_calendar",
        "get_divid_factors",
        "get_more_info",
    }
)


@dataclass(frozen=True)
class TQCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class TQPreflight:
    ready: bool
    checked_at: str
    checks: tuple[TQCheck, ...]
    endpoint: str = TQ_ENDPOINT
    install_url: str = TQ_INSTALL_URL

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = "READY" if self.ready else "PAPER_BLOCKED"
        return payload


@dataclass(frozen=True)
class USQuoteObservation:
    code: str
    fetched_at: datetime
    source_at: datetime | None
    market_status: str
    open: float | None
    last: float | None
    bid: float | None
    ask: float | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class TQRawRPCEnvelope:
    method: str
    request_bytes: bytes
    response_bytes: bytes
    fetched_at: datetime
    value: Any


def check_tq_preflight(
    *,
    system_name: str | None = None,
    installation_finder: Callable[[], list[dict[str, str]]] | None = None,
    portable_root: Path | str | None = None,
    process_finder: Callable[[], bool] | None = None,
    rpc_probe: Callable[[], dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> TQPreflight:
    """Run the mandated TQ checks in order and stop at the first blocker.

    This function is deliberately read-only.  Installation remains a manual
    operator action; an anti-bot page must never be treated as an installer.
    """

    checked = (now or datetime.now().astimezone()).isoformat()
    checks: list[TQCheck] = []
    actual_system = system_name or platform.system()
    os_ok = actual_system == "Windows"
    checks.append(TQCheck("WINDOWS", os_ok, actual_system))
    if not os_ok:
        return TQPreflight(False, checked, tuple(checks))

    finder = installation_finder or (
        lambda: _configured_installations(portable_root=portable_root)
    )
    installations = finder()
    installed = bool(installations)
    detail = (
        "; ".join(
            f"{item.get('display_name', '')} -> {item.get('install_location', '')}".strip()
            for item in installations
        )
        if installed
        else (
            "No supported TongdaXin registry installation or configured "
            "portable TQ runtime was found"
        )
    )
    checks.append(TQCheck("TQ_INSTALLED", installed, detail))
    if not installed:
        return TQPreflight(False, checked, tuple(checks))

    running = (process_finder or _tdxw_running)()
    checks.append(
        TQCheck(
            "TDXW_RUNNING",
            running,
            "TdxW.exe is running" if running else "Start and log in to TdxW.exe",
        )
    )
    if not running:
        return TQPreflight(False, checked, tuple(checks))

    try:
        response = (rpc_probe or _health_rpc)()
        rpc_ok = _rpc_succeeded(response)
        rpc_detail = "TQ HTTP JSON-RPC is available" if rpc_ok else _rpc_error(response)
    except Exception as exc:  # deployment doctor must return a blocker, not crash
        rpc_ok = False
        rpc_detail = f"{type(exc).__name__}: {exc}"
    checks.append(TQCheck("TQ_HTTP_17709", rpc_ok, rpc_detail))
    if not rpc_ok:
        return TQPreflight(False, checked, tuple(checks))
    if rpc_probe is None:
        result = response.get("result") if isinstance(response, dict) else None
        value = result.get("Value") if isinstance(result, dict) else None
        count = value.get("us_stock_list_count") if isinstance(value, dict) else None
        probe_code = value.get("us_market_data_probe") if isinstance(value, dict) else None
        us_ready = isinstance(count, int) and count > 0 and probe_code == "AAPL.US"
        checks.append(
            TQCheck(
                "TQ_US_READ_ONLY_MARKET_DATA",
                us_ready,
                (
                    f"market=103 symbols={count}; daily probe={probe_code}"
                    if us_ready
                    else "US get_stock_list/get_market_data evidence is incomplete"
                ),
            )
        )
        return TQPreflight(us_ready, checked, tuple(checks))
    return TQPreflight(True, checked, tuple(checks))


class TQReadOnlyClient:
    """Minimal JSON-RPC client that cannot invoke trading or mutation methods."""

    def __init__(self, endpoint: str = TQ_ENDPOINT, timeout_seconds: float = 5.0):
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        envelope = self.call_raw(method, params)
        value = envelope.value
        return value if isinstance(value, dict) else {"Value": value}

    def call_raw(self, method: str, params: dict[str, Any]) -> TQRawRPCEnvelope:
        if method not in READ_ONLY_METHODS:
            raise PermissionError(f"TQ method is not permitted by paper-only runtime: {method}")
        payload = {"id": 1, "method": method, "params": params}
        request_bytes = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=request_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            response_bytes = response.read()
        decoded = json.loads(response_bytes.decode("utf-8"))
        if not _rpc_succeeded(decoded):
            raise RuntimeError(_rpc_error(decoded))
        result = decoded.get("result", {})
        value = result.get("Value") if isinstance(result, dict) else result
        return TQRawRPCEnvelope(
            method=method,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            fetched_at=datetime.now(ZoneInfo("UTC")),
            value=value,
        )

    def market_snapshot(self, code: str, *, fetched_at: datetime | None = None) -> USQuoteObservation:
        normalized = str(code).strip().upper()
        if not normalized.endswith(".US"):
            raise ValueError("US paper quote source only accepts .US symbols")
        raw = self.call("get_market_snapshot", {"stock_code": normalized})
        observed = fetched_at or datetime.now(ZoneInfo("America/New_York"))
        source_at = _source_timestamp(raw, observed)
        bids = _float_list(raw.get("Buyp"))
        asks = _float_list(raw.get("Sellp"))
        return USQuoteObservation(
            code=normalized,
            fetched_at=observed,
            source_at=source_at,
            market_status=str(raw.get("MarketStatus") or raw.get("Status") or "UNKNOWN"),
            open=_optional_positive(raw.get("Open")),
            last=_optional_positive(raw.get("Now")),
            bid=bids[0] if bids else None,
            ask=asks[0] if asks else None,
            raw=raw,
        )


def _registered_installations() -> list[dict[str, str]]:
    if winreg is None:
        return []
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        for path in SUPPORTED_UNINSTALL_KEYS:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    path,
                    0,
                    winreg.KEY_READ | view,
                ) as key:
                    display = _registry_value(key, "DisplayName")
                    location = _registry_value(key, "InstallLocation")
            except OSError:
                continue
            identity = (display, location)
            if identity not in seen:
                found.append(
                    {"display_name": display, "install_location": location, "key": path}
                )
                seen.add(identity)
    return found


def _configured_installations(
    *, portable_root: Path | str | None = None
) -> list[dict[str, str]]:
    """Return registered installs or the project-confirmed portable runtime.

    The stock workspace deliberately uses a portable TongdaXin tree that does
    not create an Uninstall registry key.  Runtime health is still established
    by the process and read-only JSON-RPC checks that follow this discovery
    step; the directory alone never makes the full preflight READY.
    """

    root = _portable_runtime_root(portable_root)
    executable = root / "TdxW.exe"
    if portable_root is not None:
        if not executable.is_file():
            return []
        return [
            {
                "display_name": "TongdaXin portable TQ runtime",
                "install_location": str(root),
                "key": "PROJECT_CONFIRMED_PORTABLE_RUNTIME",
            }
        ]
    registered = _registered_installations()
    if registered:
        return registered
    if not executable.is_file():
        return []
    return [
        {
            "display_name": "TongdaXin portable TQ runtime",
            "install_location": str(root),
            "key": "PROJECT_CONFIRMED_PORTABLE_RUNTIME",
        }
    ]


def _portable_runtime_root(value: Path | str | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve()
    configured = os.getenv("TDX_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "tdx-mock"


def _registry_value(key: Any, name: str) -> str:
    try:
        return str(winreg.QueryValueEx(key, name)[0] or "")
    except OSError:
        return ""


def _tdxw_running() -> bool:
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq TdxW.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return "tdxw.exe" in completed.stdout.lower()


def _unused_legacy_health_rpc() -> dict[str, Any]:
    client = TQReadOnlyClient(timeout_seconds=3.0)
    stock_list = client.call("get_stock_list", {"market": "103", "list_type": 1})
    stock_value = stock_list.get("Value") if isinstance(stock_list, dict) else None
    if not isinstance(stock_value, list) or not stock_value:
        raise RuntimeError("get_stock_list market=103 returned no US symbols")
    probe = client.call("get_market_data", {
        "field_list": ["Open", "High", "Low", "Close", "Volume"],
        "stock_list": ["AAPL.US"], "period": "1d", "end_time": "",
        "count": 2, "dividend_type": "none", "fill_data": False,
    })
    if not isinstance(probe, dict) or not isinstance(probe.get("AAPL.US"), dict):
        raise RuntimeError("get_market_data did not return AAPL.US")
    return {
        "result": {
            "ErrorId": "0",
            "Value": client.call("get_match_stkinfo", {"key_word": "茅台"}),
        }
    }


def _health_rpc() -> dict[str, Any]:
    """Probe the actual US read-only list and daily-bar paths."""
    # The portable TQ service can take more than ten seconds to serialize the
    # full 13k+ market=103 table while it is also serving the desktop client.
    # This remains a bounded read-only deployment probe, not a low-latency
    # quote check.
    client = TQReadOnlyClient(timeout_seconds=30.0)
    stock_list = client.call("get_stock_list", {"market": "103", "list_type": 1})
    stock_value = stock_list.get("Value") if isinstance(stock_list, dict) else None
    if not isinstance(stock_value, list) or not stock_value:
        raise RuntimeError("get_stock_list market=103 returned no US symbols")
    probe = client.call(
        "get_market_data",
        {
            "field_list": ["Open", "High", "Low", "Close", "Volume"],
            "stock_list": ["AAPL.US"],
            "period": "1d",
            "end_time": "",
            "count": 2,
            "dividend_type": "none",
            "fill_data": False,
        },
    )
    if not isinstance(probe, dict) or not isinstance(probe.get("AAPL.US"), dict):
        raise RuntimeError("get_market_data did not return AAPL.US")
    return {
        "result": {
            "ErrorId": "0",
            "Value": {
                "health": client.call("get_match_stkinfo", {"key_word": "\u9ebb\u8c46"}),
                "us_stock_list_count": len(stock_value),
                "us_market_data_probe": "AAPL.US",
            },
        }
    }


def _rpc_succeeded(value: dict[str, Any]) -> bool:
    if "error" in value:
        return False
    result = value.get("result")
    if not isinstance(result, dict):
        return False
    return str(result.get("ErrorId", "0")) == "0"


def _rpc_error(value: dict[str, Any]) -> str:
    if value.get("error"):
        return json.dumps(value["error"], ensure_ascii=False, sort_keys=True)
    result = value.get("result")
    if isinstance(result, dict):
        return str(result.get("Msg") or result.get("ErrorInfo") or result)
    return "Invalid TQ JSON-RPC response"


def _float_list(value: Any) -> list[float]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []
    return [item for raw in value if (item := _optional_positive(raw)) is not None]


def _optional_positive(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _source_timestamp(raw: dict[str, Any], fetched_at: datetime) -> datetime | None:
    date_value = raw.get("HqDate") or raw.get("Date")
    time_value = raw.get("HqTime") or raw.get("Time")
    if not date_value or not time_value:
        return None
    digits = "".join(char for char in f"{date_value}{time_value}" if char.isdigit())
    if len(digits) < 14:
        return None
    try:
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    except ValueError:
        return None


__all__ = [
    "READ_ONLY_METHODS",
    "TQCheck",
    "TQPreflight",
    "TQReadOnlyClient",
    "TQRawRPCEnvelope",
    "USQuoteObservation",
    "check_tq_preflight",
]
