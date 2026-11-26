"""Tests for sql_runner_client, Step 1 read-only preflight/sampler, and Step 3 runner backfill."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLE_CHECKOUT = Path("/home/agent/projects/cle-law-and-context")
sys.path.insert(0, str(REPO_ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient, SqlRunnerError  # noqa: E402


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRunnerServer:
    """In-process HMAC-validating fake sql-runner for deterministic tests."""

    def __init__(self, token: str, handler_fn):
        self.token = token
        self.handler_fn = handler_fn
        self.requests: list[tuple[str, dict]] = []
        self.nonces: set[str] = set()
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_GET(self):
                if self.path == "/healthz":
                    payload = {
                        "ok": True,
                        "enabled": True,
                        "writes_enabled": False,
                        "max_rows": 1000,
                        "configured_keys": 2,
                    }
                    raw = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                ts = self.headers.get("X-SQL-Runner-Timestamp", "")
                nonce = self.headers.get("X-SQL-Runner-Nonce", "")
                sig = self.headers.get("X-SQL-Runner-Signature", "")
                assert sig.startswith("v1=")
                assert nonce not in server_ref.nonces
                server_ref.nonces.add(nonce)
                msg = "\n".join(
                    ["POST", self.path, ts, nonce, hashlib.sha256(body).hexdigest()]
                ).encode()
                expected = hmac.new(
                    server_ref.token.encode(), msg, hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(sig[3:], expected):
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"invalid_signature"}')
                    return
                payload = json.loads(body.decode())
                server_ref.requests.append((self.path, payload))
                status, resp = server_ref.handler_fn(self.path, payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp).encode())

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def test_read_only_client_refuses_execute():
    srv = FakeRunnerServer("tok-secret", lambda path, payload: (200, {"ok": True, "rows": []}))
    try:
        client = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=True)
        assert client.healthz()["writes_enabled"] is False
        assert client.query_rows("SELECT 1") == []
        with pytest.raises(PermissionError):
            client.execute("INSERT INTO x VALUES (1)", dry_run=False)
        assert all(path == "/query" for path, _ in srv.requests)
    finally:
        srv.close()


def test_execute_unnest_chunks_bisects_on_timeout():
    executed_sizes: list[int] = []

    def handle(path, payload):
        assert path == "/execute"
        assert payload["confirm"] == "execute-cle-v2"
        n = len(payload["params"][0])
        if n > 2:
            return 400, {"detail": {"error": "QueryCanceled", "message": "statement timeout"}}
        executed_sizes.append(n)
        return 200, {"ok": True, "row_count": n, "rows": []}

    srv = FakeRunnerServer("tok-secret", handle)
    try:
        client = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=False)
        written = client.execute_unnest_chunks(
            "INSERT INTO t SELECT * FROM unnest(%s::int[])",
            [[1, 2, 3, 4, 5]],
            chunk_size=5,
            dry_run=False,
        )
        assert written == 5
        assert executed_sizes == [2, 1, 2]
    finally:
        srv.close()


def test_step1_preflight_and_sampler_strictly_read_only(tmp_path):
    mod = _load_module(
        "preflight_and_sample_via_runner",
        REPO_ROOT / "migration" / "verify" / "preflight_and_sample_via_runner.py",
    )

    def handle(path, payload):
        assert path == "/query", f"Step 1 must never call {path}"
        sql = payload["sql"]
        if "information_schema.tables" in sql:
            return 200, {"ok": True, "rows": []}
        if "FROM case_citation" in sql and "GROUP BY source_dataset" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {"source_dataset": "rs_body_cite", "n": 1000, "resolved": 950},
                    {"source_dataset": "cellar_sparql", "n": 500, "resolved": 400},
                ],
            }
        if "FROM case_law_reference" in sql and "GROUP BY source_dataset" in sql:
            return 200, {
                "ok": True,
                "rows": [{"source_dataset": "rs_lido_sqlite", "n": 81796, "with_leg": 0, "with_prov": 0}],
            }
        if "FROM case_law_reference" in sql:
            return 200, {"ok": True, "rows": [{"n": 0}]}
        if "FROM pg_class" in sql:
            return 200, {
                "ok": True,
                "rows": [{"table_name": "case_citation", "est_rows": 1500, "total_size": "1 MB"}],
            }
        if "FROM case_citation_counts" in sql:
            return 200, {"ok": True, "rows": [{"rows": 100, "sum_cites": 200, "sum_cited_by": 200}]}
        if "to_regclass('case_cited_by')" in sql:
            return 200, {"ok": True, "rows": [{"present": True}]}
        if "FROM case_cited_by" in sql:
            return 200, {"ok": True, "rows": [{"rows": 100, "sum_cited_by": 180, "nonzero_cases": 50}]}
        if "'RS' = ANY(c.sources)" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 10,
                        "ecli": "ECLI:NL:RVS:2020:423",
                        "date_decision": "2020-02-12",
                        "language": "nl",
                        "fulltext": (
                            "Zie het arrest van het Hof van Justitie van 12 maart 2019, Tjebbes, "
                            "zaak C-221/17, ECLI:EU:C:2019:189, en artikel 267 VWEU en "
                            "Richtlijn 2011/95/EU. Vergelijk Kamerstukken II 2015/16, 34 356, nr. 3. "
                            "Beslissing: De Afdeling verklaart het hoger beroep ongegrond."
                        ),
                        "summary": None,
                    }
                ],
            }
        if "'ECHR' = ANY(c.sources)" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 20,
                        "ecli": "ECLI:CE:ECHR:2021:0511JUD001234519",
                        "date_decision": "2021-05-11",
                        "item_id": "001-209999",
                        "conclusion": "Violation of Article 8 - Right to respect for private and family life",
                        "doctype_branch": "CHAMBER",
                        "language": "en",
                        "fulltext": "The Court of Justice held in Case C-221/17 (ECLI:EU:C:2019:189) and Directive 2011/95/EU.",
                    }
                ],
            }
        if "'CJEU' = ANY(c.sources)" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 30,
                        "ecli": "ECLI:EU:C:2019:189",
                        "celex_id": "62017CJ0221",
                        "date_decision": "2019-03-12",
                        "proc_type": "Reference for a preliminary ruling",
                        "language": "en",
                        "fulltext": "See ECtHR, 27 June 2000, Frydlender v. France, Application no. 30979/96.",
                    }
                ],
            }
        return 200, {"ok": True, "rows": []}

    srv = FakeRunnerServer("tok-secret", handle)
    try:
        client = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=True)
        pre = mod.run_preflight(client)
        assert pre["existing_new_objects"] == []
        assert pre["unseeded_legacy_citation_datasets"] == []
        assert pre["pre_0014_rs_lido_with_legislation_id"] == 0

        samples = mod.sample_extractors(client, CLE_CHECKOUT, docs_per_corpus=10, max_examples_per_bucket=10)
        assert samples["total_hits_in_sample"]["rs_text_ecli"] >= 1
        assert samples["total_hits_in_sample"]["rs_text_cjeu_casenum"] >= 1
        assert samples["total_hits_in_sample"]["rs_text_eu_law"] >= 1
        assert samples["total_hits_in_sample"]["rs_authorities"] >= 1
        assert samples["total_hits_in_sample"]["echr_text_cjeu_rs"] >= 1
        assert samples["total_hits_in_sample"]["echr_outcomes"] >= 1
        assert samples["total_hits_in_sample"]["cjeu_text_echr"] >= 1
        assert samples["total_hits_in_sample"]["cjeu_outcomes"] >= 1

        md = mod.render_markdown(pre, samples)
        assert "Step 1 Read-Only Pre-Flight" in md
        assert "ECLI:NL:RVS:2020:423" in md
        assert all(path == "/query" for path, _ in srv.requests)
    finally:
        srv.close()


def test_step3_case_identifier_runner_backfill():
    mod = _load_module(
        "backfill_case_identifier_via_runner",
        REPO_ROOT / "migration" / "sql" / "65_backfill_case_identifier_via_runner.py",
    )
    inserted_rows: list[tuple] = []

    def handle(path, payload):
        sql = payload["sql"]
        if path == "/execute":
            cols = payload["params"]
            rows = list(zip(*cols))
            inserted_rows.extend(rows)
            return 200, {"ok": True, "row_count": len(rows), "rows": []}
        if "to_regclass('case_identifier')" in sql:
            return 200, {"ok": True, "rows": [{"ok": True}]}
        if "SELECT min(id) AS lo" in sql:
            return 200, {"ok": True, "rows": [{"lo": 1, "hi": 3}]}
        if "FROM cases" in sql:
            last_id = payload["params"][0]
            if last_id >= 3:
                return 200, {"ok": True, "rows": []}
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 1,
                        "ecli": "ECLI:EU:C:2013:720",
                        "celex_id": "62012CJ0199",
                        "case_number": "C-199/12 tot en met C-201/12",
                        "sources": ["CJEU"],
                    },
                    {
                        "id": 2,
                        "ecli": "ECLI:NL:RVS:2012:BW3076",
                        "celex_id": None,
                        "case_number": None,
                        "sources": ["RS"],
                    },
                    {
                        "id": 3,
                        "ecli": "ECLI:CE:ECHR:2000:0627JUD003097996",
                        "celex_id": None,
                        "case_number": "30979/96",
                        "sources": ["ECHR"],
                    },
                ],
            }
        if "FROM cjeu_document" in sql:
            return 200, {"ok": True, "rows": []}
        if "FROM rs_document" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "case_id": 2,
                        "zaaknummer": "201012342/1/T1/V2",
                        "replaces_identifier": "LJN: BW3076",
                    }
                ],
            }
        if "FROM echr_document ed" in sql:
            return 200, {"ok": True, "rows": [{"case_id": 3, "appno": "30979/96"}]}
        return 200, {"ok": True, "rows": []}

    srv = FakeRunnerServer("tok-secret", handle)
    try:
        # 1. Dry run must never call /execute
        ro_client = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=True)
        dry = mod.run_backfill(ro_client, CLE_CHECKOUT, dry_run=True)
        assert dry["cases_scanned"] == 3
        assert dry["rows_prepared"] >= 9
        assert dry["rows_inserted"] == 0
        assert not inserted_rows

        # 2. Write run inserts expanded ranges, zaaknummer variants, LJN, and appno
        rw_client = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=False)
        res = mod.run_backfill(rw_client, CLE_CHECKOUT, dry_run=False)
        assert res["rows_inserted"] == len(inserted_rows)
        triples = {(r[0], r[1], r[3]) for r in inserted_rows}
        assert (1, "cjeu_case_number", "C-199/12") in triples
        assert (1, "cjeu_case_number", "C-200/12") in triples
        assert (1, "cjeu_case_number", "C-201/12") in triples
        assert (2, "nl_zaaknummer", "201012342/1/T1/V2") in triples
        assert (2, "nl_zaaknummer", "201012342/T1/V2") in triples
        assert (2, "ljn", "BW3076") in triples
        assert (3, "echr_appno", "30979/96") in triples
    finally:
        srv.close()


def test_step3_links_runner_enforces_dark_guard():
    mod = _load_module(
        "backfill_links_via_runner",
        REPO_ROOT / "migration" / "sql" / "66_backfill_links_via_runner.py",
    )

    def handle_missing_0015(path, payload):
        if "schema_migrations" in payload["sql"]:
            return 200, {"ok": True, "rows": [{"ok": False}]}
        return 200, {"ok": True, "rows": []}

    srv = FakeRunnerServer("tok-secret", handle_missing_0015)
    try:
        client = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=False)
        with pytest.raises(RuntimeError, match="0015_citation_count_guard"):
            mod.assert_dark_guard(client, ("cjeu_ag_link",))
    finally:
        srv.close()

    def handle_counted_dataset(path, payload):
        if "schema_migrations" in payload["sql"]:
            return 200, {"ok": True, "rows": [{"ok": True}]}
        if "citation_source_dataset" in payload["sql"]:
            return 200, {
                "ok": True,
                "rows": [{"code": "cjeu_ag_link", "counted": True, "active_in_api": False}],
            }
        return 200, {"ok": True, "rows": []}

    srv2 = FakeRunnerServer("tok-secret", handle_counted_dataset)
    try:
        client = SqlRunnerClient(url=srv2.url, token="tok-secret", read_only=False)
        with pytest.raises(RuntimeError, match="NOT dark"):
            mod.assert_dark_guard(client, ("cjeu_ag_link",))
    finally:
        srv2.close()


def test_step3_ag_opinions_and_context_runner_backfills():
    links_mod = _load_module(
        "backfill_links_via_runner",
        REPO_ROOT / "migration" / "sql" / "66_backfill_links_via_runner.py",
    )
    ctx_mod = _load_module(
        "backfill_context_via_runner",
        REPO_ROOT / "migration" / "sql" / "67_backfill_context_via_runner.py",
    )
    writes: list[tuple[str, list]] = []

    def handle(path, payload):
        sql = payload["sql"]
        if path == "/execute":
            writes.append((sql, payload["params"]))
            return 200, {"ok": True, "row_count": len(payload["params"][0]), "rows": []}
        if "celex_id ~ '^6[0-9]{4}CC'" in sql:
            return 200, {"ok": True, "rows": [{"id": 100, "celex_id": "62017CC0221"}]}
        if "SELECT case_id AS id, celex_id" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {"id": 100, "celex_id": "62017CC0221"},
                    {"id": 101, "celex_id": "62017CJ0221"},
                ],
            }
        if "SELECT max(case_id) AS hi FROM cjeu_document" in sql:
            return 200, {"ok": True, "rows": [{"hi": 101}]}
        if "FROM cjeu_document" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "case_id": 101,
                        "celex_id": "62017CJ0221",
                        "proc_type": "Action for annulment - unfounded",
                    }
                ],
            }
        return 200, {"ok": True, "rows": []}

    srv = FakeRunnerServer("tok-secret", handle)
    try:
        cle_links = links_mod._load_cle(CLE_CHECKOUT)
        rw = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=False)
        ag_res = links_mod.run_ag_opinions(
            rw, cle_links, dry_run=False, chunk_size=100, explain_sql=False, max_cases=10
        )
        assert ag_res["inserted_resolved"] == 1

        cle_ctx = ctx_mod._load_cle(CLE_CHECKOUT)
        leg_res = ctx_mod.run_eu_legislation(
            rw, cle_ctx, dry_run=False, chunk_size=100, explain_sql=False
        )
        assert leg_res["acts_prepared"] == 63
        assert leg_res["acts_inserted"] == 63
        assert leg_res["aliases_inserted"] >= 100

        out_res = ctx_mod.run_cjeu_outcomes(
            rw, cle_ctx, from_id=0, to_id=None, page_size=100, chunk_size=100,
            dry_run=False, explain_sql=False, max_cases=10,
        )
        assert out_res["inserted"] == 1
    finally:
        srv.close()


def test_step3_text_links_modes_and_resolved_only():
    mod = _load_module(
        "backfill_links_via_runner",
        REPO_ROOT / "migration" / "sql" / "66_backfill_links_via_runner.py",
    )
    writes: list[tuple[str, list]] = []

    def handle(path, payload):
        sql = payload["sql"]
        if path == "/execute":
            writes.append((sql, payload["params"]))
            return 200, {"ok": True, "row_count": len(payload["params"][0]), "rows": []}
        if "SELECT min(" in sql and "AS lo, max(" in sql:
            return 200, {"ok": True, "rows": [{"lo": 1, "hi": 30}]}
        if "SELECT DISTINCT case_id" in sql and "FROM echr_document" in sql:
            return 200, {"ok": True, "rows": [{"case_id": 20}]}
        if "AND ct.source = 'RECHTSPRAAK'" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 10,
                        "ecli": "ECLI:NL:RVS:2020:423",
                        "celex_id": None,
                        "item_id": None,
                        "date_decision": "2020-02-12",
                        "sources": ["RS"],
                        "language": "nl",
                        "fulltext": (
                            "Zie Hof van Justitie 12 maart 2019, zaak C-221/17, ECLI:EU:C:2019:189, "
                            "en ECLI:EU:C:2099:999999 en EHRM nr. 30979/96."
                        ),
                        "summary": None,
                    }
                ],
            }
        if "AND ct.source = 'HUDOC'" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 20,
                        "ecli": "ECLI:CE:ECHR:2021:0511JUD001234519",
                        "celex_id": None,
                        "item_id": "001-209999",
                        "date_decision": "2021-05-11",
                        "sources": ["ECHR"],
                        "language": "en",
                        "fulltext": (
                            "THE LAW\n"
                            "The Court notes Case C-221/17 (ECLI:EU:C:2019:189) and ECLI:NL:RVS:2020:423.\n"
                        ),
                        "summary": None,
                    }
                ],
            }
        if "FROM cjeu_document d" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 30,
                        "ecli": "ECLI:EU:C:2019:189",
                        "celex_id": "62017CJ0221",
                        "item_id": None,
                        "date_decision": "2019-03-12",
                        "sources": ["CJEU"],
                    }
                ],
            }
        if "FROM case_text t" in sql and "CELLAR_ITEM" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "case_id": 30,
                        "language": "en",
                        "source": "CELLAR_ITEM",
                        "fulltext": "See ECtHR, 27 June 2000, Frydlender v. France, Application no. 30979/96.",
                    }
                ],
            }
        if "WHERE c.ecli = ANY" in sql:
            wanted = set(payload["params"][0])
            rows = []
            if "ECLI:EU:C:2019:189" in wanted:
                rows.append(
                    {
                        "value": "ECLI:EU:C:2019:189",
                        "via_identifier": 0,
                        "id": 30,
                        "ecli": "ECLI:EU:C:2019:189",
                        "celex_id": "62017CJ0221",
                        "item_id": None,
                        "date_decision": "2019-03-12",
                        "sources": ["CJEU"],
                    }
                )
            if "ECLI:NL:RVS:2020:423" in wanted:
                rows.append(
                    {
                        "value": "ECLI:NL:RVS:2020:423",
                        "via_identifier": 0,
                        "id": 10,
                        "ecli": "ECLI:NL:RVS:2020:423",
                        "celex_id": None,
                        "item_id": None,
                        "date_decision": "2020-02-12",
                        "sources": ["RS"],
                    }
                )
            return 200, {"ok": True, "rows": rows}
        if "FROM case_identifier ci" in sql:
            scheme, vals = payload["params"]
            rows = []
            if scheme == "cjeu_case_number" and "C-221/17" in vals:
                rows.append(
                    {
                        "value_normalized": "C-221/17",
                        "value": "C-221/17",
                        "id": 30,
                        "ecli": "ECLI:EU:C:2019:189",
                        "celex_id": "62017CJ0221",
                        "item_id": None,
                        "date_decision": "2019-03-12",
                        "sources": ["CJEU"],
                        "doctype": None,
                        "doctype_branch": None,
                    }
                )
            if scheme == "echr_appno" and "30979/96" in vals:
                rows.append(
                    {
                        "value_normalized": "30979/96",
                        "value": "30979/96",
                        "id": 40,
                        "ecli": "ECLI:CE:ECHR:2000:0627JUD003097996",
                        "celex_id": None,
                        "item_id": "001-58762",
                        "date_decision": "2000-06-27",
                        "sources": ["ECHR"],
                        "doctype": "HEJUD",
                        "doctype_branch": "GRANDCHAMBER",
                    }
                )
            return 200, {"ok": True, "rows": rows}
        return 200, {"ok": True, "rows": []}

    srv = FakeRunnerServer("tok-secret", handle)
    try:
        cle = mod._load_cle(CLE_CHECKOUT)
        rw = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=False)

        # rs_text with resolved_only=True skips unresolved ECLI:EU:C:2099:999999
        rs_res = mod.run_rs_text(
            rw, cle, from_id=0, to_id=None, page_size=50, chunk_size=100,
            dry_run=False, explain_sql=False, max_cases=10, resolved_only=True,
        )
        assert rs_res["inserted_resolved"] == 2  # ECLI:EU:C:2019:189 + appno 30979/96
        assert rs_res["inserted_unresolved_ecli"] == 0

        # echr_text resolves both CJEU and NL ECLI
        echr_res = mod.run_echr_text(
            rw, cle, from_id=0, to_id=None, page_size=50, chunk_size=100,
            dry_run=False, explain_sql=False, max_cases=10, resolved_only=False,
        )
        assert echr_res["inserted_resolved"] == 2

        # cjeu_text resolves ECtHR appno 30979/96 via echr_kind('HEJUD') == 'judgment'
        cjeu_res = mod.run_cjeu_text(
            rw, cle, from_id=0, to_id=None, page_size=50, chunk_size=100,
            dry_run=False, explain_sql=False, max_cases=10, resolved_only=False,
        )
        assert cjeu_res["inserted_resolved"] == 1
    finally:
        srv.close()


def test_step3_eu_law_authorities_and_dictum_modes():
    mod = _load_module(
        "backfill_context_via_runner",
        REPO_ROOT / "migration" / "sql" / "67_backfill_context_via_runner.py",
    )

    def handle(path, payload):
        sql = payload["sql"]
        if path == "/execute":
            return 200, {"ok": True, "row_count": len(payload["params"][0]), "rows": []}
        if "FROM legislation_alias" in sql or "FROM legal_provision" in sql:
            return 200, {"ok": True, "rows": []}
        if "FROM legislation" in sql and "scheme IN" in sql:
            return 200, {"ok": True, "rows": [{"id": 500, "scheme": "celex", "identifier": "32011L0095"}]}
        if "SELECT min(id) AS lo, max(id) AS hi FROM cases" in sql:
            return 200, {"ok": True, "rows": [{"lo": 10, "hi": 10}]}
        if "FROM cases c" in sql and "JOIN case_text ct" in sql:
            return 200, {
                "ok": True,
                "rows": [
                    {
                        "id": 10,
                        "ecli": "ECLI:NL:RVS:2020:423",
                        "celex_id": None,
                        "item_id": None,
                        "date_decision": "2020-02-12",
                        "language": "nl",
                        "fulltext": (
                            "Gelet op Richtlijn 2011/95/EU en Kamerstukken II 2015/16, 34 356, nr. 3.\n"
                            "Beslissing\n"
                            "De Afdeling bestuursrechtspraak van de Raad van State:\n"
                            "verklaart het hoger beroep ongegrond."
                        ),
                        "summary": None,
                    }
                ],
            }
        return 200, {"ok": True, "rows": []}

    srv = FakeRunnerServer("tok-secret", handle)
    try:
        cle = mod._load_cle(CLE_CHECKOUT)
        rw = SqlRunnerClient(url=srv.url, token="tok-secret", read_only=False)
        law_res = mod.run_eu_law_text(
            rw, cle, corpus="rs", from_id=0, to_id=None, page_size=50, chunk_size=100,
            dry_run=False, explain_sql=False, max_cases=10,
        )
        assert law_res["inserted_resolved"] >= 1

        auth_res = mod.run_authorities(
            rw, cle, corpus="rs", from_id=0, to_id=None, page_size=50, chunk_size=100,
            dry_run=False, explain_sql=False, max_cases=10,
        )
        assert auth_res["docs_inserted"] >= 1
        assert auth_res["refs_inserted"] >= 1

        dic_res = mod.run_rs_dictum(
            rw, cle, from_id=0, to_id=None, page_size=50, chunk_size=100,
            dry_run=False, explain_sql=False, max_cases=10,
        )
        assert dic_res["inserted"] == 1
    finally:
        srv.close()
