"""
Нормализация названий облигаций в имена компаний-эмитентов.

Облигации приходят из API с «именами выпусков» вида «Газпром БО-22»,
«ЭР-Телеком Холдинг выпуск 3», «Свердловская область 35008». Для связи
«одна компания — много облигаций» нужен группирующий ключ: нормализованное
имя компании плюс тип сущности (company | individual | region | sovereign).

Модуль чистый (без обращений к API и БД), чтобы легко тестироваться.
Правила построены по фактическим паттернам названий каталога bonds_catalog;
исключения правятся через company_overrides в config.yaml.

Обезличенные примеры (без ISIN личного портфеля):
    «Газпром БО-22»                -> («Газпром», company)
    «ЭР-Телеком Холдинг выпуск 3»  -> («ЭР-Телеком Холдинг», company)
    «Газпром Нефть 003P-08R»       -> («Газпром Нефть», company)
    «ГПБ005P12P»                   -> («ГПБ», company)
    «Свердловская область 35008»   -> («Свердловская область», region)
    «ОФЗ 26249»                    -> («Минфин России», sovereign)
    «Казахстан выпуск 11»          -> («Республика Казахстан», sovereign)
    «Хамдеев Р.М.»                 -> («Хамдеев Р.М.», individual)
"""
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional

# Типы сущностей в таблице companies
TYPE_COMPANY = 'company'
TYPE_INDIVIDUAL = 'individual'
TYPE_REGION = 'region'
TYPE_SOVEREIGN = 'sovereign'


@dataclass(frozen=True)
class CompanyRef:
    """Результат нормализации: ключ группировки облигаций по эмитенту."""
    name: str
    entity_type: str = TYPE_COMPANY
    inn: Optional[str] = None  # только из overrides/API; для физлиц не задаём


# --- Регулярки для отбрасывания «хвостов выпусков» ---

# «выпуск 3», «выпуск 002Р-01», «биржевая облигация 4B02-…» — срезается с номером
_RE_ISSUE_WORD = re.compile(r'\s+выпуск\s+\S+$', re.IGNORECASE)
_RE_EXCHANGE_BOND_TAIL = re.compile(r'\s+биржевая\s+облигация\s+.*$', re.IGNORECASE)

# Орг-форма в скобках в конце: «Банк ВТБ (ПАО)» -> «Банк ВТБ»
_RE_PAREN_ORG_FORM = re.compile(
    r'\s*\((?:ПАО|ОАО|ЗАО|АО|ООО|АКБ)\)\s*$', re.IGNORECASE
)

# Полное юр. наименование: «Публичное акционерное общество "Ростелеком" …»
_RE_QUOTED_LEGAL_NAME = re.compile(
    r'^(?:Публичное акционерное общество|Открытое акционерное общество|'
    r'Акционерное общество|Общество с ограниченной ответственностью)\s+"([^"]+)"'
)

# Серийные токены с известным префиксом: «БО-22», «ПБО-002Р-41», «ЗО26-1»,
# «БЗО26-1-Д», «БО-П», «БО7», «Б-1-356», «СУБ-Т1-8». После префикса обязаны
# быть цифра или дефис, чтобы не задеть обычные слова («Золото», «Банка»).
_RE_SERIES_PREFIX = re.compile(
    r'^(?:П?БО|БЗО|ЗО|ВО|ДО|ИО|КО|КБО|СУБ|Б)'
    r'(?:[-\u2010\u2011\u2012\u2013\u2014]?[0-9A-Za-zА-Яа-яЁё]+){0,3}$'
)

# Числовой номер выпуска: «35008», «01», «34011» (одиночную цифру не трогаем:
# встречается в брендах вроде «ИКС 5 Финанс»)
_RE_PLAIN_NUMBER = re.compile(r'^\d{2,6}$')

# Серия, начинающаяся с цифр: «001Р-02», «003P-08R», «1В02»
# (кириллица и латиница неразличимы на глаз — принимаем обе)
_RE_DIGIT_SERIES = re.compile(
    r'^\d{1,6}(?:[-\u2010\u2011\u2012\u2013\u2014]?[0-9A-Za-zА-Яа-яЁё]{0,8}){0,3}$'
)

