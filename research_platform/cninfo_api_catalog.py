from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import requests


PROTOCOL_VERSION = "cninfo-api-catalog-admission-v1"
MANIFEST_SCHEMA_VERSION = "cninfo-api-catalog-manifest-v1"
CATALOG_STATUS = "CATALOG_DISCOVERED_AUTHORIZATION_REQUIRED"
SOURCE_REJECTED = "SOURCE_REJECTED"
SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"

CNINFO_API_CATALOG_URL = (
    "https://webapi.cninfo.com.cn/"
    "api-cloud-gateway-manage/apiDoc/apiDocTree?type=2"
)
CATALOG_HOST = "webapi.cninfo.com.cn"
JSON_MEDIA_TYPE = "application/json"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
EXPECTED_LEAF_COUNT = 2_466
EXPECTED_UNIQUE_INTERFACE_COUNT = 2_080
MAX_TREE_DEPTH = 16
MAX_TREE_NODES = 10_000

INTERNAL_OFFER_NAME = "\u5185\u90e8\u63a5\u53e3"
AUTHORIZATION_NOTE = (
    "offerName=\u5185\u90e8\u63a5\u53e3\u4e0d\u80fd\u8bc1\u660e\u514d\u8d39/\u6388\u6743"
)

_BRANCH_FIELDS = frozenset(
    {
        "id",
        "code",
        "parentCode",
        "children",
        "type",
        "name",
        "display",
        "level",
        "sort",
        "createTime",
        "updateTime",
        "createUser",
        "updateUser",
        "del",
        "run",
    }
)
_LEAF_FIELDS = frozenset(
    {
        "code",
        "name",
        "apiType",
        "alias",
        "parentCode",
        "requestPath",
        "serverName",
        "offerName",
        "createTime",
        "type",
    }
)
_RAW_EVIDENCE_FIELDS = frozenset(
    {
        "source_id",
        "source_url",
        "method",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "http_status",
        "cas_uri",
        "object_path",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "manifest_schema_version",
        "protocol_version",
        "retrieved_at",
        "raw_catalog",
        "interfaces",
        "logical_content_sha256",
        "source_contract",
        "statistics",
    }
)
_BUILDER_SEAL = object()


class CninfoApiCatalogBlockedError(RuntimeError):
    """The public catalog failed its frozen, discovery-only contract."""

    def __init__(self, message: str, *, status: str = SOURCE_REJECTED) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CatalogInterfaceSpec:
    identity: str
    alias: str
    request_path: str
    code: str
    occurrence_count: int


