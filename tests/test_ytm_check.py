"""issues #2/#3: горизонт YTM (ytm_to/offer) и сверка с MOEX YIELD (ytm_check)."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from src.moex.api_client import MoexApiClient
from src.services.cashflow import ytm_check_fields
from src.use_cases.calculate_ytm import CalculateYtmUseCase
from src.use_cases.update_bonds_catalog import _parse_moex_yield

TODAY = date.today()
OFFER = TODAY + timedelta(days=35)
MATURITY = TODAY + timedelta(days=1290)


def bond(**overrides):
    row = {
        'isin': 'RU000A10BP79', 'name': 'МТС 002Р-11',
        'ytm_percent': 13.71, 'coupon_rate_percent': Decimal('19.0'),
        'maturity_date': MATURITY, 'offer_date': OFFER,
        'moex_yield': Decimal('14.46'), 'moex_yield_date': OFFER,
    }
    row.update(overrides)
    return row


def test_check_ok_within_threshold():
    assert ytm_check_fields(bond(), threshold_pp=2.0) == {
        'ytm_to': 'offer', 'moex_yield_pct': 14.46, 'ytm_check': 'ok'}


def test_check_mismatch_by_value():
    assert ytm_check_fields(bond(), threshold_pp=0.5)['ytm_check'] == 'mismatch'


def test_check_mismatch_by_horizon():
    # MOEX считает к погашению, мы — к оферте: числа близки, но горизонт другой
    fields = ytm_check_fields(bond(moex_yield_date=MATURITY), threshold_pp=2.0)
    assert fields['ytm_check'] == 'mismatch'


def test_check_no_moex_data():
    fields = ytm_check_fields(bond(moex_yield=None, moex_yield_date=None))
    assert fields == {'ytm_to': 'offer', 'moex_yield_pct': None, 'ytm_check': 'no_moex_data'}


def test_no_ytm_no_check():
    fields = ytm_check_fields(bond(ytm_percent=None))
    assert fields['ytm_to'] is None and fields['ytm_check'] is None


def test_ytm_to_maturity_when_offer_passed():
    fields = ytm_check_fields(bond(offer_date=TODAY - timedelta(days=1), moex_yield_date=MATURITY))
    assert fields['ytm_to'] == 'maturity' and fields['ytm_check'] == 'ok'


def test_json_row_ytm_is_not_coupon_and_has_horizon():
    row = CalculateYtmUseCase._bond_to_json(bond(), mode='buy')
    assert row['ytm_pct'] == 13.71 and row['coupon_pct'] == 19.0
    assert row['ytm_to'] == 'offer' and row['offer'] == OFFER.isoformat()
    assert row['moex_yield_pct'] == 14.46 and row['ytm_check'] in ('ok', 'mismatch')


ISS_BOARD = {
    'securities': {'columns': ['SECID', 'ISIN', 'OFFERDATE'],
                   'data': [['RU000A10BP79', 'RU000A10BP79', '2026-11-23'],
                            ['RU000NOTRADE', 'RU000NOTRADE', None]]},
    'marketdata': {'columns': ['SECID', 'YIELD'],
                   'data': [['RU000A10BP79', 14.46], ['RU000NOTRADE', 0]]},
    'marketdata_yields': {'columns': ['SECID', 'YIELDDATE', 'YIELDDATETYPE'],
                          'data': [['RU000A10BP79', '2026-11-23', 'OFFER'],
                                   ['RU000NOTRADE', '0000-00-00', None]]},
}


def test_fetch_all_bonds_parses_yield_without_network():
    with patch.object(MoexApiClient, '_make_request', return_value=ISS_BOARD) as req:
        bonds = MoexApiClient().fetch_all_bonds_data()
    assert 'marketdata_yields' in req.call_args[0][1]['iss.only']
    assert _parse_moex_yield(bonds['RU000A10BP79']) == (Decimal('14.46'), date(2026, 11, 23), 'OFFER')
    assert _parse_moex_yield(bonds['RU000NOTRADE']) == (None, None, None)
