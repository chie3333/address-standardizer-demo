"""Local HTTP entry point for the address review workbench."""
from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from hmac import compare_digest, digest
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps, loads
from pathlib import Path
from re import fullmatch
from secrets import token_bytes, token_hex
from urllib.parse import parse_qs, urlsplit

from .adapter import InputRecord, load_adapter
from .additional_analysis import AdditionalAnalysis
from .demo import DemoAdapter, SAMPLE_ADDRESSES
from .jobs import JobRunner
from .tabular import read_table, records_from

WEB = Path(__file__).with_name("web")
MAX_BODY = 8 * 1024 * 1024
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
    "/result.mjs": ("result.mjs", "text/javascript; charset=utf-8"),
    "/analysis.mjs": ("analysis.mjs", "text/javascript; charset=utf-8"),
    "/analysis.css": ("analysis.css", "text/css; charset=utf-8"),
}


# 이름 → 어댑터 팩토리. --engine으로 여러 개를 골라 한 서버에 올리면 화면에서 엔진을 고른다.
ENGINES = {
    "new_pipeline": ("새 파이프라인", "address_pipeline:create_adapter"),
    "bigquery": ("BigQuery 엔진", "address_workbench.bigquery_engine:create_adapter"),
}


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    adapter: object
    source: str


