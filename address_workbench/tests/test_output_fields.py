"""확정 주소를 제출 양식 19개 컬럼으로 펼치는 규칙을 실제 2025 RAW_DATA로 검사한다."""

from pathlib import Path
import unittest

from address_workbench.output_fields import COMBINED_COLUMN, OUTPUT_COLUMNS, annotate, building_lookup, raw_lookup

ROOT = Path(__file__).resolve().parents[2]
RAW_2025 = ROOT / "data" / "RAW_DATA_2025.parquet"
STANDARD = {"standard_road_address": "경기도 양주시 은현면 은현로312번길 158", "road_name_code": "416304436308",
            "legal_dong_code": "4163031025", "administrative_area_code": "31260310"}


@unittest.skipUnless(RAW_2025.is_file(), "2025 기준자료가 없습니다.")
class OutputFieldsTest(unittest.TestCase):
    def fields(self, row):
        annotate([row], raw_lookup({2025: RAW_2025}), 2025, building_lookup({2025: RAW_2025}))
        self.assertEqual(tuple(row["output_fields"]), (COMBINED_COLUMN, *OUTPUT_COLUMNS))
        return row["output_fields"]

    def test_resolved_row_fills_all_columns_in_order(self):
        # RAW 2911659 = 양주시 은현면 하패리 771-3 · 은현로312번길 158
        fields = self.fields({
            "status": "resolved",
            "standardization": {"status": "resolved", "address": STANDARD, "raw_record_ids": ["2911659"]},
            "review": {"detail_units": ["101동", "지하 2층", "203호"], "candidates": []},
        })
        self.assertEqual(
            [fields[c] for c in ("법정동시도명", "법정동시군구명", "법정동읍면동명", "법정동리명")],
            ["경기도", "양주시", "은현면", "하패리"])
        # 은현로312번길 158은 하패리 771-3·771-8·771-9에 걸쳐 있다. 확정 근거 771-3이 맨 앞.
        self.assertEqual((fields["대지구분코드"], fields["지번본번"], fields["지번부번"]),
                         ("1, 1, 1", "771, 771, 771", "3, 8, 9"))
        self.assertEqual(fields["지번목록"], "하패리 771-3, 하패리 771-8, 하패리 771-9")
        self.assertEqual((fields["도로명"], fields["건물본번"], fields["건물부번"]), ("은현로312번길", "158", "0"))
        self.assertEqual((fields["도로명코드"], fields["법정동코드"], fields["행정구역분류코드"]),
                         ("416304436308", "4163031025", "31260310"))
        self.assertEqual((fields["건물동명"], fields["건물층명"], fields["건물호명"], fields["층구분코드"]),
                         ("101", "2", "203", "1"))
        self.assertEqual(fields["지하구분코드"], "0")
        self.assertEqual(fields[COMBINED_COLUMN], "경기도 양주시 은현면 은현로312번길 158 101동 지하 2층 203호")

    def test_building_on_several_lots_lists_every_lot(self):
        # 망덕산로105번길 39-32(RAW 7347187 = 시동리 1622)는 시동리 1621·1622·1623 세 필지에 걸쳐 있다.
        fields = self.fields({
            "status": "resolved",
            "standardization": {"status": "resolved", "address": {"standard_road_address": "x"},
                                "raw_record_ids": ["7347187"]},
            "review": {"candidates": []},
        })
        self.assertEqual(fields["지번본번"], "1622, 1621, 1623")  # 확정 근거 지번이 맨 앞
        self.assertEqual(fields["지번부번"], "0, 0, 0")
        self.assertEqual(fields["대지구분코드"], "1, 1, 1")
        self.assertEqual(fields["지번목록"], "시동리 1622, 시동리 1621, 시동리 1623")
        self.assertEqual((fields["도로명"], fields["건물본번"], fields["건물부번"]), ("망덕산로105번길", "39", "32"))

    def test_unconfirmed_row_is_blank(self):
        fields = self.fields({"status": "held", "review": {"candidates": [{"raw_records": [[2025, 2911659]]}]}})
        self.assertTrue(all(value == "" for value in fields.values()))

    def test_approved_candidate_uses_its_raw_row(self):
        fields = self.fields({
            "status": "held",
            "approval": {"status": "reviewed_confirmed", "candidate_address": STANDARD["standard_road_address"],
                         "standard_address": STANDARD},
            "review": {"candidates": [{"address": STANDARD["standard_road_address"], "raw_records": [[2025, 2911659]]}]},
        })
        self.assertEqual((fields["도로명"], fields["건물본번"]), ("은현로312번길", "158"))
        self.assertEqual(fields["지번목록"], "하패리 771-3, 하패리 771-8, 하패리 771-9")

    def test_lot_only_leaves_road_columns_blank(self):
        fields = self.fields({
            "status": "lot_only",
            "review": {"confirmed_lot": "경기도 양주시 은현면 하패리 771-3",
                       "candidates": [{"raw_records": [[2025, 2911659]]}]},
        })
        self.assertEqual((fields["법정동리명"], fields["지번본번"]), ("하패리", "771"))
        self.assertEqual((fields["도로명"], fields["도로명코드"], fields["건물본번"]), ("", "", ""))
        self.assertEqual(fields[COMBINED_COLUMN], "")


if __name__ == "__main__":
    unittest.main()