REQUIRED_INTERFACE_SPECS: tuple[CatalogInterfaceSpec, ...] = (
    CatalogInterfaceSpec(
        "p_public0001",
        "\u4ea4\u6613\u65e5\u5386\u6570\u636e",
        "stock/p_public0001",
        "0bf76273eb724e38bf32c30cfac5ddda",
        3,
    ),
    CatalogInterfaceSpec(
        "p_public0002",
        "\u884c\u4e1a\u5206\u7c7b\u6570\u636e",
        "stock/p_public0002",
        "04add00012fc4101bb2e2303b314a6d7",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2238",
        "\u4e0a\u5e02\u516c\u53f8\u4e1a\u7ee9\u9884\u544a",
        "stock/p_stock2238",
        "97c2b582835244c1bdafc704c8fc9408",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2300",
        "\u4e2a\u80a1\u62a5\u544a\u671f\u8d44\u4ea7\u8d1f\u503a\u8868",
        "stock/p_stock2300",
        "5474f0bfcc0a497197b3fe9cdf37befb",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2301",
        "\u4e2a\u80a1\u62a5\u544a\u671f\u5229\u6da6\u8868",
        "stock/p_stock2301",
        "0838f71fe00e4d00bb4d9a8d45df8472",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2302",
        "\u4e2a\u80a1\u62a5\u544a\u671f\u73b0\u91d1\u8868",
        "stock/p_stock2302",
        "dff987d3d00e4c62afeb042bb3f6a84b",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2303",
        "\u4e2a\u80a1\u62a5\u544a\u671f\u6307\u6807\u8868",
        "stock/p_stock2303",
        "87701f2530ae482ab2a999dae0dde022",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2328",
        "\u4e1a\u7ee9\u5feb\u62a5",
        "stock/p_stock2328",
        "7ab8b9987e3e45789fb3426dcdcfd5b9",
        1,
    ),
    CatalogInterfaceSpec(
        "p_stock2402",
        "\u80a1\u7968\u5386\u53f2\u65e5\u884c\u60c5",
        "stock/p_stock2402",
        "c3c41c16bf0f420e863fdad34b0d6648",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2406",
        "\u8bc1\u5238\u590d\u6743\u56e0\u5b50",
        "stock/p_stock2406",
        "19affbd27dc94fe1be8fe356a1579f66",
        1,
    ),
    CatalogInterfaceSpec(
        "p_stock2407",
        "\u80a1\u7968\u524d\u540e\u590d\u6743\u65e5\u884c\u60c5\u8868",
        "stock/p_stock2407",
        "bbcee85fd60b4e719d2cf7ea897b0c06",
        1,
    ),
    CatalogInterfaceSpec(
        "p_stock2231",
        "\u516c\u53f8\u914d\u80a1\u9884\u6848",
        "stock/p_stock2231",
        "ac6ecb8efb7b4101ac1f3a69cef50252",
        2,
    ),
    CatalogInterfaceSpec(
        "p_stock2232",
        "\u516c\u53f8\u914d\u80a1\u5b9e\u65bd\u65b9\u6848",
        "stock/p_stock2232",
        "71a7162a7ebe434c81ef0c45c8c60240",
        2,
    ),
)


@dataclass(frozen=True)
class RawCatalogEvidence:
    source_id: str
    source_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    http_status: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogInterfaceRecord:
    identity: str
    alias: str
    request_path: str
    code: str
    offer_name: str
    occurrence_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "alias": self.alias,
            "requestPath": self.request_path,
            "code": self.code,
            "offerName": self.offer_name,
            "occurrence_count": self.occurrence_count,
            "status": CATALOG_STATUS,
            "ready": False,
            "authorization_note": AUTHORIZATION_NOTE,
        }


