import json
import os
import unittest

from address_pipeline.llm_spans import identify, verified
from address_pipeline.sources import DEFAULT_HISTORY, DEFAULT_RAW, read_sql, statements
from address_pipeline.stage123_match import match_statements


class SpanTest(unittest.TestCase):
    def test_span_must_be_in_original(self):
        text = "경북 포항시 북구 장성동 두산위브제니스 108-2306"
        self.assertEqual(verified("두산위브 제니스", text), "두산위브 제니스")   # 띄어쓰기만 다르면 받는다
        self.assertIsNone(verified("두산위브더제니스", text))                 # 원문에 없는 글자
        self.assertIsNone(verified("", text))
        self.assertIsNone(verified("null", text))

    def test_identify_drops_invented_spans_and_retries(self):
        calls = []

        def generate(prompt, request):
            calls.append(request)
            if len(calls) == 1:
                return "not json"
            rows = json.loads(request)
            return json.dumps([{"id": r["id"], "address_part": "경북 포항시 북구 장성동", "building": "두산위브제니스",
                                "detail": "108동 2306호", "other": []} for r in rows], ensure_ascii=False)

        [row] = identify({"1": "경북 포항시 북구 장성동 두산위브제니스 108-2306"}, generate)
        self.assertEqual(len(calls), 2)
        self.assertEqual((row["address_part"], row["building"]), ("경북 포항시 북구 장성동", "두산위브제니스"))
        self.assertIsNone(row["detail"])            # '108동 2306호'는 원문에 그대로 있지 않다
        self.assertEqual(row["rejected"], ["detail"])
        self.assertIsNone(row["error"])


class StatementTest(unittest.TestCase):
    def test_scope_runs_between_parse_and_match(self):
        sql = match_statements()
        self.assertIn("input_cands", sql[0])
        scope = statements(read_sql("2_scope_reference.sql"))
        self.assertEqual(sql[1:1 + len(scope)], scope)
        self.assertIn("engine_standardized", sql[-2])


@unittest.skipUnless(DEFAULT_RAW.exists() and DEFAULT_HISTORY.exists(), "RAW·폐지 이력 parquet 없음")
class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from address_pipeline import AddressPipeline
        from address_workbench.adapter import InputRecord

        os.environ["ADDRESS_PIPELINE_LLM"] = "0"
        try:
            result = AddressPipeline().run_dataset((
                InputRecord("1", "서울특별시 종로구 세종대로 175"),
                InputRecord("2", "인천광역시 남동구 구월로 1921502동 1604호 (구월동 구힐스테이트)"),
                InputRecord("3", "충남 연기군 남면 종촌리 100"),
                InputRecord("4", ""),
                InputRecord("5", "충청남도 배방읍 중리 고불로583번길 18-12"),
                InputRecord("6", "상남로 693번"),
                InputRecord("7", "경기도 생연동 중앙로 318"),
                InputRecord("8", "전라남도  완도군 고금면 덕동리 581번지"),
            ), run_id="test")
        finally:
            os.environ.pop("ADDRESS_PIPELINE_LLM", None)
        cls.rows = {row["record"]["address_id"]: row for row in result.dataset["results"]}
        cls.batch = result.dataset["batch_results"][0]

    def test_unlinked_lot_points_to_existing_building(self):
        # 덕동리 581은 2025 RAW에 없지만, 폐지 이력상 충무사길 68의 연결 지번에서 빠졌고 그 건물은 지금도 있다.
        row = self.rows["8"]
        self.assertEqual((row["status"], row["review"]["review_type"]), ("held", "연결 지번이 빠진 건물"))
        self.assertEqual([c["address"] for c in row["review"]["candidates"]], ["전라남도 완도군 고금면 충무사길 68"])

    def test_exact_match_is_confirmed_by_rules(self):
        row = self.rows["1"]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["output"]["engine"], "address_pipeline")

    def test_building_name_candidate(self):
        review = self.rows["2"]["review"]
        self.assertEqual(review["review_type"], "건물명 일치 후보")
        self.assertIn("인천광역시 남동구 구월로 192", [c["address"] for c in review["candidates"]])

    def test_unique_nationwide_without_sigungu_is_confirmed(self):
        row = self.rows["5"]
        self.assertEqual(row["status"], "resolved")
        self.assertIn("전국에 하나뿐", row["output"]["reason"])

    def test_region_must_really_be_written(self):
        # '상남로' 안의 '상남'은 상남면을 적은 것이 아니다. 지역 표기가 없으므로 확정하지 않는다.
        self.assertNotIn(self.rows["6"]["status"], {"resolved", "address_only"})

    def test_not_unique_nationwide_stays_held(self):
        # 중앙로 318은 전국 여러 시에 있다. 시군구 없이는 확정하지 않는다.
        self.assertNotIn(self.rows["7"]["status"], {"resolved", "address_only"})

    def test_blank_is_excluded(self):
        self.assertEqual(self.rows["4"]["status"], "excluded")

    def test_stage_log_records_every_stage(self):
        stages = [(entry["stage"], entry["tool"]) for entry in self.batch["stage_log"]]
        self.assertEqual(stages, [("0", "prepare"), ("123", "match"), ("4", "buildings"), ("4", "abolished"),
                                  ("4", "number_typos"), ("4", "glued_numbers"), ("6", "output")])
        self.assertEqual(self.batch["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
