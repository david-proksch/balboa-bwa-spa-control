from __future__ import annotations

import math

import pytest

from hottub.domain import InvalidTemperatureError, Temperature, TemperatureBand


class TestTemperature:
    def test_orders_by_value(self) -> None:
        assert Temperature(98) < Temperature(102)
        assert Temperature(102) == Temperature(102.0)

    def test_degrees_above_is_signed(self) -> None:
        assert Temperature(102).degrees_above(Temperature(98)) == 4
        assert Temperature(98).degrees_above(Temperature(102)) == -4

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_rejects_values_that_are_not_finite(self, value: float) -> None:
        with pytest.raises(InvalidTemperatureError):
            Temperature(value)

    def test_formats_without_trailing_zeros(self) -> None:
        assert str(Temperature(102.0)) == "102 °F"
        assert str(Temperature(99.5)) == "99.5 °F"


class TestTemperatureBand:
    def test_contains_includes_both_ends(self) -> None:
        band = TemperatureBand(floor=Temperature(90), ceiling=Temperature(102))

        assert band.contains(Temperature(90))
        assert band.contains(Temperature(102))
        assert not band.contains(Temperature(89))
        assert not band.contains(Temperature(103))

    @pytest.mark.parametrize(("floor", "ceiling"), [(102, 102), (103, 102)])
    def test_floor_must_be_below_ceiling(self, floor: float, ceiling: float) -> None:
        with pytest.raises(InvalidTemperatureError, match="must be below"):
            TemperatureBand(floor=Temperature(floor), ceiling=Temperature(ceiling))

    def test_accepts_the_full_range_of_the_controller(self) -> None:
        TemperatureBand(floor=Temperature(50), ceiling=Temperature(104))

    def test_floor_cannot_be_below_what_the_controller_accepts(self) -> None:
        with pytest.raises(InvalidTemperatureError, match="Floor"):
            TemperatureBand(floor=Temperature(49), ceiling=Temperature(100))

    def test_ceiling_cannot_exceed_the_safety_limit(self) -> None:
        with pytest.raises(InvalidTemperatureError, match="Ceiling"):
            TemperatureBand(floor=Temperature(90), ceiling=Temperature(105))


@pytest.mark.parametrize(
    ("fahrenheit", "whole"),
    [(99.0, 99.0), (99.4, 99.0), (99.5, 100.0), (100.5, 101.0), (101.6, 102.0)],
)
def test_rounding_to_a_whole_degree_takes_halves_upward(fahrenheit: float, whole: float) -> None:
    assert Temperature(fahrenheit).to_whole_degree() == Temperature(whole)