@dataclass(frozen=True)
class CninfoApiCatalogArtifact:
    retrieved_at: str
    raw_catalog: RawCatalogEvidence
    interfaces: tuple[CatalogInterfaceRecord, ...]
    leaf_count: int
    unique_interface_count: int
    branch_count: int
    logical_content_sha256: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _BUILDER_SEAL:
            raise TypeError("catalog artifacts must be rebuilt from raw CAS bytes")

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "status": CATALOG_STATUS,
            "ready": False,
            "authority": "CNINFO_PUBLIC_API_CATALOG",
            "scope": "CATALOG_METADATA_ONLY",
            "catalog_method": "GET",
            "catalog_url": CNINFO_API_CATALOG_URL,
            "redirects_allowed": False,
            "protected_endpoint_calls": 0,
            "credentials_or_tokens_stored": False,
            "authorization_proven": False,
            "free_access_proven": False,
            "caller_ready_rejected": True,
            "data_source_eligibility": False,
            "training_eligibility": False,
            "trading_eligibility": False,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        return {
            "leaf_count": self.leaf_count,
            "unique_interface_count": self.unique_interface_count,
            "branch_count": self.branch_count,
            "required_interface_count": len(self.interfaces),
            "required_interface_occurrence_count": sum(
                item.occurrence_count for item in self.interfaces
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "raw_catalog": self.raw_catalog.to_dict(),
            "interfaces": [item.to_dict() for item in self.interfaces],
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class CninfoApiCatalogManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CninfoApiCatalogCAS:
    """Immutable CAS for the public catalog response and its manifest."""

    def __init__(self, root: Path) -> None:
        self.root = _lexical_absolute(Path(root))
        _prepare_root(self.root)

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        if not content:
            raise CninfoApiCatalogBlockedError("refusing to store empty CAS data")
        digest = _sha256(content)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(self.root, path, content)
        persisted = _stable_read(self.root, path)
        if persisted != content or _sha256(persisted) != digest:
            raise CninfoApiCatalogBlockedError("CAS read-back verification failed")
        return digest, path

    def read_blob(self, digest: str) -> tuple[bytes, Path]:
        normalized = str(digest).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise CninfoApiCatalogBlockedError("invalid CAS SHA-256")
        path = self.root / "sha256" / normalized[:2] / normalized
        content = _stable_read(self.root, path)
        if _sha256(content) != normalized:
            raise CninfoApiCatalogBlockedError("CAS object hash mismatch")
        return content, path

    def capture(
        self,
        content: bytes,
        *,
        retrieved_at: str,
        content_type: str,
    ) -> RawCatalogEvidence:
        digest, path = self.put_blob(content)
        return RawCatalogEvidence(
            source_id="CNINFO_API_DOC_TREE_TYPE_2",
            source_url=CNINFO_API_CATALOG_URL,
            method="GET",
            retrieved_at=_normalize_timestamp(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            http_status=200,
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )


class CninfoApiCatalogManifestStore:
    def __init__(self, cas: CninfoApiCatalogCAS) -> None:
        if not isinstance(cas, CninfoApiCatalogCAS):
            raise TypeError("cas must be a CninfoApiCatalogCAS")
        self.cas = cas

    def seal(
        self, artifact: CninfoApiCatalogArtifact
    ) -> CninfoApiCatalogManifestReference:
        if not isinstance(artifact, CninfoApiCatalogArtifact):
            raise TypeError("artifact must be a CninfoApiCatalogArtifact")
        payload = _manifest_payload(artifact)
        rebuilt = _rebuild_from_manifest(payload, cas=self.cas)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise CninfoApiCatalogBlockedError(
                "catalog artifact is not reproducible from raw CAS evidence"
            )
        digest, path = self.cas.put_blob(content)
        return CninfoApiCatalogManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(self, manifest_sha256: str) -> CninfoApiCatalogArtifact:
        content, _path = self.cas.read_blob(manifest_sha256)
        if len(content) > MAX_MANIFEST_BYTES:
            raise CninfoApiCatalogBlockedError("catalog manifest is oversized")
        payload = _strict_json_object(content, label="catalog manifest")
        if content != _canonical_json_bytes(payload):
            raise CninfoApiCatalogBlockedError(
                "catalog manifest is not canonical JSON"
            )
        rebuilt = _rebuild_from_manifest(payload, cas=self.cas)
        if content != _canonical_json_bytes(_manifest_payload(rebuilt)):
            raise CninfoApiCatalogBlockedError(
                "catalog manifest does not cold replay exactly"
            )
        return rebuilt


class CninfoApiCatalogClient:
    """Read only the public directory; never call an advertised data API."""

    def __init__(
        self,
        *,
        cas: CninfoApiCatalogCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cas, CninfoApiCatalogCAS):
            raise TypeError("cas must be a CninfoApiCatalogCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, *, caller_ready: bool = False) -> CninfoApiCatalogArtifact:
        if caller_ready is not False:
            raise CninfoApiCatalogBlockedError(
                "catalog discovery cannot be promoted by caller-ready input",
                status=CATALOG_STATUS,
            )
        _validate_catalog_url(CNINFO_API_CATALOG_URL)
        try:
            response = self.session.get(
                CNINFO_API_CATALOG_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "tdx-research-platform/cninfo-catalog-audit-v1",
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CninfoApiCatalogBlockedError(
                f"CNINFO public API catalog is unavailable: {exc}",
                status=SOURCE_INCOMPLETE,
            ) from exc
        if response.status_code != 200:
            raise CninfoApiCatalogBlockedError(
                f"CNINFO public API catalog returned HTTP {response.status_code}",
                status=SOURCE_INCOMPLETE,
            )
        if getattr(response, "history", ()):
            raise CninfoApiCatalogBlockedError(
                "CNINFO public API catalog followed a redirect"
            )
        if (
            response.url != CNINFO_API_CATALOG_URL
            or response.headers.get("Location")
        ):
            raise CninfoApiCatalogBlockedError(
                "CNINFO public API catalog redirected away from its fixed URL"
            )
        content = bytes(response.content)
        if not content or len(content) > MAX_CATALOG_BYTES:
            raise CninfoApiCatalogBlockedError(
                "CNINFO public API catalog returned an empty or oversized body"
            )
        content_length = response.headers.get("Content-Length")
        if content_length is not None and (
            not re.fullmatch(r"\d+", str(content_length))
            or int(content_length) != len(content)
        ):
            raise CninfoApiCatalogBlockedError(
                "CNINFO public API catalog Content-Length is inconsistent"
            )
        content_type = _media_type(response.headers.get("Content-Type"))
        if content_type != JSON_MEDIA_TYPE:
            raise CninfoApiCatalogBlockedError(
                f"CNINFO public API catalog returned media type {content_type!r}"
            )
        parsed = _parse_catalog(content)
        retrieved_at = _normalize_timestamp(self._clock())
        evidence = self.cas.capture(
            content,
            retrieved_at=retrieved_at,
            content_type=content_type,
        )
        return _artifact_from_parsed(
            parsed,
            raw_catalog=evidence,
            retrieved_at=retrieved_at,
        )


@dataclass(frozen=True)
class _ParsedCatalog:
    interfaces: tuple[CatalogInterfaceRecord, ...]
    leaf_count: int
    unique_interface_count: int
    branch_count: int
    logical_content_sha256: str


def _parse_catalog(content: bytes) -> _ParsedCatalog:
    payload = _strict_json_object(content, label="CNINFO public API catalog")
    if set(payload) != {"code", "msg", "data"}:
        raise CninfoApiCatalogBlockedError("catalog envelope schema drift")
    if payload["code"] != "000000" or payload["msg"] != "success":
        raise CninfoApiCatalogBlockedError("catalog envelope status changed")
    roots = payload["data"]
    if not isinstance(roots, list) or len(roots) != 1:
        raise CninfoApiCatalogBlockedError("catalog root schema drift")

    leaves: list[dict[str, str]] = []
    branch_codes: set[str] = set()
    branch_ids: set[int] = set()
    branch_count = 0
    node_count = 0

    def walk(node: Any, *, parent_code: str, depth: int) -> None:
        nonlocal branch_count, node_count
        node_count += 1
        if node_count > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
            raise CninfoApiCatalogBlockedError("catalog tree exceeds safety bounds")
        if not isinstance(node, dict):
            raise CninfoApiCatalogBlockedError("catalog tree node is not an object")
        node_type = node.get("type")
        if type(node_type) is not int:
            raise CninfoApiCatalogBlockedError("catalog node type schema drift")
        if node_type == 1:
            if set(node) != _BRANCH_FIELDS:
                raise CninfoApiCatalogBlockedError("catalog branch schema drift")
            _validate_branch(node, parent_code=parent_code, depth=depth)
            code = node["code"]
            branch_id = node["id"]
            if code in branch_codes or branch_id in branch_ids:
                raise CninfoApiCatalogBlockedError("duplicate catalog branch identity")
            branch_codes.add(code)
            branch_ids.add(branch_id)
            branch_count += 1
            for child in node["children"]:
                walk(child, parent_code=code, depth=depth + 1)
            return
        if node_type != 0 or set(node) != _LEAF_FIELDS:
            raise CninfoApiCatalogBlockedError("catalog leaf schema drift")
        leaves.append(_validate_leaf(node, parent_code=parent_code))

    walk(roots[0], parent_code="0", depth=0)
    if len(leaves) != EXPECTED_LEAF_COUNT:
        raise CninfoApiCatalogBlockedError(
            f"catalog leaf count changed: {len(leaves)}",
            status=SOURCE_INCOMPLETE,
        )

    locations: set[tuple[str, str]] = set()
    by_identity: dict[str, tuple[str, str, str, str]] = {}
    identity_counts: dict[str, int] = {}
    code_owner: dict[str, str] = {}
    path_owner: dict[str, str] = {}
    for leaf in leaves:
        location = (leaf["parentCode"], leaf["code"])
        if location in locations:
            raise CninfoApiCatalogBlockedError("duplicate catalog leaf location")
        locations.add(location)
        identity = leaf["name"]
        metadata = (
            leaf["alias"],
            leaf["requestPath"],
            leaf["code"],
            leaf["offerName"],
        )
        previous = by_identity.setdefault(identity, metadata)
        if previous != metadata:
            raise CninfoApiCatalogBlockedError(
                f"catalog identity {identity} has conflicting duplicates"
            )
        if code_owner.setdefault(leaf["code"], identity) != identity:
            raise CninfoApiCatalogBlockedError("catalog code is assigned twice")
        if path_owner.setdefault(leaf["requestPath"], identity) != identity:
            raise CninfoApiCatalogBlockedError("catalog request path is assigned twice")
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    if len(by_identity) != EXPECTED_UNIQUE_INTERFACE_COUNT:
        raise CninfoApiCatalogBlockedError(
            f"catalog unique-interface count changed: {len(by_identity)}",
            status=SOURCE_INCOMPLETE,
        )

    selected: list[CatalogInterfaceRecord] = []
    for spec in REQUIRED_INTERFACE_SPECS:
        actual = by_identity.get(spec.identity)
        expected = (spec.alias, spec.request_path, spec.code, INTERNAL_OFFER_NAME)
        if actual != expected:
            raise CninfoApiCatalogBlockedError(
                f"required catalog interface identity drift: {spec.identity}"
            )
        count = identity_counts.get(spec.identity, 0)
        if count != spec.occurrence_count:
            raise CninfoApiCatalogBlockedError(
                f"required catalog interface occurrence drift: {spec.identity}"
            )
        selected.append(
            CatalogInterfaceRecord(
                identity=spec.identity,
                alias=spec.alias,
                request_path=spec.request_path,
                code=spec.code,
                offer_name=INTERNAL_OFFER_NAME,
                occurrence_count=count,
            )
        )

    logical_rows = sorted(
        (
            {
                "parentCode": leaf["parentCode"],
                "identity": leaf["name"],
                "alias": leaf["alias"],
                "requestPath": leaf["requestPath"],
                "code": leaf["code"],
                "offerName": leaf["offerName"],
            }
            for leaf in leaves
        ),
        key=lambda value: (
            value["parentCode"],
            value["identity"],
            value["code"],
        ),
    )
    return _ParsedCatalog(
        interfaces=tuple(selected),
        leaf_count=len(leaves),
        unique_interface_count=len(by_identity),
        branch_count=branch_count,
        logical_content_sha256=_sha256(_canonical_json_bytes(logical_rows)),
    )


def _validate_branch(
    node: Mapping[str, Any], *, parent_code: str, depth: int
) -> None:
    if node["parentCode"] != parent_code:
        raise CninfoApiCatalogBlockedError("catalog branch parent linkage changed")
    if not re.fullmatch(r"[A-Za-z0-9]{32}", str(node["code"])):
        raise CninfoApiCatalogBlockedError("catalog branch code is invalid")
    if type(node["id"]) is not int or node["id"] <= 0:
        raise CninfoApiCatalogBlockedError("catalog branch id is invalid")
    if not isinstance(node["children"], list) or not node["children"]:
        raise CninfoApiCatalogBlockedError("catalog branch has no children")
    if not isinstance(node["name"], str) or not node["name"].strip():
        raise CninfoApiCatalogBlockedError("catalog branch name is invalid")
    if type(node["display"]) is not int or node["display"] not in {0, 1}:
        raise CninfoApiCatalogBlockedError("catalog branch display flag is invalid")
    if node["level"] is not None and type(node["level"]) is not int:
        raise CninfoApiCatalogBlockedError("catalog branch level is invalid")
    if depth == 0 and node["level"] != 0:
        raise CninfoApiCatalogBlockedError("catalog root level changed")
    for field_name in ("sort", "createTime", "updateTime", "del"):
        if type(node[field_name]) is not int:
            raise CninfoApiCatalogBlockedError(
                f"catalog branch {field_name} type changed"
            )
    if node["del"] not in {0, 1} or type(node["run"]) is not bool:
        raise CninfoApiCatalogBlockedError("catalog branch state is invalid")
    for field_name in ("createUser", "updateUser"):
        if not isinstance(node[field_name], str) or not node[field_name]:
            raise CninfoApiCatalogBlockedError(
                f"catalog branch {field_name} is invalid"
            )


def _validate_leaf(
    node: Mapping[str, Any], *, parent_code: str
) -> dict[str, str]:
    if node["parentCode"] != parent_code:
        raise CninfoApiCatalogBlockedError("catalog leaf parent linkage changed")
    if not re.fullmatch(r"[0-9a-f]{32}", str(node["code"])):
        raise CninfoApiCatalogBlockedError("catalog leaf code is invalid")
    identity = node["name"]
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_]+", identity):
        raise CninfoApiCatalogBlockedError("catalog leaf identity is invalid")
    request_path = node["requestPath"]
    if (
        not isinstance(request_path, str)
        or not re.fullmatch(r"/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+", request_path)
        or not request_path.rstrip("/").endswith(f"/{identity}")
    ):
        raise CninfoApiCatalogBlockedError("catalog leaf request path is invalid")
    for field_name in ("alias", "offerName"):
        value = node[field_name]
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(ord(character) < 32 for character in value)
        ):
            raise CninfoApiCatalogBlockedError(
                f"catalog leaf {field_name} is invalid"
            )
    if any(node[field_name] is not None for field_name in ("apiType", "serverName", "createTime")):
        raise CninfoApiCatalogBlockedError("catalog leaf nullable fields changed")
    return {
        "parentCode": str(node["parentCode"]),
        "name": identity,
        "alias": node["alias"],
        "requestPath": request_path,
        "code": node["code"],
        "offerName": node["offerName"],
    }


def _artifact_from_parsed(
    parsed: _ParsedCatalog,
    *,
    raw_catalog: RawCatalogEvidence,
    retrieved_at: str,
) -> CninfoApiCatalogArtifact:
    return CninfoApiCatalogArtifact(
        retrieved_at=_normalize_timestamp(retrieved_at),
        raw_catalog=raw_catalog,
        interfaces=parsed.interfaces,
        leaf_count=parsed.leaf_count,
        unique_interface_count=parsed.unique_interface_count,
        branch_count=parsed.branch_count,
        logical_content_sha256=parsed.logical_content_sha256,
        _seal=_BUILDER_SEAL,
    )


def _manifest_payload(artifact: CninfoApiCatalogArtifact) -> dict[str, Any]:
    value = artifact.to_dict()
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        **value,
    }


def _rebuild_from_manifest(
    payload: Mapping[str, Any], *, cas: CninfoApiCatalogCAS
) -> CninfoApiCatalogArtifact:
    if set(payload) != _MANIFEST_FIELDS:
        raise CninfoApiCatalogBlockedError("catalog manifest schema drift")
    if (
        payload["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION
        or payload["protocol_version"] != PROTOCOL_VERSION
    ):
        raise CninfoApiCatalogBlockedError("catalog manifest protocol drift")
    raw_value = payload["raw_catalog"]
    if not isinstance(raw_value, dict) or set(raw_value) != _RAW_EVIDENCE_FIELDS:
        raise CninfoApiCatalogBlockedError("catalog raw-evidence schema drift")
    evidence = _raw_evidence_from_manifest(raw_value, cas=cas)
    content, _path = cas.read_blob(evidence.content_sha256)
    parsed = _parse_catalog(content)
    rebuilt = _artifact_from_parsed(
        parsed,
        raw_catalog=evidence,
        retrieved_at=_normalize_timestamp(payload["retrieved_at"]),
    )
    if _manifest_payload(rebuilt) != dict(payload):
        raise CninfoApiCatalogBlockedError(
            "catalog manifest aggregate does not match raw CAS recomputation"
        )
    return rebuilt


def _raw_evidence_from_manifest(
    value: Mapping[str, Any], *, cas: CninfoApiCatalogCAS
) -> RawCatalogEvidence:
    if (
        value["source_id"] != "CNINFO_API_DOC_TREE_TYPE_2"
        or value["source_url"] != CNINFO_API_CATALOG_URL
        or value["method"] != "GET"
        or value["content_type"] != JSON_MEDIA_TYPE
        or type(value["http_status"]) is not int
        or value["http_status"] != 200
        or type(value["byte_count"]) is not int
        or not 0 < value["byte_count"] <= MAX_CATALOG_BYTES
        or not isinstance(value["content_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["content_sha256"])
        or value["cas_uri"] != f"sha256:{value['content_sha256']}"
    ):
        raise CninfoApiCatalogBlockedError("catalog raw-evidence metadata is invalid")
    content, path = cas.read_blob(value["content_sha256"])
    if len(content) != value["byte_count"] or str(path) != value["object_path"]:
        raise CninfoApiCatalogBlockedError("catalog raw-evidence CAS binding changed")
    return RawCatalogEvidence(
        source_id=value["source_id"],
        source_url=value["source_url"],
        method=value["method"],
        retrieved_at=_normalize_timestamp(value["retrieved_at"]),
        content_sha256=value["content_sha256"],
        byte_count=value["byte_count"],
        content_type=value["content_type"],
        http_status=value["http_status"],
        cas_uri=value["cas_uri"],
        object_path=value["object_path"],
    )


def _validate_catalog_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != CATALOG_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        != "/api-cloud-gateway-manage/apiDoc/apiDocTree"
        or parsed.query != "type=2"
        or parsed.fragment
    ):
        raise CninfoApiCatalogBlockedError("catalog URL is outside the fixed contract")


def _strict_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    if not content.startswith(b"{") or content.startswith(b"\xef\xbb\xbf"):
        raise CninfoApiCatalogBlockedError(f"{label} is not strict UTF-8 JSON")

    def reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CninfoApiCatalogBlockedError(
                    f"{label} contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_raise_invalid_number(label, token)),
        )
    except CninfoApiCatalogBlockedError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CninfoApiCatalogBlockedError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CninfoApiCatalogBlockedError(f"{label} is not a JSON object")
    return value


def _raise_invalid_number(label: str, token: str) -> None:
    raise CninfoApiCatalogBlockedError(
        f"{label} contains non-finite number {token}"
    )


def _media_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _normalize_timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CninfoApiCatalogBlockedError("invalid evidence timestamp") from exc
    else:
        raise CninfoApiCatalogBlockedError("invalid evidence timestamp type")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CninfoApiCatalogBlockedError("evidence timestamp lacks timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_file_attributes", 0)),
        int(getattr(value, "st_reparse_tag", 0)),
    )


def _is_reparse(value: os.stat_result) -> bool:
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400))
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0)) & reparse
    )