def make_handler(adapter, source=None, *, public_origin: str | None = None):
    """adapter 하나(+source) 또는 Engine 목록을 받는다. 목록의 첫 엔진이 기본값이다."""
    engines = list(adapter) if isinstance(adapter, (list, tuple)) else [Engine(source, source, adapter, source)]
    if not engines or len({engine.key for engine in engines}) != len(engines):
        raise ValueError("엔진 이름이 비었거나 겹칩니다.")
    runners = {engine.key: JobRunner(engine.adapter, engine.source) for engine in engines}
    analysis = AdditionalAnalysis()
    default = engines[0]
    adapter, source = default.adapter, default.source

    def find_runner(job_id):
        return next((runner for runner in runners.values() if job_id in runner.jobs), runners[default.key])

    def latest(owner_id):
        jobs = [job for runner in runners.values() if (job := runner.latest(owner_id=owner_id))]
        return max(jobs, key=lambda job: job.started_at, default=None)
    session_key = token_bytes(32)
    cookie_name = "__Host-address_wb" if public_origin else "address_wb"
    public_host = urlsplit(public_origin).netloc if public_origin else None

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, mime="application/json; charset=utf-8", *, cookie=None):
            if not isinstance(body, bytes):
                body = dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def session(self, *, create=False):
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
            except CookieError:
                cookies.clear()
            cookie = cookies.get(cookie_name)
            if cookie and fullmatch(r"[0-9a-f]{64}\.[0-9a-f]{64}", cookie.value):
                owner_id, signature = cookie.value.split(".")
                expected = digest(session_key, owner_id.encode(), "sha256").hex()
                if compare_digest(signature, expected):
                    return owner_id, None
            if not create:
                return None, None
            owner_id = token_hex(32)
            signature = digest(session_key, owner_id.encode(), "sha256").hex()
            secure = "; Secure" if public_origin else ""
            return owner_id, f"{cookie_name}={owner_id}.{signature}; Path=/; HttpOnly; SameSite=Lax{secure}"

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in STATIC:
                name, mime = STATIC[path]
                self.reply(200, (WEB / name).read_bytes(), mime)
            elif path == "/api/config":
                _, cookie = self.session(create=True)
                self.reply(200, {
                    "source": source, "reference_version": adapter.reference_version, "default_engine": default.key,
                    "engines": [{"key": engine.key, "label": engine.label, "source": engine.source,
                                 "reference_version": engine.adapter.reference_version} for engine in engines],
                }, cookie=cookie)
            elif path == "/api/jobs/latest":
                owner_id, _ = self.session()
                job = latest(owner_id)
                self.reply(200, {"job_id": job.job_id if job else None, "status": job.status if job else None})
            elif path.startswith("/api/jobs/") and path.endswith("/analysis"):
                job_id = path.removeprefix("/api/jobs/").removesuffix("/analysis")
                address_id = parse_qs(urlsplit(self.path).query).get("address_id", [""])[0]
                owner_id, _ = self.session()
                snapshot = analysis.snapshot(find_runner(job_id), job_id, address_id, owner_id=owner_id)
                self.reply(200, snapshot) if snapshot is not None else self.reply(404, {"error": "분석할 실행 또는 주소를 찾을 수 없습니다."})
            elif path.startswith("/api/jobs/"):
                query = parse_qs(urlsplit(self.path).query)
                try:
                    since = max(0, int(query.get("since", ["0"])[0]))
                except ValueError:
                    since = 0
                owner_id, _ = self.session()
                job_id = path.removeprefix("/api/jobs/")
                snapshot = find_runner(job_id).snapshot(job_id, since, owner_id=owner_id)
                if snapshot is None:
                    self.reply(404, {"error": "실행 기록을 찾을 수 없습니다. 서버가 다시 시작되었을 수 있습니다."})
                else:
                    self.reply(200, snapshot)
            else:
                self.reply(404, {"error": "요청한 경로를 찾을 수 없습니다."})

        def do_POST(self):
            request = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(request.query).items()}
            if not self.same_origin():
                self.reply(403, {"error": "이 서버의 화면에서 요청하세요."})
                return
            if request.path.startswith("/api/jobs/") and request.path.endswith("/approve"):
                self.approve(request.path.removeprefix("/api/jobs/").removesuffix("/approve"))
                return
            if request.path.startswith("/api/jobs/") and request.path.endswith("/analysis"):
                self.analyze(request.path.removeprefix("/api/jobs/").removesuffix("/analysis"))
                return
            runner = runners.get(query.get("engine") or default.key)
            if runner is None:
                self.reply(400, {"error": "알 수 없는 처리 엔진입니다."})
                return
            try:
                records = self.read_records(request.path, query)
                if records is None:
                    return
            except (ValueError, UnicodeError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
                return
            except Exception:
                self.reply(400, {"error": "입력 파일을 읽지 못했습니다. 파일 형식과 내용을 확인하세요."})
                return
            if query.get("async") == "1":
                owner_id, cookie = self.session(create=True)
                try:
                    job = runner.start(records, owner_id=owner_id)
                except RuntimeError as exc:
                    self.reply(409, {"error": str(exc)})
                    return
                self.reply(202, {"job_id": job.job_id, "total": job.total}, cookie=cookie)
                return
            try:
                output = runner.run_sync(records)
            except Exception as exc:
                self.log_error("Adapter execution failed: %s", type(exc).__name__)
                self.reply(502, {"error": "어댑터가 결과를 반환하지 못했습니다. 연결 설정과 결과 계약을 확인하세요."})
                return
            self.reply(200, output)

        def analyze(self, job_id):
            try:
                request = loads(self.body())
                address_id = request.get("address_id")
                retry = request.get("retry", False)
                if not isinstance(address_id, str) or not address_id or len(address_id) > 2000 or type(retry) is not bool:
                    raise ValueError("분석할 주소 ID를 확인하세요.")
                owner_id, _ = self.session()
                result = analysis.start(find_runner(job_id), job_id, address_id, owner_id=owner_id, retry=retry)
            except (ValueError, AttributeError) as exc:
                self.reply(400, {"error": str(exc) if isinstance(exc, ValueError) else "요청 형식을 확인하세요."})
                return
            except RuntimeError as exc:
                self.reply(409, {"error": str(exc)})
                return
            if result is None:
                self.reply(404, {"error": "분석할 실행을 찾을 수 없습니다."})
                return
            self.reply(202 if result["status"] in {"queued", "running"} else 200,
                       {"address_id": address_id, "analysis": result})

        def approve(self, job_id):
            try:
                request = loads(self.body())
                ids = request.get("address_ids")
                if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids) or len(ids) > 20000:
                    raise ValueError
            except (ValueError, AttributeError):
                self.reply(400, {"error": "승인할 주소 ID 목록을 확인하세요."})
                return
            try:
                owner_id, _ = self.session()
                rows = find_runner(job_id).approve(job_id, ids, owner_id=owner_id, undo=bool(request.get("undo")))
            except RuntimeError as exc:
                self.reply(409, {"error": str(exc)})
                return
            if rows is None:
                self.reply(404, {"error": "실행 기록을 찾을 수 없습니다. 서버가 다시 시작되었을 수 있습니다."})
                return
            self.reply(200, {"rows": rows})

        def same_origin(self):
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if public_origin:
                return host == public_host and (origin is None or origin == public_origin)
            port = self.server.server_port
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            return host in allowed and (origin is None or origin == f"http://{host}")

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError("요청 크기는 8 MiB 이하여야 하며 빈 파일은 읽을 수 없습니다.")
            return self.rfile.read(size)

        def read_records(self, path, query):
            if path == "/api/sample" and source == "demo":
                return tuple(InputRecord(f"row-{i}", raw) for i, raw in enumerate(SAMPLE_ADDRESSES, 1))
            if path == "/api/single":
                obj = loads(self.body())
                address = obj.get("raw_address") if isinstance(obj, dict) else None
                if not isinstance(address, str) or not address.strip() or len(address) > 2000:
                    raise ValueError("주소를 1~2,000자 사이로 입력하세요.")
                return (InputRecord("row-1", address),)
            if path in {"/api/table/inspect", "/api/table/run"}:
                table, sheets = read_table(self.body(), query.get("format", ""), query.get("sheet"))
                if path.endswith("/inspect"):
                    self.reply(200, {
                        "headers": table.headers, "row_count": len(table.rows),
                        "sheets": sheets, "sheet": query.get("sheet") or (sheets[0] if sheets else None),
                    })
                    return None
                if "address_col" not in query:
                    raise ValueError("주소 열을 선택하세요.")
                return records_from(table, int(query["address_col"]),
                                    int(query["id_col"]) if "id_col" in query else None)
            self.reply(404, {"error": "요청한 경로를 찾을 수 없습니다."})
            return None

    return Handler


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-origin", help="HTTPS 프록시를 통한 접속 주소, 예: https://address.example.com")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--engine", action="append", choices=sorted(ENGINES),
                      help="올릴 처리 엔진. 여러 번 쓰면 화면에서 고른다(첫 번째가 기본). 예: --engine new_pipeline --engine bigquery")
    mode.add_argument("--adapter", help="다른 처리 어댑터의 module:factory (기본: address_pipeline)")
    mode.add_argument("--demo", action="store_true", help="시연용 예시 데이터로 실행")
    args = parser.parse_args()
    if args.public_origin:
        origin = urlsplit(args.public_origin)
        if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                or args.public_origin != f"https://{origin.netloc}"):
            parser.error("--public-origin은 경로 없이 https://호스트 형식으로 지정하세요.")
    if args.demo:
        engines = [Engine("demo", "시연", DemoAdapter(), "demo")]
    elif args.adapter:
        engines = [Engine("pipeline", "연결된 파이프라인", load_adapter(args.adapter), "pipeline")]
    else:
        engines = []
        for key in dict.fromkeys(args.engine or ["new_pipeline"]):
            label, spec = ENGINES[key]
            print(f"{label}을 불러오는 중...", flush=True)
            try:
                engines.append(Engine(key, label, load_adapter(spec), key))
            except Exception as exc:  # noqa: BLE001 - 하나가 안 되면 나머지 엔진으로 연다.
                if len(args.engine or ()) < 2:
                    raise
                print(f"  {label}을 건너뜁니다: {type(exc).__name__}: {exc}", flush=True)
        if not engines:
            parser.error("불러온 처리 엔진이 없습니다.")
    handler = make_handler(engines, public_origin=args.public_origin)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"http://127.0.0.1:{server.server_port}  ({', '.join(engine.label for engine in engines)})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
