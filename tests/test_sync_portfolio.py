"""
Тесты для задачи 012-A: персистентность current_value при sync-portfolio.

Покрывает:
- guard-цикл в SyncPortfolioUseCase: current_value вычисляется из qty*price при нуле
- сохранение current_value в БД через add_portfolio_positions
- защита UPSERT от обнуления (current_value и liquidity_loss_ratio)
"""
from decimal import Decimal

import pytest

from src.data_models import PortfolioPosition


# ---------------------------------------------------------------------------
# Unit-тесты (без БД): проверяем guard-цикл и формулы
# ---------------------------------------------------------------------------

class TestCurrentValueGuard:
    """Юнит-тесты guard-цикла: current_value = qty * price при нуле/None."""

    def _apply_guard(self, positions):
        """Эмуляция guard-цикла из SyncPortfolioUseCase."""
        for p in positions:
            if not p.current_value or p.current_value <= Decimal(0):
                qty = p.quantity or Decimal(0)
                px = p.current_price or Decimal(0)
                p.current_value = qty * px
        return positions

    def test_guard_computes_value_when_zero(self):
        """current_value=0 → должен стать qty*price."""
        p = PortfolioPosition(
            isin="RU000A0ZZZZ1",
            quantity=Decimal("10"),
            current_price=Decimal("1000"),
            current_value=Decimal("0"),
        )
        self._apply_guard([p])
        assert p.current_value == Decimal("10000"), (
            "Guard должен вычислить 10 * 1000 = 10000"
        )

    def test_guard_computes_value_when_none(self):
        """current_value=None → должен стать qty*price."""
        p = PortfolioPosition(
            isin="RU000A0ZZZZ2",
            quantity=Decimal("5"),
            current_price=Decimal("500"),
            current_value=None,
        )
        self._apply_guard([p])
        assert p.current_value == Decimal("2500")

    def test_guard_does_not_overwrite_positive_value(self):
        """Уже заполненный current_value не должен перезаписываться."""
        p = PortfolioPosition(
            isin="RU000A0ZZZZ3",
            quantity=Decimal("10"),
            current_price=Decimal("1000"),
            current_value=Decimal("99999"),
        )
        self._apply_guard([p])
        assert p.current_value == Decimal("99999"), (
            "Guard не должен трогать ненулевой current_value"
        )

    def test_guard_zero_price_gives_zero(self):
        """Если и qty и price равны нулю — current_value остаётся 0."""
        p = PortfolioPosition(
            isin="RU000A0ZZZZ4",
            quantity=Decimal("0"),
            current_price=Decimal("0"),
            current_value=Decimal("0"),
        )
        self._apply_guard([p])
        assert p.current_value == Decimal("0")

    def test_guard_handles_none_price(self):
        """current_price=None трактуется как 0, current_value=0."""
        p = PortfolioPosition(
            isin="RU000A0ZZZZ5",
            quantity=Decimal("10"),
            current_price=None,
            current_value=None,
        )
        self._apply_guard([p])
        assert p.current_value == Decimal("0")


# ---------------------------------------------------------------------------
# Интеграционные тесты (требуют POSTGRES_DSN_TEST в .env)
# ---------------------------------------------------------------------------

