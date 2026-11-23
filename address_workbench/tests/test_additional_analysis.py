from copy import deepcopy
import json
from http.client import HTTPConnection
from threading import Event
import unittest

from address_workbench.adapter import InputRecord
from address_workbench.additional_analysis import AdditionalAnalysis, MAX_CANDIDATES, explain_candidates
from address_workbench.demo import DemoAdapter, SAMPLE_ADDRESSES
from address_workbench.jobs import JobRunner
from address_workbench.tests import test_jobs as job_tests

wait_until = job_tests.wait_until


def sample():
    result = DemoAdapter().run_dataset((InputRecord("a", SAMPLE_ADDRESSES[0]),), run_id="test")
    return result.dataset["results"][0], result.candidate_comparisons["a"]


def response(prompt, request):
    context = json.loads(request)
    items = [{"candidate_index": candidate["candidate_index"], "evidence_indices": [0] if candidate["facts"] else [],
                        "evidence_summary": "도로명과 본번은 일치하지만 부번은 확인이 필요합니다.",
                        "check_points": ["원문 부번을 확인하세요."]} for candidate in context["candidates"]]
    return json.dumps({"candidates": items, "recommendation": {
        "decision": "tie", "candidate_indices": [item["candidate_index"] for item in items],
        "reason": "두 후보 모두 본번은 일치하지만 부번이 달라 우열을 판단할 수 없습니다.",
        "evidence_refs": [{"candidate_index": item["candidate_index"], "evidence_indices": [0]}
                          for item in items]}})


