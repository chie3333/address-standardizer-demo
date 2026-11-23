"""BigQuery 엔진 결과가 워크벤치의 AddressResult 계약과 승인·제출 양식 규칙을 지키는지. BigQuery 연결 없이 돈다."""
import os
import unittest

from address_workbench import bigquery_engine as engine
from address_workbench.adapter import AdapterResult, InputRecord, run
from address_workbench.jobs import approvable_candidate
from address_workbench.output_fields import _source

ROAD = {"address": "경상남도 밀양시 삼랑진읍 거족길 16-58", "sido": "경상남도", "sigungu": "밀양시", "emd": "삼랑진읍",
        "ri": "율동리", "road_name": "거족길", "main_no": "16", "sub_no": "58",
        "road_name_code": "482704808039", "legal_dong_code": "4827025024", "administrative_area_code": "38080110",
        "lot_addresses": ["경상남도 밀양시 삼랑진읍 율동리 12"], "serials": [6333175]}
OTHER = {**ROAD, "address": "경상남도 밀양시 삼랑진읍 거족길 16-60", "sub_no": "60", "serials": [6333176],
         "lot_addresses": ["경상남도 밀양시 삼랑진읍 율동리 13"]}
LOT = {"lot": "경상남도 밀양시 삼랑진읍 율동리 12", "sido": "경상남도", "sigungu": "밀양시", "emd": "삼랑진읍",
       "ri": "율동리", "mountain": "false", "lot_main": "12", "lot_sub": "0", "legal_dong_code": "4827025024",
       "road_addresses": [ROAD["address"], OTHER["address"]]}


def sql_row(status, address_id="1", **extra):
    base = {"address_id": address_id, "status": status, "reason": "사유", "reason_code": None,
            "standard_road_address": None, "standard_lot_address": None, "candidate_lots": [],
            "candidate_details": [], "lot_details": [], "main_detail": None}
    return {**base, **extra}


def resolved(address_id="1", **main):
    return sql_row("확정", address_id, standard_road_address=ROAD["address"], main_detail={**ROAD, **main})


RECORD = InputRecord("1", "경남 밀양시 삼랑진읍 거족길16-58  101동 203호")


def build(row, record=RECORD):
    return engine.build_single(row, 0, record)


class SegmentTest(unittest.TestCase):
    def test_splits_two_addresses_in_one_cell(self):
        cases = {
            "충청남도 홍성군 서부면 남당리 230-16 경북 영덕군 영해면 예주시장1길 7-2":
                ["충청남도 홍성군 서부면 남당리 230-16", "경북 영덕군 영해면 예주시장1길 7-2"],
            "경기도 화성시 송산면 독지리 산 7-9 화성시 송산면 공룡로 152, 2동 103호":
                ["경기도 화성시 송산면 독지리 산 7-9", "화성시 송산면 공룡로 152, 2동 103호"],
            "영덕군 지품면 신양리463-62\n영덕군 영덕읍 경동로 8300": ["영덕군 지품면 신양리463-62", "영덕군 영덕읍 경동로 8300"],
            "본점: 서울특별시 송파구 법원로 11길 28\n서부센터: 광주광역시 북구 무등로 180번길 12":
                ["서울특별시 송파구 법원로 11길 28", "광주광역시 북구 무등로 180번길 12"],
        }
        for text, expected in cases.items():
            self.assertEqual(engine.split_segments(text), expected, text)

    def test_keeps_one_address_with_details_or_repeated_region(self):
        for text in ["부산광역시 부산진구 부암동 52-6\n11층", "충청북도 진천군 진천읍 덕금로 186\n담당자 고객팀",
                     "강원 속초시 청호동 강원도 속초시 청호동 610 1/1", "제주시 조천읍\n와흘4길 13",
                     "경기도 강원특별자치도 인제군 인제읍 인제로187번길8"]:
            self.assertEqual(len(engine.split_segments(text)), 1, text)
        self.assertEqual(engine.split_segments(None), [])


