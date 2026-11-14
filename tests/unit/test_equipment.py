from __future__ import annotations

from ado.domain.equipment import NO_EQUIPMENT, EquipmentInventory


def test_api_roundtrip() -> None:
    inv = EquipmentInventory.from_api({"ONT": 1, "ROUTER": 2, "STB": 0})
    assert inv == EquipmentInventory(1, 2, 0)
    assert inv.to_api() == {"ONT": 1, "ROUTER": 2, "STB": 0}


def test_consumption_and_negative_balance() -> None:
    backpack = EquipmentInventory(1, 1, 0)
    need = EquipmentInventory(1, 1, 1)
    assert not backpack.covers(need)
    assert not (backpack - need).is_non_negative()
    assert backpack.covers(EquipmentInventory(1, 0, 0))
    assert (backpack - EquipmentInventory(1, 1, 0)).is_empty()


def test_zero_need_is_always_covered() -> None:
    assert NO_EQUIPMENT.covers(NO_EQUIPMENT)
    assert EquipmentInventory(0, 0, 0).covers(NO_EQUIPMENT)


def test_add() -> None:
    assert EquipmentInventory(1, 2, 3) + EquipmentInventory(1, 1, 1) == EquipmentInventory(2, 3, 4)
