# Advanced Inventory Management System

Python 3.10+, standard library only.

## Features
- Multi-warehouse stock tracking
- FIFO cost layers (accurate COGS and valuation)
- Receipts, issues, transfers (cost-preserving) and adjustments
- Immutable audit trail of every movement
- Atomic operations (full rollback on failure)
- Low-stock alerts with suggested order quantities

## Run
    python main.py
    python -m unittest discover -v

## Layout
    inventory/models.py      domain entities
    inventory/repository.py  SQLite persistence + transactions
    inventory/services.py    business rules
    inventory/reporting.py   text tables
    tests/                   unit tests
