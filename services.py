"""Business logic: stock operations, FIFO costing, and reporting."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .exceptions import (
    EntityNotFoundError,
    InsufficientStockError,
    ValidationError,
)
from .models import (
    LowStockAlert,
    MovementType,
    Product,
    StockLevel,
    StockMovement,
    Warehouse,
)
from .repository import SqliteInventoryRepository

_CENTS = Decimal("0.01")
_COST_PRECISION = Decimal("0.0001")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"{field_name} is not a valid number: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValidationError(f"{field_name} must be a non-negative number.")
    return result


def _require_positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field_name} must be a positive integer.")
    return value


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _ConsumedSlice:
    """Units drawn from one cost layer."""

    quantity: int
    unit_cost: Decimal
    received_at: datetime

    @property
    def total_cost(self) -> Decimal:
        return self.quantity * self.unit_cost


class InventoryService:
    """Application service. Every public mutation is atomic."""

    def __init__(
        self,
        repository: SqliteInventoryRepository,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repository
        self._clock = clock

    # ------------------------------------------------------------ master data
    def register_product(
        self,
        sku: str,
        name: str,
        category: str,
        sale_price: Decimal | int | str,
        reorder_level: int = 0,
        reorder_quantity: int = 0,
    ) -> Product:
        product = Product(
            sku=sku.strip().upper(),
            name=name.strip(),
            category=category.strip(),
            sale_price=_to_decimal(sale_price, "sale_price"),
            reorder_level=reorder_level,
            reorder_quantity=reorder_quantity,
        )
        return self._repo.add_product(product)

    def register_warehouse(self, code: str, name: str) -> Warehouse:
        return self._repo.add_warehouse(Warehouse(code.strip().upper(), name.strip()))

    # ------------------------------------------------------------- operations
    def receive_stock(
        self,
        sku: str,
        warehouse_code: str,
        quantity: int,
        unit_cost: Decimal | int | str,
        reference: str = "",
    ) -> None:
        """Book goods in, creating a new FIFO cost layer."""
        _require_positive_int(quantity, "quantity")
        cost = _to_decimal(unit_cost, "unit_cost")
        product, warehouse = self._resolve(sku, warehouse_code)
        now = self._clock()

        with self._repo.transaction():
            self._repo.add_cost_layer(product.id, warehouse.id, quantity, cost, now)
            self._record(
                product, warehouse, MovementType.RECEIPT, quantity, cost, now, reference
            )

    def issue_stock(
        self, sku: str, warehouse_code: str, quantity: int, reference: str = ""
    ) -> Decimal:
        """Book goods out (oldest first). Returns the cost of goods issued."""
        _require_positive_int(quantity, "quantity")
        product, warehouse = self._resolve(sku, warehouse_code)
        now = self._clock()

        with self._repo.transaction():
            slices = self._consume(product, warehouse, quantity)
            total_cost = sum((s.total_cost for s in slices), Decimal(0))
            average = (total_cost / quantity).quantize(_COST_PRECISION)
            self._record(
                product, warehouse, MovementType.ISSUE, -quantity, average, now, reference
            )
        return _money(total_cost)

    def transfer_stock(
        self,
        sku: str,
        from_warehouse_code: str,
        to_warehouse_code: str,
        quantity: int,
        reference: str = "",
    ) -> None:
        """Move stock between warehouses, preserving cost layers and age."""
        _require_positive_int(quantity, "quantity")
        if from_warehouse_code.strip().upper() == to_warehouse_code.strip().upper():
            raise ValidationError("Source and destination warehouses must differ.")
        product, source = self._resolve(sku, from_warehouse_code)
        _, target = self._resolve(sku, to_warehouse_code)
        now = self._clock()

        with self._repo.transaction():
            slices = self._consume(product, source, quantity)
            for piece in slices:
                self._repo.add_cost_layer(
                    product.id, target.id, piece.quantity, piece.unit_cost, piece.received_at
                )
            total_cost = sum((s.total_cost for s in slices), Decimal(0))
            average = (total_cost / quantity).quantize(_COST_PRECISION)
            self._record(
                product, source, MovementType.TRANSFER_OUT, -quantity, average, now, reference
            )
            self._record(
                product, target, MovementType.TRANSFER_IN, quantity, average, now, reference
            )

    def adjust_stock(
        self,
        sku: str,
        warehouse_code: str,
        delta: int,
        reason: str,
        unit_cost: Decimal | int | str | None = None,
    ) -> None:
        """Correct stock after a count (positive = found, negative = lost)."""
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise ValidationError("delta must be a non-zero integer.")
        if not reason.strip():
            raise ValidationError("An adjustment requires a reason.")
        product, warehouse = self._resolve(sku, warehouse_code)
        now = self._clock()

        with self._repo.transaction():
            if delta < 0:
                slices = self._consume(product, warehouse, -delta)
                total = sum((s.total_cost for s in slices), Decimal(0))
                cost = (total / -delta).quantize(_COST_PRECISION)
            else:
                cost = (
                    _to_decimal(unit_cost, "unit_cost")
                    if unit_cost is not None
                    else self._average_cost(product)
                )
                self._repo.add_cost_layer(product.id, warehouse.id, delta, cost, now)
            self._record(
                product, warehouse, MovementType.ADJUSTMENT, delta, cost, now, note=reason
            )

    # --------------------------------------------------------------- reporting
    def stock_on_hand(self, sku: str, warehouse_code: str) -> int:
        product, warehouse = self._resolve(sku, warehouse_code)
        return self._repo.stock_on_hand(product.id, warehouse.id)

    def stock_levels(self) -> list[StockLevel]:
        """Quantity and FIFO value per product and warehouse."""
        products = {p.id: p for p in self._repo.list_products(active_only=False)}
        warehouses = {w.id: w for w in self._repo.list_warehouses()}
        quantities: dict[tuple[int, int], int] = defaultdict(int)
        values: dict[tuple[int, int], Decimal] = defaultdict(Decimal)

        for layer in self._repo.all_open_layers():
            key = (layer.product_id, layer.warehouse_id)
            quantities[key] += layer.quantity_remaining
            values[key] += layer.quantity_remaining * layer.unit_cost

        levels = [
            StockLevel(
                sku=products[pid].sku,
                name=products[pid].name,
                warehouse_code=warehouses[wid].code,
                quantity=qty,
                value=_money(values[(pid, wid)]),
            )
            for (pid, wid), qty in quantities.items()
        ]
        return sorted(levels, key=lambda level: (level.sku, level.warehouse_code))

    def inventory_value(self) -> Decimal:
        return _money(sum((level.value for level in self.stock_levels()), Decimal(0)))

    def low_stock_alerts(self) -> list[LowStockAlert]:
        """Active products whose total stock is at or below the reorder level."""
        totals = self._repo.total_stock_by_product()
        alerts = []
        for product in self._repo.list_products():
            on_hand = totals.get(product.id, 0)
            if product.reorder_level and on_hand <= product.reorder_level:
                suggested = product.reorder_quantity or (product.reorder_level * 2 - on_hand)
                alerts.append(
                    LowStockAlert(
                        product.sku, product.name, on_hand, product.reorder_level, suggested
                    )
                )
        return alerts

    def movement_history(self, sku: str | None = None, limit: int = 50) -> list[StockMovement]:
        product_id = self._get_product(sku).id if sku else None
        return self._repo.list_movements(product_id, limit)

    # ----------------------------------------------------------------- helpers
    def _consume(
        self, product: Product, warehouse: Warehouse, quantity: int
    ) -> list[_ConsumedSlice]:
        available = self._repo.stock_on_hand(product.id, warehouse.id)
        if available < quantity:
            raise InsufficientStockError(
                f"{product.sku} @ {warehouse.code}: requested {quantity}, available {available}."
            )
        remaining = quantity
        slices: list[_ConsumedSlice] = []
        for layer in self._repo.open_layers(product.id, warehouse.id):
            if remaining == 0:
                break
            taken = min(layer.quantity_remaining, remaining)
            self._repo.set_layer_quantity(layer.id, layer.quantity_remaining - taken)
            slices.append(_ConsumedSlice(taken, layer.unit_cost, layer.received_at))
            remaining -= taken
        return slices

    def _average_cost(self, product: Product) -> Decimal:
        quantity, value = 0, Decimal(0)
        for layer in self._repo.all_open_layers():
            if layer.product_id == product.id:
                quantity += layer.quantity_remaining
                value += layer.quantity_remaining * layer.unit_cost
        if quantity == 0:
            raise ValidationError(
                "No existing stock to derive a cost from; supply unit_cost explicitly."
            )
        return (value / quantity).quantize(_COST_PRECISION)

    def _record(
        self,
        product: Product,
        warehouse: Warehouse,
        movement_type: MovementType,
        quantity: int,
        unit_cost: Decimal,
        created_at: datetime,
        reference: str = "",
        note: str = "",
    ) -> None:
        self._repo.record_movement(
            StockMovement(
                product_id=product.id,
                warehouse_id=warehouse.id,
                movement_type=movement_type,
                quantity=quantity,
                unit_cost=unit_cost,
                created_at=created_at,
                reference=reference,
                note=note,
            )
        )

    def _get_product(self, sku: str) -> Product:
        product = self._repo.get_product_by_sku(sku.strip().upper())
        if product is None:
            raise EntityNotFoundError(f"Unknown SKU: {sku}")
        return product

    def _resolve(self, sku: str, warehouse_code: str) -> tuple[Product, Warehouse]:
        product = self._get_product(sku)
        warehouse = self._repo.get_warehouse_by_code(warehouse_code.strip().upper())
        if warehouse is None:
            raise EntityNotFoundError(f"Unknown warehouse: {warehouse_code}")
        return product, warehouse