class CompareTest(unittest.TestCase):
    def test_tokens_cut_at_name_boundaries(self):
        self.assertEqual(engine.stated_words("충청남도공주시동현새터길68"),
                         [("충청남도", "시도"), ("공주시", "시군구"), ("동현새터길", "도로명")])
        self.assertEqual(engine.stated_words("전라북도특별자치도 정읍시 충정로 234"),
                         [("전라북도", "시도"), ("정읍시", "시군구"), ("충정로", "도로명")])

    def test_component_outcomes(self):
        place = engine.Place("road", "울산광역시 울주군 삼남읍 공암2길 13", "울산광역시", "울주군", "삼남읍", "상천리",
                             "공암2길", 13, 0, False, "3171026522", "317104319071")
        out = {c["component"]: c for c in engine.compare("울산광역시 울주군 삼남면 공암2길 13-1", place)}
        self.assertEqual({k: v["outcome"] for k, v in out.items()}, {
            "시도": "match", "시군구": "match", "읍면동": "mismatch", "리": "unknown", "도로명": "match", "건물번호": "mismatch"})
        self.assertEqual(out["건물번호"]["evidence"], "본번은 같고 부번이 다름")
        self.assertEqual(out["읍면동"]["input_value"], "삼남면")

    def test_short_names_count_only_as_whole_words(self):
        place = engine.Place("road", "충청남도 공주시 동현새터길 68", "충청남도", "공주시", "동현동", None, "동현새터길", 68)
        out = {c["component"]: c["outcome"] for c in engine.compare("충청남도공주시동현새터길68", place)}
        self.assertEqual(out["읍면동"], "unknown")  # 동현새터길의 '동현'은 동현동이 아니다
        out = {c["component"]: c["input_value"] for c in engine.compare("정읍 충정로 234", engine.Place(
            "road", "x", "전북특별자치도", "정읍시", "수성동", None, "충정로", 234))}
        self.assertEqual(out["시군구"], "정읍")

    def test_city_only_sigungu_is_partial(self):
        place = engine.Place("road", "x", "경상북도", "포항시 북구", "기계면", None, "기계로", 160)
        out = {c["component"]: c for c in engine.compare("경상북도 포항시 기계면 기계로 160", place)}
        self.assertEqual((out["시군구"]["outcome"], out["시군구"]["input_value"]), ("unknown", "포항시"))
        self.assertIn("북구", out["시군구"]["evidence"])
        out = {c["component"]: c for c in engine.compare("경상북도 포항시 북구 기계면 기계로 160", place)}
        self.assertEqual(out["시군구"]["outcome"], "match")

    def test_lot_number_reads_beonji_ho(self):
        place = engine.Place.from_lot({**LOT, "lot_main": "449", "lot_sub": "1"})
        out = {c["component"]: c for c in engine.compare("경남 밀양시 삼랑진읍 율동리 449번지 1호", place)}
        self.assertEqual((out["지번"]["input_value"], out["지번"]["outcome"]), ("449-1", "match"))

    def test_region_validation_marks_unmatched_names(self):
        place = engine.Place("road", "x", "울산광역시", "울주군", "삼남읍", None, "공암2길", 13)
        validation, tokens = engine.region_validation("울산광역시 울주군 삼남면 공암2길 13", [place], "1", 2025)
        self.assertEqual(validation["status"], "no_match")
        self.assertEqual(validation["partial_match"]["unmatched_tokens"], [2])
        self.assertEqual(tokens[2]["raw"], "삼남면")
        validation, _ = engine.region_validation("울산광역시 울주군 삼남읍 공암2길 13", [place], "1", 2025)
        self.assertEqual(validation["status"], "unique_path")

    def test_road_lot_relation(self):
        self.assertEqual(engine.road_lot_relation({"a": ["L"], "b": ["L"]})["kind"], "1:N")
        self.assertEqual(engine.road_lot_relation({"a": ["L1", "L2"]})["kind"], "N:1")
        self.assertEqual(engine.road_lot_relation({"a": ["L1", "L2"], "b": ["L1"]})["kind"], "N:M")
        self.assertIsNone(engine.road_lot_relation({"a": ["L1"]}))


class ConvertTest(unittest.TestCase):
    def test_resolved_row(self):
        built = build(resolved())
        row = built.row
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["standardization"]["raw_record_ids"], ["6333175"])
        self.assertEqual(row["review"]["detail_units"], ["101동", "203호"])
        self.assertEqual(_source(row, 2025)[1], [(2025, 6333175)])
        self.assertEqual({c["component"]: c["outcome"] for c in built.comparisons[0]}["건물번호"], "match")
        self.assertEqual(row["region_validation"]["paths"][0][1]["name"], "밀양시")

    def test_building_on_two_lots_is_n_to_1(self):
        row = build(resolved(lot_addresses=["경상남도 밀양시 삼랑진읍 율동리 12", "경상남도 밀양시 삼랑진읍 율동리 13"])).row
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["output"]["road_lot_relation"]["kind"], "N:1")
        self.assertIn("도로명·지번 관계 N:1", row["review"]["candidates"][0]["evidence"][-1])

    def test_missing_code_is_address_only(self):
        row = build(resolved(administrative_area_code=None)).row
        self.assertEqual((row["status"], row["review"]["tier"]), ("address_only", "near_confirmed"))

    def test_single_candidate_hold_can_be_approved(self):
        row = build(sql_row("검토 필요", reason_code="region_partial", candidate_details=[ROAD])).row
        self.assertEqual((row["status"], row["review"]["review_type"], row["review"]["tier"]),
                         ("held", "지역명 미일치·유사 지명 후보", "check_needed"))
        self.assertEqual(approvable_candidate(row)["raw_records"], [[2025, 6333175]])

    def test_lot_with_two_buildings_is_1_to_n(self):
        row = build(sql_row("지번만 확정", reason_code="multiple_road_addresses", standard_lot_address=LOT["lot"],
                            candidate_details=[{**ROAD, "lot_addresses": [LOT["lot"]]},
                                               {**OTHER, "lot_addresses": [LOT["lot"]]}])).row
        self.assertEqual((row["status"], row["review"]["confirmed_lot"]), ("lot_only", LOT["lot"]))
        self.assertEqual(row["output"]["road_lot_relation"]["kind"], "1:N")
        self.assertEqual(row["review"]["status"], "tied_candidates")

    def test_tied_lots_keep_linked_roads(self):
        row = build(sql_row("검토 필요", reason_code="multiple_standard_outputs", lot_details=[LOT, {**LOT, "lot": "B"}])).row
        self.assertEqual([c["kind"] for c in row["review"]["candidates"]], ["lot", "lot"])
        self.assertEqual(row["review"]["candidates"][0]["linked_road_addresses"], LOT["road_addresses"])

    def test_no_match_and_blank(self):
        miss = build(sql_row("미일치", reason_code="number_not_found")).row
        self.assertEqual((miss["status"], miss["review"]["review_type"], miss["review"]["status"]),
                         ("no_match", "기준연도 번호 미일치", "no_candidate"))
        self.assertEqual(miss["region_validation"]["status"], "no_match")
        blank = build(sql_row("입력 없음", reason_code="blank_input"), InputRecord("1", None)).row
        self.assertEqual((blank["status"], blank["output"]["reason_code"], blank["review"]),
                         ("excluded", "blank_input", None))


