import unittest

from address_workbench.tabular import MAX_ROWS, read_table, records_from


def csv_rows(count):
    return ("id,주소\n" + "".join(f"r{i},서울특별시 서대문구 대신동 {i}\n" for i in range(1, count + 1))).encode()


class RowLimitTest(unittest.TestCase):
    def test_limit_is_16000_rows(self):
        self.assertEqual(MAX_ROWS, 16000)
        table, _ = read_table(csv_rows(16000), "csv", None)
        self.assertEqual(len(records_from(table, 1, 0)), 16000)

    def test_one_more_row_is_rejected(self):
        with self.assertRaises(ValueError):
            table, _ = read_table(csv_rows(16001), "csv", None)
            records_from(table, 1, 0)


if __name__ == "__main__":
    unittest.main()
