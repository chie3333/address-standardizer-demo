from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread
import unittest

from openpyxl import Workbook

from address_workbench.adapter import AdapterResult, InputRecord, PipelineAdapter, json_value, run
from address_workbench.demo import DemoAdapter, SAMPLE_ADDRESSES
from address_workbench.server import make_handler
from address_workbench.tabular import read_table, records_from


def workbook():
    book = Workbook()
    book.active.append(["주소"])
    book.active.append([SAMPLE_ADDRESSES[1]])
    sheet = book.create_sheet("과거 후보")
    sheet.append(["id", "원본 주소"])
    sheet.append(["0003", SAMPLE_ADDRESSES[4]])
    sheet.append(["0004", None])
    out = BytesIO()
    book.save(out)
    book.close()
    return out.getvalue()


class AdapterTests(unittest.TestCase):
    def test_json_preserves_large_raw_identifiers(self):
        self.assertEqual(json_value((2025, 9007199254740993)), [2025, "9007199254740993"])

    def test_demo_distinguishes_ties_no_candidate_and_past_candidate(self):
        records = tuple(InputRecord(str(i), raw) for i, raw in enumerate(SAMPLE_ADDRESSES))
        payload = run(DemoAdapter(), records, source="demo")
        rows = payload["dataset"]["results"]
        self.assertEqual(payload["source"], "demo")
        self.assertEqual(rows[0]["review"]["status"], "tied_candidates")
        self.assertIsNone(rows[0]["standardization"])
        self.assertEqual(rows[3]["review"]["status"], "no_candidate")
        self.assertEqual(rows[4]["review"]["candidates"][0]["reference_years"], [2024])
        self.assertEqual(rows[4]["status"], "no_match")
        self.assertEqual(rows[6]["output"]["representative_address_id"], "2")
        self.assertEqual(rows[9]["status"], "address_only")
        self.assertEqual(payload["candidate_comparisons"]["0"]["0"][2]["outcome"], "mismatch")

    def test_demo_does_not_claim_verification_for_arbitrary_input(self):
        payload = run(DemoAdapter(), (InputRecord("a", "임의 주소"),), source="demo")
        row = payload["dataset"]["results"][0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["traces"][0]["reason_code"], "demo_no_fixture")
        self.assertIsNone(row["region_validation"])
        self.assertIsNone(row["standardization"])

    def test_adapter_rejects_reordered_rows(self):
        class Broken(DemoAdapter):
            def run_dataset(self, records, *, run_id):
                output = super().run_dataset(records, run_id=run_id)
                output.dataset["results"].reverse()
                return output

        with self.assertRaisesRegex(ValueError, "원본 행"):
            run(Broken(), (InputRecord("a", None), InputRecord("b", "other")), source="pipeline")

    def test_existing_pipeline_serialization_and_missing_review(self):
        try:
            from address_standardizer import DEFAULT_PREPROCESSOR, Pipeline
            from address_standardizer.contracts import Module, ModuleResult, ModuleTrace, ParseOutput
        except ImportError as exc:
            self.skipTest(f"선택 의존성 address_standardizer가 필요합니다. {exc}")

        parser = Module("contract-test", lambda *_: ModuleResult(ParseOutput("passed"), ModuleTrace("completed")))
        adapter = PipelineAdapter(Pipeline(parser=parser, preprocessor=DEFAULT_PREPROCESSOR), "2025", batch_size=1)
        records = (InputRecord("01", " 가온시 "), InputRecord("02", None), InputRecord("03", "가온시"))
        payload = run(adapter, records, source="pipeline")
        dataset = payload["dataset"]
        self.assertEqual(payload["candidate_comparisons"], {})
        # 03은 01과 원문이 같아 01의 결과를 물려받는다.
        self.assertEqual([r["status"] for r in dataset["results"]], ["parse_unresolved", "excluded", "parse_unresolved"])
        self.assertEqual([r["inherited_from"] for r in dataset["results"]], [None, None, "01"])
        self.assertEqual(dataset["results"][0]["record"]["raw_address"], " 가온시 ")
        self.assertEqual(dataset["results"][0]["record"]["normalized_address"], "가온시")
        self.assertIsNone(dataset["results"][0]["review"])
        self.assertEqual(dataset["batch_results"][0]["parsed_df"], [])

    def test_optional_review_fields_are_passed_through(self):
        class Custom(DemoAdapter):
            def run_dataset(self, records, *, run_id):
                result = super().run_dataset(records, run_id=run_id)
                result.dataset["results"][0]["status"] = "future_status"
                result.dataset["results"][0]["review"] = {"candidates": {"future": True}, "extra": ["a"]}
                return AdapterResult(result.dataset)

        result = run(Custom(), (InputRecord("a", "other"),), source="pipeline")
        self.assertEqual(result["dataset"]["results"][0]["review"]["extra"], ["a"])
        self.assertEqual(result["dataset"]["results"][0]["status"], "future_status")


