#!/usr/bin/env python3
"""issue #3: ответ агента не должен называть купонную ставку «доходностью».

Проверка ответа живого агента:
  ./venv/bin/python skills/scripts/check_yield_wording.py \
      --coupon 19 --ytm 13.71 --reply "купон 19%, доходность к оферте 23.11.2026 ≈13.7%"
Exit 0 — ок, 1 — нарушения (печатаются в stdout).

Тесты (сеть/агент не нужны):
  ./venv/bin/python -m pytest skills/scripts/check_yield_wording.py -v
"""
import argparse
import pathlib
import re
import sys
from typing import List

# «доходность ... 19%» в пределах одного предложения
# точка между цифрами (дата 23.11.2026) — не конец предложения
_YIELD_NUM = re.compile(r'доходност\w*((?:[^.;\n%]|(?<=\d)\.(?=\d)){0,60}?)(\d+(?:[.,]\d+)?)\s*%', re.IGNORECASE)
_HORIZON = re.compile(r'к\s+(оферт|погашени)', re.IGNORECASE)
SKILLS = ('buy-candidates', 'rebalance-advisor', 'portfolio-export', 'sql-recipes')


def check_reply(reply: str, coupon_pct: float, ytm_pct: float, tol: float = 0.15) -> List[str]:
    """Нарушения: купон назван доходностью; доходность без горизонта."""
    problems = []
    for m in _YIELD_NUM.finditer(reply):
        value = float(m.group(2).replace(',', '.'))
        if abs(value - coupon_pct) <= tol and abs(value - ytm_pct) > tol:
            problems.append(f'купон {coupon_pct}% назван доходностью: «{m.group(0)}»')
        elif not _HORIZON.search(m.group(1)):
            problems.append(f'доходность без горизонта (к оферте/погашению): «{m.group(0)}»')
    return problems


def test_correct_reply_passes():
    assert check_reply('Купон 19%, доходность к оферте 23.11.2026 ≈13.7%.', 19, 13.71) == []


def test_reply_with_offer_date_is_matched():
    reply = 'Купон: 19,0043% годовых.\nДоходность к оферте 23.11.2026: 13,7102% годовых.'
    assert _YIELD_NUM.search(reply) and check_reply(reply, 19.0043, 13.7102) == []
    assert check_reply('Доходность к оферте 23.11.2026: 19,0%.', 19.0043, 13.7102)


def test_coupon_called_yield_fails():
    assert check_reply('Доходность бумаги 19% годовых.', 19, 13.71)


def test_yield_without_horizon_fails():
    assert check_reply('Доходность 13.7%.', 19, 13.71)


def test_skills_carry_rule():
    root = pathlib.Path(__file__).resolve().parent.parent / 'finance'
    for name in SKILLS:
        text = (root / f'finbonds-{name}' / 'SKILL.md').read_text(encoding='utf-8')
        assert 'Доходность ≠ купон' in text and 'ytm_to' in text, name


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--reply', required=True)
    ap.add_argument('--coupon', type=float, required=True)
    ap.add_argument('--ytm', type=float, required=True)
    a = ap.parse_args()
    found = check_reply(a.reply, a.coupon, a.ytm)
    print('\n'.join(found) or 'OK')
    sys.exit(1 if found else 0)