# Слово с номером через дефис: «Химки-1» -> «Химки».
# Аббревиатуры верхним регистром («ТГК-14») — часть имени, их не режем.
_RE_WORD_DASH_NUMBER = re.compile(r'^\D+[-\u2010\u2014]\d{1,4}$')

# Склеенный токен из букв и цифр: «GOLD01», «обб3П15», «12840119V», «ГПБ005P12P»,
# «TCS-perp1», «П02-БО-11»
_RE_GLUED_ALNUM = re.compile(r'^(?=.*\d)(?=.*[^\d])[\w.\-\u2010\u2011\u2012\u2013\u2014]+$')

# Переход «буква -> цифра» внутри склеенного токена (для среза хвоста серии)
_RE_LETTER_TO_DIGIT = re.compile(r'(?<=[^\d_])\d')

# Серийный «кусок» в склеенном токене: цифры с 1–2 буквами-разделителями
# («005P12P», «3P1», «3П15»), но не часть бренда («5Фин» из «ИКС5Фин»)
_RE_SERIES_CHUNK = re.compile(r'^\d{1,8}(?:[A-ZА-ЯЁ]{0,2}\d{0,4}){1,2}$')

# Физлицо-эмитент: «Фамилия И.О.»
_RE_INDIVIDUAL = re.compile(r'^[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.?$')

# Организационно-правовые формы в конце имени: «Трансмашхолдинг АО» -> «Трансмашхолдинг»
_ORG_FORMS = {'ООО', 'ПАО', 'ОАО', 'ЗАО', 'АО', 'АКБ'}

# Города федерального значения — отдельные субъекты РФ (регионы для облигаций)
_CITY_REGIONS = {'Москва', 'Санкт-Петербург', 'Севастополь'}

# Суверенные эмитенты: имя после нормализации -> каноническое имя
_SOVEREIGN_NAMES = {
    'Казахстан': 'Республика Казахстан',
    'Беларусь': 'Республика Беларусь',
}


def _is_series_token(token: str) -> bool:
    """Похож ли последний токен на номер/серию выпуска."""
    if _RE_PLAIN_NUMBER.match(token):
        return True
    if _RE_SERIES_PREFIX.match(token):
        rest = re.sub(r'^(?:П?БО|БЗО|ЗО|ВО|ДО|ИО|КО|КБО)', '', token)
        # после префикса должна быть цифра или дефис, иначе это слово («Золото»)
        return bool(re.search(r'[\d-]', rest))
    if _RE_DIGIT_SERIES.match(token) and re.search(r'\d', token):
        # «001Р-02» — ок; но «5 Финанс» защищено требованием 2+ знаков или серии
        return len(token) >= 2
    return False


def _strip_last_token(name: str) -> str:
    """Пытается отбросить/подрезать последний токен как хвост выпуска."""
    parts = name.split()
    if len(parts) < 2:
        return name
    token = parts[-1]

    if token in _ORG_FORMS:
        return ' '.join(parts[:-1])

    if _is_series_token(token):
        return ' '.join(parts[:-1])

    if _RE_WORD_DASH_NUMBER.match(token):
        word = re.split(r'[-\u2010\u2011\u2012\u2013\u2014]', token)[0]
        if not word.isupper():  # «Химки-1» -> «Химки»
            return ' '.join(parts[:-1] + [word])
        return name  # аббревиатура вида «ТГК-14» — часть имени, не трогаем

    if _RE_GLUED_ALNUM.match(token):
        return ' '.join(parts[:-1])

    return name


def _cut_glued_series(name: str) -> str:
    """
    Имя целиком — один склеенный токен («ГПБ005P12P»): срезаем серийный хвост.
    Отрезаем только «цифровые» хвосты, чтобы не испортить бренды («ИКС5Фин»
    режется до «ИКС5Фин», но не до «ИКС»).
    """
    matches = list(_RE_LETTER_TO_DIGIT.finditer(name))
    for match in reversed(matches):
        tail = name[match.start():]
        if len(tail) >= 2 and _RE_SERIES_CHUNK.match(tail):
            return name[:match.start()]
    return name


