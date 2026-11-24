from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.request

from dotenv import load_dotenv
from analyzer import analyze

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DEFAULT_URL = "http://opendata.trudvsem.ru/api/v1/vacancies/region/39"


def extract_vacancies(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError("JSON должен быть объектом или массивом")
    results = data.get("results", {})
    if isinstance(results, dict) and isinstance(results.get("vacancies"), list):
        return results["vacancies"]
    if isinstance(results, list):
        return results
    if isinstance(data.get("vacancies"), list):
        return data["vacancies"]
    if "vacancy" in data or "id" in data:
        return [data]
    raise ValueError("Не найден массив вакансий (ожидается results.vacancies)")


def _page_url(url: str, offset: int, limit: int) -> str:
    """Build API URL; Trudvsem offset is a zero-based page number."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"offset": str(offset), "limit": str(limit)})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _vacancy_id(item) -> str:
    if not isinstance(item, dict):
        return ""
    vacancy = item.get("vacancy", item)
    if not isinstance(vacancy, dict):
        return ""
    return str(vacancy.get("id") or vacancy.get("vacancy_id") or item.get("id") or "")


def fetch_api(url: str, max_vacancies: int, page_size: int = 100) -> list:
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL источника должен начинаться с http:// или https://")
    if max_vacancies < 1:
        raise ValueError("max_vacancies должен быть не меньше 1")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size должен находиться в диапазоне от 1 до 100")

    collected = []
    seen_ids = set()
    page_number = 0

    while len(collected) < max_vacancies:
        limit = min(page_size, max_vacancies - len(collected))
        request = urllib.request.Request(
            _page_url(url, page_number, limit),
            headers={"User-Agent": "TransparentVacancy/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"API вернул HTTP {response.status}")
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, dict):
            raise RuntimeError("API вернул ответ неожиданного формата")
        if str(data.get("status", "200")) != "200":
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            raise RuntimeError(f"Ошибка API: {meta.get('error', data.get('status'))}")

        page = extract_vacancies(data)
        if not page:
            break

        for item in page:
            ident = _vacancy_id(item)
            if ident and ident in seen_ids:
                continue
            if ident:
                seen_ids.add(ident)
            collected.append(item)
            if len(collected) >= max_vacancies:
                break

        # API offset is the page number (0, 1, 2...), not a record count.
        page_number += 1
        if len(page) < limit:
            break

    return collected[:max_vacancies]


def atomic_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def build_report(vacancies, use_ai=True):
    results = []
    seen_ids = set()
    seen_without_id = set()

    for item in vacancies:
        result = analyze(item, use_ai=use_ai)
        ident = result["id"]
        if ident != "unknown":
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
        else:
            # Do not collapse every ID-less vacancy into one "unknown" record.
            fingerprint = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if fingerprint in seen_without_id:
                continue
            seen_without_id.add(fingerprint)
        results.append(result)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "region": "39",
        "vacancies_count": len(results),
        "vacancies": results,
    }


def run_api(args, exit_on_error=True):
    output = Path(args.output)
    try:
        jobs = fetch_api(args.url, args.max_vacancies, args.page_size)
        report = build_report(jobs, use_ai=not args.no_ai)
        atomic_json(output, report)
        print(f"Готово: обработано {len(jobs)} вакансий. Отчёт: {output}")
        return True
    except Exception as error:
        print(f"[ERROR] Не удалось получить/обработать данные API: {error}", file=sys.stderr)
        if output.exists():
            print(f"Последний успешный отчёт сохранён: {output}", file=sys.stderr)
        if exit_on_error:
            return False
        return False


def run_file(args):
    source = Path(args.input)
    if not source.exists():
        raise FileNotFoundError(f"Файл не найден: {source}")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    jobs = extract_vacancies(data)
    report = build_report(jobs, use_ai=not args.no_ai)
    atomic_json(Path(args.output), report)
    print(f"Готово: обработано {len(jobs)} вакансий. Отчёт: {args.output}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Анализатор понятности вакансий портала Работа в России"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    api = subparsers.add_parser("api", help="Загрузить вакансии из API региона 39")
    api.add_argument("--url", default=os.getenv("SOURCE_URL", DEFAULT_URL))
    api.add_argument("--max-vacancies", type=int, default=int(os.getenv("MAX_VACANCIES", "200")))
    api.add_argument("--page-size", type=int, default=100)
    api.add_argument("--output", default=os.getenv("REPORT_PATH", "reports/latest.json"))
    api.add_argument("--no-ai", action="store_true", help="Только базовые правила")
    api.set_defaults(func=run_api)

    local = subparsers.add_parser("file", help="Проверочный режим: обработать локальный JSON")
    local.add_argument("input", help="Путь к JSON в формате API")
    local.add_argument("--output", default="reports/check.json")
    local.add_argument("--no-ai", action="store_true")
    local.set_defaults(func=run_file)

    serve = subparsers.add_parser("serve", help="Запуск по интервалу из .env")
    serve.add_argument("--no-ai", action="store_true")
    serve.set_defaults(func=None)

    args = parser.parse_args()
    if args.command == "serve":
        from apscheduler.schedulers.blocking import BlockingScheduler

        interval = max(1, int(os.getenv("SCHEDULE_HOURS", "24")))

        class Settings:
            pass

        settings = Settings()
        settings.url = os.getenv("SOURCE_URL", DEFAULT_URL)
        settings.max_vacancies = int(os.getenv("MAX_VACANCIES", "200"))
        settings.page_size = 100
        settings.output = os.getenv("REPORT_PATH", "reports/latest.json")
        settings.no_ai = args.no_ai

        print(f"Планировщик запущен: каждые {interval} ч. Первый запуск — сейчас.")
        run_api(settings, exit_on_error=False)
        scheduler = BlockingScheduler(timezone=os.getenv("TIMEZONE", "Europe/Kaliningrad"))
        scheduler.add_job(
            run_api, "interval", hours=interval, args=[settings, False],
            max_instances=1, coalesce=True,
        )
        scheduler.start()
        return

    if not args.func(args):
        sys.exit(1)


if __name__ == "__main__":
    main()
