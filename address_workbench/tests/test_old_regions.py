import unittest
from unittest import mock
from importlib.util import find_spec

from pandas import DataFrame

from address_workbench.adapter import InputRecord, PipelineAdapter, run
from address_workbench.old_regions import rename_for_matching


class RenameTests(unittest.TestCase):
    def test_rename_and_collapse(self):
        text, notes = rename_for_matching("충남 연기군 조치원읍 신흥리 100")
        self.assertEqual(text, "세종특별자치시 조치원읍 신흥리 100")
        self.assertEqual(notes[0]["old"], "충청남도 연기군")
        self.assertEqual(rename_for_matching("제주 제주시 조천읍 신흥리 제주 북제주군 조천읍 신흥리 7")[0],
                         "제주 제주시 조천읍 신흥리 7")
        self.assertEqual(rename_for_matching("인천 남구 주안동 1")[0], "인천 미추홀구 주안동 1")
        # 바꿀 게 없으면 원문 그대로(공백·줄 구분 포함)
        self.assertEqual(rename_for_matching("서울  중구\n세종대로 110"), ("서울  중구\n세종대로 110", []))
        self.assertEqual(rename_for_matching(None), (None, []))


@unittest.skipUnless(find_spec("address_standardizer"), "address_standardizer를 불러올 수 없음")
class PipelineAdapterRenameTests(unittest.TestCase):
    def test_pipeline_sees_new_name_and_result_keeps_original(self):
        from address_standardizer.contracts import AddressResult, BatchResult, DatasetResult

        seen = []

        def fake_run_dataset(records, pipeline, *, run_id, config, batch_size, progress):
            seen.extend(record.raw_address for record in records)
            results = tuple(AddressResult(record, index, "no_match", (), None) for index, record in enumerate(records))
            batch = BatchResult(run_id, "2025", results, {"no_match": len(results)}, DataFrame())
            if progress:
                progress(results, batch)
            return DatasetResult(run_id, "2025", results, {"no_match": len(results)}, (batch,))

        streamed = []
        records = (InputRecord("a", "충청남도 연기군 남면 종촌리 1"), InputRecord("b", "서울 중구 세종대로 110"))
        with mock.patch("address_standardizer.run_dataset", fake_run_dataset):
            payload = run(PipelineAdapter(object(), "2025"), records, source="pipeline",
                          progress=lambda rows, batch: streamed.extend(rows))
        self.assertEqual(seen, ["세종특별자치시 연기면 종촌리 1", "서울 중구 세종대로 110"])
        first, second = payload["dataset"]["results"]
        self.assertEqual(first["record"]["raw_address"], "충청남도 연기군 남면 종촌리 1")
        self.assertEqual(first["traces"][0]["module_name"], "old_region_rename")
        self.assertEqual(second["traces"], [])
        self.assertEqual(streamed[0]["record"]["raw_address"], "충청남도 연기군 남면 종촌리 1")
        self.assertEqual(payload["dataset"]["batch_results"][0]["results"][0]["record"]["raw_address"],
                         "충청남도 연기군 남면 종촌리 1")


if __name__ == "__main__":
    unittest.main()
