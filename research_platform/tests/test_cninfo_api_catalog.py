from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research_platform import cninfo_api_catalog as catalog


FIXED_NOW = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)


def _branch(
    branch_id: int,
    code: str,
    parent_code: str,
    children: list[dict[str, object]],
    *,
    level: int | None = None,
) -> dict[str, object]:
    return {
        "id": branch_id,
        "code": code,
        "parentCode": parent_code,
        "children": children,
        "type": 1,
        "name": f"Fixture branch {branch_id}",
        "display": 1,
        "level": level,
        "sort": branch_id,
        "createTime": 1_567_566_392_000,
        "updateTime": 1_761_014_861_000,
        "createUser": "fixture",
        "updateUser": "fixture",
        "del": 0,
        "run": True,
    }


def _leaf(
    *,
    identity: str,
    alias: str,
    request_path: str,
    code: str,
    parent_code: str,
    offer_name: str = catalog.INTERNAL_OFFER_NAME,
) -> dict[str, object]:
    return {
        "code": code,
        "name": identity,
        "apiType": None,
        "alias": alias,
        "parentCode": parent_code,
        "requestPath": request_path,
        "serverName": None,
        "offerName": offer_name,
        "createTime": None,
        "type": 0,
    }


def _catalog_payload() -> dict[str, object]:
    root_code = "a" * 32
    group_codes = ("b" * 32, "c" * 32, "d" * 32)
    identities: dict[str, dict[str, str]] = {
        spec.identity: {
            "identity": spec.identity,
            "alias": spec.alias,
            "request_path": spec.request_path,
            "code": spec.code,
        }
        for spec in catalog.REQUIRED_INTERFACE_SPECS
    }
    filler_count = catalog.EXPECTED_UNIQUE_INTERFACE_COUNT - len(identities)
    filler_names: list[str] = []
    for index in range(filler_count):
        identity = f"fixture_{index:04d}"
        filler_names.append(identity)
        identities[identity] = {
            "identity": identity,
            "alias": f"Fixture interface {index}",
            "request_path": f"fixture/{identity}",
            "code": f"{100_000 + index:032x}",
        }

    def make(identity: str, parent_code: str) -> dict[str, object]:
        value = identities[identity]
        return _leaf(parent_code=parent_code, **value)

    primary = [make(identity, group_codes[0]) for identity in identities]
    duplicate = [
        make(spec.identity, group_codes[1])
        for spec in catalog.REQUIRED_INTERFACE_SPECS
        if spec.occurrence_count >= 2
    ]
    duplicate.extend(
        make(identity, group_codes[1]) for identity in filler_names[:375]
    )
    third = [
        make(spec.identity, group_codes[2])
        for spec in catalog.REQUIRED_INTERFACE_SPECS
        if spec.occurrence_count >= 3
    ]
    groups = [
        _branch(2, group_codes[0], root_code, primary),
        _branch(3, group_codes[1], root_code, duplicate),
        _branch(4, group_codes[2], root_code, third),
    ]
    root = _branch(1, root_code, "0", groups, level=0)
    return {"code": "000000", "msg": "success", "data": [root]}


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


class _Response:
    def __init__(
        self,
        content: bytes,
        *,
        url: str = catalog.CNINFO_API_CATALOG_URL,
        content_type: str = "application/json;charset=UTF-8",
        status_code: int = 200,
        location: str | None = None,
        history: tuple[object, ...] = (),
    ) -> None:
        self.content = content
        self.url = url
        self.status_code = status_code
        self.history = history
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
        }
        if location is not None:
            self.headers["Location"] = location


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return self.response


class CninfoApiCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _catalog_payload()
        cls.content = _json_bytes(cls.payload)

    def _client(
        self,
        root: Path,
        *,
        content: bytes | None = None,
        response: _Response | None = None,
    ) -> tuple[catalog.CninfoApiCatalogClient, _Session]:
        response = response or _Response(content or self.content)
        session = _Session(response)
        client = catalog.CninfoApiCatalogClient(
            cas=catalog.CninfoApiCatalogCAS(root),
            session=session,  # type: ignore[arg-type]
            clock=lambda: FIXED_NOW,
        )
        return client, session

    def test_fetch_discovers_fixed_interfaces_without_authorizing_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client, session = self._client(Path(directory))
            artifact = client.fetch()

        self.assertEqual(artifact.leaf_count, 2_466)
        self.assertEqual(artifact.unique_interface_count, 2_080)
        self.assertEqual(len(artifact.interfaces), 13)
        self.assertFalse(artifact.source_contract["ready"])
        self.assertEqual(artifact.source_contract["status"], catalog.CATALOG_STATUS)
        self.assertEqual(artifact.source_contract["protected_endpoint_calls"], 0)
        self.assertFalse(artifact.source_contract["authorization_proven"])
        self.assertFalse(artifact.source_contract["free_access_proven"])
        for item in artifact.interfaces:
            value = item.to_dict()
            self.assertEqual(value["status"], catalog.CATALOG_STATUS)
            self.assertFalse(value["ready"])
            self.assertEqual(value["offerName"], catalog.INTERNAL_OFFER_NAME)
            self.assertEqual(value["authorization_note"], catalog.AUTHORIZATION_NOTE)
        self.assertEqual(len(session.calls), 1)
        called_url, kwargs = session.calls[0]
        self.assertEqual(called_url, catalog.CNINFO_API_CATALOG_URL)
        self.assertIs(kwargs["allow_redirects"], False)
        headers = kwargs["headers"]
        self.assertNotIn("Authorization", headers)  # type: ignore[operator]
        self.assertNotIn("Cookie", headers)  # type: ignore[operator]

    def test_manifest_cold_replays_from_raw_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = catalog.CninfoApiCatalogCAS(Path(directory))
            session = _Session(_Response(self.content))
            artifact = catalog.CninfoApiCatalogClient(
                cas=cas,
                session=session,  # type: ignore[arg-type]
                clock=lambda: FIXED_NOW,
            ).fetch()
            store = catalog.CninfoApiCatalogManifestStore(cas)
            reference = store.seal(artifact)
            replayed = store.replay(reference.manifest_sha256)

        self.assertEqual(replayed.to_dict(), artifact.to_dict())
        self.assertEqual(reference.cas_uri, f"sha256:{reference.manifest_sha256}")

    def test_schema_drift_fails_closed(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["unexpected"] = True
        with tempfile.TemporaryDirectory() as directory:
            client, _session = self._client(
                Path(directory), content=_json_bytes(payload)
            )
            with self.assertRaisesRegex(
                catalog.CninfoApiCatalogBlockedError, "schema drift"
            ):
                client.fetch()

    def test_required_interface_identity_drift_fails_closed(self) -> None:
        payload = copy.deepcopy(self.payload)
        primary = payload["data"][0]["children"][0]["children"]  # type: ignore[index]
        target = next(item for item in primary if item["name"] == "p_stock2406")
        target["alias"] = "Changed alias"
        with tempfile.TemporaryDirectory() as directory:
            client, _session = self._client(
                Path(directory), content=_json_bytes(payload)
            )
            with self.assertRaisesRegex(
                catalog.CninfoApiCatalogBlockedError, "identity drift"
            ):
                client.fetch()

    def test_duplicate_leaf_location_fails_closed(self) -> None:
        payload = copy.deepcopy(self.payload)
        primary = payload["data"][0]["children"][0]["children"]  # type: ignore[index]
        primary[1] = copy.deepcopy(primary[0])
        with tempfile.TemporaryDirectory() as directory:
            client, _session = self._client(
                Path(directory), content=_json_bytes(payload)
            )
            with self.assertRaisesRegex(
                catalog.CninfoApiCatalogBlockedError, "duplicate catalog leaf location"
            ):
                client.fetch()

    def test_duplicate_json_key_fails_closed(self) -> None:
        content = self.content.replace(
            b'{"code":"000000",',
            b'{"code":"000000","code":"000000",',
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            client, _session = self._client(Path(directory), content=content)
            with self.assertRaisesRegex(
                catalog.CninfoApiCatalogBlockedError, "duplicate JSON key"
            ):
                client.fetch()

    def test_caller_cannot_forge_ready_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client, session = self._client(Path(directory))
            with self.assertRaisesRegex(
                catalog.CninfoApiCatalogBlockedError, "cannot be promoted"
            ) as context:
                client.fetch(caller_ready=True)

        self.assertEqual(context.exception.status, catalog.CATALOG_STATUS)
        self.assertEqual(session.calls, [])

    def test_manifest_ready_forgery_fails_cold_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = catalog.CninfoApiCatalogCAS(Path(directory))
            artifact = catalog.CninfoApiCatalogClient(
                cas=cas,
                session=_Session(_Response(self.content)),  # type: ignore[arg-type]
                clock=lambda: FIXED_NOW,
            ).fetch()
            store = catalog.CninfoApiCatalogManifestStore(cas)
            reference = store.seal(artifact)
            raw, _path = cas.read_blob(reference.manifest_sha256)
            payload = json.loads(raw)
            payload["source_contract"]["ready"] = True
            forged, _forged_path = cas.put_blob(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            with self.assertRaisesRegex(
                catalog.CninfoApiCatalogBlockedError, "aggregate"
            ):
                store.replay(forged)

    def test_raw_cas_tampering_blocks_manifest_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = catalog.CninfoApiCatalogCAS(Path(directory))
            artifact = catalog.CninfoApiCatalogClient(
                cas=cas,
                session=_Session(_Response(self.content)),  # type: ignore[arg-type]
                clock=lambda: FIXED_NOW,
            ).fetch()
            store = catalog.CninfoApiCatalogManifestStore(cas)
            reference = store.seal(artifact)
            raw_path = Path(artifact.raw_catalog.object_path)
            raw_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                catalog.CninfoApiCatalogBlockedError, "hash mismatch"
            ):
                store.replay(reference.manifest_sha256)

    def test_redirect_and_non_json_media_type_fail_closed(self) -> None:
        cases = (
            _Response(self.content, url="https://evil.example/catalog"),
            _Response(self.content, content_type="text/html"),
        )
        for response in cases:
            with self.subTest(response=response):
                with tempfile.TemporaryDirectory() as directory:
                    client, _session = self._client(
                        Path(directory), response=response
                    )
                    with self.assertRaises(catalog.CninfoApiCatalogBlockedError):
                        client.fetch()


if __name__ == "__main__":
    unittest.main()
