"""Domain models. Pure data with light validation; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .exceptions import ValidationError


class MovementType(str, Enum):
    RECEIPT = "RECEIPT"
    ISSUE = "ISSUE"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    ADJUSTMENT = "ADJUSTMENT"


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    sale_price: Decimal
    reorder_level: int = 0
    reorder_quantity: int = 0
    active: bool = True
    id: int | None = None

    def __post_init__(self) -> None:
        if not self.sku:
            raise ValidationError("SKU must not be empty.")
        if not self.name:
            raise ValidationError("Product name must not be empty.")
        if self.sale_price < 0:
            raise ValidationError("Sale price cannot be negative.")
        if self.reorder_level < 0 or self.reorder_quantity < 0:
            raise ValidationError("Reorder settings cannot be negative.")


@dataclass(frozen=True)
class Warehouse:
    code: str
    name: str
    id: int | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.name:
            raise ValidationError("Warehouse code and name must not be empty.")


@dataclass(frozen=True)
class CostLayer:
    """A FIFO cost layer: a batch of units received at one unit cost."""

    id: int
    product_id: int
    warehouse_id: int
    received_at: datetime
    quantity_remaining: int
    unit_cost: Decimal


@dataclass(frozen=True)
class StockMovement:
    """Immutable audit-trail entry. Quantity is signed (+in / -out)."""

    product_id: int
    warehouse_id: int
    movement_type: MovementType
    quantity: int
    unit_cost: Decimal
    created_at: datetime
    reference: str = ""
    note: str = ""
    id: int | None = None
    sku: str = ""
    warehouse_code: str = ""


@dataclass(frozen=True)
class StockLevel:
    sku: str
    name: str
    warehouse_code: str
    quantity: int
    value: Decimal


@dataclass(frozen=True)
class LowStockAlert:
    sku: str
    name: str
    on_hand: int
    reorder_level: int
    suggested_order_quantity: int