def _strip_issue_tails(bond_name: str) -> str:
    """Итеративно отбрасывает хвосты выпусков, пока имя не стабилизируется."""
    name = bond_name.strip()
    while True:
        stripped = _RE_EXCHANGE_BOND_TAIL.sub('', name).strip()
        if stripped != name:
            name = stripped
            continue
        stripped = _RE_ISSUE_WORD.sub('', name).strip()
        if stripped != name:
            name = stripped
            continue
        stripped = _RE_PAREN_ORG_FORM.sub('', name).strip()
        if stripped != name:
            name = stripped
            continue
        stripped = _strip_last_token(name)
        if stripped != name:
            name = stripped
            continue
        if ' ' not in name:
            if _RE_WORD_DASH_NUMBER.match(name):
                head = re.split(r'[-\u2010\u2011\u2012\u2013\u2014]', name)[0]
                if head.isupper():
                    break  # «ТГК-14» целиком — аббревиатура с номером
            cut = _cut_glued_series(name)
            if cut != name:
                name = cut
                continue
        break
    # подчистить висячие дефисы/пробелы после срезов
    return re.sub(r'[\s\u2010\u2011\u2012\u2013\u2014-]+$', '', name).strip()


def _is_region(name: str) -> bool:
    if name.startswith('Республика') or 'автономный округ' in name or 'автономная область' in name:
        return True
    if name.endswith(('область', 'край')):
        return True
    return name in _CITY_REGIONS


def normalize_bond_name(bond_name: str) -> CompanyRef:
    """
    Приводит название выпуска облигации к имени компании-эмитента.
    Возвращает CompanyRef — ключ группировки (имя + тип сущности).
    """
    name = (bond_name or '').strip()
    if not name:
        return CompanyRef(name='(без названия)')

    # Суверенные выпуски узнаём до зачистки хвостов
    if name.upper().startswith('ОФЗ'):
        return CompanyRef(name='Минфин России', entity_type=TYPE_SOVEREIGN)
    if name.split() and name.split()[0] == 'Russia':
        return CompanyRef(name='Россия', entity_type=TYPE_SOVEREIGN)

    # Полное юр. наименование — берём имя в кавычках:
    # «Публичное акционерное общество "Ростелеком" биржевая облигация …»
    quoted = _RE_QUOTED_LEGAL_NAME.match(name)
    if quoted:
        name = quoted.group(1)

    stripped = _strip_issue_tails(name)
    if not stripped:
        # имя целиком состояло из серии — оставляем как есть
        stripped = name

    if stripped in _SOVEREIGN_NAMES:
        return CompanyRef(name=_SOVEREIGN_NAMES[stripped], entity_type=TYPE_SOVEREIGN)

    if _is_region(stripped):
        return CompanyRef(name=stripped, entity_type=TYPE_REGION)

    if _RE_INDIVIDUAL.match(stripped):
        return CompanyRef(name=stripped, entity_type=TYPE_INDIVIDUAL)

    return CompanyRef(name=stripped, entity_type=TYPE_COMPANY)


def resolve_company(
    bond_name: str,
    overrides: Optional[Dict[str, Any]] = None
) -> CompanyRef:
    """
    Нормализует название выпуска и применяет ручные переопределения
    из config.yaml (company_overrides). Ключ переопределения — точное
    название выпуска либо нормализованное имя; значение — строка
    (каноническое имя) или словарь {name, entity_type, inn}.
    """
    ref = normalize_bond_name(bond_name)
    if not overrides:
        return ref

    override = overrides.get(bond_name) or overrides.get(ref.name)
    if isinstance(override, str):
        return replace(ref, name=override)
    if isinstance(override, dict):
        return CompanyRef(
            name=override.get('name', ref.name),
            entity_type=override.get('entity_type', ref.entity_type),
            inn=override.get('inn'),
        )
    return ref