class MultiTest(unittest.TestCase):
    def test_each_segment_is_matched_and_kept_as_candidates(self):
        record = InputRecord("9", "경남 밀양시 삼랑진읍 율동리 99 경남 밀양시 삼랑진읍 거족길 16-58")
        segments = engine.split_segments(record.raw_address)
        rows = [sql_row("미일치", "9#1", reason_code="number_not_found"), resolved("9#2")]
        dataset, comparisons = engine.build_dataset((record,), rows, run_id="r", steps=[], segments={"9": segments})
        row = dataset["results"][0]
        self.assertEqual((row["status"], row["review"]["review_type"]), ("held", "기타·복수 주소/필지"))
        self.assertEqual([p["segment"] for p in row["output"]["address_candidates"]], segments)
        self.assertEqual(row["output"]["address_candidates"][1]["standard_road_address"], ROAD["address"])
        self.assertTrue(row["review"]["candidates"][0]["evidence"][0].startswith("주소 2/2:"))
        self.assertEqual({c["component"]: c["outcome"] for c in comparisons["9"][0]}["건물번호"], "match")


class FakeEngine(engine.BigQueryEngineAdapter):
    """BigQuery 대신 정해 둔 행을 돌려준다. 워크벤치 run()의 계약 검사를 그대로 거친다."""

    def __init__(self, rows):
        self.reference_version = "2025"
        self.rows = rows

    def run_dataset(self, records, *, run_id, progress=None):
        dataset, comparisons = engine.build_dataset(records, self.rows, run_id=run_id, steps=[
            {"module_name": "bigquery_match", "module_version": "2", "status": "completed",
             "reason_code": None, "elapsed_ms": 10.0, "details": {}}])
        return AdapterResult(dataset, comparisons)

    def annotate_rows(self, rows):
        for row in rows:
            row["output_fields"] = {}


class ContractTest(unittest.TestCase):
    def test_workbench_run_accepts_engine_dataset(self):
        records = (RECORD, InputRecord("2", None), InputRecord("3", "어딘가"))
        rows = [resolved("1"), sql_row("입력 없음", "2", reason_code="blank_input"),
                sql_row("미일치", "3", reason_code="scope_not_found")]
        payload = run(FakeEngine(rows), records, source="bigquery", run_id="r1")
        dataset = payload["dataset"]
        self.assertEqual([row["status"] for row in dataset["results"]], ["resolved", "excluded", "no_match"])
        self.assertEqual(dataset["review_summary"]["기본 도로명주소 확인"], {"verified": 1})
        self.assertEqual(payload["candidate_comparisons"]["1"]["0"][0]["component"], "시도")

    def test_missing_rows_are_rejected(self):
        with self.assertRaises(ValueError):
            engine.build_dataset((RECORD, InputRecord("2", "x")), [resolved("1")], run_id="r", steps=[])


if __name__ == "__main__":
    unittest.main()


class PrepareTest(unittest.TestCase):
    def test_normalizes_order_names_and_repeats(self):
        cases = {
            "141-1 상촌길 백암리 대양면 합천군 경상남도": ["경상남도 합천군 대양면 백암리 상촌길 141-1"],
            "김해시 경원로11번길 경상남도 내동 17-10": ["경상남도 김해시 내동 경원로11번길 17-10"],
            "Chungnam 논산시 연산면 선비로755번길 2-6": ["충청남도 논산시 연산면 선비로755번길 2-6"],
            "경상남독 창원시 마산합포구 완월남3길 57": ["경상남도 창원시 마산합포구 완월남3길 57"],
            "부산광역시 부산광역시 영도구 해양로 82": ["부산광역시 영도구 해양로 82"],
            # 정상 어순의 지번·도로명 병기는 그대로 둔다.
            "전라남도 신안군 암태면 와촌리 238 중부로 1656-11": ["전라남도 신안군 암태면 와촌리 238 중부로 1656-11"],
        }
        for text, expected in cases.items():
            self.assertEqual(engine.prepare(text), expected, text)
        self.assertEqual(engine.prepare("   "), [])

    def test_numeric_only_is_excluded(self):
        row = build(sql_row("미일치", reason_code="missing_address_number"), InputRecord("1", "655")).row
        self.assertEqual((row["status"], row["output"]["reason_code"]), ("excluded", "numeric_only"))


