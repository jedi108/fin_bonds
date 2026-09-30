"""
Переиспользуемые фильтры бумаг Python-тулкита (задачи 005.1/005.3, 023).

Одна реализация для rebalance-report (--freq-min/--freq-max/--freq-in/
--exclude-sovereign) и calculate-ytm (--coupon-freq-min/--coupon-freq-max/
--coupon-freq-in/--exclude-sovereign), чтобы конвенции фильтров команд
совпадали:
- P-C: частота купонов — «от N/год» и/или «максимум N/год» (диапазон)
  или «только из списка»;
- P-G: суверенные эмитенты — ОФЗ и евро-РФ.

Фильтры применяются к строкам выборки движка calculate-ytm (dict) в Python —
это даёт честную воронку «сколько отсечено и почему» (R9) и одинаковые
правила в обеих командах.
"""
import argparse
from typing import List, Optional


def parse_freq_list(raw: str, flag: str = '--freq-in') -> List[int]:
    """Разбирает '2,4' в список целых частот (для argparse type=).

    flag — имя CLI-флага для сообщений об ошибке (у команд разный:
    --freq-in у rebalance-report, --coupon-freq-in у calculate-ytm).
    """
    values: List[int] = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{flag}: нецелое значение частоты {part!r}"
            )
    if not values:
        raise argparse.ArgumentTypeError(f"{flag}: пустой список частот")
    return values


def is_sovereign(row: dict) -> bool:
    """Суверенный эмитент: связка с companies (entity_type='sovereign') либо
    каталог-имя ОФЗ (фолбэк на случай незаполненной связки company_id)."""
    if row.get('entity_type') == 'sovereign':
        return True
    name = (row.get('name') or '').strip().upper()
    return name.startswith('ОФЗ')


def freq_ok(
    freq: Optional[int],
    freq_max: Optional[int] = None,
    freq_in: Optional[List[int]] = None,
    freq_min: Optional[int] = None,
) -> bool:
    """Фильтр частоты купонов (P-C): без флагов проходит всё;
    NULL-частота фильтру не проходит (конвенция rebalance-report 005.1).

    023: freq_min/freq_max вместе задают диапазон (напр. 4..12 — «от
    квартальных до ежемесячных»); freq_in — список точных значений,
    взаимоисключим с диапазоном на уровне валидации CLI. Нулевая частота
    строгий диапазон не проходит (0 < freq_min при freq_min >= 1), при
    старом вызове «только --freq-max» прежняя семантика сохранена
    (0 <= freq_max проходил и проходит).
    """
    if freq_max is None and freq_in is None and freq_min is None:
        return True
    if freq is None:
        return False
    if freq_in is not None:
        return freq in freq_in
    if freq_min is not None and freq < freq_min:
        return False
    if freq_max is not None and freq > freq_max:
        return False
    return True
