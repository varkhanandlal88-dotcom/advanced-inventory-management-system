import unittest
from decimal import Decimal
from unittest.mock import patch

from inventory import (
    DuplicateEntityError,
    EntityNotFoundError,
    InsufficientStockError,
    InventoryService,
    SqliteInventoryRepository,
    ValidationError,
)


class InventoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SqliteInventoryRepository()
        self.service = InventoryService(self.repo)
        self.service.register_warehouse("MAIN", "Main")
        self.service.register_warehouse("EAST", "East")
        self.service.register_product("A1", "Widget", "Parts", "10.00", 5, 50)

    def tearDown(self) -> None:
        self.repo.close()

    def test_fifo_costing(self) -> None:
        self.service.receive_stock("A1", "MAIN", 10, "2.00")
        self.service.receive_stock("A1", "MAIN", 10, "3.00")
        cogs = self.service.issue_stock("A1", "MAIN", 15)
        self.assertEqual(cogs, Decimal("35.00"))  # 10*2 + 5*3
        self.assertEqual(self.service.stock_on_hand("A1", "MAIN"), 5)
        self.assertEqual(self.service.inventory_value(), Decimal("15.00"))

    def test_insufficient_stock_is_rejected(self) -> None:
        self.service.receive_stock("A1", "MAIN", 3, "2.00")
        with self.assertRaises(InsufficientStockError):
            self.service.issue_stock("A1", "MAIN", 4)
        self.assertEqual(self.service.stock_on_hand("A1", "MAIN"), 3)

    def test_transfer_preserves_cost(self) -> None:
        self.service.receive_stock("A1", "MAIN", 5, "2.00")
        self.service.receive_stock("A1", "MAIN", 5, "4.00")
        self.service.transfer_stock("A1", "MAIN", "EAST", 7)
        self.assertEqual(self.service.stock_on_hand("A1", "MAIN"), 3)
        self.assertEqual(self.service.stock_on_hand("A1", "EAST"), 7)
        east = next(l for l in self.service.stock_levels() if l.warehouse_code == "EAST")
        self.assertEqual(east.value, Decimal("18.00"))  # 5*2 + 2*4

    def test_transfer_to_same_warehouse_fails(self) -> None:
        self.service.receive_stock("A1", "MAIN", 5, "2.00")
        with self.assertRaises(ValidationError):
            self.service.transfer_stock("A1", "MAIN", "main", 1)

    def test_adjustments(self) -> None:
        self.service.receive_stock("A1", "MAIN", 10, "2.00")
        self.service.adjust_stock("A1", "MAIN", -4, reason="Damaged")
        self.service.adjust_stock("A1", "MAIN", 2, reason="Recount")
        self.assertEqual(self.service.stock_on_hand("A1", "MAIN"), 8)
        with self.assertRaises(ValidationError):
            self.service.adjust_stock("A1", "MAIN", 1, reason=" ")

    def test_failed_operation_rolls_back(self) -> None:
        self.service.receive_stock("A1", "MAIN", 10, "2.00")
        with patch.object(self.repo, "record_movement", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.service.issue_stock("A1", "MAIN", 6)
        self.assertEqual(self.service.stock_on_hand("A1", "MAIN"), 10)

    def test_low_stock_alert(self) -> None:
        self.service.receive_stock("A1", "MAIN", 4, "2.00")
        alerts = self.service.low_stock_alerts()
        self.assertEqual([a.sku for a in alerts], ["A1"])
        self.assertEqual(alerts[0].suggested_order_quantity, 50)

    def test_duplicate_and_unknown_entities(self) -> None:
        with self.assertRaises(DuplicateEntityError):
            self.service.register_product("a1", "Dup", "Parts", "1")
        with self.assertRaises(EntityNotFoundError):
            self.service.receive_stock("ZZZ", "MAIN", 1, "1")

    def test_invalid_input(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.receive_stock("A1", "MAIN", 0, "1")
        with self.assertRaises(ValidationError):
            self.service.receive_stock("A1", "MAIN", 1, "abc")

    def test_audit_trail(self) -> None:
        self.service.receive_stock("A1", "MAIN", 5, "2.00", reference="PO-1")
        self.service.issue_stock("A1", "MAIN", 2, reference="SO-1")
        history = self.service.movement_history("A1")
        self.assertEqual([m.quantity for m in history], [-2, 5])


if __name__ == "__main__":
    unittest.main()