class StrictTest(unittest.TestCase):
    def test_stated_dong_that_differs_blocks_confirmation(self):
        main = {**ROAD, "emd": "합정동", "admin_dongs": ["합정동"], "address": "서울특별시 마포구 독막로 223",
                "sido": "서울특별시", "sigungu": "마포구", "ri": None, "road_name": "독막로", "main_no": "223", "sub_no": "0"}
        row = build(sql_row("확정", standard_road_address=main["address"], main_detail=main),
                    InputRecord("1", "서울특별시 마포구 상수동 독막로 223")).row
        self.assertEqual((row["status"], row["output"]["reason_code"], row["review"]["tier"]),
                         ("held", "region_partial", "check_needed"))
        self.assertIn("상수동", row["output"]["reason"])

    def test_admin_dong_counts_as_match(self):
        main = {**ROAD, "emd": "서교동", "admin_dongs": ["합정동"], "address": "서울특별시 마포구 독막로 1",
                "sido": "서울특별시", "sigungu": "마포구", "ri": None, "road_name": "독막로", "main_no": "1", "sub_no": "0"}
        built = build(sql_row("확정", standard_road_address=main["address"], main_detail=main),
                      InputRecord("1", "서울특별시 마포구 합정동 독막로 1"))
        self.assertEqual(built.row["status"], "resolved")
        dong = next(c for c in built.comparisons[0] if c["component"] == "읍면동")
        self.assertEqual((dong["outcome"], dong["evidence"]), ("match", "행정동 합정동 일치 · 법정동 서교동"))


class SubNumberWarningTest(unittest.TestCase):
    def test_confirmed_without_sub_number_gets_warning_when_siblings_exist(self):
        main = {**ROAD, "address": "경상남도 밀양시 삼랑진읍 거족길 16", "sub_no": "0", "sibling_subs": [58, 60]}
        row = build(sql_row("확정", standard_road_address=main["address"], main_detail=main),
                    InputRecord("1", "경남 밀양시 삼랑진읍 거족길 16")).row
        self.assertEqual(row["status"], "resolved")
        self.assertIn("같은 본번에 부번이 붙은 건물 2개", row["output"]["warnings"][0])
        self.assertIn("거족길 16-58, 16-60", row["review"]["candidates"][0]["evidence"][-1])
        self.assertEqual(row["traces"][-1]["reason_code"], "sub_number_siblings")

    def test_no_warning_when_sub_number_given_or_no_siblings(self):
        self.assertIsNone(engine.sub_number_warning({**ROAD, "sibling_subs": [1]}))   # 16-58로 확정
        self.assertIsNone(engine.sub_number_warning({**ROAD, "sub_no": "0", "sibling_subs": []}))
        row = build(resolved()).row
        self.assertNotIn("warnings", row["output"])


class AiCorrectionTest(unittest.TestCase):
    def test_targets_only_rows_ai_can_fix(self):
        def row(i, status, code, reason="x", candidates=()):
            return {"record": {"address_id": i}, "status": status, "output": {"reason_code": code, "reason": reason},
                    "review": {"candidates": list(candidates)}}
        results = [row("a", "no_match", "scope_not_found"), row("b", "no_match", "missing_address_number"),
                   row("c", "no_match", "number_not_found", "리·동은 있으나 그 지번(…)이 건물 기준자료에 없음"),
                   row("d", "no_match", "number_not_found", "도로명은 있으나 그 건물번호(…)가 기준자료에 없음"),
                   row("e", "held", "scope_not_found", candidates=[{}]), row("f", "resolved", None)]
        self.assertEqual(engine.ai_targets(results), ["a", "d"])

    def test_verified_correction_becomes_approvable_candidate_not_confirmation(self):
        record = InputRecord("1", "경남 밀양시 삼랑진읍 거족길16-58")
        base = build(sql_row("미일치", reason_code="scope_not_found"), record).row
        results, comparisons = [base], {}
        fixes = [{"address_id": "1", "original_address": record.raw_address, "corrected_address": ROAD["address"],
                  "changes": "거족길 띄어쓰기", "inferred": None}]
        counts = engine.apply_ai(results, comparisons, fixes, [resolved("1")], {"1": record.raw_address})
        row = results[0]
        self.assertEqual(counts["verified"], 1)
        self.assertEqual((row["status"], row["review"]["review_type"], row["review"]["tier"]),
                         ("held", "AI 교정 후보", "check_needed"))
        self.assertTrue(row["review"]["candidates"][0]["evidence"][0].startswith("AI 교정:"))
        self.assertEqual(approvable_candidate(row)["address"], ROAD["address"])
        self.assertTrue(row["output"]["ai_correction"]["verified"])
        self.assertEqual(row["traces"][-1]["reason_code"], "ai_corrected_verified")
        self.assertIn(0, comparisons["1"])

    def test_unverified_correction_leaves_row_unchanged(self):
        record = InputRecord("1", "어딘가 99")
        base = build(sql_row("미일치", reason_code="scope_not_found"), record).row
        results = [base]
        fixes = [{"address_id": "1", "original_address": "어딘가 99", "corrected_address": "서울특별시 어딘가로 99"}]
        counts = engine.apply_ai(results, {}, fixes, [sql_row("미일치", reason_code="scope_not_found")], {})
        self.assertEqual((counts["unverified"], results[0]["status"]), (1, "no_match"))
        self.assertFalse(results[0]["output"]["ai_correction"]["verified"])

    def test_openai_settings_default_and_env(self):
        settings = engine.BigQueryEngineAdapter.ai_openai_settings.fget
        names = ("ADDRESS_BQ_AI_BATCH", "ADDRESS_BQ_AI_CONCURRENCY", "ADDRESS_BQ_AI_CHUNK")
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            self.assertEqual(settings(None), (24, 16, 480))
            os.environ.update(dict(zip(names, ("12", "8", "384"))))
            self.assertEqual(settings(None), (12, 8, 384))
            os.environ["ADDRESS_BQ_AI_CHUNK"] = "0"
            with self.assertRaises(ValueError):
                settings(None)
        finally:
            for name, value in saved.items():
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value

    def test_region_rewrite_is_flagged_and_not_fast_approvable(self):
        record = InputRecord("1", "서울특별시 밀양시 삼랑진읍 거족길 16-58")
        base = build(sql_row("미일치", reason_code="scope_not_found"), record).row
        results = [base]
        fixes = [{"address_id": "1", "original_address": record.raw_address, "corrected_address": ROAD["address"]}]
        counts = engine.apply_ai(results, {}, fixes, [resolved("1")], {"1": record.raw_address})
        row = results[0]
        self.assertEqual(counts["verified_region_changed"], 1)
        self.assertEqual(row["review"]["tier"], "general")
        self.assertTrue(row["review"]["candidates"][0]["evidence"][0].startswith("주의: AI가 원문의 시도"))
        self.assertIsNone(approvable_candidate(row))


