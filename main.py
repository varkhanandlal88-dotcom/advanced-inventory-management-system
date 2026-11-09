"""Demo of the inventory system."""

from inventory import InsufficientStockError, InventoryService, SqliteInventoryRepository
from inventory.reporting import render_table


def main() -> None:
    service = InventoryService(SqliteInventoryRepository())

    service.register_warehouse("MAIN", "Main Warehouse")
    service.register_warehouse("EAST", "East Distribution Center")
    service.register_product("LAP-001", "Business Laptop", "Computers", "1200.00", 5, 20)
    service.register_product("MOU-010", "Wireless Mouse", "Accessories", "25.00", 30, 100)

    service.receive_stock("LAP-001", "MAIN", 10, "900.00", reference="PO-1001")
    service.receive_stock("LAP-001", "MAIN", 10, "950.00", reference="PO-1002")
    service.receive_stock("MOU-010", "MAIN", 40, "11.50", reference="PO-1003")

    cogs = service.issue_stock("LAP-001", "MAIN", 15, reference="SO-2001")
    print(f"COGS for SO-2001 (FIFO): {cogs}\n")

    service.transfer_stock("LAP-001", "MAIN", "EAST", 3, reference="TR-3001")
    service.adjust_stock("MOU-010", "MAIN", -12, reason="Damaged in storage")

    try:
        service.issue_stock("LAP-001", "EAST", 99)
    except InsufficientStockError as error:
        print(f"Rejected: {error}\n")

    print("STOCK LEVELS")
    print(render_table(
        ["SKU", "Product", "Warehouse", "Qty", "Value"],
        [(l.sku, l.name, l.warehouse_code, l.quantity, l.value) for l in service.stock_levels()],
    ))
    print(f"\nTotal inventory value: {service.inventory_value()}\n")

    print("LOW STOCK ALERTS")
    print(render_table(
        ["SKU", "Product", "On hand", "Reorder level", "Suggested order"],
        [(a.sku, a.name, a.on_hand, a.reorder_level, a.suggested_order_quantity)
         for a in service.low_stock_alerts()],
    ))

    print("\nRECENT MOVEMENTS")
    print(render_table(
        ["ID", "SKU", "Warehouse", "Type", "Qty", "Unit cost", "Ref/Note"],
        [(m.id, m.sku, m.warehouse_code, m.movement_type.value, m.quantity,
          m.unit_cost, m.reference or m.note) for m in service.movement_history(limit=10)],
    ))


if __name__ == "__main__":
    main()
