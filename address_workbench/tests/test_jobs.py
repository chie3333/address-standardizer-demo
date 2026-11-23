from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from threading import Barrier, Event, Thread
from time import sleep, time
import unittest

from address_workbench.adapter import AdapterResult, InputRecord
from address_workbench.demo import DemoAdapter
from address_workbench.jobs import JobRunner
from address_workbench.server import make_handler


def row(index, record, status="resolved"):
    return {"record": {"address_id": record.address_id, "raw_address": record.raw_address},
            "input_index": index, "status": status, "traces": []}


class BatchAdapter:
    """배치 두 개로 나눠 진행을 보고하는 가짜 어댑터. fail=True면 두 번째 배치 전에 실패한다."""
    reference_version = "2025"

    def __init__(self, fail=False):
        self.fail = fail
        self.first_batch_sent = Event()
        self.release = Event()

    def run_dataset(self, records, *, run_id, progress=None):
        half = len(records) // 2
        rows = [row(i, r) for i, r in enumerate(records)]
        progress(rows[:half], {"batch_id": f"{run_id}:1"})
        self.first_batch_sent.set()
        self.release.wait(5)
        if self.fail:
            raise RuntimeError("LLM 응답 실패")
        progress(rows[half:], {"batch_id": f"{run_id}:2"})
        return AdapterResult({"run_id": run_id, "reference_version": "2025", "summary": {"resolved": len(rows)},
                              "batch_results": [], "results": rows})


def records(count):
    return tuple(InputRecord(f"row-{i}", f"주소 {i}") for i in range(1, count + 1))


def wait_until(predicate, timeout=5):
    deadline = time() + timeout
    while not predicate():
        if time() > deadline:
            raise AssertionError("시간 안에 끝나지 않았습니다.")
        sleep(0.02)


class JobRunnerTest(unittest.TestCase):
    def test_rows_arrive_per_batch_and_final_payload_is_validated(self):
        adapter = BatchAdapter()
        runner = JobRunner(adapter, "pipeline")
        job = runner.start(records(4), owner_id="a")
        adapter.first_batch_sent.wait(5)
        wait_until(lambda: runner.snapshot(job.job_id, owner_id="a")["processed"] == 2)
        partial = runner.snapshot(job.job_id, owner_id="a")
        self.assertEqual((partial["status"], partial["processed"], partial["batches_done"]), ("running", 2, 1))
        self.assertEqual([r["record"]["address_id"] for r in partial["rows"]], ["row-1", "row-2"])
        self.assertEqual(runner.snapshot(job.job_id, since=2, owner_id="a")["rows"], [])
        adapter.release.set()
        wait_until(lambda: runner.snapshot(job.job_id, owner_id="a")["status"] == "done")
        done = runner.snapshot(job.job_id, owner_id="a")
        self.assertEqual(len(done["rows"]), 4)
        self.assertIsNone(done["payload"]["dataset"]["results"])  # 행은 다시 보내지 않는다.
        self.assertEqual(done["batches_done"], 2)
        # 이미 받은 2행 이후만 이어 받는다. 도착 순서가 그대로라 중복·누락이 없다.
        rest = runner.snapshot(job.job_id, since=2, owner_id="a")["rows"]
        self.assertEqual([r["record"]["address_id"] for r in rest], ["row-3", "row-4"])

    def test_failure_keeps_finished_batches(self):
        adapter = BatchAdapter(fail=True)
        runner = JobRunner(adapter, "pipeline")
        job = runner.start(records(4), owner_id="a")
        adapter.first_batch_sent.wait(5)
        adapter.release.set()
        wait_until(lambda: runner.snapshot(job.job_id, owner_id="a")["status"] == "failed")
        failed = runner.snapshot(job.job_id, owner_id="a")
        self.assertEqual((failed["processed"], failed["error"]), (2, "RuntimeError"))
        self.assertEqual(len(failed["rows"]), 2)
        self.assertIsNone(failed["payload"])

    def test_only_one_job_runs_at_a_time(self):
        adapter = BatchAdapter()
        runner = JobRunner(adapter, "pipeline")
        runner.start(records(2), owner_id="a")
        with self.assertRaises(RuntimeError):
            runner.start(records(2), owner_id="b")
        adapter.release.set()

    def test_adapter_without_progress_fills_rows_at_the_end(self):
        runner = JobRunner(DemoAdapter(), "demo")
        job = runner.start((InputRecord("a", "임의 주소"),), owner_id="a")
        wait_until(lambda: runner.snapshot(job.job_id, owner_id="a")["status"] == "done")
        self.assertEqual(runner.snapshot(job.job_id, owner_id="a")["processed"], 1)


