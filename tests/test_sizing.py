import unittest
from decimal import Decimal

from copytrader.models import SymbolSpec
from copytrader.sizing import buy_volume, price_deviation_pct, sell_volume


SPEC = SymbolSpec(
    symbol="AAPL",
    contract_size=Decimal("1"),
    volume_min=Decimal("0.1"),
    volume_max=Decimal("100"),
    volume_step=Decimal("0.1"),
    point=Decimal("0.01"),
    currency_profit="USD",
    trade_mode=4,
)


class SizingTests(unittest.TestCase):
    def test_equity_ratio_and_rounding(self):
        volume, residual = buy_volume(
            fill_quantity=Decimal("3"),
            fill_price=Decimal("100"),
            source_equity=Decimal("100000"),
            target_equity=Decimal("50000"),
            target_price=Decimal("100"),
            spec=SPEC,
            residual_lots=Decimal("0"),
        )
        self.assertEqual(volume, Decimal("1.5"))
        self.assertEqual(residual, Decimal("0.0"))

    def test_subminimum_accumulates(self):
        volume, residual = buy_volume(
            fill_quantity=Decimal("0.1"),
            fill_price=Decimal("100"),
            source_equity=Decimal("100000"),
            target_equity=Decimal("50000"),
            target_price=Decimal("100"),
            spec=SPEC,
            residual_lots=Decimal("0"),
        )
        self.assertEqual(volume, Decimal("0"))
        self.assertEqual(residual, Decimal("0.05"))

    def test_full_sell_closes_all(self):
        result = sell_volume(
            fill_quantity=Decimal("10"),
            managed_source_quantity=Decimal("10"),
            managed_target_volume=Decimal("4.9"),
            residual_lots=Decimal("0.05"),
            spec=SPEC,
        )
        self.assertEqual(result, (Decimal("4.9"), Decimal("0"), Decimal("0"), Decimal("0")))

    def test_sell_cannot_exceed_managed_long(self):
        close, source, target, residual = sell_volume(
            fill_quantity=Decimal("100"),
            managed_source_quantity=Decimal("10"),
            managed_target_volume=Decimal("5"),
            residual_lots=Decimal("0"),
            spec=SPEC,
        )
        self.assertEqual(close, Decimal("5"))
        self.assertEqual(source, Decimal("0"))
        self.assertEqual(target, Decimal("0"))

    def test_price_deviation(self):
        self.assertEqual(price_deviation_pct(Decimal("100"), Decimal("100.5")), Decimal("0.500"))

    def test_sizing_does_not_silently_cap_broker_maximum(self):
        volume, residual = buy_volume(
            fill_quantity=Decimal("500"),
            fill_price=Decimal("100"),
            source_equity=Decimal("100"),
            target_equity=Decimal("100"),
            target_price=Decimal("100"),
            spec=SPEC,
            residual_lots=Decimal("0"),
        )
        self.assertEqual(volume, Decimal("500"))
        self.assertEqual(residual, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
