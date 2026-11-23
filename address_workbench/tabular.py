"""Read table uploads without changing source row order."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from io import BytesIO, StringIO
from zipfile import BadZipFile, ZipFile

from .adapter import InputRecord

MAX_ROWS = 16000
MAX_COLUMNS = 256


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]


def as_text(value) -> str | None:
    return None if value is None else str(value)


def read_table(content: bytes, fmt: str, sheet: str | None = None) -> tuple[Table, list[str]]:
    if fmt == "csv":
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                decoded = content.decode("cp949")
            except UnicodeDecodeError as exc:
                raise ValueError("CSV 인코딩을 확인하세요. UTF-8 또는 CP949를 사용하세요.") from exc
        try:
            return collect(csv.reader(StringIO(decoded, newline=""), strict=True)), []
        except csv.Error as exc:
            raise ValueError(f"CSV 형식 오류: {exc}") from exc
    if fmt != "xlsx":
        raise ValueError("CSV 또는 XLSX 파일을 선택하세요.")
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as exc:
        raise ValueError("XLSX를 읽으려면 requirements.txt의 openpyxl을 설치하세요.") from exc
    try:
        with ZipFile(BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 32 * 1024 * 1024:
                raise ValueError("XLSX 압축 해제 크기는 32 MiB 이하여야 합니다.")
        book = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            sheets = [ws.title for ws in book.worksheets]
            if not sheets or (sheet is not None and sheet not in sheets):
                raise ValueError("읽을 수 있는 워크시트를 선택하세요.")
            ws = book[sheet or sheets[0]]
            if (ws.max_row or 0) > MAX_ROWS + 1 or (ws.max_column or 0) > MAX_COLUMNS:
                raise ValueError(f"시트 크기는 머리글 외 {MAX_ROWS}행, {MAX_COLUMNS}열 이하여야 합니다.")
            return collect(ws.values), sheets
        finally:
            book.close()
    except (BadZipFile, InvalidFileException, KeyError, OSError, SyntaxError) as exc:
        raise ValueError("XLSX 파일을 열 수 없습니다. 파일 형식을 확인하세요.") from exc


def collect(values) -> Table:
    iterator = iter(values)
    headers = tuple(as_text(value) or "" for value in next(iterator, ()))
    if not any(name.strip() for name in headers):
        raise ValueError("첫 행에 주소 열 이름이 있는 머리글을 넣으세요.")
    if len(headers) > MAX_COLUMNS:
        raise ValueError(f"열은 {MAX_COLUMNS}개 이하여야 합니다.")
    rows = []
    for index, row in enumerate(iterator, 1):
        if index > MAX_ROWS:
            raise ValueError(f"데이터 행은 {MAX_ROWS}개 이하여야 합니다.")
        if len(row) > len(headers):
            raise ValueError(f"데이터 {index}행의 열 개수가 머리글보다 많습니다.")
        rows.append(tuple(map(as_text, row)) + (None,) * (len(headers) - len(row)))
    if not rows:
        raise ValueError("머리글 아래에 처리할 데이터가 없습니다.")
    return Table(headers, tuple(rows))


def records_from(table: Table, address_col: int, id_col: int | None = None) -> tuple[InputRecord, ...]:
    for column in (address_col,) if id_col is None else (address_col, id_col):
        if not 0 <= column < len(table.headers):
            raise ValueError("파일에 있는 열을 선택하세요.")
        name = table.headers[column]
        if not name.strip() or table.headers.count(name) != 1:
            raise ValueError("선택한 열의 이름이 비어 있거나 중복됩니다.")
    if address_col == id_col:
        raise ValueError("주소와 ID는 서로 다른 열을 선택하세요.")
    records = []
    seen = set()
    for index, row in enumerate(table.rows, 1):
        address_id = row[id_col] if id_col is not None else f"row-{index}"
        if address_id is None or not address_id.strip() or address_id in seen:
            raise ValueError(f"데이터 {index}행의 ID가 비어 있거나 중복됩니다.")
        seen.add(address_id)
        records.append(InputRecord(address_id, row[address_col]))
    return tuple(records)
