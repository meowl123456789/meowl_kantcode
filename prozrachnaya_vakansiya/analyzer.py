"""Vacancy clarity analyzer: deterministic checks + optional local Ollama review."""
from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from typing import Any

FIELDS = {
    "title": ("Название должности", ["job-name", "job_name", "title", "name"]),
    "duties": ("Обязанности", ["duty", "responsibilities", "duties"]),
    "requirements": ("Требования", ["requirement", "requirements"]),
    "salary": ("Оплата", ["salary", "salary_min", "salary_max"]),
    "schedule": ("График", ["schedule", "work_schedule", "график"]),
    "location": ("Место работы", ["addresses", "address", "location", "work-places", "work_places"]),
    "employment": ("Тип занятости", ["employment", "employment_type", "занятость"]),
}


def as_text(value: Any) -> str:
    """Convert nested API values into readable text without Python repr noise."""
    if value is None or value == "":
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        return "; ".join(text for item in value if (text := as_text(item)))
    if isinstance(value, dict):
        return "; ".join(
            f"{key}: {as_text(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        )
    return str(value).strip()


def _first_present(mapping: dict, names: list[str]) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _location_text(value: Any) -> str:
    """Handle API shape addresses.address = [{location: ...}, ...]."""
    if isinstance(value, dict):
        if "address" in value:
            return _location_text(value["address"])
        parts = []
        for key in ("location", "address", "formatted", "description"):
            if value.get(key):
                parts.append(as_text(value[key]))
        return "; ".join(parts) if parts else as_text(value)
    if isinstance(value, list):
        return "; ".join(filter(None, (_location_text(item) for item in value)))
    return as_text(value)


def _salary_text(v: dict) -> str:
    direct = _first_present(v, ["salary"])
    if direct is not None:
        return as_text(direct)
    low = _first_present(v, ["salary_min"])
    high = _first_present(v, ["salary_max"])
    currency = as_text(v.get("currency"))
    if low is None and high is None:
        return ""
    if low is not None and high is not None:
        amount = f"{as_text(low)}–{as_text(high)}"
    elif low is not None:
        amount = f"от {as_text(low)}"
    else:
        amount = f"до {as_text(high)}"
    return f"{amount} {currency}".strip()


def vacancy_fields(raw: dict) -> tuple[dict, dict]:
    """Return original vacancy object and normalized fields."""
    v = raw.get("vacancy", raw) if isinstance(raw, dict) else {}
    if not isinstance(v, dict):
        v = {}

    values = {}
    for key, (_, aliases) in FIELDS.items():
        values[key] = _first_present(v, aliases)

    # Salary is a combined concept: preserve a direct salary string when present,
    # otherwise combine bounds and currency.
    values["salary"] = _salary_text(v)

    if values["location"] is not None:
        values["location"] = _location_text(values["location"])

    requirement = values.get("requirements")
    if isinstance(requirement, dict):
        values["requirements"] = requirement

    company = v.get("company")
    employer = company.get("name", "") if isinstance(company, dict) else as_text(company)

    desc_parts = []
    for key in ("description", "vacancy-desc", "vacancy_description", "text"):
        if v.get(key):
            desc_parts.append(as_text(v[key]))
    description = "\n".join(desc_parts)

    normalized = {key: as_text(value) for key, value in values.items()}
    normalized["employer"] = as_text(employer)
    normalized["description"] = description

    # Keep the complete description available to rules/AI; don't pretend it is
    # a structured field when it cannot be reliably classified.
    return v, normalized


def add_issue(items: list, topic: str, field: str, quote: str, problem: str, recommendation: str):
    items.append({
        "topic": topic,
        "source_field": field,
        "quote": quote,
        "problem": problem,
        "recommendation": recommendation,
    })



def _description_contains_details(key: str, description: str) -> bool:
    """Conservative checks for clearly labelled facts in the free-text description."""
    text = description.lower()
    labels = {
        "title": r"(?:должность|вакансия)\s*[:\-]\s*\S+",
        "duties": r"(?:обязанности|должностные обязанности|задачи)\s*[:\-]\s*\S+",
        "requirements": r"(?:требования|кандидат должен|необходимые навыки)\s*[:\-]\s*\S+",
        "salary": r"(?:зарплата|заработная плата|оплата труда|оклад)\s*[:\-]?\s*(?:от\s*)?\d[\d\s]*(?:руб|₽|тыс)?",
        "schedule": r"(?:график работы|режим работы)\s*[:\-]\s*\S+",
        "location": r"(?:место работы|адрес работы|местонахождение)\s*[:\-]\s*\S+",
        "employment": r"(?:тип занятости|занятость)\s*[:\-]\s*\S+",
    }
    pattern = labels.get(key)
    return bool(pattern and re.search(pattern, text))


def rule_checks(v: dict, f: dict) -> list[dict]:
    issues: list[dict] = []
    specs = [
        ("title", "Название должности", "Укажите конкретное название должности, чтобы соискатель понимал, на какую работу откликается."),
        ("duties", "Обязанности", "Перечислите основные задачи и зону ответственности."),
        ("requirements", "Требования", "Уточните обязательные образование, навыки, квалификацию и опыт; если ограничений нет, укажите это."),
        ("salary", "Оплата", "Укажите размер оплаты или диапазон, валюту и период выплаты; при наличии — условия премирования."),
        ("schedule", "График", "Уточните рабочие дни, время начала и окончания смены, продолжительность перерывов и режим выходных."),
        ("location", "Место работы", "Укажите населённый пункт и конкретное место работы либо условия удалённой работы."),
        ("employment", "Тип занятости", "Уточните формат занятости: полная/частичная, постоянная/временная или иной вариант."),
    ]
    for key, label, recommendation in specs:
        if not f.get(key) and not _description_contains_details(key, f.get("description", "")):
            add_issue(
                issues, label, FIELDS[key][1][0], "",
                f"В структурированных данных не найдено значение поля «{label.lower()}».",
                recommendation,
            )

    # Search both the dedicated schedule field and free-text description.
    schedule_source = " ".join(filter(None, (f.get("schedule", ""), f.get("description", ""))))
    schedule_lower = schedule_source.lower()
    ambiguous = ("гибкий график", "свободный график", "по договоренности", "по договорённости")
    if any(phrase in schedule_lower for phrase in ambiguous):
        concrete = re.search(
            r"\b\d{1,2}\s*/\s*\d{1,2}\b|\b\d{1,2}:\d{2}\b|"
            r"\b(пн|вт|ср|чт|пт|сб|вс)\b",
            schedule_lower,
        )
        if not concrete:
            phrase = next(phrase for phrase in ambiguous if phrase in schedule_lower)
            add_issue(
                issues, "График", "schedule / description", phrase,
                "Использована общая формулировка без конкретных дней и часов работы.",
                "Уточните, какие дни и часы предусмотрены, сколько длится смена и как составляется расписание.",
            )
    return issues


SYSTEM_PROMPT = """Ты — редактор вакансий. Проверь, насколько понятны условия работы соискателю.
Используй ТОЛЬКО сведения из переданного объекта. Не делай юридических выводов и не выдумывай факты.
Не считай отсутствие повтора в описании проблемой, если сведения есть в структурированных полях.
Ищи реальные пропуски, неоднозначности и противоречия. Для каждого замечания верни тему,
поле источника, точную цитату/значение (для пропуска пустую строку), суть проблемы и конкретную
рекомендацию работодателю. Для противоречия укажи оба исходных значения в quote.
Не дублируй очевидные замечания из rule_findings.
Верни строго JSON-массив объектов с ключами topic, source_field, quote, problem, recommendation.
Если проблем нет — []."""


def llm_review(v: dict, f: dict, rule_findings: list[dict]) -> list[dict]:
    """Call local Ollama; return only well-formed findings grounded in source text."""
    base = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                {"vacancy": v, "normalized_fields": f, "rule_findings": rule_findings},
                ensure_ascii=False,
            )},
        ],
        "options": {"temperature": 0.1},
    }
    request = urllib.request.Request(
        base + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(os.getenv("OLLAMA_TIMEOUT", "90"))) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body.get("message", {}).get("content", "[]")
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            parsed = parsed.get("issues", [])
        if not isinstance(parsed, list):
            return []

        source_text = json.dumps(v, ensure_ascii=False)
        clean = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            finding = {
                key: as_text(item.get(key, ""))
                for key in ("topic", "source_field", "quote", "problem", "recommendation")
            }
            if not all((finding["topic"], finding["source_field"], finding["problem"], finding["recommendation"])):
                continue
            # Quotes must be empty for a missing value or actually occur in source data.
            if finding["quote"] and finding["quote"] not in source_text:
                continue
            clean.append(finding)
        return clean
    except (OSError, ValueError, KeyError, urllib.error.URLError, TimeoutError) as error:
        print(f"[WARN] Локальная ИИ-модель недоступна или вернула некорректный ответ ({error}). "
              "Используются только базовые проверки.")
        return []


def analyze(raw: dict, use_ai: bool = True) -> dict:
    vacancy, fields = vacancy_fields(raw)
    findings = rule_checks(vacancy, fields)
    if use_ai:
        ai_findings = llm_review(vacancy, fields, findings)
        seen = {
            (item["topic"].casefold(), item["source_field"].casefold(), item["problem"].casefold())
            for item in findings
        }
        for item in ai_findings:
            key = (item["topic"].casefold(), item["source_field"].casefold(), item["problem"].casefold())
            if key not in seen:
                findings.append(item)
                seen.add(key)

    vacancy_id = (
        vacancy.get("id") or vacancy.get("vacancy_id")
        or (raw.get("id") if isinstance(raw, dict) else None) or "unknown"
    )
    return {
        "id": str(vacancy_id),
        "url": vacancy.get("vac_url") or vacancy.get("url") or "",
        "issues": findings,
    }