def building_match(address_id, item=None, kind=1, same=1, found=1, name="거족빌라"):
    item = item or ROAD
    return {**item, "address_id": address_id, "match_kind": kind, "building_name": name,
            "same_name_in_sigungu": same, "found_buildings": found, "emd_hit": True}


class BuildingTest(unittest.TestCase):
    def test_phrase_extraction(self):
        self.assertEqual(engine.building_phrase("경남 통영시 정량동 종우원에이 아파트 705")[1:], ("종우원에이아파트", "종우원에이"))
        self.assertEqual(engine.building_phrase("경남 통영시 용남면 동달리 청구아파트 206동 105호")[2], "청구")
        self.assertEqual(engine.building_phrase("제주 제주시 건입동661-1장원사라빌라가동201호")[1], "장원사라빌라")
        self.assertIsNone(engine.building_phrase("경상남도 사천시 신수동 신수서길 37번지"))
        self.assertIsNone(engine.building_phrase("경남 거제시 장평동 아파트 106동"))

    def test_rows_without_candidates_get_building_candidates(self):
        record = InputRecord("1", "경남 밀양시 삼랑진읍 거족빌라 101동")
        base = build(sql_row("미일치", reason_code="missing_address_number"), record).row
        results, comparisons = [base], {}
        phrases = engine.building_requests(results, {"1": record.raw_address})
        self.assertEqual(phrases["1"][1], "거족빌라")
        counts = engine.apply_buildings(results, comparisons, [building_match("1")], phrases, {"1": record.raw_address})
        row = results[0]
        self.assertEqual(counts["candidates_added"], 1)
        self.assertEqual((row["status"], row["review"]["review_type"], row["review"]["tier"]),
                         ("held", "건물명 일치 후보", "check_needed"))
        candidate = row["review"]["candidates"][0]
        self.assertEqual((candidate["building_name"], candidate["building_input"]), ("거족빌라", "거족빌라"))
        self.assertIn("시군구 안 같은 이름 1곳", candidate["evidence"])
        self.assertEqual(comparisons["1"][0][-1]["component"], "건물명")
        self.assertEqual(approvable_candidate(row)["address"], ROAD["address"])

    def test_same_building_under_two_names_is_one_candidate(self):
        record = InputRecord("1", "경남 밀양시 삼랑진읍 거족빌라 101동")
        results = [build(sql_row("미일치", reason_code="missing_address_number"), record).row]
        phrases = engine.building_requests(results, {"1": record.raw_address})
        engine.apply_buildings(results, {}, [building_match("1", name="거족빌라"), building_match("1", name="거족 빌라트"),
                                             building_match("1", OTHER, name="거족빌라")], phrases, {"1": record.raw_address})
        candidates = results[0]["review"]["candidates"]
        self.assertEqual([c["address"] for c in candidates], [ROAD["address"], OTHER["address"]])
        self.assertEqual(candidates[0]["building_name"], "거족빌라 / 거족 빌라트")

    def test_common_or_loose_names_stay_general(self):
        record = InputRecord("1", "경남 밀양시 삼랑진읍 거족빌라 101동")
        for match in ([building_match("1", same=3)], [building_match("1", kind=3)],
                      [building_match("1", found=2), building_match("1", OTHER, found=2)]):
            base = build(sql_row("미일치", reason_code="missing_address_number"), record).row
            results = [base]
            engine.apply_buildings(results, {}, match, engine.building_requests(results, {"1": record.raw_address}),
                                   {"1": record.raw_address})
            self.assertEqual(results[0]["review"]["tier"], "general")

    def test_building_name_narrows_tied_candidates(self):
        record = InputRecord("1", "경남 밀양시 삼랑진읍 거족길 16 거족빌라")
        base = build(sql_row("검토 필요", reason_code="sub_number_omitted", candidate_details=[ROAD, OTHER]), record).row
        results = [base]
        phrases = engine.building_requests(results, {"1": record.raw_address})
        counts = engine.apply_buildings(results, {}, [building_match("1", OTHER, kind=0)], phrases,
                                        {"1": record.raw_address})
        review = results[0]["review"]
        self.assertEqual(counts["narrowed"], 1)
        self.assertEqual([c["rank"] for c in review["candidates"]], [2, 1])
        self.assertEqual((review["status"], review["tier"]), ("preferred_candidate", "check_needed"))
        self.assertEqual(approvable_candidate(results[0])["address"], OTHER["address"])

    def test_untyped_name_core_match_stays_general(self):
        record = InputRecord("1", "강원도 홍천군 홍천읍 무궁화 81")      # 도로명(무궁화로) 오타일 수 있음
        base = build(sql_row("미일치", reason_code="scope_not_found"), record).row
        results = [base]
        phrases = engine.building_requests(results, {"1": record.raw_address})
        engine.apply_buildings(results, {}, [building_match("1", kind=1, name="무궁화연립")], phrases, {"1": record.raw_address})
        self.assertEqual(results[0]["review"]["tier"], "general")

    def test_phrase_keeps_complex_number(self):
        self.assertEqual(engine.building_phrase("경남 통영시 무전동 무전8길 22 진우하이빌 3차 703호")[1], "진우하이빌3차")


