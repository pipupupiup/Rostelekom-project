"""Абонентское оборудование (АО) — «рюкзак монтажника».

Правило H7: старт — `staff.equipment_start`; визит списывает `order.required_equipment` в момент
`start`; остаток не может стать отрицательным; `RESTOCK` доводит остаток до `equipment_capacity`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EquipmentInventory:
    ont: int = 0
    router: int = 0
    stb: int = 0

    @classmethod
    def from_api(cls, data: Mapping[str, int]) -> EquipmentInventory:
        """Из объекта `Equipment` спецификации (`{"ONT":…, "ROUTER":…, "STB":…}`)."""
        return cls(ont=int(data["ONT"]), router=int(data["ROUTER"]), stb=int(data["STB"]))

    def to_api(self) -> dict[str, int]:
        return {"ONT": self.ont, "ROUTER": self.router, "STB": self.stb}

    def __add__(self, other: EquipmentInventory) -> EquipmentInventory:
        return EquipmentInventory(self.ont + other.ont, self.router + other.router, self.stb + other.stb)

    def __sub__(self, other: EquipmentInventory) -> EquipmentInventory:
        return EquipmentInventory(self.ont - other.ont, self.router - other.router, self.stb - other.stb)

    def covers(self, need: EquipmentInventory) -> bool:
        """Хватает ли остатка на `need`."""
        return self.ont >= need.ont and self.router >= need.router and self.stb >= need.stb

    def is_non_negative(self) -> bool:
        return self.ont >= 0 and self.router >= 0 and self.stb >= 0

    def is_empty(self) -> bool:
        return self.ont == 0 and self.router == 0 and self.stb == 0


NO_EQUIPMENT = EquipmentInventory()
