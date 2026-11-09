"""
Регрессионный тест публичного примера конфигурации config.example.yaml.

Проверяет:
1. config.example.yaml существует в корне проекта и валиден по синтаксису YAML.
2. Содержит только секцию company_overrides (является фрагментом, а не полной заменой config.yaml).
3. company_overrides является mapping'ом и содержит оба поддерживаемых вида:
   - строку с каноническим именем;
   - mapping со строковым name и допустимым entity_type (company, individual, region, sovereign).
4. В mapping overrides не рекламируется неподдерживаемое поле inn.
5. Прогоняет все примеры из config.example.yaml через resolve_company() и проверяет корректность разрешения.
6. Не читает локальный config.yaml, .env, базу данных и данные личного портфеля.
"""
from pathlib import Path
import yaml
import pytest

from src.company_normalization import (
    TYPE_COMPANY,
    TYPE_INDIVIDUAL,
    TYPE_REGION,
    TYPE_SOVEREIGN,
    resolve_company,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = PROJECT_ROOT / "config.example.yaml"


def test_config_example_file_structure():
    assert EXAMPLE_PATH.is_file(), "config.example.yaml должен существовать в корне проекта"

    with open(EXAMPLE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert isinstance(data, dict), "config.example.yaml должен парситься в словарь"
    assert list(data.keys()) == ["company_overrides"], (
        "config.example.yaml должен содержать исключительно секцию company_overrides"
    )

    overrides = data["company_overrides"]
    assert isinstance(overrides, dict), "company_overrides должен быть словарём (mapping)"
    assert len(overrides) > 0, "company_overrides не должен быть пустым"

    allowed_types = {TYPE_COMPANY, TYPE_INDIVIDUAL, TYPE_REGION, TYPE_SOVEREIGN}
    has_str_override = False
    has_dict_override = False

    for key, val in overrides.items():
        assert isinstance(key, str) and key.strip(), f"Ключ {key!r} должен быть непустой строкой"
        if isinstance(val, str):
            has_str_override = True
            assert val.strip(), f"Строковое значение для {key!r} не должно быть пустым"
        elif isinstance(val, dict):
            has_dict_override = True
            assert "name" in val and isinstance(val["name"], str) and val["name"].strip(), (
                f"Словарь-override для {key!r} должен содержать непустую строку 'name'"
            )
            assert "entity_type" in val, f"Словарь-override для {key!r} должен содержать 'entity_type'"
            assert val["entity_type"] in allowed_types, (
                f"Недопустимый entity_type {val['entity_type']!r} для {key!r}; "
                f"допустимы: {allowed_types}"
            )
            assert "inn" not in val, (
                f"Поле 'inn' не должно рекламироваться в {key!r}, так как не поддерживается привязкой к БД"
            )
        else:
            pytest.fail(f"Значение для {key!r} должно быть строкой или словарём, получено: {type(val)}")

    assert has_str_override, "В примере должен присутствовать хотя бы один строковый override"
    assert has_dict_override, "В примере должен присутствовать хотя бы один mapping-override (name + entity_type)"


def test_config_example_resolve_company():
    with open(EXAMPLE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    overrides = data["company_overrides"]

    # 1. Проверка строкового override: "МФК Быстроденьги": "Быстроденьги"
    ref_str = resolve_company("МФК Быстроденьги 002P-05", overrides)
    assert ref_str.name == "Быстроденьги"
    assert ref_str.entity_type == TYPE_COMPANY

    # 2. Проверка mapping override: "Газпром": name: "Газпром Капитал", entity_type: "company"
    ref_dict = resolve_company("Газпром БО-22", overrides)
    assert ref_dict.name == "Газпром Капитал"
    assert ref_dict.entity_type == TYPE_COMPANY

    # 3. Проверка приоритета точного названия выпуска над нормализованным
    ref_exact = resolve_company("ЭР-Телеком Холдинг выпуск 3", overrides)
    assert ref_exact.name == "ЭР-Телеком Холдинг"

    # 4. Проверка региона/суверена
    ref_region = resolve_company("Субъект РФ Примерный 01", overrides)
    assert ref_region.name == "Примерная область"
    assert ref_region.entity_type == TYPE_REGION
