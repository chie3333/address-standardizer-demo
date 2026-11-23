-- 원문 주소에서 도로명·지번 후보를 뽑는 JavaScript UDF.
-- 공백을 지운 문자열에서 '…로/길 + 번호', '…동/리/가 + (산) + 번호'를 모두 찾는다.
-- 띄어쓰기가 없거나 틀린 원문(충청남도공주시동현새터길68)도 다루려고, 번호 앞 이름은 가능한 끝부분(접미 문자열)을
-- 모두 후보로 내고 기준표와의 정확 조인으로 실제 있는 이름만 남긴다.
-- 실행: bq query --use_legacy_sql=false < bigquery/sql/04_parse_function.sql
CREATE OR REPLACE FUNCTION `your-gcp-project.address.parse_address_candidates`(text STRING)
RETURNS ARRAY<STRUCT<kind STRING, name STRING, mountain BOOL, main FLOAT64, sub FLOAT64, matched STRING>>
LANGUAGE js
OPTIONS (description = '원문에서 도로명(road)·지번(lot) 후보 이름과 번호를 뽑는다. sub가 NULL이면 원문에 부번이 없음')
AS r"""
  if (!text) return [];
  let s = text.normalize('NFKC')
    .replace(/&middot;/g, '·').replace(/&amp;/g, '&').replace(/&nbsp;/g, ' ')
    // 전화번호(010-1234-5678, 061 123 4567). 앞뒤가 숫자·하이픈이 아니고 실제 국번으로 시작할 때만 지운다(00018-00001은 번지).
    .replace(/(^|[^\d-])0(?:1[016789]|2|[3-6][1-5])[-.\s]?\d{3,4}[-.\s]?\d{4}(?![\d-])/g, '$1 ')
    .replace(/\([^()0-9]*\)/g, ' ')                                // 숫자 없는 괄호(마을 이름·건물명)는 이름과 번호 사이를 끊으므로 뺀다
    .replace(/(\d)\s+(?=\d)/g, '$1|');                             // 띄어 쓴 숫자(197 8층)가 공백 제거로 붙지 않게 경계를 남긴다
  const c = s.replace(/\s+/g, '').replace(/~/g, '-');
  const out = [];
  const seen = new Set();
  function add(kind, name, mountain, main, sub, matched) {
    // 행정리 표기(옥산2리, 신전1리)는 법정리 이름(옥산리)으로도 찾는다.
    if (kind === 'lot' && /[가-힣][0-9]+리$/.test(name)) {
      add(kind, name.replace(/([가-힣])[0-9]+리$/, '$1리'), mountain, main, sub, matched);
    }
    // 이름 끝부분을 길이 2~25로 모두 후보로 낸다. 지번 이름은 숫자로 시작하지 않는다(도로명은 2산단로처럼 숫자로 시작할 수 있다).
    for (let k = 2; k <= Math.min(25, name.length); k++) {
      const n = name.slice(name.length - k);
      if (kind === 'lot' && /^[0-9]/.test(n)) continue;
      const key = [kind, n, mountain, main, sub].join('|');
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({kind: kind, name: n, mountain: mountain, main: main, sub: sub, matched: matched});
    }
  }
  // 도로명: 이름(로·길·거리, 'N길'·'N번길'·'N번안길'·'N번가길'·'N나길' 포함) + 건물번호(-부번).
  // 번호 바로 뒤가 숫자·한글 한 글자+길(10나길)·'가'(종로1가)·'번+한글'(22번긷)·'동·층·호'(108동 304호)면 건물번호가 아니다.
  const road = /([가-힣A-Za-z0-9·.]*?(?:로|길|거리)(?:\d+번?[가-힣]?길)?)(\d+)(?:-(\d+))?(?![\d가동층호]|번?[가-힣]?길|번[가-힣])/g;
  let m;
  while ((m = road.exec(c)) !== null) {
    add('road', m[1], false, Number(m[2]), m[3] === undefined ? null : Number(m[3]), m[0]);
  }
  // 지번: 이름(동·리·가, 리 없이 쓴 읍·면) + (N통·N반) + 산 + 본번 + (번지) + (-부번 또는 부번호).
  // 통·반 번호(주문6리 6반), 아파트 동·층·호(529동, 죽림리 202호), 'N가'(남포동1가)는 지번이 아니다.
  const lot = /([가-힣0-9]*?(?:동|리|가|읍|면))(?:\d+[통반])*(산)?(\d+)(?![\d통반동층가호])(?:번지)?(?:-(\d+)(?!\d)(?:번지|호)?|(\d+)(?!\d)호)?/g;
  while ((m = lot.exec(c)) !== null) {
    const sub = m[4] !== undefined ? Number(m[4]) : (m[5] !== undefined ? Number(m[5]) : null);
    add('lot', m[1], m[2] !== undefined, Number(m[3]), sub, m[0]);
  }
  return out;
""";
