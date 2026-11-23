import json
import threading
import unittest

from address_workbench.openai_correct import correct


def echo_fix(prompt, request):
    rows = json.loads(request)
    return json.dumps([{"id": r["id"], "corrected_address": r["address"].replace("번긷", "번길"),
                        "changes": "번긷→번길" if "번긷" in r["address"] else ""} for r in rows], ensure_ascii=False)


class CorrectTests(unittest.TestCase):
    def test_batches_and_keeps_ids(self):
        calls = []
        lock = threading.Lock()

        def generate(prompt, request):
            with lock:
                calls.append(len(json.loads(request)))
            return echo_fix(prompt, request)

        texts = {f"a{i}": f"망덕산로{i}번긷 1" for i in range(50)}
        fixes = correct(texts, generate, batch_size=24, concurrency=4)
        self.assertEqual(sorted(calls), [2, 24, 24])
        by_id = {f["address_id"]: f for f in fixes}
        self.assertEqual(by_id["a7"]["corrected_address"], "망덕산로7번길 1")
        self.assertEqual(by_id["a7"]["original_address"], "망덕산로7번긷 1")
        self.assertEqual(by_id["a7"]["changes"], "번긷→번길")

    def test_retries_once_then_gives_up(self):
        attempts = []

        def flaky(prompt, request):
            attempts.append(1)
            if len(attempts) == 1:
                return "not json"
            return echo_fix(prompt, request)

        self.assertEqual(correct({"a": "x로 1번긷"}, flaky)[0]["corrected_address"], "x로 1번길")

        def broken(prompt, request):
            raise TimeoutError

        fix = correct({"a": "x로 1"}, broken)[0]
        self.assertIsNone(fix["corrected_address"])
        self.assertEqual(fix["error"], "TimeoutError")

    def test_streams_requests_and_reports_each_chunk(self):
        texts = {f"a{i:02}": f"망덕산로{i}번긷 1" for i in range(50)}
        running, peak, lock = [0], [0], threading.Lock()
        release = threading.Event()

        def generate(prompt, request):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
                if running[0] == 3:
                    release.set()
            release.wait(2)  # 동시 3개가 모일 때까지 기다린다: 묶음 경계에서 멈추지 않는지 본다
            with lock:
                running[0] -= 1
            return echo_fix(prompt, request)

        reports = []
        fixes = correct(texts, generate, batch_size=10, concurrency=3, chunk_size=20,
                        on_chunk=lambda chunk, done, total: reports.append(([f["address_id"] for f in chunk], done, total)))
        self.assertEqual(peak[0], 3)
        self.assertEqual([(done, total) for _, done, total in reports], [(20, 50), (40, 50), (50, 50)])
        self.assertEqual(reports[0][0], [f"a{i:02}" for i in range(20)])
        self.assertEqual([f["address_id"] for f in fixes], sorted(texts))

    def test_rejects_non_positive_sizes(self):
        with self.assertRaises(ValueError):
            correct({"a": "x"}, echo_fix, chunk_size=0)

    def test_empty_and_null_answers_are_no_correction(self):
        def answer(prompt, request):
            return '```json\n[{"id":0,"corrected_address":"null"},{"id":1,"corrected_address":""}]\n```'

        fixes = correct({"a": "주소1", "b": "주소2"}, answer)
        self.assertEqual([f["corrected_address"] for f in fixes], [None, None])


if __name__ == "__main__":
    unittest.main()