class ReviewAdapter:
    """검토 단계가 붙은 결과를 돌려주는 가짜 어댑터."""
    reference_version = "2025"
    standard = {"standard_road_address": "서울특별시 종로구 세종대로 209", "road_name_code": "111102005001",
                "legal_dong_code": "1111011900", "administrative_area_code": "11110515"}

    def run_dataset(self, records, *, run_id, progress=None):
        def review(tier, candidates):
            return {"tier": tier, "candidates": candidates}
        top = {"rank": 1, "kind": "road", "address": self.standard["standard_road_address"],
               "evidence": ["건물명 일치"], "standard_address": self.standard}
        rows = [
            {**row(0, records[0], "no_match"), "review": review("near_confirmed", [top])},
            {**row(1, records[1], "held"), "review": review("general", [top, {**top, "address": "다른 주소"}])},
            {**row(2, records[2], "resolved"), "review": review("", [top])},
        ]
        return AdapterResult({"run_id": run_id, "reference_version": "2025", "summary": {},
                              "batch_results": [], "results": rows})


class ApprovalTest(unittest.TestCase):
    def test_only_single_first_candidates_are_approved_and_can_be_undone(self):
        runner = JobRunner(ReviewAdapter(), "pipeline")
        job = runner.start(records(3), owner_id="a")
        wait_until(lambda: runner.snapshot(job.job_id, owner_id="a")["status"] == "done")
        changed = runner.approve(job.job_id, ["row-1", "row-2", "row-3"], owner_id="a")
        self.assertEqual([item["record"]["address_id"] for item in changed], ["row-1"])
        approval = changed[0]["approval"]
        self.assertEqual(approval["status"], "reviewed_confirmed")
        self.assertEqual(approval["standard_address"], ReviewAdapter.standard)
        # 원문 그대로 맞은 확정과 섞이지 않도록 상태는 그대로 둔다.
        self.assertEqual(changed[0]["status"], "no_match")
        self.assertEqual(runner.approve(job.job_id, ["row-1"], owner_id="a"), [])  # 이미 승인됨
        undone = runner.approve(job.job_id, ["row-1"], owner_id="a", undo=True)
        self.assertNotIn("approval", undone[0])
        self.assertIsNone(runner.approve("unknown", ["row-1"], owner_id="a"))


