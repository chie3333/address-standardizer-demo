"""DuckDB 엔진이 SQL에서 부르는 함수. BigQuery의 JavaScript 함수와 NORMALIZE(…, NFKC)를 Python으로 옮겼다.

- parse_address_candidates(text): bigquery/sql/04_parse_function.sql과 같은 규칙으로 '이름 + 번호' 후보를 뽑는다.
- typo_kind(a, b): bigquery/sql/10_number_typo_candidates.sql의 임시 함수와 같다. SQL에서는 같은 규칙의 매크로를 쓰고,
  이 Python 함수는 매크로를 검사하는 기준으로 둔다.
- nfkc(text): BigQuery NORMALIZE(text, NFKC). DuckDB에는 NFC만 있다.
- 매크로 all_parts_in / any_part_in: BigQuery SQL에서 되풀이되는 'UNNEST(SPLIT(시군구, ' '))' 확인을 한 곳에 모았다.

JavaScript 정규식과 결과가 같도록 숫자는 [0-9]로 적었다(Python의 \\d는 다른 문자 체계의 숫자도 받는다).
"""
from __future__ import annotations

import re
import unicodedata

PARSE_TYPE = "STRUCT(kind VARCHAR, name VARCHAR, mountain BOOLEAN, main BIGINT, sub BIGINT, matched VARCHAR)[]"

_PHONE = re.compile(r"(^|[^0-9-])0(?:1[016789]|2|[3-6][1-5])[-.\s]?[0-9]{3,4}[-.\s]?[0-9]{4}(?![0-9-])")
_PAREN = re.compile(r"\([^()0-9]*\)")
_SPACED_DIGITS = re.compile(r"([0-9])\s+(?=[0-9])")
_ROAD = re.compile(r"([가-힣A-Za-z0-9·.]*?(?:로|길|거리)(?:[0-9]+번?[가-힣]?길)?)([0-9]+)(?:-([0-9]+))?"
                   r"(?![0-9가동층호]|번?[가-힣]?길|번[가-힣])")
_LOT = re.compile(r"([가-힣0-9]*?(?:동|리|가|읍|면))(?:[0-9]+[통반])*(산)?([0-9]+)(?![0-9통반동층가호])(?:번지)?"
                  r"(?:-([0-9]+)(?![0-9])(?:번지|호)?|([0-9]+)(?![0-9])호)?")
_ADMIN_RI = re.compile(r"[가-힣][0-9]+리$")


def parse_address_candidates(text: str | None) -> list[dict]:
    """원문에서 도로명(road)·지번(lot) 후보 이름과 번호를 뽑는다. sub가 None이면 원문에 부번이 없다."""
    if not text:
        return []
    s = unicodedata.normalize("NFKC", text).replace("&middot;", "·").replace("&amp;", "&").replace("&nbsp;", " ")
    s = _PHONE.sub(r"\1 ", s)
    s = _PAREN.sub(" ", s)
    s = _SPACED_DIGITS.sub(r"\1|", s)
    c = re.sub(r"\s+", "", s).replace("~", "-")
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(kind: str, name: str, mountain: bool, main: int, sub: int | None, matched: str) -> None:
        if kind == "lot" and _ADMIN_RI.search(name):          # 행정리(옥산2리)는 법정리(옥산리)로도 찾는다
            add(kind, re.sub(r"([가-힣])[0-9]+리$", r"\1리", name), mountain, main, sub, matched)
        if main >= 10 ** 12 or (sub or 0) >= 10 ** 12:     # 주소 번호일 수 없는 긴 숫자(BIGINT 범위 밖 방지)
            return
        for k in range(2, min(25, len(name)) + 1):
            n = name[len(name) - k:]
            if kind == "lot" and n[0].isascii() and n[0].isdigit():
                continue
            key = (kind, n, mountain, main, sub)
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "name": n, "mountain": mountain, "main": main, "sub": sub, "matched": matched})

    for m in _ROAD.finditer(c):
        add("road", m[1], False, int(m[2]), None if m[3] is None else int(m[3]), m[0])
    for m in _LOT.finditer(c):
        sub = int(m[4]) if m[4] is not None else (int(m[5]) if m[5] is not None else None)
        add("lot", m[1], m[2] is not None, int(m[3]), sub, m[0])
    return out


def typo_kind(a: str | None, b: str | None) -> str | None:
    """두 번호의 차이: swap(이웃 자리 바뀜), digit(한 자리 다름), missing(a에 하나 빠짐), extra(a에 하나 더 있음)."""
    if a is None or b is None or a == b:
        return None
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        if len(d) == 1:
            return "digit"
        if len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]]:
            return "swap"
        return None
    if abs(len(a) - len(b)) != 1:
        return None
    short, long = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long)):
        if long[:i] + long[i + 1:] == short:
            return "missing" if len(a) < len(b) else "extra"
    return None