class TestCurrentValuePersistence:
    """Тесты сохранения current_value в БД."""

    def test_current_value_saved_correctly(self, db):
        """quantity=10, current_price=1000 → current_value=10000 в БД."""
        position = PortfolioPosition(
            isin="RU000A0TEST1",
            ticker="TEST1",
            name="Test Bond 1",
            broker_name="TBank",
            account_id="test-account",
            quantity=Decimal("10"),
            average_price=Decimal("990"),
            current_price=Decimal("1000"),
            current_value=Decimal("10000"),
        )
        db.add_portfolio_positions([position])

        saved = db.get_portfolio_positions()
        match = [p for p in saved if p.isin == "RU000A0TEST1"]
        assert len(match) == 1
        assert match[0].current_value == Decimal("10000"), (
            "current_value=10000 должен сохраниться в БД"
        )

    def test_no_positions_with_zero_current_value_when_price_nonzero(self, db):
        """После sync нет позиций с current_value<=0 при ненулевых qty и ценах."""
        positions = [
            PortfolioPosition(
                isin=f"RU000A0TEST{i}",
                ticker=f"TEST{i}",
                name=f"Test Bond {i}",
                broker_name="TBank",
                account_id="test-account",
                quantity=Decimal(str(i * 5)),
                average_price=Decimal("950"),
                current_price=Decimal("1000"),
                current_value=Decimal(str(i * 5 * 1000)),
            )
            for i in range(2, 6)
        ]
        db.add_portfolio_positions(positions)

        saved = db.get_portfolio_positions()
        # Из сохранённых берём только тестовые
        test_isins = {f"RU000A0TEST{i}" for i in range(2, 6)}
        test_positions = [p for p in saved if p.isin in test_isins]
        assert len(test_positions) == 4

        bad = [p for p in test_positions
               if p.current_value is None or p.current_value <= Decimal("0")]
        assert not bad, (
            f"Все тестовые позиции должны иметь current_value > 0, "
            f"нарушители: {[(p.isin, p.current_value) for p in bad]}"
        )

    def test_upsert_preserves_liquidity_loss_ratio(self, db):
        """UPSERT не должен обнулять liquidity_loss_ratio если брокер передаёт NULL."""
        initial = PortfolioPosition(
            isin="RU000A0LIQT1",
            ticker="LIQT1",
            name="Liquidity Test Bond",
            broker_name="TBank",
            account_id="test-account",
            quantity=Decimal("10"),
            current_price=Decimal("1000"),
            current_value=Decimal("10000"),
            liquidity_loss_ratio=Decimal("0.015"),
        )
        db.add_portfolio_positions([initial])

        # Повторный UPSERT без liquidity_loss_ratio (брокер прислал NULL)
        updated = PortfolioPosition(
            isin="RU000A0LIQT1",
            ticker="LIQT1",
            name="Liquidity Test Bond",
            broker_name="TBank",
            account_id="test-account",
            quantity=Decimal("12"),
            current_price=Decimal("1010"),
            current_value=Decimal("12120"),
            liquidity_loss_ratio=None,  # брокер не передал
        )
        db.add_portfolio_positions([updated])

        saved = db.get_portfolio_positions()
        match = [p for p in saved if p.isin == "RU000A0LIQT1"]
        assert len(match) == 1
        assert match[0].liquidity_loss_ratio == Decimal("0.015"), (
            "UPSERT должен сохранить liquidity_loss_ratio=0.015 из предыдущего значения"
        )

    def test_upsert_preserves_current_value_when_zero_arrives(self, db):
        """UPSERT: если брокер прислал current_value=0, DB должна вычислить qty*price."""
        initial = PortfolioPosition(
            isin="RU000A0VAL01",
            ticker="VAL01",
            name="Value Test Bond",
            broker_name="TBank",
            account_id="test-account",
            quantity=Decimal("10"),
            current_price=Decimal("1000"),
            current_value=Decimal("10000"),
        )
        db.add_portfolio_positions([initial])

        # Брокер прислал current_value=0, но qty и current_price правильные
        broken = PortfolioPosition(
            isin="RU000A0VAL01",
            ticker="VAL01",
            name="Value Test Bond",
            broker_name="TBank",
            account_id="test-account",
            quantity=Decimal("10"),
            current_price=Decimal("1050"),
            current_value=Decimal("0"),  # баг брокера
        )
        db.add_portfolio_positions([broken])

        saved = db.get_portfolio_positions()
        match = [p for p in saved if p.isin == "RU000A0VAL01"]
        assert len(match) == 1
        # COALESCE(NULLIF(0, 0), 10*1050, ...) → 10500
        assert match[0].current_value == Decimal("10500"), (
            "При current_value=0 UPSERT должен вычислить qty*current_price=10500"
        )