class JobHttpTest(unittest.TestCase):
    def start_server(self, adapter, source="pipeline", **kwargs):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(adapter, source, **kwargs))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def client(server, *, origin=None):
        origin = origin or f"http://127.0.0.1:{server.server_port}"
        cookie = ""

        def request(method, path, body=None, headers=None):
            nonlocal cookie
            request_headers = {"Host": origin.split("://", 1)[1], "Origin": origin, "Cookie": cookie}
            request_headers.update(headers or {})
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(method, path, body, request_headers)
                response = connection.getresponse()
                if set_cookie := response.getheader("Set-Cookie"):
                    cookie = set_cookie.split(";", 1)[0]
                return response.status, json.loads(response.read()), response.getheader("Set-Cookie")
            finally:
                connection.close()

        return request

    def test_async_run_and_polling(self):
        server = self.start_server(DemoAdapter(), "demo")
        request = self.client(server)
        status, started, cookie = request("POST", "/api/single?async=1", json.dumps({"raw_address": "임의 주소"}))
        self.assertEqual((status, started["total"]), (202, 1))
        self.assertIn("HttpOnly", cookie)
        wait_until(lambda: request("GET", f"/api/jobs/{started['job_id']}")[1]["status"] == "done")
        _, done, _ = request("GET", f"/api/jobs/{started['job_id']}")
        self.assertEqual(len(done["rows"]), 1)
        self.assertEqual(done["payload"]["source"], "demo")
        self.assertEqual(request("GET", "/api/jobs/latest")[1]["job_id"], started["job_id"])
        self.assertEqual(request("GET", "/api/jobs/unknown")[0], 404)

    def test_concurrent_uploads_with_same_row_id_stay_private(self):
        both_running = Event()
        barrier = Barrier(2, action=both_running.set, timeout=5)
        release = Event()

        class ParallelAdapter:
            reference_version = "2025"
            parallel_jobs = 2

            def run_dataset(self, records, *, run_id, progress=None):
                barrier.wait()
                if not release.wait(5):
                    raise RuntimeError("test did not release jobs")
                results = [row(i, record) for i, record in enumerate(records)]
                return AdapterResult({"run_id": run_id, "reference_version": "2025", "summary": {"resolved": 1},
                                      "batch_results": [], "results": results})

        server = self.start_server(ParallelAdapter())
        self.addCleanup(release.set)
        a, b, guest = (self.client(server) for _ in range(3))
        self.assertNotEqual(a("GET", "/api/config")[2], b("GET", "/api/config")[2])
        url = "/api/table/run?format=csv&address_col=0&async=1"
        with ThreadPoolExecutor(max_workers=2) as clients:
            uploads = [clients.submit(client, "POST", url, f"주소\n{address}\n".encode())
                       for client, address in ((a, "서울 세종대로 209"), (b, "부산 중앙대로 1001"))]
            status_a, job_a, _ = uploads[0].result()
            status_b, job_b, _ = uploads[1].result()
        self.assertEqual((status_a, status_b), (202, 202))
        self.assertTrue(both_running.wait(5), "both uploads must run before either can finish")
        path_a, path_b = (f"/api/jobs/{job['job_id']}" for job in (job_a, job_b))
        self.assertIsNone(guest("GET", "/api/jobs/latest")[1]["job_id"])
        self.assertEqual(guest("GET", path_a)[0], 404)
        self.assertEqual(a("GET", path_b)[0], 404)
        self.assertEqual(b("GET", path_a)[0], 404)
        release.set()
        wait_until(lambda: a("GET", path_a)[1]["status"] == b("GET", path_b)[1]["status"] == "done")
        for client, job, path, address in ((a, job_a, path_a, "서울 세종대로 209"),
                                            (b, job_b, path_b, "부산 중앙대로 1001")):
            self.assertEqual(client("GET", "/api/jobs/latest")[1]["job_id"], job["job_id"])
            self.assertEqual([result["record"] for result in client("GET", path)[1]["rows"]],
                             [{"address_id": "row-1", "raw_address": address}])
        self.assertEqual(a("GET", path_b)[0], 404)
        self.assertEqual(b("GET", path_a)[0], 404)
        approval = json.dumps({"address_ids": ["row-1"]})
        self.assertEqual(a("POST", f"{path_b}/approve", approval)[0], 404)
        self.assertEqual(b("POST", f"{path_a}/approve", approval)[0], 404)

    def test_public_origin_uses_secure_cookie_and_rejects_foreign_origin(self):
        origin = "https://address.example.com"
        server = self.start_server(DemoAdapter(), "demo", public_origin=origin)
        request = self.client(server, origin=origin)
        status, _, cookie = request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith("__Host-address_wb="))
        self.assertIn("; Secure", cookie)
        body = json.dumps({"raw_address": "임의 주소"})
        self.assertEqual(request("POST", "/api/single", body, {"Origin": "https://other.example.com"})[0], 403)
        self.assertEqual(request("POST", "/api/single", body)[0], 200)


if __name__ == "__main__":
    unittest.main()