def nfkc(text: str | None) -> str | None:
    return None if text is None else unicodedata.normalize("NFKC", text)


MACROS = (
    # 시군구 이름의 모든 단어가 원문에 있다(단어가 3자 이상이면 끝 글자 뺀 형태도 받음). 시군구가 NULL이면 참.
    """CREATE OR REPLACE TEMP MACRO all_parts_in(t, sg) AS
       sg IS NULL OR len(list_filter(string_split(sg, ' '), lambda p:
         NOT (strpos(t, p) > 0 OR (length(p) >= 3 AND strpos(t, substr(p, 1, length(p) - 1)) > 0)))) = 0""",
    # 시군구 이름의 단어 하나라도 원문에 있다.
    """CREATE OR REPLACE TEMP MACRO any_part_in(t, sg) AS
       sg IS NOT NULL AND len(list_filter(string_split(sg, ' '), lambda p:
         strpos(t, p) > 0 OR (length(p) >= 3 AND strpos(t, substr(p, 1, length(p) - 1)) > 0))) > 0""",
    # 목록에서 처음 나온 순서대로 중복을 뺀다(BigQuery의 SELECT DISTINCT FROM UNNEST 결과 순서와 맞춘다).
    """CREATE OR REPLACE TEMP MACRO uniq(l) AS
       list_filter(list_filter(l, lambda x: x IS NOT NULL), lambda x, i:
         list_position(list_filter(l, lambda y: y IS NOT NULL), x) = i)""",
    # 번호 오타 종류(10). typo_kind 함수와 같은 결과를 SQL로 낸다(큰 쿼리 안에서 Python 함수를 부르지 않으려고).
    """CREATE OR REPLACE TEMP MACRO typo_kind(a, b) AS CASE
       WHEN a IS NULL OR b IS NULL OR a = b THEN NULL
       WHEN length(a) = length(b) THEN CASE
         WHEN len(list_filter(range(1, length(a) + 1), lambda i: substr(a, i, 1) != substr(b, i, 1))) = 1 THEN 'digit'
         WHEN len(list_filter(range(1, length(a) + 1), lambda i: substr(a, i, 1) != substr(b, i, 1))) = 2
          AND len(list_filter(range(1, length(a)), lambda i: substr(a, i, 1) = substr(b, i + 1, 1)
                AND substr(a, i + 1, 1) = substr(b, i, 1) AND substr(a, i, 1) != substr(b, i, 1))) = 1 THEN 'swap'
         END
       WHEN length(a) + 1 = length(b) AND len(list_filter(range(1, length(b) + 1),
              lambda i: substr(b, 1, i - 1) || substr(b, i + 1) = a)) > 0 THEN 'missing'
       WHEN length(a) = length(b) + 1 AND len(list_filter(range(1, length(a) + 1),
              lambda i: substr(a, 1, i - 1) || substr(a, i + 1) = b)) > 0 THEN 'extra'
       END""",
    # 글자 단위 편집 거리가 1 이하인지. DuckDB의 levenshtein은 바이트를 세어 한글 한 글자 차이가 2~3이 된다
    # (BigQuery EDIT_DISTANCE는 글자를 센다). 08 건물명 철자 유사 규칙(EDIT_DISTANCE(…) <= 1)에 쓴다.
    """CREATE OR REPLACE TEMP MACRO within_one_edit(a, b) AS CASE
       WHEN a IS NULL OR b IS NULL THEN NULL
       WHEN a = b THEN TRUE
       WHEN length(a) = length(b) THEN
         len(list_filter(range(1, length(a) + 1), lambda i: substr(a, i, 1) != substr(b, i, 1))) <= 1
       WHEN length(a) = length(b) + 1 THEN
         len(list_filter(range(1, length(a) + 1), lambda i: substr(a, 1, i - 1) || substr(a, i + 1) = b)) > 0
       WHEN length(b) = length(a) + 1 THEN
         len(list_filter(range(1, length(b) + 1), lambda i: substr(b, 1, i - 1) || substr(b, i + 1) = a)) > 0
       ELSE FALSE END""",
)


def register(con) -> None:
    """연결 하나에 함수와 매크로를 등록한다. 매크로는 연결마다 따로 있다."""
    names = {row[0] for row in con.execute(
        "SELECT function_name FROM duckdb_functions() WHERE function_name IN "
        "('parse_address_candidates', 'nfkc')").fetchall()}
    if "parse_address_candidates" not in names:
        con.create_function("parse_address_candidates", parse_address_candidates, ["VARCHAR"], PARSE_TYPE,
                            null_handling="special")
    if "nfkc" not in names:
        con.create_function("nfkc", nfkc, ["VARCHAR"], "VARCHAR", null_handling="special")
    for macro in MACROS:
        con.execute(macro)