class UploadTests(unittest.TestCase):
    def test_csv_bom_blank_row_and_quoted_values(self):
        raw = '\ufeffid,주소\r\n0001,"서울, 강남"\r\n0002,\r\n0003,"도로\n건물"\r\n'.encode()
        table, sheets = read_table(raw, "csv")
        self.assertEqual(sheets, [])
        self.assertEqual(records_from(table, 1, 0), (
            InputRecord("0001", "서울, 강남"), InputRecord("0002", ""), InputRecord("0003", "도로\n건물"),
        ))
        cp949, _ = read_table("주소\n서울\n".encode("cp949"), "csv")
        self.assertEqual(cp949.rows, (("서울",),))

    def test_xlsx_sheet_choice_blank_and_leading_zero(self):
        table, sheets = read_table(workbook(), "xlsx", "과거 후보")
        self.assertEqual(len(sheets), 2)
        self.assertEqual(records_from(table, 1, 0), (
            InputRecord("0003", SAMPLE_ADDRESSES[4]), InputRecord("0004", None),
        ))

    def test_duplicate_ids_and_duplicate_headers(self):
        table, _ = read_table(b"id,address\na,one\na,two\n", "csv")
        with self.assertRaisesRegex(ValueError, "ID"):
            records_from(table, 1, 0)
        duplicate, _ = read_table(b"address,address\nfirst,second\n", "csv")
        with self.assertRaisesRegex(ValueError, "열의 이름"):
            records_from(duplicate, 0)

    def test_format_errors(self):
        with self.assertRaisesRegex(ValueError, "XLSX"):
            read_table(b"not a workbook", "xlsx")
        with self.assertRaisesRegex(ValueError, "CSV 형식"):
            read_table(b'address\n"unclosed', "csv")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DemoAdapter(), "demo"))
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_http_entry_point_and_single_address(self):
        status, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/app.mjs", html)
        status, result = self.request("POST", "/api/single", json.dumps({"raw_address": SAMPLE_ADDRESSES[0]}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)["dataset"]["summary"], {"held": 1})

    def test_http_xlsx_with_second_sheet(self):
        from urllib.parse import urlencode

        query = urlencode({"format": "xlsx", "sheet": "과거 후보", "address_col": 1, "id_col": 0})
        status, result = self.request("POST", "/api/table/inspect?" + query, workbook())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)["headers"], ["id", "원본 주소"])
        status, result = self.request("POST", "/api/table/run?" + query, workbook())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)["dataset"]["summary"], {"no_match": 1, "excluded": 1})

    def test_http_csv_and_error_response(self):
        status, result = self.request("POST", "/api/table/run?format=csv&address_col=1&id_col=0",
                                      f"id,주소\n0001,{SAMPLE_ADDRESSES[1]}\n".encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)["dataset"]["results"][0]["record"]["address_id"], "0001")
        status, result = self.request("POST", "/api/single", b"{}")
        self.assertEqual(status, 400)
        self.assertIn("주소", json.loads(result)["error"])


if __name__ == "__main__":
    unittest.main()


class MultiEngineHTTPTests(unittest.TestCase):
    """두 엔진을 한 서버에 올리면 요청의 engine 값으로 고르고, 작업은 어느 엔진 것이든 찾는다."""

    @classmethod
    def setUpClass(cls):
        from address_workbench.server import Engine

        class Second(DemoAdapter):
            pass

        engines = [Engine("pipeline", "기존 파이프라인", DemoAdapter(), "pipeline"),
                   Engine("bigquery", "BigQuery 엔진", Second(), "bigquery")]
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engines))
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    request = HTTPTests.request

    def test_config_lists_engines(self):
        status, body = self.request("GET", "/api/config")
        config = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(config["default_engine"], "pipeline")
        self.assertEqual([item["key"] for item in config["engines"]], ["pipeline", "bigquery"])

    def test_engine_choice_and_job_lookup(self):
        body = json.dumps({"raw_address": SAMPLE_ADDRESSES[0]})
        for key in ("pipeline", "bigquery"):
            status, result = self.request("POST", f"/api/single?engine={key}", body)
            self.assertEqual((status, json.loads(result)["source"]), (200, key))
        status, _ = self.request("POST", "/api/single?engine=nope", body)
        self.assertEqual(status, 400)
        from http.client import HTTPConnection
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("GET", "/api/config")
        response = connection.getresponse(); response.read()
        cookie = response.getheader("Set-Cookie").split(";")[0]
        connection.close()
        status, started = self.request("POST", "/api/single?engine=bigquery&async=1", body, {"Cookie": cookie})
        self.assertEqual(status, 202)
        job_id = json.loads(started)["job_id"]
        for _ in range(50):
            status, snap = self.request("GET", f"/api/jobs/{job_id}", headers={"Cookie": cookie})
            snap = json.loads(snap)
            if snap["status"] == "done":
                break
            __import__("time").sleep(0.05)
        self.assertEqual((status, snap["status"], snap["source"]), (200, "done", "bigquery"))