class AbolishedTest(unittest.TestCase):
    def test_abolished_address_gets_reason_and_reference_candidates(self):
        record = InputRecord("1", "서울특별시 종로구 옥인동 필운대로11길 17")
        base = build(sql_row("미일치", reason_code="number_not_found"), record).row
        results = [base]
        self.assertEqual(engine.abolished_targets(results, {"1": record.raw_address}), ["1"])
        hit = {"address_id": "1", "kind": "road", "abolished_address": "서울특별시 종로구 필운대로11길 17",
               "lot_address": "서울특별시 종로구 옥인동 47-196", "changed_month": "202508", "road_name_code": "111104100",
               "legal_dong_code": "1111011600", "current_roads": ["서울특별시 종로구 필운대로11길 17-1"]}
        counts = engine.apply_abolished(results, [hit])
        row = results[0]
        self.assertEqual((counts["abolished"], counts["with_current_buildings"]), (1, 1))
        self.assertEqual((row["status"], row["output"]["reason_code"], row["review"]["review_type"]),
                         ("no_match", "abolished_address", "폐지된 주소"))
        self.assertIn("2025년 8월", row["output"]["reason"])
        self.assertEqual(row["review"]["candidates"][0]["rank"], 2)
        self.assertIsNone(approvable_candidate(row))
        # 폐지 주소로 판정된 행은 AI 교정 대상에서 빠진다.
        self.assertEqual(engine.ai_targets(results), [])

    def test_rows_with_candidates_are_not_targets(self):
        record = InputRecord("1", "x")
        held = build(sql_row("검토 필요", reason_code="sub_number_omitted", candidate_details=[ROAD]), record).row
        self.assertEqual(engine.abolished_targets([held], {"1": "x"}), [])


class RegionLedBuildingTest(unittest.TestCase):
    def test_region_led_names_are_buildings(self):
        for word in ("대전광역시청", "공주시청", "경상남도교육청", "서구청"):
            self.assertTrue(engine.region_led_building(word), word)
        for word in ("경상남도사천시", "신수동신수서길", "둔산동352-42", "포천시"):
            self.assertFalse(engine.region_led_building(word), word)
        self.assertEqual(engine.building_phrase("대전광역시 서구 둔산동 352-42 대전광역시청")[1], "대전광역시청")
        # 건물명 끝의 시도 이름 때문에 주소 두 개로 나누지 않는다.
        self.assertEqual(len(engine.prepare("대전광역시 서구 둔산동 352-42 대전광역시청")), 1)

    def test_exact_unique_name_adds_building_not_found_by_number(self):
        record = InputRecord("1", "충청남도 공주시 봉황동 325-52 공주시청")
        base = build(sql_row("검토 필요", reason_code="sub_number_mismatch", candidate_details=[OTHER]), record).row
        results, comparisons = [base], {}
        phrases = engine.building_requests(results, {"1": record.raw_address})
        self.assertEqual(phrases["1"][1], "공주시청")
        counts = engine.apply_buildings(results, comparisons, [building_match("1", kind=0, name="공주시청")], phrases,
                                        {"1": record.raw_address})
        review = results[0]["review"]
        self.assertEqual(counts["added_by_name"], 1)
        self.assertEqual([(c["address"], c["rank"]) for c in review["candidates"]], [(OTHER["address"], 2), (ROAD["address"], 1)])
        self.assertEqual((review["status"], review["tier"]), ("preferred_candidate", "check_needed"))
        self.assertIn(1, comparisons["1"])
        self.assertEqual(approvable_candidate(results[0])["address"], ROAD["address"])
        self.assertEqual(results[0]["status"], "held")

    def test_loose_name_does_not_override_number_candidates(self):
        record = InputRecord("1", "충청남도 공주시 봉황동 325-52 공주시청")
        base = build(sql_row("검토 필요", reason_code="sub_number_mismatch", candidate_details=[OTHER]), record).row
        results = [base]
        phrases = engine.building_requests(results, {"1": record.raw_address})
        counts = engine.apply_buildings(results, {}, [building_match("1", kind=2, name="공주시청년센터")], phrases,
                                        {"1": record.raw_address})
        self.assertEqual(counts["added_by_name"], 0)
        self.assertEqual(len(results[0]["review"]["candidates"]), 1)


