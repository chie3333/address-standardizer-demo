"""BigQuery 단계의 변환·병합 규칙. BigQuery 연결 없이 돈다."""
import unittest

from address_workbench import bigquery_stage as stage


def pipeline(status, road="", **extra):
    return {"status": status, "standard_road_address": road, "road_name_code": "1" if road else "",
            "legal_dong_code": "2" if road else "", "administrative_area_code": "3" if road else "", **extra}


def sql(status, road="", lot="", reason="사유"):
    return {"status": status, "standard_road_address": road, "standard_lot_address": lot, "reason": reason,
            "road_name_code": "9" if road else "", "legal_dong_code": "8" if road else "",
            "administrative_area_code": "7" if road else ""}


SOURCE = {"address_id": "1", "road_address": "원문 도로명", "lot_address": "원문 지번"}


class InputRowsTest(unittest.TestCase):
    def test_keeps_text_and_skips_empty_rows(self):
        rows = stage.input_rows_from_values([(1, None, " 가지리 453번지"), (None, None, None), (2, "갈현로 1", None)], "202512")
        self.assertEqual([r["address_id"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["road_address"], "")
        self.assertEqual(rows[0]["lot_address"], " 가지리 453번지")
        self.assertEqual(rows[1]["source_row"], 4)

    def test_rejects_duplicate_or_missing_id(self):
        with self.assertRaises(ValueError):
            stage.input_rows_from_values([(1, "a", None), (1, "b", None)], "b")
        with self.assertRaises(ValueError):
            stage.input_rows_from_values([(None, "a", None)], "b")


class ResultRowTest(unittest.TestCase):
    def test_resolved_row_keeps_codes_as_text(self):
        row = {
            "record": {"address_id": "7", "raw_address": "밀양시 거족길 16-58"}, "input_index": 6, "status": "resolved",
            "output": {}, "traces": [], "review": None, "region_validation": None,
            "standardization": {"address": {"standard_road_address": "경상남도 밀양시 삼랑진읍 거족길 16-58",
                                            "road_name_code": "482704808039", "legal_dong_code": "4827025024",
                                            "administrative_area_code": "38080110"}},
        }
        out = stage.result_row(row, "run", "2025", "2026-11-16 00:00:00")
        self.assertEqual(out["road_name_code"], "482704808039")
        self.assertEqual(out["excluded_reason"], "")
        self.assertEqual(set(out), {
            "run_id", "loaded_at", "reference_year", "address_id", "input_index", "raw_address", "status",
            "excluded_reason", "failed_reason", "review_type", "base_reason", "review_status", "tier",
            "candidate_count", "top_candidate", "confirmed_lot", "sido", "sigungu", "standard_road_address",
            "road_name_code", "legal_dong_code", "administrative_area_code"})

    def test_excluded_and_held_reasons(self):
        base = {"record": {"address_id": "1"}, "input_index": 0, "traces": [], "review": None,
                "region_validation": None, "standardization": None}
        excluded = stage.result_row({**base, "status": "excluded", "output": {"reason_code": "po_box"}}, "r", "2025", "t")
        held = stage.result_row({**base, "status": "held", "output": {"reason_code": "multiple_addresses"}}, "r", "2025", "t")
        self.assertEqual((excluded["excluded_reason"], excluded["base_reason"]), ("po_box", ""))
        self.assertEqual((held["excluded_reason"], held["base_reason"]), ("", "multiple_addresses"))


class MergeTest(unittest.TestCase):
    def test_pipeline_confirmation_wins_and_records_agreement(self):
        out = stage.merge_row(SOURCE, pipeline("resolved", "A"), sql("확정", "A"))
        self.assertEqual((out["최종_판정"], out["최종_도로명주소"], out["판정_출처"], out["두_방식_일치"]),
                         ("확정", "A", "파이프라인", "일치"))
        self.assertEqual(out["도로명코드"], "1")

    def test_conflicting_confirmations_go_to_review(self):
        out = stage.merge_row(SOURCE, pipeline("resolved", "A"), sql("확정", "B"))
        self.assertEqual((out["최종_판정"], out["최종_도로명주소"], out["판정_출처"]), ("검토 필요", "", "두 방식 불일치"))

    def test_bigquery_fills_rows_the_pipeline_could_not_confirm(self):
        out = stage.merge_row(SOURCE, pipeline("held"), sql("확정", "B", lot="가지리 453", reason="지번 일치"))
        self.assertEqual((out["최종_판정"], out["최종_도로명주소"], out["최종_지번주소"], out["판정_출처"], out["판정_사유"]),
                         ("확정", "B", "가지리 453", "BigQuery 보조", "지번 일치"))
        self.assertEqual(out["도로명코드"], "9")

    def test_policy_exclusion_is_kept(self):
        out = stage.merge_row(SOURCE, pipeline("excluded", excluded_reason="po_box"), sql("확정", "B"))
        self.assertEqual((out["최종_판정"], out["판정_출처"]), ("제외", "파이프라인 정책"))

    def test_duplicate_or_blank_exclusion_uses_bigquery(self):
        out = stage.merge_row(SOURCE, pipeline("excluded", excluded_reason="duplicate_input"), sql("지번만 확정", lot="L"))
        self.assertEqual((out["최종_판정"], out["최종_지번주소"]), ("지번만 확정", "L"))

    def test_pipeline_lot_only_is_kept(self):
        out = stage.merge_row(SOURCE, pipeline("lot_only", confirmed_lot="P"), sql("확정", "B"))
        self.assertEqual((out["최종_판정"], out["최종_지번주소"], out["판정_출처"]), ("지번만 확정", "P", "파이프라인"))

    def test_unresolved_rows(self):
        review = stage.merge_row(SOURCE, pipeline("no_match"), sql("검토 필요", reason="부번 생략"))
        missing = stage.merge_row(SOURCE, pipeline("no_match", base_reason="number_not_found"), sql("미일치", reason="없음"))
        no_bigquery = stage.merge_row(SOURCE, pipeline("held", review_type="부번 생략"), None)
        self.assertEqual((review["최종_판정"], review["판정_사유"]), ("검토 필요", "부번 생략"))
        self.assertEqual((missing["최종_판정"], missing["판정_사유"]), ("미일치", "없음"))
        self.assertEqual((no_bigquery["최종_판정"], no_bigquery["판정_출처"], no_bigquery["판정_사유"]),
                         ("검토 필요", "파이프라인", "부번 생략"))

    def test_every_row_has_all_columns(self):
        rows = stage.merge([SOURCE, {**SOURCE, "address_id": "2"}], {"1": pipeline("resolved", "A")}, {})
        self.assertEqual([list(row) for row in rows], [list(stage.MERGED_COLUMNS)] * 2)
        self.assertEqual(rows[1]["최종_판정"], "미일치")


class SettingsTest(unittest.TestCase):
    def test_sql_namespace_follows_settings(self):
        text = stage.Settings("other-project", "sandbox").sql("05_standardize_all.sql")
        self.assertIn("`other-project.sandbox.input_raw`", text)
        self.assertNotIn(stage.SQL_NAMESPACE, text)
        self.assertIn("batch = @batch", text)


if __name__ == "__main__":
    unittest.main()