def _prepare_root(root: Path) -> None:
    current = root
    missing: list[Path] = []
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    while True:
        value = os.lstat(current)
        if _is_reparse(value):
            raise CninfoApiCatalogBlockedError("CAS root contains a reparse point")
        parent = current.parent
        if parent == current:
            break
        current = parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        value = os.lstat(path)
        if _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise CninfoApiCatalogBlockedError("CAS root creation is unsafe")
    _path_snapshot(root, root, leaf_is_file=False)


def _path_snapshot(
    root: Path, target: Path, *, leaf_is_file: bool
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    root = _lexical_absolute(root)
    target = _lexical_absolute(target)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise CninfoApiCatalogBlockedError("CAS path escapes root") from exc
    components = [root]
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise CninfoApiCatalogBlockedError("CAS path is invalid")
        current /= part
        components.append(current)
    result: list[tuple[str, tuple[int, ...]]] = []
    for index, component in enumerate(components):
        try:
            value = os.lstat(component)
        except OSError as exc:
            raise CninfoApiCatalogBlockedError(
                f"CAS path component is missing: {component}"
            ) from exc
        if _is_reparse(value):
            raise CninfoApiCatalogBlockedError("CAS path contains a reparse point")
        expect_file = leaf_is_file and index == len(components) - 1
        if expect_file:
            if not stat.S_ISREG(value.st_mode):
                raise CninfoApiCatalogBlockedError("CAS object is not a regular file")
        elif not stat.S_ISDIR(value.st_mode):
            raise CninfoApiCatalogBlockedError(
                "CAS path component is not a directory"
            )
        result.append((str(component), _fingerprint(value)))
    return tuple(result)


def _stable_read(root: Path, path: Path) -> bytes:
    before = _path_snapshot(root, path, leaf_is_file=True)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CninfoApiCatalogBlockedError("CAS object cannot be opened safely") from exc
    try:
        handle_before = os.fstat(descriptor)
        if _is_reparse(handle_before) or not stat.S_ISREG(handle_before.st_mode):
            raise CninfoApiCatalogBlockedError("CAS handle is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        handle_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = _path_snapshot(root, path, leaf_is_file=True)
    if (
        before != after
        or _fingerprint(handle_before) != _fingerprint(handle_after)
        or _fingerprint(handle_before) != before[-1][1]
    ):
        raise CninfoApiCatalogBlockedError("CAS object changed during stable read")
    return b"".join(chunks)


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    root = _lexical_absolute(root)
    path = _lexical_absolute(path)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CninfoApiCatalogBlockedError("CAS write escapes root") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    _path_snapshot(root, path.parent, leaf_is_file=False)
    if path.exists():
        if _stable_read(root, path) != content:
            raise CninfoApiCatalogBlockedError("immutable CAS collision")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | int(getattr(os, "O_BINARY", 0))
            | int(getattr(os, "O_NOFOLLOW", 0)),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _path_snapshot(root, temporary, leaf_is_file=True)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _stable_read(root, path) != content:
                raise CninfoApiCatalogBlockedError("immutable CAS collision")
        except OSError as exc:
            raise CninfoApiCatalogBlockedError(
                "CAS cannot publish an immutable object"
            ) from exc
        if _stable_read(root, path) != content:
            raise CninfoApiCatalogBlockedError("published CAS verification failed")
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "AUTHORIZATION_NOTE",
    "CATALOG_STATUS",
    "CNINFO_API_CATALOG_URL",
    "EXPECTED_LEAF_COUNT",
    "EXPECTED_UNIQUE_INTERFACE_COUNT",
    "INTERNAL_OFFER_NAME",
    "PROTOCOL_VERSION",
    "REQUIRED_INTERFACE_SPECS",
    "CatalogInterfaceRecord",
    "CatalogInterfaceSpec",
    "CninfoApiCatalogArtifact",
    "CninfoApiCatalogBlockedError",
    "CninfoApiCatalogCAS",
    "CninfoApiCatalogClient",
    "CninfoApiCatalogManifestReference",
    "CninfoApiCatalogManifestStore",
    "RawCatalogEvidence",
]