class OldRegionTest(unittest.TestCase):
    def test_renames_old_regions(self):
        cases = {
            "충청남도 연기군 전동면 노장리 311번지 1호": "세종특별자치시 전동면 노장리 311번지 1호",
            "충청남도 연기군 남면 연기리 334번지": "세종특별자치시 연기면 연기리 334번지",
            "경남 마산시 합포구 진동면 진동리 384": "경남 창원시 마산합포구 진동면 진동리 384",
            "경남 진해시 성내동 195-1": "경남 창원시 진해구 성내동 195-1",
            "인천광역시 남구 매소홀로 68": "인천광역시 미추홀구 매소홀로 68",
            "제주도 북제주군 한림읍 옹포리": "제주도 제주시 한림읍 옹포리",
            "경북 군위군 효령면 경북대로 2952": "대구광역시 군위군 효령면 경북대로 2952",
            "충북 청원군 강외면 정중리 454번지": "충북 청주시 강외면 정중리 454번지",
            "서울특별시 중구 세종대로 110": "서울특별시 중구 세종대로 110",
        }
        for old, new in cases.items():
            self.assertEqual(engine.rename_old_regions(old)[0], new, old)
        notes = engine.rename_old_regions("인천 남구 주안동 1")[1]
        self.assertEqual(notes, [{"old": "인천 남구", "new": "미추홀구", "effective": "2018-07-01"}])
        self.assertEqual(engine.prepare("충청남도 연기군 서면 부동리 70번지"), ["세종특별자치시 서면 부동리 70번지"])

    def test_notes_are_shown(self):
        record = InputRecord("1", "충청남도 연기군 서면 부동리 70번지")
        row = build(sql_row("검토 필요", reason_code="sub_number_omitted", candidate_details=[ROAD]), record).row
        engine.apply_region_notes([row], {"1": engine.rename_old_regions(record.raw_address)[1]})
        self.assertTrue(row["output"]["reason"].startswith("옛 행정구역 이름을 현재 이름으로 바꿔 대조: 충청남도 연기군 → 세종특별자치시"))
        self.assertTrue(row["review"]["candidates"][0]["evidence"][0].startswith("옛 행정구역"))
        self.assertEqual(row["traces"][-1]["reason_code"], "old_region_renamed")

    def test_repeated_region_block_is_collapsed(self):
        self.assertEqual(engine.prepare("제주 제주시 조천읍 신흥리 제주 북제주군 조천읍 신흥리 7"), ["제주 제주시 조천읍 신흥리 7"])
        # 번호가 있는 진짜 두 주소는 그대로 나눈다.
        self.assertEqual(len(engine.prepare("영덕군 지품면 신양리463-62\n영덕군 영덕읍 경동로 8300")), 2)


class GluedNumberTest(unittest.TestCase):
    def rows(self, ids, status="no_match"):
        return [{"record": {"address_id": i}, "status": status, "review": None} for i in ids]

    def test_splits_only_four_or_more_digits_before_dong(self):
        texts = {"a": "부산광역시 강서구 신호산단3로 27106동 1101호", "b": "경상남도 거제시 수양로 107동 202호",
                 "c": "경상북도 포항시 남구 지곡로 1027동 303호", "d": "서울 강남구 테헤란로 152 101동"}
        found = engine.glued_targets(self.rows("abcd"), texts)
        self.assertEqual(list(found), ["a"])
        self.assertEqual(found["a"], ("부산광역시 강서구 신호산단3로 27 106동 1101호", "27", "106", "27106동"))

    def test_rows_with_candidates_are_skipped(self):
        rows = self.rows("a", status="held")
        rows[0]["review"] = {"candidates": [{}]}
        self.assertEqual(engine.glued_targets(rows, {"a": "신호산단3로 27106동"}), {})

    def test_match_becomes_candidate_not_confirmation(self):
        record = InputRecord("1", "경남 밀양시 삼랑진읍 거족길 16123동 101호")
        results, comparisons = [build(sql_row("미일치", reason_code="missing_address_number"), record).row], {}
        targets = {"1": ("경남 밀양시 삼랑진읍 거족길 16 123동 101호", "16", "123", "16123동")}
        counts = engine.apply_glued(results, comparisons, targets, [resolved("1")])
        row = results[0]
        self.assertEqual(counts["candidate"], 1)
        self.assertEqual((row["status"], row["review"]["review_type"], row["review"]["tier"]),
                         ("held", engine.GLUED_REVIEW, "check_needed"))
        self.assertTrue(row["review"]["candidates"][0]["evidence"][0].startswith("번호 나눠 읽기"))
        self.assertEqual(row["record"]["raw_address"], record.raw_address)

    def test_no_match_after_split_leaves_row(self):
        record = InputRecord("1", "어딘가로 16123동")
        results = [build(sql_row("미일치", reason_code="missing_address_number"), record).row]
        counts = engine.apply_glued(results, {}, {"1": ("어딘가로 16 123동", "16", "123", "16123동")},
                                    [sql_row("미일치", reason_code="number_not_found")])
        self.assertEqual((counts["not_found"], results[0]["status"]), (1, "no_match"))


def typo_item(address_id, typo, pos, item=None, via="road", ref_main=16, input_main=61):
    return {**(item or ROAD), "address_id": address_id, "via": via, "typo": typo, "pos": pos, "input_main": input_main,
            "ref_main": ref_main, "sub": 58, "lot_address": "경상남도 밀양시 삼랑진읍 율동리 12" if via == "lot" else None}


