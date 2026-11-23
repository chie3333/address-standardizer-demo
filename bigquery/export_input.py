"""원본 입력 엑셀(농림어업DB 주소표준화 대상건)을 BigQuery 업로드용 CSV로 바꾼다.

    python bigquery/export_input.py "(2025.12월 기준)농림어업DB AI 주소표준화 대상건(15988건).xlsx" \
        bigquery/out/input_202512.csv --batch 202512

원문은 손대지 않고 그대로 옮긴다(앞뒤 공백 포함). 빈 셀만 빈 문자열로 둔다.
도로명주소와 지번주소를 각각 열로 남겨 BigQuery에서 함께 쓸 수 있게 한다.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import openpyxl

COLUMNS = ["batch", "address_id", "road_address", "lot_address", "source_row"]
HEADER = ("일련번호", "STDIZBFRADDR (도로명주소)", "STADDADDR (지번주소)")


def _text(value) -> str:
    return "" if value is None else str(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--batch", required=True, help="입력 묶음 이름, 예: 202512")
    parser.add_argument("--sheet", default="Data")
    args = parser.parse_args()

    sheet = openpyxl.load_workbook(args.source, read_only=True)[args.sheet]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)[:3]
    if tuple(header) != HEADER:
        raise ValueError(f"머리글이 예상과 다릅니다: {header}")
    Path(args.target).parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    count = 0
    with open(args.target, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(COLUMNS)
        for line, (serial, road, lot, *_) in enumerate(rows, start=2):
            if serial is None and road is None and lot is None:
                continue
            address_id = _text(serial)
            if not address_id or address_id in seen:
                raise ValueError(f"{line}행: 일련번호가 비었거나 중복입니다 ({address_id!r}).")
            seen.add(address_id)
            writer.writerow([args.batch, address_id, _text(road), _text(lot), line])
            count += 1
    print(f"{count}행 → {args.target}")


if __name__ == "__main__":
    main()
