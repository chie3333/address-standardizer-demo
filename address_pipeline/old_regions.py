"""address_pipeline 전용 복사본(원본: address_workbench/old_regions.py). 따로 고친다.

행정구역 개편으로 바뀐 옛 지역명을 2025-12-31 기준자료의 이름으로 바꾼다.

두 엔진(기존 파이프라인 어댑터, BigQuery 엔진)이 같은 표를 쓴다. 엔진에 넘기기 전에 원문을 바꾸고,
결과에는 원문을 되돌려 놓은 뒤 무엇을 바꿨는지 처리 이력과 후보 근거에 남긴다.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

# (정규식, 바꿀 이름, 옛 이름 표시, 시행일)
# 순서가 중요하다: 좁은 규칙(연기군 남면, 청원군 부용면, 마산시 합포구)을 넓은 규칙(연기군, 청원군, 마산시)보다 먼저 둔다.
# 시도가 바뀐 경우(연기군, 청원군 부용면, 군위군)는 원문의 옛 시도 이름까지 함께 바꿔야 시도 불일치로 걸러지지 않는다.
OLD_REGIONS = (
    (r"(?:충청남도|충남)?\s*연기군\s*남면", "세종특별자치시 연기면", "충청남도 연기군 남면", "2012-07-01"),
    (r"(?:충청남도|충남)?\s*연기군", "세종특별자치시", "충청남도 연기군", "2012-07-01"),
    (r"(?:충청북도|충북)?\s*청원군\s*부용면", "세종특별자치시 부강면", "충청북도 청원군 부용면", "2012-07-01"),
    (r"청원군", "청주시", "청원군", "2014-07-01"),
    (r"마산시\s*합포구", "창원시 마산합포구", "마산시 합포구", "2010-07-01"),
    (r"마산시\s*회원구", "창원시 마산회원구", "마산시 회원구", "2010-07-01"),
    # 마산시의 읍면은 어느 구인지 정해져 있다(구산·진동·진북·진전면 → 마산합포구, 내서읍 → 마산회원구).
    (r"마산시\s*(구산면|진동면|진북면|진전면)", r"창원시 마산합포구 \1", "마산시", "2010-07-01"),
    (r"마산시\s*(내서읍)", r"창원시 마산회원구 \1", "마산시", "2010-07-01"),
    (r"마산시", "창원시", "마산시", "2010-07-01"),
    (r"진해시", "창원시 진해구", "진해시", "2010-07-01"),
    (r"(인천광역시|인천)\s*남구", r"\1 미추홀구", "인천 남구", "2018-07-01"),
    (r"북제주군", "제주시", "북제주군", "2006-07-01"),
    (r"남제주군", "서귀포시", "남제주군", "2006-07-01"),
    (r"(?:경상북도|경북)\s*군위군", "대구광역시 군위군", "경상북도 군위군", "2023-07-01"),
)


def rename_old_regions(text: str | None) -> tuple[str, list[dict]]:
    """옛 행정구역 이름을 현재 이름으로 바꾼 원문과 바꾼 내역을 돌려준다."""
    if not text:
        return text or "", []
    notes = []
    for pattern, new, old, date in OLD_REGIONS:
        text, count = re.subn(pattern, new, text)
        if count:
            notes.append({"old": old, "new": re.sub(r"\s*\\1\s*", "", new), "effective": date})
    return text, notes


def drop_repeated_block(words: list[str]) -> list[str]:
    """번호 없이 같은 지역 낱말 묶음이 바로 이어서 두 번 나오면 하나만 남긴다
    (제주 제주시 조천읍 신흥리 제주 제주시 조천읍 신흥리 7 → 제주 제주시 조천읍 신흥리 7)."""
    for size in range(len(words) // 2, 1, -1):
        for start in range(len(words) - 2 * size + 1):
            block = words[start:start + size]
            if block == words[start + size:start + 2 * size] and not any(ch.isdigit() for ch in "".join(block)):
                return drop_repeated_block(words[:start + size] + words[start + 2 * size:])
    return words


def rename_for_matching(text: str | None) -> tuple[str | None, list[dict]]:
    """엔진에 넘길 원문. 바꾼 게 없으면 원문을 그대로 돌려준다(줄 구분도 유지)."""
    renamed, notes = rename_old_regions(text)
    if not notes:
        return text, []
    lines = (" ".join(drop_repeated_block(line.split())) for line in renamed.splitlines())
    return "\n".join(line for line in lines if line), notes


def note_line(notes: list[dict]) -> str:
    text = ", ".join(f"{n['old']} → {n['new']}({n['effective']})" for n in notes)
    return f"옛 행정구역 이름을 현재 이름으로 바꿔 대조: {text}"


def restore_rows(rows: list[dict], renamed: Mapping[str, tuple[str | None, list[dict]]], version: str) -> None:
    """바꿔서 처리한 행에 원문을 되돌리고, 바꾼 내역을 처리 이력 맨 앞과 후보 근거에 남긴다(JSON 행을 고친다)."""
    for row in rows:
        entry = renamed.get(row["record"]["address_id"])
        if entry is None:
            continue
        original, notes = entry
        line = note_line(notes)
        row["record"]["raw_address"] = original
        if isinstance(row.get("output"), dict):
            row["output"]["old_region"] = notes
            if "reason" in row["output"]:
                row["output"]["reason"] = f"{line} · {row['output'].get('reason') or ''}".rstrip(" ·")
        for candidate in (row.get("review") or {}).get("candidates") or []:
            evidence = candidate.get("evidence")
            if isinstance(evidence, list) and line not in evidence:
                evidence.insert(0, line)
        if not any(trace.get("module_name") == "old_region_rename" for trace in row["traces"]):
            row["traces"].insert(0, {
                "status": "completed", "reason_code": "old_region_renamed", "details": {"renamed": notes},
                "module_name": "old_region_rename", "module_version": version, "elapsed_ms": 0.0,
            })
