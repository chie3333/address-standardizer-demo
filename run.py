"""Load repository-local settings and start the address workbench."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    if (ROOT / ".env").is_file():
        try:
            from dotenv import load_dotenv
        except ImportError:
            raise SystemExit("설정 파일을 읽으려면 python -m pip install -r address_workbench/requirements.txt 를 실행하세요.")
        load_dotenv(ROOT / ".env", override=False, interpolate=False)
    from address_workbench.server import main as serve

    serve()


if __name__ == "__main__":
    main()
