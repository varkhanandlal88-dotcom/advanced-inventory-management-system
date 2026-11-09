"""SQLite persistence layer.

Stock on hand is derived from FIFO cost layers, so quantity and valuation
can never drift apart.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from .exceptions import DuplicateEntityError
from .models import CostLayer, MovementType, Product, StockMovement, Warehouse

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sku              TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,
    sale_price       TEXT NOT NULL,
    reorder_level    INTEGER NOT NULL CHECK (reorder_level >= 0),
    reorder_quantity INTEGER NOT NULL CHECK (reorder_quantity >= 0),
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS warehouses (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_layers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id         INTEGER NOT NULL REFERENCES products(id),
    warehouse_id       INTEGER NOT NULL REFERENCES warehouses(id),
    received_at        TEXT NOT NULL,
    quantity_remaining INTEGER NOT NULL CHECK (quantity_remaining >= 0),
    unit_cost          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_layers_lookup
    ON cost_layers (product_id, warehouse_id, received_at, id);

CREATE TABLE IF NOT EXISTS movements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id),
    movement_type TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    unit_cost     TEXT NOT NULL,
    reference     TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
"""


class SqliteInventoryRepository:
    """Data-access object with re-entrant transaction support."""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # explicit transaction control
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ transactions
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic unit of work. Nested calls join the outermost transaction."""
        if self._depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("COMMIT")

    # ---------------------------------------------------------------- products
    def add_product(self, product: Product) -> Product:
        try:
            cursor = self._conn.execute(
                "INSERT INTO products (sku, name, category, sale_price,"
                " reorder_level, reorder_quantity, active) VALUES (?,?,?,?,?,?,?)",
                (
                    product.sku,
                    product.name,
                    product.category,
                    str(product.sale_price),
                    product.reorder_level,
                    product.reorder_quantity,
                    int(product.active),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEntityError(f"SKU already exists: {product.sku}") from exc
        return replace(product, id=cursor.lastrowid)

    def get_product_by_sku(self, sku: str) -> Product | None:
        row = self._conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
        return self._to_product(row) if row else None

    def list_products(self, active_only: bool = True) -> list[Product]:
        query = "SELECT * FROM products"
        if active_only:
            query += " WHERE active = 1"
        rows = self._conn.execute(query + " ORDER BY sku").fetchall()
        return [self._to_product(row) for row in rows]

    # -------------------------------------------------------------- warehouses
    def add_warehouse(self, warehouse: Warehouse) -> Warehouse:
        try:
            cursor = self._conn.execute(
                "INSERT INTO warehouses (code, name) VALUES (?, ?)",
                (warehouse.code, warehouse.name),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEntityError(
                f"Warehouse already exists: {warehouse.code}"
            ) from exc
        return replace(warehouse, id=cursor.lastrowid)

    def get_warehouse_by_code(self, code: str) -> Warehouse | None:
        row = self._conn.execute(
            "SELECT * FROM warehouses WHERE code = ?", (code,)
        ).fetchone()
        return Warehouse(row["code"], row["name"], row["id"]) if row else None

    def list_warehouses(self) -> list[Warehouse]:
        rows = self._conn.execute("SELECT * FROM warehouses ORDER BY code").fetchall()
        return [Warehouse(r["code"], r["name"], r["id"]) for r in rows]

    # ------------------------------------------------------------- cost layers
    def add_cost_layer(
        self,
        product_id: int,
        warehouse_id: int,
        quantity: int,
        unit_cost: Decimal,
        received_at: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO cost_layers (product_id, warehouse_id, received_at,"
            " quantity_remaining, unit_cost) VALUES (?,?,?,?,?)",
            (product_id, warehouse_id, received_at.isoformat(), quantity, str(unit_cost)),
        )

    def open_layers(self, product_id: int, warehouse_id: int) -> list[CostLayer]:
        """Layers with remaining stock, oldest first (FIFO order)."""
        rows = self._conn.execute(
            "SELECT * FROM cost_layers WHERE product_id = ? AND warehouse_id = ?"
            " AND quantity_remaining > 0 ORDER BY received_at, id",
            (product_id, warehouse_id),
        ).fetchall()
        return [self._to_layer(row) for row in rows]

    def all_open_layers(self) -> list[CostLayer]:
        rows = self._conn.execute(
            "SELECT * FROM cost_layers WHERE quantity_remaining > 0"
        ).fetchall()
        return [self._to_layer(row) for row in rows]

    def set_layer_quantity(self, layer_id: int, quantity: int) -> None:
        self._conn.execute(
            "UPDATE cost_layers SET quantity_remaining = ? WHERE id = ?",
            (quantity, layer_id),
        )

    def stock_on_hand(self, product_id: int, warehouse_id: int) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(quantity_remaining), 0) AS qty FROM cost_layers"
            " WHERE product_id = ? AND warehouse_id = ?",
            (product_id, warehouse_id),
        ).fetchone()
        return int(row["qty"])

    def total_stock_by_product(self) -> dict[int, int]:
        rows = self._conn.execute(
            "SELECT product_id, SUM(quantity_remaining) AS qty"
            " FROM cost_layers GROUP BY product_id"
        ).fetchall()
        return {int(r["product_id"]): int(r["qty"]) for r in rows}

    # --------------------------------------------------------------- movements
    def record_movement(self, movement: StockMovement) -> None:
        self._conn.execute(
            "INSERT INTO movements (product_id, warehouse_id, movement_type,"
            " quantity, unit_cost, reference, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                movement.product_id,
                movement.warehouse_id,
                movement.movement_type.value,
                movement.quantity,
                str(movement.unit_cost),
                movement.reference,
                movement.note,
                movement.created_at.isoformat(),
            ),
        )

    def list_movements(
        self, product_id: int | None = None, limit: int = 50
    ) -> list[StockMovement]:
        query = (
            "SELECT m.*, p.sku AS sku, w.code AS warehouse_code FROM movements m"
            " JOIN products p ON p.id = m.product_id"
            " JOIN warehouses w ON w.id = m.warehouse_id"
        )
        params: list[object] = []
        if product_id is not None:
            query += " WHERE m.product_id = ?"
            params.append(product_id)
        query += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [
            StockMovement(
                id=r["id"],
                product_id=r["product_id"],
                warehouse_id=r["warehouse_id"],
                movement_type=MovementType(r["movement_type"]),
                quantity=r["quantity"],
                unit_cost=Decimal(r["unit_cost"]),
                reference=r["reference"],
                note=r["note"],
                created_at=datetime.fromisoformat(r["created_at"]),
                sku=r["sku"],
                warehouse_code=r["warehouse_code"],
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- mappers
    @staticmethod
    def _to_product(row: sqlite3.Row) -> Product:
        return Product(
            id=row["id"],
            sku=row["sku"],
            name=row["name"],
            category=row["category"],
            sale_price=Decimal(row["sale_price"]),
            reorder_level=row["reorder_level"],
            reorder_quantity=row["reorder_quantity"],
            active=bool(row["active"]),
        )

    @staticmethod
    def _to_layer(row: sqlite3.Row) -> CostLayer:
        return CostLayer(
            id=row["id"],
            product_id=row["product_id"],
            warehouse_id=row["warehouse_id"],
            received_at=datetime.fromisoformat(row["received_at"]),
            quantity_remaining=row["quantity_remaining"],
            unit_cost=Decimal(row["unit_cost"]),
        )
