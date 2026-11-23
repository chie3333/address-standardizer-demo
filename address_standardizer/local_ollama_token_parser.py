"""Ollama 모델을 기존 명칭 추출 파서에 연결한다.

Ollama 앱을 실행하거나 별도 터미널에서 ``ollama serve``를 실행한다.
모델이 없으면 ``ollama pull qwen3:1.7b``로 받는다.

    from address_standardizer.local_ollama_token_parser import make_local_ollama_token_parser

    parser = make_local_ollama_token_parser()
    # 기존 Pipeline의 parser에 전달한다. RegionValidator가 DB 라벨을 판정한다.

단건은 명칭 한 줄, 배치는 [{"id": 0, "tokens": "서울 강남구"}] 형식이다.
주소번호와 상세주소는 명칭에서 제외하고 원문 대조에서 남은 구간에 보존한다.
명칭 자체에 들어 있는 숫자(대치2동, 개야도4길)는 유지한다.

Pipeline은 Ollama 오류를 module_exception으로 기록한다. trace.details의
error_type이 OllamaConnectionError이면 서버를, OllamaModelNotFoundError이면
모델 설치를 확인한다. OllamaTimeoutError이면 timeout을 늘리거나
BatchConfig.llm_batch_size를 줄일 수 있다. OllamaResponseError는 HTTP 오류,
비정상 응답 또는 생성이 중단된 응답을 뜻한다. 배치 ID와 명칭 형식 오류는
기존 파서가 처리하고 DB 검증 후 명칭 재시도도 기존 Pipeline에서 실행한다.
"""

from dataclasses import replace
from functools import partial
from http.client import HTTPConnection, HTTPException
import json
import re

from .contracts import AddressRecord, Module, ParseOutput
from .token_only_parser import make_token_only_parser


MODEL_NAME = "qwen3:1.7b"
# 로컬 Ollama는 기본(OLLAMA_NUM_PARALLEL=1)으로 요청을 하나씩 처리한다. OpenAI 기준 동시 요청(16)을 그대로
# 보내면 나머지는 대기열에서 기다리며, 대기 시간도 timeout에 포함된다. 2개면 하나를 처리하는 동안 다음 요청이
# 바로 이어져 서버가 쉬지 않고, 대기는 요청 하나 분량으로 제한된다. BatchConfig.llm_max_concurrency가 우선한다.
DEFAULT_OLLAMA_CONCURRENCY = 2
BATCH_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "tokens": {"type": "string"}},
        "required": ["id", "tokens"],
        "additionalProperties": False,
    },
}
BATCH_EXAMPLE = (
    '\n\n[배치 예시]\n입력: [{"id":4,"address":"서울강남대치2동 100-2"},'
    '{"id":7,"address":"용당로331번길 14 101동 203호"}]\n'
    '출력: [{"id":4,"tokens":"서울 강남 대치2동"},'
    '{"id":7,"tokens":"용당로331번길"}]'
)
# 숫자로 시작하는 독립 상세주소만 제거한다. 대치2동·용당로331번길은 명칭으로 남긴다.
_DETAIL_TOKENS = re.compile(
    r"(?<!\S)(?:(?:지하[ \t]*)?\d+[ \t]*[동층호](?:[ \t]*\d+[ \t]*[동층호])*"
    r"|(?:산[ \t]*)?\d+(?:[ \t]*-[ \t]*\d+)?[ \t]*번지)(?!\S)"
)


class OllamaConnectionError(ConnectionError):
    """로컬 Ollama 서버에 연결할 수 없다."""


class OllamaModelNotFoundError(RuntimeError):
    """Ollama 서버에 지정한 모델이 없다."""


class OllamaTimeoutError(TimeoutError):
    """Ollama 호출 제한 시간을 초과했다."""


class OllamaResponseError(RuntimeError):
    """Ollama가 완료된 채팅 응답을 반환하지 않았다."""


def _chat(prompt: str, text: str, *, model: str, port: int, timeout: float, batch: bool) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 2048 if batch else 256},
    }
    if batch:
        payload["format"] = BATCH_RESPONSE_SCHEMA

    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "POST", "/api/chat", json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        with connection.getresponse() as response:
            status, body = response.status, response.read()
    except TimeoutError as exc:
        raise OllamaTimeoutError("Ollama 응답 시간이 초과되었습니다.") from exc
    except (OSError, HTTPException) as exc:
        raise OllamaConnectionError(f"로컬 Ollama({port})를 확인하세요. 실행 명령은 ollama serve입니다.") from exc
    finally:
        connection.close()

    if status == 404 and model.encode() in body:
        raise OllamaModelNotFoundError(f"ollama pull {model}을 실행하세요.")
    if status != 200:
        raise OllamaResponseError(f"Ollama /api/chat HTTP {status}")

    try:
        result = json.loads(body)
        content = result["message"]["content"]
    except (KeyError, TypeError, ValueError) as exc:
        raise OllamaResponseError("Ollama 채팅 응답 형식이 잘못되었습니다.") from exc
    if result.get("done") is not True or result.get("done_reason") == "length" or not isinstance(content, str):
        raise OllamaResponseError("Ollama가 완료된 명칭 문자열을 반환하지 않았습니다.")
    return content


def _without_detail_tokens(text: str) -> str:
    if len(text.strip().splitlines()) > 1:
        return text  # 기존 파서가 여러 줄 응답을 오류로 기록한다.
    return _DETAIL_TOKENS.sub("", text)


def _generate(prompt: str, text: str, *, model: str, port: int, timeout: float) -> str:
    return _without_detail_tokens(_chat(prompt, text, model=model, port=port, timeout=timeout, batch=False))


def _generate_batch(prompt: str, text: str, *, model: str, port: int, timeout: float) -> str:
    content = _chat(prompt + BATCH_EXAMPLE, text, model=model, port=port, timeout=timeout, batch=True)
    try:
        rows = json.loads(content)
    except ValueError:
        return content  # 응답 형식 오류는 기존 배치 파서가 기록한다.
    if not isinstance(rows, list):
        return content
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("tokens"), str):
            row["tokens"] = _without_detail_tokens(row["tokens"])
    return json.dumps(rows, ensure_ascii=False)


def make_local_ollama_token_parser(
    *, model: str = MODEL_NAME, port: int = 11434, timeout: float = 120,
    concurrency: int = DEFAULT_OLLAMA_CONCURRENCY,
) -> Module[AddressRecord, ParseOutput]:
    """로컬 Ollama에 연결하는 단건·배치·재시도 파서를 반환한다.

    concurrency는 BatchConfig.llm_max_concurrency를 지정하지 않았을 때 쓰는 동시 요청 수다.
    Ollama 서버의 OLLAMA_NUM_PARALLEL을 늘렸다면 그만큼 올릴 수 있다.
    """
    model = model.strip()
    if not model:
        raise ValueError("Ollama 모델 이름을 지정하세요.")
    if concurrency < 1:
        raise ValueError("Ollama 동시 요청 수는 1 이상이어야 합니다.")
    generate = partial(_generate, model=model, port=port, timeout=timeout)
    generate_batch = partial(_generate_batch, model=model, port=port, timeout=timeout)
    parser = make_token_only_parser(generate, generate_batch=generate_batch)
    retry = replace(parser.retry_parser, llm_default_concurrency=concurrency) if parser.retry_parser else None
    return replace(parser, llm_default_concurrency=concurrency, retry_parser=retry)