class NumberTypoTest(unittest.TestCase):
    def base(self):
        record = InputRecord("1", "경남 밀양시 삼랑진읍 거족길 61-58")
        return [build(sql_row("미일치", reason_code="number_not_found"), record).row]

    def test_targets_only_number_not_found_without_candidates(self):
        def row(i, status, code, candidates=()):
            return {"record": {"address_id": i}, "status": status, "output": {"reason_code": code},
                    "review": {"candidates": list(candidates)}}
        results = [row("a", "no_match", "number_not_found"), row("b", "no_match", "scope_not_found"),
                   row("c", "no_match", "number_not_found", [{}]), row("d", "held", "number_not_found")]
        self.assertEqual(engine.typo_targets(results, {"a": "x", "b": "x", "c": "x", "d": "x"}), ["a"])

    def test_candidates_are_ranked_warned_and_never_fast_approvable(self):
        results, comparisons = self.base(), {}
        items = [typo_item("1", "digit", 2, OTHER, ref_main=16, input_main=61), typo_item("1", "swap", 1)]
        counts = engine.apply_typos(results, comparisons, items, {"1": "경남 밀양시 삼랑진읍 거족길 61-58"})
        row = results[0]
        self.assertEqual((counts["rows"], row["status"], row["review"]["review_type"], row["review"]["tier"]),
                         (1, "no_match", engine.TYPO_REVIEW, "general"))
        first, second = row["review"]["candidates"]
        self.assertEqual((first["address"], first["rank"], second["rank"]), (ROAD["address"], 1, 2))
        self.assertIn("숫자 자리 바뀜", first["evidence"][0])
        self.assertTrue(first["evidence"][1].startswith("주의"))
        self.assertEqual(row["review"]["status"], "preferred_candidate")
        self.assertIsNone(approvable_candidate(row))
        self.assertEqual(set(comparisons["1"]), {0, 1})

    def test_lot_typo_names_the_parcel(self):
        results = self.base()
        engine.apply_typos(results, {}, [typo_item("1", "swap", 1, via="lot")], {"1": "x"})
        self.assertIn("지번 경상남도 밀양시 삼랑진읍 율동리 12", results[0]["review"]["candidates"][0]["evidence"][0])


class ParcelEvidenceTest(unittest.TestCase):
    def test_each_candidate_shows_its_own_parcel(self):
        rows = [{"output": {"reason": "지번 부번 생략(경상북도경주시내남면용장리134번지): 같은 본번의 기준자료 부번 1,3"},
                 "review": {"candidates": [
                     {"evidence": ["공통"], "linked_lot_addresses": ["경상북도 경주시 내남면 용장리 134-1", "경상북도 경주시 내남면 용장리 285-14"]},
                     {"evidence": ["공통"], "linked_lot_addresses": ["경상북도 경주시 내남면 용장리 산 134-3"]}]}},
                {"output": {"reason": "도로명 부번 생략(…)"}, "review": {"candidates": [{"evidence": ["공통"]}]}}]
        self.assertEqual(engine.add_parcel_evidence(rows), 2)
        self.assertEqual([c["evidence"][0] for c in rows[0]["review"]["candidates"]],
                         ["이 후보의 지번: 용장리 134-1", "이 후보의 지번: 용장리 산 134-3"])
        self.assertEqual(rows[1]["review"]["candidates"][0]["evidence"], ["공통"])


class UnlinkedLotTest(unittest.TestCase):
    def test_still_registered_building_becomes_candidate(self):
        record = InputRecord("1", "전라남도 완도군 고금면 덕동리 581번지")
        results = [build(sql_row("미일치", reason_code="number_not_found"), record).row]
        comparisons = {}
        now = {"address": "전라남도 완도군 고금면 충무사길 68", "road_name_code": "468904645161", "legal_dong_code": "4689033030",
               "administrative_area_code": "46890330", "lot_addresses": ["전라남도 완도군 고금면 덕동리 578"],
               "admin_dongs": ["고금면"], "serials": [1], "sido": "전라남도", "sigungu": "완도군", "emd": "고금면", "ri": "덕동리",
               "main_no": 68, "sub_no": 0}
        hit = {"address_id": "1", "kind": "lot", "abolished_address": now["address"],
               "lot_address": "전라남도 완도군 고금면 덕동리 581", "changed_month": "202411", "still_registered": True,
               "now_building": now, "current_roads": []}
        counts = engine.apply_abolished(results, [hit], comparisons=comparisons, texts={"1": record.raw_address})
        row = results[0]
        self.assertEqual(counts["lot_unlinked_building"], 1)
        self.assertEqual((row["status"], row["review"]["review_type"], row["review"]["tier"]),
                         ("held", "연결 지번이 빠진 건물", "general"))
        self.assertEqual(row["review"]["candidates"][0]["address"], now["address"])
        self.assertIn("덕동리 578", " ".join(row["review"]["candidates"][0]["evidence"]))
        self.assertIsNone(approvable_candidate(row))
        self.assertIn("1", comparisons)

    def test_really_abolished_stays_no_match(self):
        record = InputRecord("1", "전라남도 완도군 고금면 덕동리 581번지")
        results = [build(sql_row("미일치", reason_code="number_not_found"), record).row]
        hit = {"address_id": "1", "kind": "lot", "abolished_address": "전라남도 완도군 고금면 충무사길 68",
               "lot_address": "전라남도 완도군 고금면 덕동리 581", "changed_month": "202411", "still_registered": False,
               "now_building": None, "current_roads": []}
        engine.apply_abolished(results, [hit])
        self.assertEqual((results[0]["status"], results[0]["review"]["review_type"]), ("no_match", "폐지된 주소"))