class ExplanationTest(unittest.TestCase):
    def test_explains_existing_candidates_and_keeps_mismatches_even_when_ai_omits_them(self):
        row, comparisons = sample()
        original = deepcopy(row)
        result = explain_candidates(row, comparisons, source="bigquery", generate=response)
        self.assertEqual(row, original)
        self.assertEqual([item["address"] for item in result["candidates"]],
                         [item["address"] for item in row["review"]["candidates"]])
        self.assertIn("건물부번 불일치", " ".join(result["candidates"][0]["check_points"]))
        self.assertTrue(result["candidates"][0]["cited_evidence"])

    def test_rejects_invented_candidate_and_source_ids(self):
        row, comparisons = sample()
        for candidate, evidence in ((99, [0]), (0, [999]), (0, [])):
            def invalid(_prompt, _request):
                result = json.loads(response(_prompt, _request))
                result["candidates"] = [{"candidate_index": candidate, "evidence_indices": evidence,
                                    "evidence_summary": "가짜 근거", "check_points": []},
                                   {"candidate_index": 1, "evidence_indices": [0],
                                    "evidence_summary": "설명", "check_points": []}]
                return json.dumps(result)
            with self.subTest(candidate=candidate, evidence=evidence), self.assertRaises(ValueError):
                explain_candidates(row, comparisons, source="bigquery", generate=invalid)

    def test_does_not_take_identity_or_lookup_history_from_ai(self):
        row, comparisons = sample()
        def generated(prompt, request):
            result = json.loads(response(prompt, request))
            for item in result["candidates"]:
                item.update(address="AI가 지어낸 주소", rank=99, lookups=["웹 검색"])
            return json.dumps(result)
        result = explain_candidates(row, comparisons, source="bigquery", generate=generated)
        self.assertNotIn("웹 검색", result["lookups"])
        self.assertEqual(result["candidates"][0]["address"], row["review"]["candidates"][0]["address"])
        self.assertEqual(result["candidates"][0]["rank"], row["review"]["candidates"][0]["rank"])

    def test_demo_uses_only_fixtures_without_an_ai_request(self):
        row, comparisons = sample()
        def unexpected(*_args):
            self.fail("시연 모드에서 AI 호출")
        self.assertEqual(explain_candidates(row, comparisons, source="demo", generate=unexpected)["mode"], "demo")

    def test_single_recommendation_is_separate_from_original_candidates_rank_and_approval(self):
        row, comparisons = sample()
        row["approval"] = {"status": "reviewed_confirmed", "candidate_address": "원래 승인 주소"}
        original = deepcopy(row)
        def recommend(prompt, request):
            result = json.loads(response(prompt, request))
            result["recommendation"].update(decision="recommend", candidate_indices=[1],
                                             reason="후보 2의 기존 근거가 원문의 건물명 단서를 더 지지합니다.")
            return json.dumps(result)
        result = explain_candidates(row, comparisons, source="bigquery", generate=recommend)
        self.assertEqual(result["recommendation"]["candidate_indices"], [1])
        self.assertEqual([item["recommended"] for item in result["candidates"]], [False, True])
        self.assertEqual(row, original)

    def test_tied_candidates_remain_tied_and_missing_evidence_can_withhold_recommendation(self):
        row, comparisons = sample()
        result = explain_candidates(row, comparisons, source="bigquery", generate=response)
        self.assertEqual(result["recommendation"]["decision"], "tie")
        self.assertEqual(result["recommendation"]["candidate_indices"], [0, 1])
        for item in row["review"]["candidates"]:
            item["evidence"] = []
        def insufficient(_prompt, request):
            items = json.loads(request)["candidates"]
            return json.dumps({"recommendation": {"decision": "insufficient", "candidate_indices": [],
                "reason": "기존 검증 근거가 없어 후보의 우열을 판단할 수 없습니다.", "evidence_refs": []},
                "candidates": [{"candidate_index": item["candidate_index"], "evidence_indices": [],
                                "evidence_summary": "근거 부족", "check_points": []} for item in items]})
        result = explain_candidates(row, {}, source="bigquery", generate=insufficient)
        self.assertEqual(result["recommendation"]["candidate_indices"], [])
        self.assertFalse(any(item["recommended"] for item in result["candidates"]))

    def test_rejects_invalid_recommendation_and_unsubstantiated_narrowing(self):
        row, comparisons = sample()
        variants = [
            {"candidate_indices": [99]}, {"candidate_indices": [0, 0]},
            {"decision": "recommend", "candidate_indices": [0, 1]},
            {"decision": "insufficient", "candidate_indices": [0]},
            {"decision": "tie", "candidate_indices": [0]},
            {"decision": "recommend", "candidate_indices": [0], "evidence_refs": []},
            {"decision": "recommend", "candidate_indices": [0],
             "evidence_refs": [{"candidate_index": 0, "evidence_indices": [0]}]},
            {"evidence_refs": [{"candidate_index": 0, "evidence_indices": [999]}]},
        ]
        for variant in variants:
            def invalid(prompt, request):
                result = json.loads(response(prompt, request))
                result["recommendation"].update(variant)
                return json.dumps(result)
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                explain_candidates(row, comparisons, source="bigquery", generate=invalid)

    def test_partial_comparison_cannot_recommend_globally(self):
        row, _ = sample()
        first = row["review"]["candidates"][0]
        row["review"]["candidates"] = [{**deepcopy(first), "address": f"기존 주소 {i}"} for i in range(MAX_CANDIDATES + 1)]
        result = explain_candidates(row, {}, source="bigquery", generate=response)
        self.assertEqual(result["recommendation"]["decision"], "insufficient")
        self.assertEqual(result["recommendation"]["candidate_indices"], [])
        self.assertEqual(result["total_candidates"], MAX_CANDIDATES + 1)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.runner = JobRunner(DemoAdapter(), "demo")
        self.job = self.runner.start((InputRecord("a", SAMPLE_ADDRESSES[0]),), owner_id="owner")
        wait_until(lambda: self.job.status == "done")
        self.addCleanup(self.runner.executor.shutdown)

    def service(self, analyze):
        service = AdditionalAnalysis(analyze=analyze)
        self.addCleanup(service.executor.shutdown)
        return service

    def test_background_analysis_is_cached_private_and_does_not_mutate_base_results(self):
        started, release = Event(), Event()
        calls = []
        def analyze(row, comparisons, **kwargs):
            calls.append(row)
            started.set()
            release.wait(5)
            return explain_candidates(row, comparisons, **kwargs)
        service = self.service(analyze)
        self.addCleanup(release.set)
        original = deepcopy(self.job.rows[0])
        first = service.start(self.runner, self.job.job_id, "a", owner_id="owner")
        self.assertIn(first["status"], {"queued", "running"})
        self.assertTrue(started.wait(2))
        self.assertIsNone(service.start(self.runner, self.job.job_id, "a", owner_id="other"))
        self.assertIsNone(service.snapshot(self.runner, self.job.job_id, "a", owner_id="other"))
        service.start(self.runner, self.job.job_id, "a", owner_id="owner")
        release.set()
        wait_until(lambda: self.job.rows[0]["additional_analysis"]["status"] == "done")
        self.assertEqual(service.start(self.runner, self.job.job_id, "a", owner_id="owner")["status"], "done")
        self.assertEqual(len(calls), 1)
        self.assertEqual({k: v for k, v in self.job.rows[0].items() if k != "additional_analysis"}, original)
        self.assertEqual(self.job.payload["dataset"]["results"][0]["additional_analysis"]["status"], "done")

    def test_failure_requires_explicit_retry_and_keeps_candidates(self):
        calls = []
        def analyze(row, comparisons, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("secret-key-should-not-appear")
            return explain_candidates(row, comparisons, **kwargs)
        service = self.service(analyze)
        service.start(self.runner, self.job.job_id, "a", owner_id="owner")
        wait_until(lambda: self.job.rows[0]["additional_analysis"]["status"] == "failed")
        failed = service.start(self.runner, self.job.job_id, "a", owner_id="owner")
        self.assertNotIn("secret-key", failed["message"])
        self.assertEqual(len(calls), 1)
        service.start(self.runner, self.job.job_id, "a", owner_id="owner", retry=True)
        wait_until(lambda: self.job.rows[0]["additional_analysis"]["status"] == "done")
        self.assertEqual(len(calls), 2)

    def test_old_explanation_cache_is_replaced_only_when_requested(self):
        self.job.rows[0]["additional_analysis"] = {"status": "done", "candidates": []}
        calls = []
        def analyze(row, comparisons, **kwargs):
            calls.append(1)
            return explain_candidates(row, comparisons, **kwargs)
        service = self.service(analyze)
        service.start(self.runner, self.job.job_id, "a", owner_id="owner")
        wait_until(lambda: self.job.rows[0]["additional_analysis"]["status"] == "done")
        self.assertEqual(len(calls), 1)
        self.assertIn("recommendation", self.job.rows[0]["additional_analysis"])

    def test_job_with_running_analysis_is_not_pruned(self):
        service = self.service(lambda *_args, **_kwargs: {})
        self.job.rows[0]["additional_analysis"] = {"status": "running"}
        self.runner.keep = 1
        for _ in range(2):
            job = self.runner.start((InputRecord("x", "임의 주소"),), owner_id="owner")
            wait_until(lambda: job.status == "done")
        self.assertIsNotNone(service.snapshot(self.runner, self.job.job_id, "a", owner_id="owner"))


class HTTPTest(unittest.TestCase):
    start_server = job_tests.JobHttpTest.start_server
    client = staticmethod(job_tests.JobHttpTest.client)

    def test_post_and_poll_use_same_browser_session_and_results_survive_reload(self):
        server = self.start_server(DemoAdapter(), "demo")
        owner, stranger = self.client(server), self.client(server)
        _, job, _ = owner("POST", "/api/single?async=1", json.dumps({"raw_address": SAMPLE_ADDRESSES[0]}))
        path = f"/api/jobs/{job['job_id']}"
        wait_until(lambda: owner("GET", path)[1]["status"] == "done")
        body = json.dumps({"address_id": "row-1"})
        self.assertEqual(stranger("POST", path + "/analysis", body)[0], 404)
        self.assertEqual(owner("POST", path + "/analysis", body)[0], 202)
        wait_until(lambda: owner("GET", path + "/analysis?address_id=row-1")[1]["analysis"]["status"] == "done")
        self.assertEqual(stranger("GET", path + "/analysis?address_id=row-1")[0], 404)
        self.assertEqual(owner("POST", path + "/analysis", body)[0], 200)
        restored = owner("GET", path)[1]["rows"][0]
        self.assertEqual(restored["additional_analysis"]["status"], "done")
        self.assertEqual(restored["status"], "held")
        self.assertEqual(len(restored["review"]["candidates"]), 2)
        self.assertEqual(owner("POST", path + "/analysis", body, {"Origin": "https://other.example"})[0], 403)
        for invalid in ({}, [], {"address_id": "row-1", "retry": "yes"}):
            self.assertEqual(owner("POST", path + "/analysis", json.dumps(invalid))[0], 400)

    def test_no_candidates_is_a_validation_error_and_static_modules_are_served(self):
        server = self.start_server(DemoAdapter(), "demo")
        owner = self.client(server)
        _, job, _ = owner("POST", "/api/single?async=1", json.dumps({"raw_address": SAMPLE_ADDRESSES[3]}))
        path = f"/api/jobs/{job['job_id']}"
        wait_until(lambda: owner("GET", path)[1]["status"] == "done")
        self.assertEqual(owner("POST", path + "/analysis", json.dumps({"address_id": "row-1"}))[0], 400)
        for route, mime in (("/analysis.mjs", "text/javascript"), ("/analysis.css", "text/css")):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", route)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(response.getheader("Content-Type").startswith(mime))
                self.assertTrue(response.read())
            finally:
                connection.close()
