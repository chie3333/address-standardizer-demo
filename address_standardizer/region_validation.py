"""Label and validate region tokens against the lite address catalog.

Python 3.10+. Install duckdb (a version compatible with the catalog).
See the runnable example at the bottom.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from unicodedata import normalize
from itertools import combinations, product
from pathlib import Path
import re
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Iterable

from rapidfuzz.distance import Levenshtein

if TYPE_CHECKING:
    from .contracts import AddressRecord, LabeledToken, RegionValidationResult

_NUMBER_TOKEN = re.compile(r"[0-9]+(?:-[0-9]+)?")
LABELS = {"시도", "시군구", "읍면동", "리", "도로명"}
PARENT_LABELS = {"시도", "시군구"}
CHILD_LABELS = {"읍면동", "리"}
REGION_WORD = re.compile(r"[가-힣0-9]*[가-힣]")
ROAD_ENDINGS = ("거리", "길", "로", "가", "개", "선", "장", "터")
EMD_PREFERRED_NAMES = {"시장북로"}
SUGGESTION_MIN_SCORE = 0.6  # 자모 기준 유사도 하한 (짧은 이름의 과도한 후보 방지)
SUGGESTION_MAX_JAMO_DISTANCE = 2  # 자모 편집 거리 상한
SUGGESTION_LIMIT = 20
SEOUL_SEJONGRO_HINTS = {"서울": "시도", "서울시": "시도", "서울특별시": "시도",
                      "종로": "시군구", "종로구": "시군구"}
SEJONG_SEJONGRO_HINTS = {"세종": "시도", "세종시": "시도", "세종특별자치시": "시도",
                       "종촌동": "읍면동", "고운동": "읍면동"}
SIDO_ALIASES = dict(zip(
    "서울 부산 대구 인천 광주 대전 울산 세종 경기 강원 충북 충남 전북 전남 경북 경남 제주".split(),
    "서울특별시 부산광역시 대구광역시 인천광역시 광주광역시 대전광역시 울산광역시 세종특별자치시 경기도 강원특별자치도 충청북도 충청남도 전북특별자치도 전라남도 경상북도 경상남도 제주특별자치도".split(),
))
SIDO_ALIASES.update({short + "시": SIDO_ALIASES[short]
                     for short in "서울 부산 대구 인천 광주 대전 울산 세종".split()})
# 옛 이름과 새 이름을 섞은 표기(전라북도특별자치도)도 같은 시도로 읽는다.
HISTORICAL_NAMES = {
    "강원특별자치도": ("강원도", "강원도특별자치도"),
    "전북특별자치도": ("전라북도", "전라북도특별자치도"),
    "제주특별자치도": ("제주도", "제주도특별자치도"),
}



def _jamo(text: str) -> str:
    """한글 음절을 초성·중성·종성 자모로 풀어 쓴다(선 → ㅅㅓㄴ)."""
    return normalize("NFD", text)

@dataclass(frozen=True)
class Region:
    label: str
    name: str
    code_prefix: str | None
    node_id: str | None = None


@dataclass(frozen=True)
class PreLLMRegionLabeling:
    tokens: list[LabeledToken]
    remaining_numbers: tuple[str, ...]
    validation: RegionValidationResult

    @property
    def fully_labeled(self) -> bool:
        labels = self.validation.get("db_labels", [])
        return (
            self.validation["status"] in {"unique_path", "multiple_paths"}
            and bool(self.validation["paths"])
            and len(labels) == len(self.tokens)
            and all(labels)
        )


class RegionValidator:
    version = "8"

    def __init__(self, catalog: str | Path, year: int = 2025):
        import duckdb

        self.year = year
        self.catalog = Path(catalog)
        self.paths = []
        self.index = defaultdict(set)
        self.path_legal_codes = []
        self.path_sigungu_ids = []
        self._parent_paths = []
        self._parent_index = defaultdict(set)
        self._parent_scope_ids = []
        self._legal_path_ids = defaultdict(set)
        self._road_cache = {}
        self._road_lock = Lock()
        # 읍면동 노드(code_prefix)별 행정동 이름. 행정동 이름으로 찾은 경로를 번지 조회에서 좁히는 데 쓴다.
        self.emd_admin_names = defaultdict(set)
        db = duckdb.connect(str(self.catalog), read_only=True)
        try:
            rows = db.execute("""
                SELECT node_id, parent_id, node_type, name, observed_legal_code_prefix
                FROM region_hierarchy WHERE 기준연도 = ?
            """, [year]).fetchall()
            if not rows:
                raise ValueError(f"카탈로그에 기준연도 {year}의 지역 노드가 없습니다.")
            nodes = {node: (parent, kind, name, code) for node, parent, kind, name, code in rows}
            official = {name for _, _, kind, name, _ in rows if kind == "시도"}
            self.aliases = {}
            for current, old in HISTORICAL_NAMES.items():
                available = next((name for name in (current, *old) if name in official), current)
                self.aliases.update(dict.fromkeys((current, *old), available))
            self.aliases.update({short: self.aliases.get(name, name)
                                 for short, name in SIDO_ALIASES.items()})

            district_paths = {}
            sigungu_aliases = defaultdict(set)
            for code, original, leaf in db.execute("""
                SELECT sigungu_id, original_sigungu_name, leaf_node_id
                FROM sigungu_hierarchy_links WHERE 기준연도 = ?
            """, [year]).fetchall():
                path = []
                while leaf is not None:
                    node_id = leaf
                    leaf, kind, name, prefix = nodes[node_id]
                    if name:
                        path.append(Region("시도" if kind == "시도" else "시군구",
                                           name, prefix, node_id))
                path = tuple(reversed(path))
                district_paths[code, original] = path
                names = {region.name for region in path if region.label == "시군구"}
                if original:
                    names.add(original)
                for name in names:
                    sigungu_aliases[name].add(name)
                    if name.endswith(("시", "군", "구")) and len(name) > 1:
                        sigungu_aliases[name[:-1]].add(name)
            self.sigungu_aliases = {
                alias: tuple(sorted(names)) for alias, names in sigungu_aliases.items()
            }
            # 시도 약칭이면서 시군구 별칭인 이름(광주 → 광주광역시·경기도 광주시 등).
            # 라벨과 무관하게 두 해석을 함께 후보로 두고, 하위 토큰으로 좁혀지지 않으면 복수 경로로 남긴다.
            self.ambiguous_parent_names = frozenset(
                name for name in self.aliases
                if name in self.sigungu_aliases and name not in official
            )

            admin_names = defaultdict(set)
            for code, name in db.execute("""
                SELECT DISTINCT l.legal_code, a.행정동명
                FROM legal_admin_links l
                JOIN administrative_units a USING (기준연도, admin_code)
                WHERE 기준연도 = ? AND a.행정동명 IS NOT NULL
            """, [year]).fetchall():
                admin_names[code].add(name)

            legal_paths = {}
            legal_sigungu = {}
            parent_ids = {}
            for code, district, original, emd_code, emd, ri in db.execute("""
                SELECT DISTINCT legal_code, sigungu_id, 법정동시군구명,
                                emd_id, 법정동읍면동명, 법정동리명
                FROM legal_regions WHERE 기준연도 = ?
            """, [year]).fetchall():
                path = district_paths[district, original]
                options = [{(region.label, region.name)} for region in path]
                if original and path[-1].label == "시군구":
                    options[-1].add(("시군구", original))
                parent_key = (district, original)
                parent_id = parent_ids.get(parent_key)
                if parent_id is None:
                    parent_id = parent_ids[parent_key] = len(self._parent_paths)
                    self._parent_paths.append((path, tuple(options)))
                    self._parent_scope_ids.append(set())
                    for names in options:
                        for term in names:
                            self._parent_index[term].add(parent_id)
                if emd:
                    path += (Region("읍면동", emd, emd_code),)
                    options.append({("읍면동", name) for name in {emd, *admin_names[code]}})
                    self.emd_admin_names[emd_code].update(admin_names[code])
                if ri:
                    path += (Region("리", ri, code),)
                    options.append({("리", ri)})
                legal_paths[code] = (path, options)
                legal_sigungu[code] = district
                path_id = len(self.paths)
                self._parent_scope_ids[parent_id].add(path_id)
                self._legal_path_ids[code].add(path_id)
                self.paths.append((path, options))
                self.path_legal_codes.append(code)
                self.path_sigungu_ids.append(district)
                for names in options:
                    for term in names:
                        self.index[term].add(path_id)

            self._legal_paths = legal_paths
            self._legal_sigungu = legal_sigungu
            self._max_path_size = max((len(path) for path, _ in self.paths), default=0) + 1
        finally:
            db.close()

    def _lookup_roads(self, names):
        """Fetch only requested road names and cache their legal-region links."""
        requested = {name for name in names if name}
        if not requested:
            return {}, 0.0, 0, 0
        with self._road_lock:
            missing = sorted(requested.difference(self._road_cache))
            hits = len(requested) - len(missing)
            lookup_ms = 0.0
            if missing:
                import duckdb

                start = perf_counter()
                found = defaultdict(list)
                db = duckdb.connect(str(self.catalog), read_only=True)
                try:
                    rows = db.execute("""
                        SELECT DISTINCT r.road_names[1], l.legal_code, r.road_id
                        FROM road_catalog r
                        JOIN legal_road_links l
                          ON r.기준연도 = l.기준연도 AND r.road_id = l.road_code
                        WHERE r.기준연도 = ? AND list_has_any(r.road_names, ?)
                    """, [self.year, missing]).fetchall()
                finally:
                    db.close()
                for road_name, legal_code, road_id in rows:
                    if legal_code in self._legal_paths:
                        found[road_name].append((legal_code, road_id))
                for name in missing:
                    self._road_cache[name] = tuple(found.get(name, ()))
                lookup_ms = (perf_counter() - start) * 1000
            return (
                {name: self._road_cache[name] for name in requested},
                lookup_ms,
                hits,
                len(missing),
            )

    def _contextual_labels(self, tokens):
        names = [self._token_name(token) for token in tokens]
        # 임시 동명 규칙. 남성로는 두 해석을 자동으로 확정하지 않고 검토로 넘긴다.
        if "남성로" in names:
            return {}, "namseongro_held"
        if "세종로" not in names:
            return {}, None

        # 단서는 실제 입력 토큰에서만 취한다. 라벨이 있는 토큰은 해당 역할도
        # 후보에 있어야 한다. 예를 들어 도로명으로만 제안된 종로는 시군구 단서가 아니다.
        def hints(known_names):
            return {
                i: known_names[name] for i, (name, token) in enumerate(zip(names, tokens, strict=True))
                if name in known_names
                and (not token["labels"] or known_names[name] in token["labels"])
            }

        seoul = hints(SEOUL_SEJONGRO_HINTS)
        sejong = hints(SEJONG_SEJONGRO_HINTS)
        if seoul and sejong:
            return {}, "sejongro_conflicting_context"
        if not seoul and not sejong:
            return {}, "sejongro_missing_context"

        # 단서가 가리키는 라벨만 허용한다. 세종로뿐 아니라 단서 토큰의 역할도
        # 같은 DB 경로에서 검증하고, 일치하지 않아도 반대 해석을 자동 채택하지 않는다.
        label = "읍면동" if seoul else "도로명"
        overrides = seoul or sejong
        overrides.update({i: label for i, name in enumerate(names) if name == "세종로"})
        return overrides, None

    def _token_name(self, token) -> str:
        """토큰을 카탈로그 비교용 이름으로 바꾼다.

        전각 숫자(９길)는 NFKC로 맞추고, 명칭 안에 끼어든 공백(흑 석로9길)은 없앤다.
        '수원시 장안구'처럼 카탈로그에 공백이 있는 명칭과 서로 떨어진 숫자는 유지한다.
        """
        name = " ".join(normalize("NFKC", token["raw"]).split())
        if " " in name and not self._known_region(name) and not re.search(r"\d \d", name):
            return name.replace(" ", "")
        return name

    def _prepare_token(self, token, *, forced_label=None, allow_emd_road_fallback=False):
        raw, labels = token["raw"], token["labels"]
        if (not isinstance(raw, str) or not raw.strip() or not isinstance(labels, list)
                or len(labels) > 2 or any(label not in LABELS for label in labels)):
            raise ValueError("각 토큰에는 raw 문자열과 허용 라벨 0~2개가 필요합니다.")
        name = self._token_name(token)
        if forced_label is not None:
            labels = [forced_label]
        elif name in EMD_PREFERRED_NAMES:
            labels = ["읍면동", "도로명"] if allow_emd_road_fallback else ["읍면동"]
        candidates = []
        for label in labels or sorted(LABELS):
            if label in PARENT_LABELS and name in self.ambiguous_parent_names:
                sido = (("시도", self.aliases[name]),)
                sigungu = tuple(("시군구", candidate) for candidate in self.sigungu_aliases[name])
                candidates.append(sido + sigungu if label == "시도" else sigungu + sido)
                continue
            if label == "시도":
                names = (self.aliases.get(name, name),)
            elif label == "시군구":
                names = self.sigungu_aliases.get(name, (name,))
            else:
                names = (name,)
            candidates.append(tuple((label, candidate) for candidate in names))
        return tuple(candidates)

    def official_names(self, name: str, label: str) -> set[str]:
        """약칭·옛 이름을 카탈로그의 정식 명칭으로 바꾼다(서울 → 서울특별시, 서대문 → 서대문구).

        지역 검증과 번지 조회가 같은 약칭 표(SIDO_ALIASES, HISTORICAL_NAMES, 시군구 접미사 규칙)를 쓰도록 한다.
        """
        name = " ".join(name.split())
        if label == "시도":
            return {self.aliases.get(name, name)}
        if label == "시군구":
            return set(self.sigungu_aliases.get(name, (name,)))
        return {name}

    def _known_region(self, name: str) -> bool:
        return (name in self.aliases or name in self.sigungu_aliases
                or any((label, name) in self.index for label in PARENT_LABELS | CHILD_LABELS))

    def region_name_splits(self, name: str, *, limit: int | None = 2) -> tuple[tuple[str, ...], ...]:
        if self._known_region(name):
            return ()
        # Retry hints only need two splits. DB validation checks all possible splits.
        by_end = {0: [()]}
        for end in range(2, len(name) + 1):
            splits = []
            for start, prefixes in by_end.items():
                part = name[start:end]
                if len(part) < 2 or not self._known_region(part):
                    continue
                splits.extend((*prefix, part) for prefix in prefixes
                              if limit is not None or len(prefix) < self._max_path_size)
                if limit is not None and len(splits) >= limit:
                    break
            if splits:
                by_end[end] = splits[:limit]
        return tuple(by_end.get(len(name), ()))

    def can_split_region(self, name: str) -> bool:
        return bool(self.region_name_splits(name))

    def matches_consecutive_names(self, path: list[dict], names: tuple[str, ...]) -> bool:
        """Check that the split follows consecutive parent–child nodes in source order."""
        options = [
            {term for terms in self._prepare_token({"raw": name, "labels": []}) for term in terms}
            for name in names
        ]
        return any(
            all((node["label"], node["name"]) in terms
                for node, terms in zip(path[start:start + len(names)], options, strict=True))
            for start in range(len(path) - len(names) + 1)
        )

    def tokenization_retry_reasons(self, rows: Iterable[dict]) -> dict[str, str]:
        """Find catalog evidence of a name lost or split incorrectly during extraction."""
        checks = {}
        road_names = set()
        for row in rows:
            names = [t["raw"] for t in row["tokens"]]
            gaps = {part for fragment in row["remaining"] for part in REGION_WORD.findall(fragment)}
            source = row["address"]
            # A short residue can be the missing part of a name, e.g. 서 + 상리.
            joined = {
                value for name in names for gap in gaps
                for value in (gap + name, name + gap) if value in source
            }
            joined.update(a + b for a, b in zip(names, names[1:]) if a + b in source)
            present = {node["name"] for node in row["path"]} | set(names)
            candidates = (gaps | joined) - present
            road_names.update(name for name in candidates if name.endswith(ROAD_ENDINGS))
            checks[row["address_id"]] = (names, candidates, joined, row["status"])

        roads, _, _, _ = self._lookup_roads(road_names)

        reasons = {}
        for address_id, (names, candidates, joined, status) in checks.items():
            evidence = {name for name in candidates if self._known_region(name) or roads.get(name)}
            if evidence & joined or status == "no_match" and any(map(self.can_split_region, names)):
                reasons[address_id] = "invalid_name_boundary"
            elif evidence or status == "parse_unresolved" and any(map(self.can_split_region, candidates)):
                reasons[address_id] = "missing_address_name"
        return reasons

    def matches_restored_parent(
        self, tokens: list[LabeledToken], position: int, restored: RegionValidationResult,
        original: RegionValidationResult,
    ) -> bool:
        """Require the restored name and its direct parent to match input in one path."""
        if restored["status"] != "unique_path":
            return False
        path, = restored["paths"]
        original_path = original["paths"][0] if original["status"] == "unique_path" else []
        for index, node in enumerate(path[1:], 1):
            if node["name"] != tokens[position]["raw"] or node["label"] not in restored["db_labels"][position]:
                continue
            parent = path[index - 1]
            shared_depth = min(index, len(original_path))
            if original_path[:shared_depth] != path[:shared_depth]:
                continue
            if any(
                parent["label"] in labels and any(
                    (parent["label"], parent["name"]) in terms
                    for terms in self._prepare_token(token, forced_label=parent["label"])
                )
                for token, labels in zip(tokens[:position], restored["db_labels"][:position], strict=True)
            ):
                return True
        return False

    def _match_regions(self, key, *, parent_only=False, scope=None):
        # Select the smallest pool first, then assign distinct nodes to all tokens.
        if not key:
            return set(), set()
        index = self._parent_index if parent_only else self.index
        paths = self._parent_paths if parent_only else self.paths
        pools = [set().union(*(index.get(term, ()) for term in token)) for token in key]
        if scope is not None:
            pools.append(scope)
        possible = min(pools, key=len).intersection(*pools)
        found = set()
        matched_path_ids = set()
        for path_id in possible:
            path, options = paths[path_id]
            assignments = {0}
            for token in key:
                nodes = [1 << i for i, names in enumerate(options) if names.intersection(token)]
                assignments = {used | node for used in assignments for node in nodes if not used & node}
                if not assignments:
                    break
            if assignments:
                matched_path_ids.add(path_id)
            found.update(path[:used.bit_length()] for used in assignments)
        return found, matched_path_ids

    def _match_roads(self, road_name, road_records, region_path_ids=None):
        found = set()
        for legal_code, road_id in road_records.get(road_name, ()):
            if region_path_ids is not None and self._legal_path_ids[legal_code].isdisjoint(region_path_ids):
                continue
            base_path, _ = self._legal_paths[legal_code]
            found.add(base_path + (Region("도로명", road_name, road_id),))
        return found

    def _match_key(self, key, road_records, parent_cache, stats):
        parents = tuple(token for token in key if token[0][0] in PARENT_LABELS)
        children = tuple(token for token in key if token[0][0] in CHILD_LABELS)
        roads = [token[0][1] for token in key if token[0][0] == "도로명"]
        if len(roads) > 1:
            return set()

        paths, scope = set(), None
        if parents:
            if parents in parent_cache:
                stats["parent_cache_hit_count"] += 1
            else:
                parent_paths, parent_ids = self._match_regions(parents, parent_only=True)
                scope_ids = set().union(*(self._parent_scope_ids[parent_id] for parent_id in parent_ids))
                parent_cache[parents] = (parent_paths, scope_ids)
                stats["parent_search_count"] += 1
            paths, scope = parent_cache[parents]
            if not scope:
                return set()
        if children:
            paths, scope = self._match_regions(children, scope=scope)
            stats["child_search_count"] += 1
        if roads:
            stats["road_search_count"] += 1
            return self._match_roads(roads[0], road_records, scope)
        return set(paths)

    def _similar_names(self, raw: str, paths: list[tuple[Region, ...]], used: set[tuple[str, str]]):
        """Rank region names under the partially matched paths by spelling similarity to `raw`."""
        name = " ".join(raw.split())
        prefixes = {tuple((r.label, r.name) for r in path) for path in paths}
        pool = {}
        for prefix in prefixes:
            last = prefix[-1]
            for path_id in self.index.get(last, ()):
                path, options = self.paths[path_id]
                if tuple((r.label, r.name) for r in path[:len(prefix)]) != prefix:
                    continue
                for names in options[len(prefix):]:
                    for label, candidate in names:
                        pool.setdefault((label, candidate), None)
            # 경로 중간 노드(예: 시군구 오타)도 비교 대상으로 둔다.
            for label, candidate in prefix:
                pool.setdefault((label, candidate), None)
        source = _jamo(name)
        scored = []
        for label, candidate in pool:
            if (label, candidate) in used or candidate == name:
                continue
            forms = {candidate}
            if label in PARENT_LABELS:
                # 서대문구 ↔ 서대믄, 서울특별시 ↔ 서을 처럼 약칭으로 쓴 입력도 비교한다.
                forms.add(candidate[:-1] if candidate.endswith(("시", "군", "구")) else candidate)
                forms.update(short for short, full in self.aliases.items() if full == candidate)
            # 자모 단위 편집 거리: 대산동→대신동은 1(ㅏ→ㅣ), 대산동→대학동은 2(ㅅ→ㅎ, ㄴ→ㄱ).
            distance, form = min((Levenshtein.distance(source, _jamo(form)), form) for form in forms if form)
            if distance > SUGGESTION_MAX_JAMO_DISTANCE:
                continue
            similarity = 1 - distance / max(len(source), len(_jamo(form)))
            if similarity < SUGGESTION_MIN_SCORE:
                continue
            # 같은 거리면 끝 글자(동·구·리)가 같은 이름, 길이가 같은 이름을 앞에 둔다.
            scored.append(((distance, candidate[-1:] != name[-1:], abs(len(candidate) - len(name)), candidate),
                           label, candidate, round(similarity, 3), distance))
        scored.sort(key=lambda item: item[0])
        return [(label, candidate, similarity, distance)
                for _, label, candidate, similarity, distance in scored[:SUGGESTION_LIMIT]]

    def _add_partial_matches(self, rows, results, *, group: bool) -> int:
        """For unmatched rows, keep the largest token subsets that form a path and suggest names for the rest.

        같은 개수를 뺀 해석이 여러 개면 모두 남긴다. 주소는 큰 지역부터 쓰므로 앞쪽 토큰을 남긴 해석을
        대표로 두고(예: 서울 대산동 → 서울특별시), 나머지는 다른 해석으로 함께 반환한다.
        """
        pending = {
            index: len(row["tokens"]) for index, row in enumerate(rows)
            if results[index]["status"] == "no_match" and len(row["tokens"]) > 1
        }
        found = defaultdict(list)
        for drop in range(1, max(pending.values(), default=1)):
            trials, owners = [], []
            for index, size in pending.items():
                if index in found or drop >= size:
                    continue
                for removed in combinations(range(size), drop):
                    kept = [i for i in range(size) if i not in removed]
                    trials.append({"address_id": str(len(trials)),
                                   "tokens": [rows[index]["tokens"][i] for i in kept]})
                    owners.append((index, tuple(kept), removed))
            if not trials:
                break
            checked, _ = self._validate_names(trials, group=group)
            for (index, kept, removed), result in zip(owners, checked, strict=True):
                if result["paths"]:
                    found[index].append((kept, removed, result))
        verify, verify_owners = [], []
        partials = {}
        for index, interpretations in found.items():
            interpretations.sort(key=lambda item: item[0])
            suggestions = {}
            for kept, removed, result in interpretations:
                paths = [tuple(Region(**node) for node in path) for path in result["paths"]]
                if any(region.label == "도로명" for path in paths for region in path):
                    continue
                # 남은 토큰과 비교할 때 이미 다른 토큰이 차지한 지명만 제외한다.
                used = {term for i in kept for terms in self._prepare_token(
                    {"raw": rows[index]["tokens"][i]["raw"], "labels": []}) for term in terms}
                for i in removed:
                    for label, candidate, score, distance in self._similar_names(
                            rows[index]["tokens"][i]["raw"], paths, used):
                        key = (i, label, candidate)
                        if key in suggestions:
                            continue
                        tokens = [dict(token) for token in rows[index]["tokens"]]
                        tokens[i] = {"raw": candidate, "labels": [label] if tokens[i]["labels"] else []}
                        suggestions[key] = {
                            "token_index": i, "raw": rows[index]["tokens"][i]["raw"], "name": candidate,
                            "label": label, "similarity": score, "jamo_distance": distance,
                            "status": "unchecked", "paths": [],
                        }
                        verify.append({"address_id": str(len(verify)), "tokens": tokens})
                        verify_owners.append(suggestions[key])
            (kept, removed, result), *others = interpretations
            partials[index] = {
                "matched_tokens": list(kept), "unmatched_tokens": list(removed),
                "paths": result["paths"], "db_labels": result["db_labels"],
                "alternatives": [
                    {"matched_tokens": list(k), "unmatched_tokens": list(r),
                     "paths": alt["paths"], "db_labels": alt["db_labels"]}
                    for k, r, alt in others
                ],
                "suggestions": list(suggestions.values()),
            }
        if verify:
            checked, _ = self._validate_names(verify, group=group)
            for suggestion, result in zip(verify_owners, checked, strict=True):
                suggestion.update(status=result["status"], paths=result["paths"], db_labels=result["db_labels"])
        for index, partial in partials.items():
            # 전체 경로가 성립하는 대체 지명만 남기고, 유사도 순서를 유지한다.
            partial["suggestions"] = sorted(
                (s for s in partial["suggestions"] if s["paths"]),
                key=lambda s: (s["jamo_distance"], -s["similarity"], s["name"]),
            )[:SUGGESTION_LIMIT]
            results[index]["partial_match"] = partial
        return len(partials)

    def label_before_llm(
        self, records: Iterable[AddressRecord],
    ) -> tuple[dict[str, PreLLMRegionLabeling], dict[str, int | float]]:
        """공백으로 나눈 명칭에 DB 라벨을 붙이고 분리한 번호를 함께 반환한다."""
        from .preprocessing import normalize_address

        rows = {}
        remaining_numbers = {}
        for record in records:
            words = normalize_address(record.normalized_address or record.raw_address or "").split()
            tokens: list[LabeledToken] = []
            numbers = []
            for word in words:
                if _NUMBER_TOKEN.fullmatch(word):
                    numbers.append(word)
                else:
                    tokens.append({"raw": word, "labels": []})
            if tokens:
                rows[record.address_id] = {"address_id": record.address_id, "tokens": tokens}
                remaining_numbers[record.address_id] = tuple(numbers)
        if not rows:
            return {}, {}

        matches, stats = self.validate_batch(rows.values())
        labeled = {}
        for match in matches:
            address_id = match["address_id"]
            labeled[address_id] = PreLLMRegionLabeling(
                rows[address_id]["tokens"], remaining_numbers[address_id], match,
            )
        return labeled, stats

    def validate_batch(
        self, records: Iterable[dict], *, group: bool = True, suggest: bool = False,
    ) -> tuple[list[RegionValidationResult], dict[str, int | float]]:
        """Check raw names first, then retry unmatched rows with proposed corrections.

        With `suggest=True`, rows that still do not match get `partial_match`: the largest token
        subset that forms a catalog path, and similarly spelled names for the remaining tokens.
        """
        start = perf_counter()
        rows = tuple(records)
        results, stats = self._validate_names(rows, group=group)
        pending = {
            index: tuple(i for i, token in enumerate(row["tokens"]) if token.get("corrected"))
            for index, row in enumerate(rows) if results[index]["status"] == "no_match"
        }
        pending = {index: positions for index, positions in pending.items() if positions}
        correction_match_count = 0
        correction_path_search_count = 0
        for count in range(1, max(map(len, pending.values()), default=0) + 1):
            trials = []
            owners = []
            for index, positions in pending.items():
                for corrected_indices in combinations(positions, count):
                    tokens = [
                        {"raw": token["corrected"] if i in corrected_indices else token["raw"],
                         "labels": token["labels"]}
                        for i, token in enumerate(rows[index]["tokens"])
                    ]
                    trials.append({"address_id": str(len(trials)), "tokens": tokens})
                    owners.append(index)
            if not trials:
                break
            checked, retry_stats = self._validate_names(trials, group=group)
            correction_path_search_count += retry_stats["path_search_count"]
            for name, value in retry_stats.items():
                if name not in {"input_count", "unique_group_count", "elapsed_ms"}:
                    stats[name] += value
            hits = defaultdict(dict)
            checked_labels = {}
            for index, result in zip(owners, checked, strict=True):
                if result["paths"]:
                    labels = checked_labels.setdefault(index, [set() for _ in rows[index]["tokens"]])
                    for combined, matched in zip(labels, result["db_labels"], strict=True):
                        combined.update(matched)
                for path in result["paths"]:
                    key = tuple(
                        (node["label"], node["name"], node["code_prefix"] or "", node["node_id"] or "")
                        for node in path
                    )
                    hits[index][key] = path
            for index, paths in hits.items():
                results[index]["paths"] = [paths[key] for key in sorted(paths)]
                results[index]["status"] = "unique_path" if len(paths) == 1 else "multiple_paths"
                results[index]["used_correction"] = True
                results[index]["db_labels"] = [sorted(labels) for labels in checked_labels[index]]
                del pending[index]
            correction_match_count += len(hits)
        stats.update(
            correction_match_count=correction_match_count,
            correction_path_search_count=correction_path_search_count,
            partial_match_count=self._add_partial_matches(rows, results, group=group) if suggest else 0,
            elapsed_ms=(perf_counter() - start) * 1000,
        )
        return results, stats

    def _validate_names(
        self, records: Iterable[dict], *, group: bool,
    ) -> tuple[list[RegionValidationResult], dict[str, int | float]]:
        rows = tuple(records)
        results, stats = self._search_names(rows, group=group)
        pending = [
            (index, row) for index, row in enumerate(rows)
            if results[index]["status"] == "no_match"
            and any(self._token_name(token) in EMD_PREFERRED_NAMES for token in row["tokens"])
        ]
        if pending:
            retried, retry_stats = self._search_names(
                (row for _, row in pending), group=group, allow_emd_road_fallback=True,
            )
            for (index, _), result in zip(pending, retried, strict=True):
                results[index] = result
            for name, value in retry_stats.items():
                if name not in {"input_count", "unique_group_count"}:
                    stats[name] += value
        return results, stats

    def _search_names(
        self, records: Iterable[dict], *, group: bool, allow_emd_road_fallback: bool = False,
    ) -> tuple[list[RegionValidationResult], dict[str, int | float]]:
        start = perf_counter()
        prepared = []
        searches = {}
        for row in records:
            tokens = row["tokens"]
            has_labels = {bool(token["labels"]) for token in tokens}
            if len(has_labels) > 1:
                raise ValueError("주소 한 건에는 같은 방식으로 생성된 토큰을 전달해야 합니다.")
            inferred = has_labels == {False}
            overrides, hold_reason = self._contextual_labels(tokens)
            if hold_reason:
                prepared.append((row["address_id"], None, tuple(range(len(tokens))), hold_reason))
                continue
            ordered = sorted(
                (self._prepare_token(token, forced_label=overrides.get(i),
                                     allow_emd_road_fallback=allow_emd_road_fallback), i)
                for i, token in enumerate(tokens)
            )
            key = tuple(terms for terms, _ in ordered)
            search_id = (inferred, key) if group else len(prepared)
            prepared.append((row["address_id"], search_id, tuple(i for _, i in ordered), None))
            if key:
                searches[search_id] = (inferred, key)

        primary = {
            search_id: tuple(token[0] for token in key)
            for search_id, (inferred, key) in searches.items() if not inferred
        }
        requested_roads = {
            name
            for inferred, key in searches.values() for token in key
            for terms in (token if inferred else token[:1]) for label, name in terms if label == "도로명"
        }
        road_records, road_lookup_ms, road_cache_hits, road_cache_misses = (
            self._lookup_roads(requested_roads)
        )
        road_query_count = int(road_cache_misses > 0)
        matched = {search_id: set() for search_id in searches}
        labels_by_search = {
            search_id: [set() for _ in key] for search_id, (_, key) in searches.items()
        }
        fallback = {}
        parent_cache = {}
        stats = {
            "parent_search_count": 0,
            "parent_cache_hit_count": 0,
            "child_search_count": 0,
            "road_search_count": 0,
        }
        def match(search_id, key):
            paths = self._match_key(key, road_records, parent_cache if group else {}, stats)
            if paths:
                matched[search_id].update(paths)
                for labels, terms in zip(labels_by_search[search_id], key, strict=True):
                    if len({label for label, _ in terms}) == 1:
                        labels.add(terms[0][0])
                        continue
                    # 시도·시군구 동명 토큰은 경로마다 실제로 일치한 라벨을 기록한다.
                    hit = {label for path in paths for label, name in terms
                           if any(r.label == label and r.name == name for r in path)}
                    labels.update(hit or {terms[0][0]})
            return bool(paths)

        for search_id, key in primary.items():
            if not match(search_id, key):
                fallback[search_id] = searches[search_id][1]

        inferred_options = {}
        for search_id, (inferred, key) in searches.items():
            if not inferred or len(key) > self._max_path_size:
                continue
            for token in key:
                if token not in inferred_options:
                    inferred_options[token] = tuple(
                        terms for terms in token
                        if (road_records.get(terms[0][1]) if terms[0][0] == "도로명"
                            else any(term in self.index for term in terms))
                    )
            for candidate in product(*(inferred_options[token] for token in key)):
                match(search_id, candidate)

        secondary_roads = {
            name for key in fallback.values() for token in key
            for terms in token[1:] for label, name in terms if label == "도로명"
        }
        secondary, lookup_ms, hits, misses = self._lookup_roads(secondary_roads - requested_roads)
        road_records.update(secondary)
        road_lookup_ms += lookup_ms
        road_cache_hits += hits
        road_cache_misses += misses
        road_query_count += int(misses > 0)

        for search_id, key in fallback.items():
            alternatives = [i for i, token in enumerate(key) if len(token) == 2]
            paths = matched[search_id]
            for count in range(1, len(alternatives) + 1):
                for indices in combinations(alternatives, count):
                    candidate = tuple(
                        token[1] if i in indices else token[0] for i, token in enumerate(key)
                    )
                    match(search_id, candidate)
                if paths:
                    break

        sorted_paths = {
            search_id: sorted(paths, key=lambda path: tuple(
                (r.label, r.name, r.code_prefix or "", r.node_id or "") for r in path
            ))
            for search_id, paths in matched.items()
        }
        results: list[RegionValidationResult] = []
        for address_id, search_id, token_order, hold_reason in prepared:
            if hold_reason:
                results.append({
                    "address_id": address_id, "reference_year": self.year,
                    "status": "held", "paths": [], "db_labels": [[] for _ in token_order],
                    "reason_code": hold_reason,
                })
                continue
            paths = sorted_paths.get(search_id, ())
            status = "no_region_tokens" if search_id not in searches else {
                0: "no_match", 1: "unique_path",
            }.get(len(paths), "multiple_paths")
            results.append({
                "address_id": address_id, "reference_year": self.year,
                "status": status,
                "paths": [[asdict(region) for region in path] for path in paths],
                "db_labels": [
                    sorted(labels_by_search[search_id][i])
                    for i in sorted(range(len(token_order)), key=token_order.__getitem__)
                ],
            })
        return results, {
            "input_count": len(results), "unique_group_count": len(set(searches.values())),
            "path_search_count": len(searches),
            "region_search_count": stats["parent_search_count"] + stats["child_search_count"],
            **stats, "road_lookup_ms": road_lookup_ms, "road_query_count": road_query_count,
            "road_cache_hit_count": road_cache_hits,
            "road_cache_miss_count": road_cache_misses,
            "elapsed_ms": (perf_counter() - start) * 1000,
        }


if __name__ == "__main__":
    import argparse
    import json

    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("catalog", type=Path)
    cli.add_argument("--year", type=int, default=2025)
    cli.add_argument("--input", type=Path, help="JSONL with address_id and tokens")
    args = cli.parse_args()
    if args.input:
        with args.input.open(encoding="utf-8") as file:
            records = [json.loads(line) for line in file if line.strip()]
    else:
        records = [
            {"address_id": "example-1",
             "tokens": [{"raw": "경북", "labels": ["시도"]},
                        {"raw": "구미시", "labels": ["시군구"]},
                        {"raw": "송정동", "labels": ["읍면동"]}]},
            {"address_id": "example-2",
             "tokens": [{"raw": "경상북도", "labels": ["시도"]},
                        {"raw": "구미시", "labels": ["시군구"]},
                        {"raw": "송정동", "labels": ["읍면동"]}]},
            {"address_id": "example-3",
             "tokens": [{"raw": "경상북도", "labels": ["시도"]},
                        {"raw": "문경시", "labels": ["시군구"]},
                        {"raw": "당교로", "labels": ["도로명"]}]},
        ]
    start = perf_counter()
    validator = RegionValidator(args.catalog, args.year)
    load_ms = (perf_counter() - start) * 1000
    results, grouped = validator.validate_batch(records)
    baseline, ungrouped = validator.validate_batch(records, group=False)
    if results != baseline:
        raise AssertionError("그룹 검증과 건별 검증 결과가 다릅니다.")
    print(json.dumps({"catalog_load_ms": load_ms, "grouped": grouped,
                      "ungrouped": ungrouped, "results": results}, ensure_ascii=False, indent=2))
