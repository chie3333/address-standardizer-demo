"""기준자료 연결. 표를 파일로 저장하지 않고 RAW parquet와 폐지 이력 parquet를 DuckDB로 그대로 읽는다.

- 연결은 프로세스에 하나, 커서는 스레드마다 하나다. 한 번의 실행(run)은 한 스레드에서 돌고, 그 실행의 범위 임시 표
  (2_scope_reference, 4_building_scope)는 그 스레드 커서에만 있어 동시에 돌아가는 다른 실행과 섞이지 않는다.
- 원문(input_raw)은 메모리 표에 실행 묶음(batch)으로 올렸다가 끝나면 지운다.

환경 변수
    ADDRESS_RAW_PARQUET      RAW_DATA_2025.parquet (기본: 제공 자료 폴더)
    ADDRESS_HISTORY_PARQUET  폐지 이력 parquet (기본: data/ADDRESS_HISTORY_202410_202512.parquet)
    ADDRESS_PIPELINE_MEMORY  DuckDB 메모리 상한 (기본 4GB)
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Mapping

from .duckdb_functions import register

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = Path(__file__).resolve().parent / "sql"
DEFAULT_RAW = ROOT / "data" / "RAW_DATA_2025.parquet"
DEFAULT_HISTORY = ROOT / "data" / "ADDRESS_HISTORY_202410_202512.parquet"
INPUT_COLUMNS = ("batch", "address_id", "road_address", "lot_address", "source_row")
# NFKC 정규화로 바뀔 수 있는 RAW 이름 열. 바뀌는 이름만 nfkc_changes에 담아 조회 키를 만들 때 쓴다.
NAME_COLUMNS = ("도로명", "법정동읍면동명", "법정동리명", "행정동명", "건물명", "시군구관리건물명")


def read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def statements(sql: str) -> list[str]:
    """SQL 파일을 문장 단위로 나눈다. 문장 끝은 줄 끝의 ';'로 본다(문자열 안의 ';'는 쓰지 않는다)."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [part.strip() for part in re.split(r";\s*(?:\n|$)", body) if part.strip()]


def _params(sql: str, values: Mapping) -> dict:
    """문장에 실제로 쓰인 $이름 파라미터만 넘긴다(DuckDB는 안 쓰는 파라미터를 받으면 오류를 낸다)."""
    return {name: value for name, value in values.items() if re.search(rf"\${name}\b", sql)}


def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


class Sources:
    def __init__(self, raw: str | Path | None = None, history: str | Path | None = None):
        self.raw = Path(raw or os.environ.get("ADDRESS_RAW_PARQUET") or DEFAULT_RAW)
        self.history = Path(history or os.environ.get("ADDRESS_HISTORY_PARQUET") or DEFAULT_HISTORY)
        self.memory_limit = os.environ.get("ADDRESS_PIPELINE_MEMORY", "4GB")
        self._con = None
        self._generation = 0
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def con(self):
        with self._lock:
            if self._con is None:
                self._con = self._open()
        return self._con

    def reset(self) -> None:
        """연결을 버리고 다음 조회 때 새로 연다. DuckDB 내부 오류로 연결이 못 쓰게 됐을 때 부른다(메모리의 원문 묶음도 사라진다)."""
        with self._lock:
            old, self._con = self._con, None
            self._generation += 1
        try:
            if old is not None:
                old.close()
        except Exception:  # noqa: BLE001 - 이미 망가진 연결이면 닫기도 실패할 수 있다.
            pass

    def _open(self):
        import duckdb

        for path in (self.raw, self.history):
            if not path.exists():
                raise FileNotFoundError(f"기준자료 파일이 없습니다: {path}")
        con = duckdb.connect()
        # 기본값(컴퓨터 메모리의 80%) 대신 상한을 둔다. 넘치면 DuckDB가 임시 파일로 내려 쓴다.
        con.execute(f"SET memory_limit = '{self.memory_limit}'")
        register(con)
        # NFKC로 바뀌는 이름만 모은다(2025 RAW에서는 도로명 1개, 건물명 2천여 개). 전체 800만 행에 Python 함수를 쓰지 않는다.
        changes = []
        for column in NAME_COLUMNS:
            values = con.execute(f'SELECT DISTINCT "{column}" FROM read_parquet(?) WHERE "{column}" IS NOT NULL',
                                 [str(self.raw)]).fetchall()
            changes += [(value, norm) for (value,) in values if (norm := unicodedata.normalize("NFKC", value)) != value]
        con.execute("CREATE TABLE nfkc_changes (raw VARCHAR, norm VARCHAR)")
        if changes:
            con.executemany("INSERT INTO nfkc_changes VALUES (?, ?)", sorted(set(changes)))
        sql = read_sql("0_sources.sql").replace("{raw_parquet}", _quote(self.raw)).replace(
            "{history_parquet}", _quote(self.history))
        for statement in statements(sql):
            con.execute(statement)
        con.execute("CREATE TABLE input_raw (batch VARCHAR, address_id VARCHAR, road_address VARCHAR, "
                    "lot_address VARCHAR, source_row BIGINT)")
        return con

    def cursor(self):
        """이 스레드의 커서. 임시 표(실행 범위)가 이 커서에 남으므로 실행 하나는 같은 스레드에서 끝까지 돈다."""
        cur = getattr(self._local, "cursor", None)
        if cur is None or getattr(self._local, "generation", None) != self._generation:
            con = self.con
            with self._lock:
                cur = con.cursor()
                generation = self._generation
            register(cur)
            cur.execute("SET search_path = 'temp.main,memory.main'")
            self._local.cursor, self._local.generation = cur, generation
        return cur

    def query(self, sql: str | list[str], params: Mapping | None = None,
              array: Mapping[str, tuple[str, list]] | None = None) -> list[dict]:
        """sql이 목록이면 차례로 실행하고 마지막 결과를 dict 목록으로 돌려준다."""
        values = {**(params or {}), **{name: list(items) for name, (_, items) in (array or {}).items()}}
        cur = self.cursor()
        result = None
        for statement in ([sql] if isinstance(sql, str) else sql):
            try:
                result = cur.execute(statement, _params(statement, values))
            except Exception as exc:
                exc.add_note("DuckDB 문장: " + " ".join(statement.split())[:160])
                raise
        if result is None or result.description is None:
            return []
        names = [column[0] for column in result.description]
        return [dict(zip(names, row)) for row in result.fetchall()]

    def append_rows(self, table: str, rows: list[dict]) -> int:
        if table != "input_raw":
            raise ValueError("input_raw에만 행을 올린다.")
        if rows:
            self.cursor().executemany("INSERT INTO memory.main.input_raw VALUES (?, ?, ?, ?, ?)",
                                      [tuple(row.get(column) for column in INPUT_COLUMNS) for row in rows])
        return len(rows)

    def delete_batches(self, batches: list[str]) -> None:
        self.query("DELETE FROM memory.main.input_raw WHERE list_contains($batches, batch)",
                   array={"batches": ("STRING", batches)})


def is_broken_connection(exc: BaseException) -> bool:
    """DuckDB 내부 오류인지. DuckDB 2.0은 가끔 'dict-surviving cache … changed identity' 내부 오류를 내고
    그 연결을 못 쓰게 만든다. 이때는 연결을 새로 열고 다시 실행한다."""
    return type(exc).__name__ in {"InternalException", "FatalException"} and type(exc).__module__.startswith("_duckdb")
