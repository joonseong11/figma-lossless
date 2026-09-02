from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from figma_lossless.collector import (
    CollectOptions,
    CollectorError,
    FigmaCollector,
    HTTPResponse,
    _parse_token_file,
    resolve_token,
)
from figma_lossless.util import read_json, sha256_file, write_json


def file_body(version: str, name: str = "Demo File") -> bytes:
    return json.dumps({"version": version, "name": name}).encode("utf-8")


def node_body(node_id: str) -> bytes:
    return json.dumps(
        {"nodes": {node_id: {"document": {"id": node_id, "type": "FRAME"}}}}
    ).encode("utf-8")


class FakeTransport:
    """Records every request and answers it via a test-supplied handler."""

    def __init__(self, handler):
        self.calls: list[str] = []
        self.headers_seen: list[dict[str, str]] = []
        self._handler = handler

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        self.calls.append(url)
        self.headers_seen.append(dict(headers))
        return self._handler(url)


class CollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "evidence"
        self.token_file = self.root / "token"
        self.token_file.write_text("figd_test_token", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _options(self, transport, node_ids, **overrides) -> CollectOptions:
        kwargs = dict(
            file_key="ABC123",
            output_dir=self.output,
            node_ids=node_ids,
            transport=transport,
            token_file=self.token_file,
            sleep=lambda seconds: None,
        )
        kwargs.update(overrides)
        return CollectOptions(**kwargs)

    # ---- happy path -----------------------------------------------------

    def test_happy_path_two_nodes(self) -> None:
        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                self.assertEqual(params["version"], ["42"])
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            self.assertEqual(parsed.path, "/v1/files/ABC123")
            return HTTPResponse(200, {}, file_body(version="42"))

        transport = FakeTransport(handler)
        options = self._options(transport, ["22143:63319", "22143-63320"])
        manifest = FigmaCollector(options).collect()

        self.assertTrue(manifest["complete"])
        self.assertEqual(
            manifest["expectedNodeIds"], ["22143:63319", "22143:63320"]
        )
        self.assertEqual(
            manifest["collectedNodeIds"], ["22143:63319", "22143:63320"]
        )
        self.assertEqual(manifest["fileVersion"], "42")
        self.assertEqual(manifest["failures"], [])

        for node_id in manifest["expectedNodeIds"]:
            entry = manifest["perNode"][node_id]
            node_path = self.output / entry["path"]
            self.assertTrue(node_path.exists())
            self.assertEqual(entry["sha256"], sha256_file(node_path))
            self.assertEqual(entry["httpStatus"], 200)

        dash_path = self.output / "rest" / "nodes" / "22143-63320.json"
        self.assertTrue(dash_path.exists())

        on_disk = read_json(self.output / "rest" / "collection-manifest.json")
        self.assertEqual(on_disk, manifest)

    # ---- node-level err payload ------------------------------------------

    def test_node_error_payload_marks_incomplete(self) -> None:
        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                node_id = params["ids"][0]
                if node_id == "1:2":
                    return HTTPResponse(
                        200, {}, json.dumps({"err": "Node not found"}).encode("utf-8")
                    )
                return HTTPResponse(200, {}, node_body(node_id))
            return HTTPResponse(200, {}, file_body(version="3"))

        transport = FakeTransport(handler)
        options = self._options(transport, ["1:1", "1:2"])
        manifest = FigmaCollector(options).collect()

        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["collectedNodeIds"], ["1:1"])
        failure = next(item for item in manifest["failures"] if item["id"] == "1:2")
        self.assertIn("Node not found", failure["reason"])
        # Mirrors the CLI's exit-code mapping: incomplete runs must exit 2.
        exit_code = 0 if manifest["complete"] else 2
        self.assertEqual(exit_code, 2)

    # ---- token parsing -----------------------------------------------------

    def test_parse_token_file_bare_token(self) -> None:
        self.assertEqual(_parse_token_file("figd_bare_token"), "figd_bare_token")

    def test_parse_token_file_key_value_form(self) -> None:
        self.assertEqual(
            _parse_token_file("FIGMA_TOKEN=figd_kv_token"), "figd_kv_token"
        )

    def test_parse_token_file_trailing_newline_and_quotes(self) -> None:
        self.assertEqual(
            _parse_token_file('FIGMA_TOKEN="figd_quoted_token"\n'), "figd_quoted_token"
        )
        self.assertEqual(_parse_token_file("figd_bare_token\n\n"), "figd_bare_token")

    def test_parse_token_file_picks_the_figma_entry_out_of_a_shared_env(
        self,
    ) -> None:
        # Taking the first value would ship the AWS secret to api.figma.com.
        content = (
            "# deploy credentials\n"
            "AWS_SECRET_ACCESS_KEY=aws_super_secret_value\n"
            "DATABASE_URL=postgres://user:pw@host/db\n"
            "FIGMA_TOKEN=figd_the_only_right_one\n"
        )
        self.assertEqual(_parse_token_file(content), "figd_the_only_right_one")

    def test_parse_token_file_accepts_the_personal_access_token_name(self) -> None:
        self.assertEqual(
            _parse_token_file(
                "OPENAI_API_KEY=sk-nope\nFIGMA_PERSONAL_ACCESS_TOKEN=figd_pat\n"
            ),
            "figd_pat",
        )

    def test_parse_token_file_accepts_figma_scoped_token_names(self) -> None:
        self.assertEqual(
            _parse_token_file("FIGMA_READ_TOKEN=figd_read\n"), "figd_read"
        )
        self.assertEqual(
            _parse_token_file("FIGMA_API_TOKEN=figd_api\n"), "figd_api"
        )

    def test_parse_token_file_refuses_figma_named_non_token_keys(self) -> None:
        with self.assertRaises(CollectorError):
            _parse_token_file("FIGMA_SECRET=not_a_token_name\n")

    def test_parse_token_file_refuses_a_file_with_no_figma_entry(self) -> None:
        with self.assertRaises(CollectorError):
            _parse_token_file("AWS_SECRET_ACCESS_KEY=aws_super_secret_value\n")

    def test_parse_token_file_accepts_a_shell_export_prefix(self) -> None:
        self.assertEqual(
            _parse_token_file("export FIGMA_TOKEN=figd_exported\n"),
            "figd_exported",
        )
        self.assertEqual(
            _parse_token_file('  export   FIGMA_TOKEN="figd_spaced"  \n'),
            "figd_spaced",
        )

    def test_parse_token_file_still_refuses_an_exported_foreign_key(self) -> None:
        with self.assertRaises(CollectorError):
            _parse_token_file("export AWS_SECRET_ACCESS_KEY=aws_secret\n")

    def test_foreign_secret_is_never_sent_to_the_figma_api(self) -> None:
        env_file = self.root / "shared.env"
        env_file.write_text(
            "AWS_SECRET_ACCESS_KEY=aws_super_secret_value\n"
            "DATABASE_URL=postgres://user:pw@host/db\n",
            encoding="utf-8",
        )
        transport = FakeTransport(
            lambda url: HTTPResponse(200, {}, file_body(version="1"))
        )
        options = self._options(transport, ["1:1"], token_file=env_file)
        with self.assertRaises(CollectorError):
            FigmaCollector(options).collect()
        self.assertEqual(transport.calls, [])
        self.assertEqual(transport.headers_seen, [])
        self.assertNotIn(
            "aws_super_secret_value", json.dumps(transport.headers_seen)
        )

    def test_resolve_token_falls_back_to_env(self) -> None:
        missing = self.root / "does-not-exist"
        token = resolve_token(missing, env={"FIGMA_TOKEN": " figd_env_token \n"})
        self.assertEqual(token, "figd_env_token")

    def test_resolve_token_raises_without_file_or_env(self) -> None:
        missing = self.root / "does-not-exist"
        with self.assertRaises(CollectorError):
            resolve_token(missing, env={})

    def test_token_never_appears_in_written_evidence(self) -> None:
        secret_token_file = self.root / "secret-token"
        secret_token_file.write_text(
            "FIGMA_TOKEN=figd_super_secret_value\n", encoding="utf-8"
        )

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="1"))

        transport = FakeTransport(handler)
        options = self._options(
            transport, ["1:1"], token_file=secret_token_file
        )
        manifest = FigmaCollector(options).collect()
        self.assertTrue(manifest["complete"])

        self.assertNotIn("figd_super_secret_value", json.dumps(manifest))
        for path in self.output.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(b"figd_super_secret_value", content)

    # ---- retry behaviour -----------------------------------------------

    def test_retries_on_429_then_succeeds(self) -> None:
        node_calls = {"count": 0}
        sleeps: list[float] = []

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                node_calls["count"] += 1
                if node_calls["count"] == 1:
                    return HTTPResponse(429, {"Retry-After": "2"}, b"")
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="7"))

        transport = FakeTransport(handler)
        options = self._options(transport, ["1:1"], sleep=sleeps.append)
        manifest = FigmaCollector(options).collect()

        self.assertTrue(manifest["complete"])
        self.assertEqual(node_calls["count"], 2)
        self.assertEqual(sleeps, [2.0])

    # ---- version pinning -------------------------------------------------

    def test_file_endpoint_called_first_and_version_pins_node_urls(self) -> None:
        order: list[str] = []

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                order.append("nodes")
                params = parse_qs(parsed.query)
                self.assertEqual(params["version"], ["99"])
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            order.append("file")
            return HTTPResponse(200, {}, file_body(version="99"))

        transport = FakeTransport(handler)
        options = self._options(transport, ["1:1", "1:2"])
        FigmaCollector(options).collect()

        self.assertEqual(order[0], "file")
        self.assertTrue(all(item == "nodes" for item in order[1:]))
        self.assertEqual(len(order), 3)

    # ---- resume ----------------------------------------------------------

    def test_resume_skips_already_collected_nodes(self) -> None:
        node_calls = {"count": 0}
        file_calls = {"count": 0}

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                node_calls["count"] += 1
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            file_calls["count"] += 1
            return HTTPResponse(200, {}, file_body(version="5"))

        transport = FakeTransport(handler)
        first = FigmaCollector(
            self._options(transport, ["1:1", "1:2"])
        ).collect()
        self.assertTrue(first["complete"])
        self.assertEqual(node_calls["count"], 2)
        self.assertEqual(file_calls["count"], 1)

        second = FigmaCollector(
            self._options(transport, ["1:1", "1:2"])
        ).collect()
        self.assertTrue(second["complete"])
        # No additional node fetches: both nodes resumed from disk.
        self.assertEqual(node_calls["count"], 2)
        # The file endpoint is always re-checked to (re)pin the version.
        self.assertEqual(file_calls["count"], 2)

    def test_resume_refetches_everything_when_version_changes(self) -> None:
        node_calls = {"count": 0}
        version = {"current": "1"}

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                node_calls["count"] += 1
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version=version["current"]))

        transport = FakeTransport(handler)
        FigmaCollector(self._options(transport, ["1:1"])).collect()
        self.assertEqual(node_calls["count"], 1)

        version["current"] = "2"
        second = FigmaCollector(self._options(transport, ["1:1"])).collect()
        self.assertTrue(second["complete"])
        self.assertEqual(node_calls["count"], 2)
        self.assertEqual(second["fileVersion"], "2")

    # ---- node id sourcing --------------------------------------------------

    def test_union_of_cli_node_ids_and_manifest(self) -> None:
        manifest_path = self.root / "expected.json"
        write_json(manifest_path, {"expectedNodeIds": ["9:9", "1-1"]})

        def handler(url: str) -> HTTPResponse:
            parsed = urlsplit(url)
            if parsed.path.endswith("/nodes"):
                params = parse_qs(parsed.query)
                return HTTPResponse(200, {}, node_body(params["ids"][0]))
            return HTTPResponse(200, {}, file_body(version="1"))

        transport = FakeTransport(handler)
        options = self._options(
            transport, ["1:1", "2:2"], manifest_path=manifest_path
        )
        manifest = FigmaCollector(options).collect()
        self.assertEqual(manifest["expectedNodeIds"], ["1:1", "2:2", "9:9"])

    def test_empty_node_id_union_raises(self) -> None:
        options = self._options(FakeTransport(lambda url: HTTPResponse(200, {}, b"{}")), [])
        with self.assertRaises(CollectorError):
            FigmaCollector(options).collect()


if __name__ == "__main__":
    unittest.main()
