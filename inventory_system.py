"""
Advanced Inventory Management System
====================================
A single-file desktop application built with Python, CustomTkinter, SQLite
and Matplotlib.

Setup (run once, in the VS Code terminal):
    pip install customtkinter matplotlib

Run:
    python inventory_system.py

First login:   username  admin   /   password  Admin@123
(Change the password in "Backup & Settings" after your first login.)

File layout (top to bottom):
    1. Configuration, logging, exceptions, validation helpers
    2. Models            : Product, Category, Supplier
    3. DatabaseManager   : SQLite connection, schema, transactions, backup
    4. Managers          : Authentication, Category, Supplier, Product,
                           Inventory, Purchase, Sales, Report
    5. GUI building blocks and pages
    6. MainApplication and the program entry point
"""

from __future__ import annotations

import csv
import functools
import hashlib
import hmac
import logging
import math
import re
import secrets
import shutil
import sqlite3
import tkinter as tk
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterator, Sequence

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# =============================================================================
# 1. CONFIGURATION, LOGGING, EXCEPTIONS, VALIDATION
# =============================================================================
APP_NAME = "Advanced Inventory Management System"
APP_VERSION = "1.0.0"
CURRENCY = "$"  # change to your currency symbol if you like

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "inventory.db"
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "Admin@123"

log = logging.getLogger("inventory")


def setup_logging() -> None:
    """Write logs to logs/inventory.log (rotating) and to the console."""
    LOG_DIR.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    file_handler = RotatingFileHandler(
        LOG_DIR / "inventory.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.addHandler(file_handler)
    log.addHandler(console)


class InventoryError(Exception):
    """Base class for errors that are safe to show to the user."""


class ValidationError(InventoryError):
    """Input is missing or malformed."""


class DuplicateError(InventoryError):
    """A unique value already exists."""


class NotFoundError(InventoryError):
    """The requested record does not exist."""


class InUseError(InventoryError):
    """The record is referenced by other records and cannot be removed."""


class InsufficientStockError(InventoryError):
    """Not enough stock to complete the operation."""


class AuthenticationError(InventoryError):
    """Login failed or the password is not acceptable."""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_money(value: float) -> str:
    return f"{CURRENCY}{value:,.2f}"


def short_id(product_id: str) -> str:
    return product_id[:8].upper()


class Validate:
    """Reusable input validators. Each returns a clean value or raises."""

    EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    PHONE_RE = re.compile(r"^[0-9+\-()\s]{7,20}$")

    @staticmethod
    def text(value: str | None, field_name: str, required: bool = True,
             max_len: int = 100) -> str:
        text = (value or "").strip()
        if required and not text:
            raise ValidationError(f"{field_name} is required.")
        if len(text) > max_len:
            raise ValidationError(f"{field_name} must be at most {max_len} characters.")
        return text

    @staticmethod
    def integer(value: object, field_name: str, minimum: int = 0) -> int:
        try:
            number = int(str(value).strip())
        except ValueError:
            raise ValidationError(f"{field_name} must be a whole number.") from None
        if number < minimum:
            raise ValidationError(f"{field_name} must be at least {minimum}.")
        return number

    @staticmethod
    def money(value: object, field_name: str) -> float:
        try:
            number = float(str(value).strip().replace(",", ""))
        except ValueError:
            raise ValidationError(f"{field_name} must be a valid amount.") from None
        if not math.isfinite(number) or number < 0:
            raise ValidationError(f"{field_name} must be a non-negative amount.")
        return round(number, 2)

    @staticmethod
    def date(value: str | None, field_name: str) -> str | None:
        text = (value or "").strip()
        if not text:
            return None
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise ValidationError(f"{field_name} must use the format YYYY-MM-DD.") from None
        return text

    @classmethod
    def email(cls, value: str | None) -> str:
        text = Validate.text(value, "Email", required=False, max_len=120)
        if text and not cls.EMAIL_RE.match(text):
            raise ValidationError("Email address is not valid.")
        return text

    @classmethod
    def phone(cls, value: str | None) -> str:
        text = Validate.text(value, "Phone", required=False, max_len=20)
        if text and not cls.PHONE_RE.match(text):
            raise ValidationError("Phone may contain digits, spaces, + - ( ) only (7-20 chars).")
        return text


def translate_integrity_error(exc: sqlite3.IntegrityError, duplicate: str,
                              in_use: str) -> InventoryError:
    """Turn a raw SQLite constraint failure into a friendly error."""
    if "UNIQUE" in str(exc).upper():
        return DuplicateError(duplicate)
    return InUseError(in_use)


# =============================================================================
# 2. MODELS
# =============================================================================
@dataclass
class Category:
    name: str
    description: str = ""

    @classmethod
    def from_form(cls, name: str, description: str) -> "Category":
        return cls(Validate.text(name, "Category name", max_len=60),
                   Validate.text(description, "Description", required=False, max_len=200))


@dataclass
class Supplier:
    name: str
    contact_person: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""

    @classmethod
    def from_form(cls, name: str, contact: str, phone: str, email: str,
                  address: str) -> "Supplier":
        return cls(
            Validate.text(name, "Supplier name", max_len=80),
            Validate.text(contact, "Contact person", required=False, max_len=80),
            Validate.phone(phone),
            Validate.email(email),
            Validate.text(address, "Address", required=False, max_len=200),
        )


@dataclass
class Product:
    name: str
    category_id: int
    supplier_id: int | None
    cost_price: float
    sale_price: float
    quantity: int = 0
    reorder_level: int = 10
    id: str = field(default_factory=lambda: str(uuid.uuid4()))  # unique UUID

    @classmethod
    def from_form(cls, name: str, category_id: int | None, supplier_id: int | None,
                  cost: object, price: object, quantity: object,
                  reorder: object) -> "Product":
        if category_id is None:
            raise ValidationError("Please select a category.")
        return cls(
            name=Validate.text(name, "Product name", max_len=100),
            category_id=category_id,
            supplier_id=supplier_id,
            cost_price=Validate.money(cost, "Cost price"),
            sale_price=Validate.money(price, "Sale price"),
            quantity=Validate.integer(quantity, "Quantity", 0),
            reorder_level=Validate.integer(reorder, "Reorder level", 0),
        )


@dataclass
class LineItem:
    """One row of a purchase or sale."""
    product_id: str
    quantity: int
    price: float


@dataclass
class SaleResult:
    sale_id: int
    total: float
    low_stock: list[str]


# =============================================================================
# 3. DATABASE MANAGER
# =============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS suppliers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    contact_person TEXT NOT NULL DEFAULT '',
    phone          TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL DEFAULT '',
    address        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS products (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    supplier_id   INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    cost_price    REAL NOT NULL CHECK (cost_price >= 0),
    sale_price    REAL NOT NULL CHECK (sale_price >= 0),
    quantity      INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    reorder_level INTEGER NOT NULL DEFAULT 10 CHECK (reorder_level >= 0),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_name_category
    ON products (name COLLATE NOCASE, category_id);
CREATE TABLE IF NOT EXISTS purchases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id   INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    purchase_date TEXT NOT NULL,
    total         REAL NOT NULL CHECK (total >= 0),
    notes         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS purchase_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    product_id  TEXT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    unit_cost   REAL NOT NULL CHECK (unit_cost >= 0)
);
CREATE TABLE IF NOT EXISTS sales (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL DEFAULT '',
    sale_date     TEXT NOT NULL,
    total         REAL NOT NULL CHECK (total >= 0),
    notes         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sale_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id    INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0)
);
CREATE TABLE IF NOT EXISTS stock_movements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    movement_type   TEXT NOT NULL
        CHECK (movement_type IN ('PURCHASE','SALE','STOCK_IN','STOCK_OUT')),
    quantity_change INTEGER NOT NULL CHECK (quantity_change <> 0),
    quantity_after  INTEGER NOT NULL CHECK (quantity_after >= 0),
    reference       TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_product ON stock_movements (product_id);
CREATE INDEX IF NOT EXISTS idx_movements_date    ON stock_movements (created_at);
"""

REQUIRED_TABLES = {
    "users", "products", "categories", "suppliers", "purchases",
    "purchase_items", "sales", "sale_items", "stock_movements",
}


class DatabaseManager:
    """Owns the SQLite connection and guarantees transaction safety."""

    def __init__(self, path: str | Path = DB_PATH) -> None:
        self.path = str(path)
        self._depth = 0
        self.conn = self._connect()
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # we control BEGIN/COMMIT ourselves
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _create_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO categories (name, description) "
                          "VALUES ('General', 'Default category')")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing block. Nested use joins the outer transaction."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # ---- backup & restore --------------------------------------------------
    def backup(self, destination: str | Path | None = None) -> Path:
        """Create a consistent copy of the database using SQLite's backup API."""
        if self._depth:
            raise InventoryError("Cannot back up while a transaction is running.")
        if destination is None:
            BACKUP_DIR.mkdir(exist_ok=True)
            destination = BACKUP_DIR / f"inventory_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
        destination = Path(destination)
        with closing(sqlite3.connect(destination)) as target:
            self.conn.backup(target)
        log.info("Backup created: %s", destination)
        return destination

    def restore(self, source: str | Path) -> None:
        """Replace current data with a backup file (a safety copy is made first)."""
        source = Path(source)
        if not source.is_file():
            raise NotFoundError("Backup file was not found.")
        try:
            with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as src:
                found = {r[0] for r in src.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES <= found:
                    raise ValidationError("This file is not a valid inventory backup.")
                BACKUP_DIR.mkdir(exist_ok=True)
                safety = self.backup(BACKUP_DIR / f"pre_restore_{datetime.now():%Y%m%d_%H%M%S}.db")
                src.backup(self.conn)
        except sqlite3.DatabaseError as exc:
            raise ValidationError("This file is not a valid SQLite database.") from exc
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        log.info("Database restored from %s (safety copy: %s)", source, safety)


# =============================================================================
# 4. MANAGERS (business logic)
# =============================================================================
class AuthenticationManager:
    """Admin login with salted PBKDF2 password hashing."""

    ITERATIONS = 200_000
    MIN_PASSWORD_LENGTH = 8

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self.current_user: sqlite3.Row | None = None
        if self.db.query_one("SELECT 1 FROM users") is None:
            self.create_user(DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
            log.info("Default admin account created.")

    def _hash(self, password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                   self.ITERATIONS).hex()

    def _check_policy(self, password: str) -> None:
        if len(password) < self.MIN_PASSWORD_LENGTH:
            raise AuthenticationError(
                f"Password must be at least {self.MIN_PASSWORD_LENGTH} characters long.")

    def create_user(self, username: str, password: str, role: str = "admin") -> None:
        username = Validate.text(username, "Username", max_len=40)
        self._check_policy(password)
        salt = secrets.token_bytes(16)
        try:
            self.db.execute(
                "INSERT INTO users (username, password_hash, salt, role, created_at) "
                "VALUES (?,?,?,?,?)",
                (username, self._hash(password, salt), salt.hex(), role, now_str()))
        except sqlite3.IntegrityError as exc:
            raise DuplicateError("That username already exists.") from exc

    def login(self, username: str, password: str) -> sqlite3.Row:
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if row is None:
            self._hash(password, b"\x00" * 16)  # keep timing similar
            log.warning("Failed login for unknown user '%s'", username)
            raise AuthenticationError("Invalid username or password.")
        expected = row["password_hash"]
        actual = self._hash(password, bytes.fromhex(row["salt"]))
        if not hmac.compare_digest(expected, actual):
            log.warning("Failed login for user '%s'", username)
            raise AuthenticationError("Invalid username or password.")
        self.current_user = row
        log.info("User '%s' logged in.", row["username"])
        return row

    def logout(self) -> None:
        if self.current_user:
            log.info("User '%s' logged out.", self.current_user["username"])
        self.current_user = None

    def change_password(self, old: str, new: str, confirm: str) -> None:
        if self.current_user is None:
            raise AuthenticationError("You are not logged in.")
        if new != confirm:
            raise AuthenticationError("New password and confirmation do not match.")
        self._check_policy(new)
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (self.current_user["id"],))
        if not hmac.compare_digest(row["password_hash"],
                                   self._hash(old, bytes.fromhex(row["salt"]))):
            raise AuthenticationError("Current password is incorrect.")
        salt = secrets.token_bytes(16)
        self.db.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
                        (self._hash(new, salt), salt.hex(), row["id"]))
        log.info("Password changed for '%s'.", row["username"])


class CategoryManager:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def list(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT c.id, c.name, c.description, COUNT(p.id) AS products "
            "FROM categories c LEFT JOIN products p ON p.category_id = c.id "
            "GROUP BY c.id ORDER BY c.name")

    def get(self, category_id: int) -> sqlite3.Row:
        row = self.db.query_one("SELECT * FROM categories WHERE id=?", (category_id,))
        if row is None:
            raise NotFoundError("Category not found.")
        return row

    def add(self, name: str, description: str) -> int:
        cat = Category.from_form(name, description)
        try:
            cur = self.db.execute("INSERT INTO categories (name, description) VALUES (?,?)",
                                  (cat.name, cat.description))
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(f"Category '{cat.name}' already exists.") from exc
        log.info("Category added: %s", cat.name)
        return cur.lastrowid

    def update(self, category_id: int, name: str, description: str) -> None:
        cat = Category.from_form(name, description)
        try:
            cur = self.db.execute("UPDATE categories SET name=?, description=? WHERE id=?",
                                  (cat.name, cat.description, category_id))
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(f"Category '{cat.name}' already exists.") from exc
        if cur.rowcount == 0:
            raise NotFoundError("Category not found.")
        log.info("Category updated: %s", cat.name)

    def delete(self, category_id: int) -> None:
        try:
            cur = self.db.execute("DELETE FROM categories WHERE id=?", (category_id,))
        except sqlite3.IntegrityError as exc:
            raise InUseError("This category still has products. "
                             "Move or delete them first.") from exc
        if cur.rowcount == 0:
            raise NotFoundError("Category not found.")
        log.info("Category deleted: id=%s", category_id)


class SupplierManager:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def list(self, search: str = "") -> list[sqlite3.Row]:
        like = f"%{search.strip()}%"
        return self.db.query(
            "SELECT s.*, COUNT(p.id) AS products FROM suppliers s "
            "LEFT JOIN products p ON p.supplier_id = s.id "
            "WHERE s.name LIKE ? OR s.contact_person LIKE ? OR s.phone LIKE ? OR s.email LIKE ? "
            "GROUP BY s.id ORDER BY s.name", (like, like, like, like))

    def get(self, supplier_id: int) -> sqlite3.Row:
        row = self.db.query_one("SELECT * FROM suppliers WHERE id=?", (supplier_id,))
        if row is None:
            raise NotFoundError("Supplier not found.")
        return row

    def add(self, name: str, contact: str, phone: str, email: str, address: str) -> int:
        s = Supplier.from_form(name, contact, phone, email, address)
        try:
            cur = self.db.execute(
                "INSERT INTO suppliers (name, contact_person, phone, email, address) "
                "VALUES (?,?,?,?,?)", (s.name, s.contact_person, s.phone, s.email, s.address))
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(f"Supplier '{s.name}' already exists.") from exc
        log.info("Supplier added: %s", s.name)
        return cur.lastrowid

    def update(self, supplier_id: int, name: str, contact: str, phone: str,
               email: str, address: str) -> None:
        s = Supplier.from_form(name, contact, phone, email, address)
        try:
            cur = self.db.execute(
                "UPDATE suppliers SET name=?, contact_person=?, phone=?, email=?, address=? "
                "WHERE id=?", (s.name, s.contact_person, s.phone, s.email, s.address, supplier_id))
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(f"Supplier '{s.name}' already exists.") from exc
        if cur.rowcount == 0:
            raise NotFoundError("Supplier not found.")
        log.info("Supplier updated: %s", s.name)

    def delete(self, supplier_id: int) -> None:
        try:
            cur = self.db.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
        except sqlite3.IntegrityError as exc:
            raise InUseError("This supplier has purchase history and cannot be deleted.") from exc
        if cur.rowcount == 0:
            raise NotFoundError("Supplier not found.")
        log.info("Supplier deleted: id=%s", supplier_id)


class InventoryManager:
    """The single place where stock quantities change (keeps history exact)."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def apply_movement(self, product_id: str, change: int, movement_type: str,
                       reference: str = "", note: str = "") -> int:
        """Change stock by `change` units, record it, and return the new quantity."""
        if change == 0:
            raise ValidationError("Quantity must not be zero.")
        with self.db.transaction():
            row = self.db.query_one("SELECT name, quantity FROM products WHERE id=?",
                                    (product_id,))
            if row is None:
                raise NotFoundError("Product not found.")
            new_qty = row["quantity"] + change
            if new_qty < 0:
                raise InsufficientStockError(
                    f"Insufficient stock for '{row['name']}': "
                    f"available {row['quantity']}, requested {-change}.")
            stamp = now_str()
            self.db.execute("UPDATE products SET quantity=?, updated_at=? WHERE id=?",
                            (new_qty, stamp, product_id))
            self.db.execute(
                "INSERT INTO stock_movements (product_id, movement_type, quantity_change, "
                "quantity_after, reference, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (product_id, movement_type, change, new_qty, reference, note, stamp))
        return new_qty

    def stock_in(self, product_id: str | None, quantity: object, note: str = "") -> int:
        if not product_id:
            raise ValidationError("Please select a product.")
        qty = Validate.integer(quantity, "Quantity", 1)
        new_qty = self.apply_movement(product_id, qty, "STOCK_IN", "MANUAL",
                                      Validate.text(note, "Note", False, 200))
        log.info("Stock IN: product=%s qty=%s", product_id, qty)
        return new_qty

    def stock_out(self, product_id: str | None, quantity: object, note: str = "") -> int:
        if not product_id:
            raise ValidationError("Please select a product.")
        qty = Validate.integer(quantity, "Quantity", 1)
        new_qty = self.apply_movement(product_id, -qty, "STOCK_OUT", "MANUAL",
                                      Validate.text(note, "Note", False, 200))
        log.info("Stock OUT: product=%s qty=%s", product_id, qty)
        return new_qty

    def is_low(self, product_id: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM products WHERE id=? AND quantity <= reorder_level", (product_id,))
        return row is not None

    def low_stock_products(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT p.id, p.name, c.name AS category, p.quantity, p.reorder_level "
            "FROM products p JOIN categories c ON c.id = p.category_id "
            "WHERE p.quantity <= p.reorder_level ORDER BY p.quantity, p.name")

    def history(self, product_id: str | None = None, start: str | None = None,
                end: str | None = None, limit: int = 1000) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT m.id, m.created_at, p.name AS product, m.movement_type, "
            "m.quantity_change, m.quantity_after, m.reference, m.note "
            "FROM stock_movements m JOIN products p ON p.id = m.product_id "
            "WHERE (? IS NULL OR m.product_id = ?) "
            "AND substr(m.created_at,1,10) BETWEEN ? AND ? "
            "ORDER BY m.id DESC LIMIT ?",
            (product_id, product_id, start or "0000-01-01", end or "9999-12-31", limit))


class ProductManager:
    def __init__(self, db: DatabaseManager, inventory: InventoryManager) -> None:
        self.db = db
        self.inventory = inventory

    def add(self, name: str, category_id: int | None, supplier_id: int | None,
            cost: object, price: object, quantity: object, reorder: object) -> str:
        product = Product.from_form(name, category_id, supplier_id, cost, price,
                                    quantity, reorder)
        try:
            with self.db.transaction():
                stamp = now_str()
                self.db.execute(
                    "INSERT INTO products (id, name, category_id, supplier_id, cost_price, "
                    "sale_price, quantity, reorder_level, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,0,?,?,?)",
                    (product.id, product.name, product.category_id, product.supplier_id,
                     product.cost_price, product.sale_price, product.reorder_level,
                     stamp, stamp))
                if product.quantity > 0:
                    self.inventory.apply_movement(product.id, product.quantity, "STOCK_IN",
                                                  "OPENING", "Opening stock")
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(
                exc, f"'{product.name}' already exists in this category.",
                "Selected category or supplier is invalid.") from exc
        log.info("Product added: %s (%s)", product.name, product.id)
        return product.id

    def update(self, product_id: str, name: str, category_id: int | None,
               supplier_id: int | None, cost: object, price: object,
               reorder: object) -> None:
        p = Product.from_form(name, category_id, supplier_id, cost, price, 0, reorder)
        try:
            cur = self.db.execute(
                "UPDATE products SET name=?, category_id=?, supplier_id=?, cost_price=?, "
                "sale_price=?, reorder_level=?, updated_at=? WHERE id=?",
                (p.name, p.category_id, p.supplier_id, p.cost_price, p.sale_price,
                 p.reorder_level, now_str(), product_id))
        except sqlite3.IntegrityError as exc:
            raise translate_integrity_error(
                exc, f"'{p.name}' already exists in this category.",
                "Selected category or supplier is invalid.") from exc
        if cur.rowcount == 0:
            raise NotFoundError("Product not found.")
        log.info("Product updated: %s", product_id)

    def delete(self, product_id: str) -> None:
        with self.db.transaction():
            used = self.db.query_one(
                "SELECT 1 FROM sale_items WHERE product_id=? "
                "UNION SELECT 1 FROM purchase_items WHERE product_id=?",
                (product_id, product_id))
            if used:
                raise InUseError("This product appears in sales or purchases and cannot be "
                                 "deleted. Set its reorder level to 0 and stop selling it.")
            self.db.execute("DELETE FROM stock_movements WHERE product_id=?", (product_id,))
            cur = self.db.execute("DELETE FROM products WHERE id=?", (product_id,))
            if cur.rowcount == 0:
                raise NotFoundError("Product not found.")
        log.info("Product deleted: %s", product_id)

    def get(self, product_id: str) -> sqlite3.Row:
        row = self.db.query_one(
            "SELECT p.*, c.name AS category, s.name AS supplier FROM products p "
            "JOIN categories c ON c.id = p.category_id "
            "LEFT JOIN suppliers s ON s.id = p.supplier_id WHERE p.id=?", (product_id,))
        if row is None:
            raise NotFoundError("Product not found.")
        return row

    def search(self, text: str = "", category_id: int | None = None,
               low_only: bool = False) -> list[sqlite3.Row]:
        like = f"%{text.strip()}%"
        sql = ("SELECT p.*, c.name AS category, s.name AS supplier FROM products p "
               "JOIN categories c ON c.id = p.category_id "
               "LEFT JOIN suppliers s ON s.id = p.supplier_id "
               "WHERE (p.name LIKE ? OR p.id LIKE ? OR c.name LIKE ? OR s.name LIKE ?)")
        params: list = [like, like, like, like]
        if category_id is not None:
            sql += " AND p.category_id = ?"
            params.append(category_id)
        if low_only:
            sql += " AND p.quantity <= p.reorder_level"
        return self.db.query(sql + " ORDER BY p.name", params)


class TradeManager:
    """Shared logic for purchases and sales (both are lists of line items)."""

    HEADER = ""      # header table name
    ITEMS = ""       # items table name
    FK = ""          # items.<fk> -> header.id
    DATE = ""        # date column
    PRICE = ""       # unit price column in the items table
    PARTY_SQL = ""   # SQL expression for the customer/supplier name
    PARTY_JOIN = ""  # optional JOIN clause

    def __init__(self, db: DatabaseManager, inventory: InventoryManager) -> None:
        self.db = db
        self.inventory = inventory

    @staticmethod
    def _validate_items(items: Sequence[LineItem]) -> float:
        if not items:
            raise ValidationError("Add at least one product to the list first.")
        for item in items:
            if item.quantity < 1:
                raise ValidationError("Quantities must be at least 1.")
            if item.price < 0:
                raise ValidationError("Prices cannot be negative.")
        return round(sum(i.quantity * i.price for i in items), 2)

    def list(self, start: str | None = None, end: str | None = None) -> list[sqlite3.Row]:
        return self.db.query(
            f"SELECT t.id, t.{self.DATE} AS date, {self.PARTY_SQL} AS party, "
            f"(SELECT COUNT(*) FROM {self.ITEMS} i WHERE i.{self.FK} = t.id) AS items, "
            f"t.total, t.notes FROM {self.HEADER} t {self.PARTY_JOIN} "
            f"WHERE substr(t.{self.DATE},1,10) BETWEEN ? AND ? ORDER BY t.id DESC",
            (start or "0000-01-01", end or "9999-12-31"))

    def items_for(self, trade_id: int) -> list[sqlite3.Row]:
        return self.db.query(
            f"SELECT p.name, i.quantity, i.{self.PRICE} AS price, "
            f"i.quantity * i.{self.PRICE} AS subtotal FROM {self.ITEMS} i "
            f"JOIN products p ON p.id = i.product_id WHERE i.{self.FK} = ?", (trade_id,))

    def total_amount(self) -> float:
        row = self.db.query_one(f"SELECT COALESCE(SUM(total), 0) AS t FROM {self.HEADER}")
        return float(row["t"])


class PurchaseManager(TradeManager):
    HEADER, ITEMS, FK = "purchases", "purchase_items", "purchase_id"
    DATE, PRICE = "purchase_date", "unit_cost"
    PARTY_SQL = "s.name"
    PARTY_JOIN = "JOIN suppliers s ON s.id = t.supplier_id"

    def create_purchase(self, supplier_id: int | None, items: Sequence[LineItem],
                        notes: str = "") -> int:
        if supplier_id is None:
            raise ValidationError("Please select a supplier.")
        notes = Validate.text(notes, "Notes", required=False, max_len=200)
        total = self._validate_items(items)
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO purchases (supplier_id, purchase_date, total, notes) "
                "VALUES (?,?,?,?)", (supplier_id, now_str(), total, notes))
            purchase_id = cur.lastrowid
            for item in items:
                self.db.execute(
                    "INSERT INTO purchase_items (purchase_id, product_id, quantity, unit_cost) "
                    "VALUES (?,?,?,?)", (purchase_id, item.product_id, item.quantity, item.price))
                self.inventory.apply_movement(item.product_id, item.quantity, "PURCHASE",
                                              f"PO-{purchase_id}")
                self.db.execute("UPDATE products SET cost_price=? WHERE id=?",
                                (item.price, item.product_id))
        log.info("Purchase #%s saved, total=%.2f", purchase_id, total)
        return purchase_id


class SalesManager(TradeManager):
    HEADER, ITEMS, FK = "sales", "sale_items", "sale_id"
    DATE, PRICE = "sale_date", "unit_price"
    PARTY_SQL = "COALESCE(NULLIF(t.customer_name, ''), 'Walk-in customer')"

    def create_sale(self, customer: str, items: Sequence[LineItem],
                    notes: str = "") -> SaleResult:
        customer = Validate.text(customer, "Customer name", required=False, max_len=80)
        notes = Validate.text(notes, "Notes", required=False, max_len=200)
        total = self._validate_items(items)
        with self.db.transaction():  # any stock error rolls back the whole sale
            cur = self.db.execute(
                "INSERT INTO sales (customer_name, sale_date, total, notes) VALUES (?,?,?,?)",
                (customer, now_str(), total, notes))
            sale_id = cur.lastrowid
            for item in items:
                self.db.execute(
                    "INSERT INTO sale_items (sale_id, product_id, quantity, unit_price) "
                    "VALUES (?,?,?,?)", (sale_id, item.product_id, item.quantity, item.price))
                self.inventory.apply_movement(item.product_id, -item.quantity, "SALE",
                                              f"SALE-{sale_id}")
        low = []
        for item in items:
            if self.inventory.is_low(item.product_id):
                row = self.db.query_one("SELECT name, quantity FROM products WHERE id=?",
                                        (item.product_id,))
                low.append(f"{row['name']} (only {row['quantity']} left)")
        log.info("Sale #%s saved, total=%.2f", sale_id, total)
        return SaleResult(sale_id, total, low)


class ReportManager:
    """Dashboard statistics, reports and CSV export."""

    REPORT_TYPES = ["Inventory Stock", "Low Stock", "Sales", "Purchases",
                    "Stock Movements", "Daily Transactions"]

    def __init__(self, db: DatabaseManager, inventory: InventoryManager) -> None:
        self.db = db
        self.inventory = inventory

    def dashboard_stats(self) -> dict:
        q = self.db.query_one
        return {
            "products": q("SELECT COUNT(*) AS n FROM products")["n"],
            "stock": q("SELECT COALESCE(SUM(quantity),0) AS n FROM products")["n"],
            "categories": q("SELECT COUNT(*) AS n FROM categories")["n"],
            "low": q("SELECT COUNT(*) AS n FROM products WHERE quantity <= reorder_level")["n"],
            "sales": float(q("SELECT COALESCE(SUM(total),0) AS n FROM sales")["n"]),
            "stock_value": float(q("SELECT COALESCE(SUM(quantity*cost_price),0) AS n "
                                   "FROM products")["n"]),
        }

    def recent_transactions(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT m.id, m.created_at, m.movement_type, p.name AS product, "
            "m.quantity_change, m.quantity_after, m.reference FROM stock_movements m "
            "JOIN products p ON p.id = m.product_id ORDER BY m.id DESC LIMIT ?", (limit,))

    def stock_by_category(self) -> list[tuple[str, int]]:
        rows = self.db.query(
            "SELECT c.name, COALESCE(SUM(p.quantity),0) AS qty FROM categories c "
            "LEFT JOIN products p ON p.category_id = c.id GROUP BY c.id ORDER BY qty DESC")
        return [(r["name"], r["qty"]) for r in rows]

    def sales_last_days(self, days: int = 7) -> list[tuple[str, float]]:
        today = datetime.now().date()
        result = []
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            row = self.db.query_one(
                "SELECT COALESCE(SUM(total),0) AS t FROM sales WHERE substr(sale_date,1,10)=?",
                (day,))
            result.append((day[5:], float(row["t"])))
        return result

    # ---- reports -----------------------------------------------------------
    def build(self, kind: str, start: str | None = None,
              end: str | None = None) -> tuple[list[str], list[tuple]]:
        builders = {
            "Inventory Stock": lambda: self._inventory_report(False),
            "Low Stock": lambda: self._inventory_report(True),
            "Sales": lambda: self._items_report("sales", "sale_items", "sale_id",
                                                "sale_date", "unit_price", "Customer",
                                                "COALESCE(NULLIF(t.customer_name,''),'Walk-in')",
                                                "", start, end),
            "Purchases": lambda: self._items_report("purchases", "purchase_items",
                                                    "purchase_id", "purchase_date", "unit_cost",
                                                    "Supplier", "s.name",
                                                    "JOIN suppliers s ON s.id = t.supplier_id",
                                                    start, end),
            "Stock Movements": lambda: self._movements_report(start, end),
            "Daily Transactions": lambda: self._daily_report(start, end),
        }
        if kind not in builders:
            raise ValidationError("Please choose a report type.")
        return builders[kind]()

    def _inventory_report(self, low_only: bool) -> tuple[list[str], list[tuple]]:
        sql = ("SELECT p.id, p.name, c.name, COALESCE(s.name,''), p.quantity, "
               "p.reorder_level, p.cost_price, p.sale_price, p.quantity*p.cost_price, "
               "CASE WHEN p.quantity <= p.reorder_level THEN 'LOW' ELSE 'OK' END "
               "FROM products p JOIN categories c ON c.id = p.category_id "
               "LEFT JOIN suppliers s ON s.id = p.supplier_id ")
        if low_only:
            sql += "WHERE p.quantity <= p.reorder_level "
        rows = [tuple(r) for r in self.db.query(sql + "ORDER BY p.name")]
        headers = ["Product ID", "Name", "Category", "Supplier", "Quantity", "Reorder Level",
                   "Cost Price", "Sale Price", "Stock Value", "Status"]
        return headers, rows

    def _items_report(self, header: str, items: str, fk: str, date_col: str, price: str,
                      party_label: str, party_sql: str, join: str, start, end):
        sql = (f"SELECT t.id, t.{date_col}, {party_sql}, p.name, i.quantity, i.{price}, "
               f"i.quantity*i.{price} FROM {header} t JOIN {items} i ON i.{fk} = t.id "
               f"JOIN products p ON p.id = i.product_id {join} "
               f"WHERE substr(t.{date_col},1,10) BETWEEN ? AND ? ORDER BY t.id DESC")
        rows = [tuple(r) for r in self.db.query(sql, (start or "0000-01-01", end or "9999-12-31"))]
        headers = ["Ref #", "Date", party_label, "Product", "Quantity", "Unit Price", "Line Total"]
        return headers, rows

    def _movements_report(self, start, end):
        rows = [(r["created_at"], r["product"], r["movement_type"], r["quantity_change"],
                 r["quantity_after"], r["reference"], r["note"])
                for r in self.inventory.history(None, start, end, limit=100000)]
        headers = ["Date", "Product", "Type", "Change", "Balance", "Reference", "Note"]
        return headers, rows

    def _daily_report(self, start, end):
        sql = ("WITH days AS (SELECT substr(sale_date,1,10) AS d FROM sales "
               "UNION SELECT substr(purchase_date,1,10) FROM purchases) "
               "SELECT d, "
               "(SELECT COUNT(*) FROM sales WHERE substr(sale_date,1,10)=d), "
               "(SELECT COALESCE(SUM(total),0) FROM sales WHERE substr(sale_date,1,10)=d), "
               "(SELECT COUNT(*) FROM purchases WHERE substr(purchase_date,1,10)=d), "
               "(SELECT COALESCE(SUM(total),0) FROM purchases WHERE substr(purchase_date,1,10)=d) "
               "FROM days WHERE d BETWEEN ? AND ? ORDER BY d DESC")
        rows = [tuple(r) for r in self.db.query(sql, (start or "0000-01-01", end or "9999-12-31"))]
        headers = ["Date", "Sales Count", "Sales Total", "Purchase Count", "Purchase Total"]
        return headers, rows

    @staticmethod
    def export_csv(path: str | Path, headers: Sequence[str], rows: Sequence[Sequence]) -> int:
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        log.info("Report exported to %s (%d rows)", path, len(rows))
        return len(rows)


# =============================================================================
# 5. GUI BUILDING BLOCKS
# =============================================================================
class Theme:
    BG = "#f1f5f9"
    SIDEBAR = "#1e293b"
    SIDEBAR_HOVER = "#334155"
    PRIMARY = "#2563eb"
    PRIMARY_HOVER = "#1d4ed8"
    SUCCESS = "#16a34a"
    SUCCESS_HOVER = "#15803d"
    WARNING = "#f59e0b"
    DANGER = "#dc2626"
    DANGER_HOVER = "#b91c1c"
    CARD = "#ffffff"
    TEXT = "#0f172a"
    MUTED = "#64748b"
    BORDER = "#e2e8f0"
    FONT = "Segoe UI"


def ui_font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=Theme.FONT, size=size, weight=weight)


def guarded(method: Callable) -> Callable:
    """Decorator: show friendly dialogs instead of crashing on errors."""
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except InventoryError as exc:
            messagebox.showwarning("Action not completed", str(exc))
        except Exception:
            log.exception("Unexpected error in %s", method.__name__)
            messagebox.showerror("Unexpected error",
                                 "Something went wrong. Details were saved in logs/inventory.log.")
    return wrapper


def make_button(parent, text: str, command: Callable | None, kind: str = "primary",
                width: int = 120) -> ctk.CTkButton:
    palette = {
        "primary": (Theme.PRIMARY, Theme.PRIMARY_HOVER, "white"),
        "success": (Theme.SUCCESS, Theme.SUCCESS_HOVER, "white"),
        "danger": (Theme.DANGER, Theme.DANGER_HOVER, "white"),
        "secondary": ("#e2e8f0", "#cbd5e1", Theme.TEXT),
    }
    fg, hover, text_color = palette[kind]
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=34,
                         corner_radius=6, fg_color=fg, hover_color=hover,
                         text_color=text_color, font=ui_font(13, "bold"))


def set_entry(entry: ctk.CTkEntry, value: object) -> None:
    """Replace an entry's text (works even if the entry is disabled)."""
    state = entry.cget("state")
    entry.configure(state="normal")
    entry.delete(0, "end")
    entry.insert(0, "" if value is None else str(value))
    entry.configure(state=state)


def center_window(window, width: int, height: int) -> None:
    x = max((window.winfo_screenwidth() - width) // 2, 0)
    y = max((window.winfo_screenheight() - height) // 2 - 20, 0)
    window.geometry(f"{width}x{height}+{x}+{y}")


class ChoiceBox:
    """A read-only dropdown that maps visible labels to real ids."""

    def __init__(self, parent, placeholder: str = "Select...", width: int = 200,
                 command: Callable | None = None) -> None:
        self.placeholder = placeholder
        self._by_label: dict[str, object] = {}
        self._by_key: dict[object, str] = {}
        self.widget = ctk.CTkComboBox(parent, values=[], state="readonly", width=width,
                                      height=34, command=command, font=ui_font(13))
        self.widget.set(placeholder)

    def grid(self, **kwargs) -> None:
        self.widget.grid(**kwargs)

    def pack(self, **kwargs) -> None:
        self.widget.pack(**kwargs)

    def set_choices(self, pairs: Sequence[tuple[object, str]], default_first: bool = False) -> None:
        labels = [label for _, label in pairs]
        self._by_label = {label: key for key, label in pairs}
        self._by_key = {key: label for key, label in pairs}
        previous = self.widget.get()
        self.widget.configure(values=labels)
        if previous in self._by_label:
            self.widget.set(previous)
        elif default_first and labels:
            self.widget.set(labels[0])
        else:
            self.widget.set(self.placeholder)

    def selected_id(self):
        return self._by_label.get(self.widget.get())

    def select_id(self, key: object, fallback_first: bool = False) -> None:
        label = self._by_key.get(key)
        if label is not None:
            self.widget.set(label)
        elif fallback_first and self._by_label:
            self.widget.set(next(iter(self._by_label)))
        else:
            self.widget.set(self.placeholder)

    def reset(self, fallback_first: bool = False) -> None:
        if fallback_first and self._by_label:
            self.widget.set(next(iter(self._by_label)))
        else:
            self.widget.set(self.placeholder)


class Panel(ctk.CTkFrame):
    """White rounded card with an optional title; put content in `.body`."""

    def __init__(self, master, title: str = "") -> None:
        super().__init__(master, fg_color=Theme.CARD, corner_radius=10,
                         border_width=1, border_color=Theme.BORDER)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self, text=title, font=ui_font(15, "bold"), text_color=Theme.TEXT,
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)


class StatCard(ctk.CTkFrame):
    def __init__(self, master, icon: str, title: str, color: str) -> None:
        super().__init__(master, fg_color=Theme.CARD, corner_radius=10,
                         border_width=1, border_color=Theme.BORDER)
        ctk.CTkLabel(self, text=icon, width=46, height=46, corner_radius=23, fg_color=color,
                     text_color="white", font=ui_font(20)).grid(
            row=0, column=0, rowspan=2, padx=(14, 10), pady=14)
        ctk.CTkLabel(self, text=title, font=ui_font(12), text_color=Theme.MUTED,
                     anchor="w").grid(row=0, column=1, sticky="sw", padx=(0, 12), pady=(14, 0))
        self.value_label = ctk.CTkLabel(self, text="0", font=ui_font(22, "bold"),
                                        text_color=Theme.TEXT, anchor="w")
        self.value_label.grid(row=1, column=1, sticky="nw", padx=(0, 12), pady=(0, 14))

    def set_value(self, text: str) -> None:
        self.value_label.configure(text=text)


class DataTable(ctk.CTkFrame):
    """Scrollable table. Columns are (title, width[, anchor]) tuples."""

    def __init__(self, master, columns: Sequence[tuple], height: int = 10,
                 on_select: Callable | None = None,
                 on_double_click: Callable | None = None) -> None:
        super().__init__(master, fg_color="white", corner_radius=6,
                         border_width=1, border_color=Theme.BORDER)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, show="headings", height=height, selectmode="browse")
        yscroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 0))
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("low", foreground=Theme.DANGER)
        self.tree.tag_configure("odd", background="#f8fafc")
        self.set_columns(columns)
        if on_select:
            self.tree.bind("<<TreeviewSelect>>", lambda _e: on_select(self.selected()))
        if on_double_click:
            self.tree.bind("<Double-1>", lambda _e: on_double_click(self.selected()))

    def set_columns(self, columns: Sequence[tuple]) -> None:
        ids = [f"c{i}" for i in range(len(columns))]
        self.tree.configure(columns=ids)
        for cid, spec in zip(ids, columns):
            anchor = spec[2] if len(spec) > 2 else "w"
            self.tree.heading(cid, text=spec[0])
            self.tree.column(cid, width=spec[1], minwidth=50, anchor=anchor, stretch=True)

    def load(self, rows: Sequence[tuple]) -> None:
        """rows: (iid, values[, tag]). iid may be None for automatic ids."""
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(rows):
            iid, values = row[0], row[1]
            tag = row[2] if len(row) > 2 and row[2] else ("odd" if index % 2 else "")
            kwargs = {"iid": str(iid)} if iid is not None else {}
            self.tree.insert("", "end", values=list(values), tags=(tag,) if tag else (), **kwargs)

    def selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def clear_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())


class FormBuilder:
    """Stacks labelled inputs vertically inside a parent frame."""

    def __init__(self, parent) -> None:
        self.parent = parent
        self.row = 0
        parent.grid_columnconfigure(0, weight=1)

    def title(self, text: str) -> None:
        ctk.CTkLabel(self.parent, text=text, font=ui_font(15, "bold"), text_color=Theme.TEXT,
                     anchor="w").grid(row=self.row, column=0, sticky="ew", padx=16, pady=(14, 0))
        self.row += 1

    def _label(self, text: str) -> ctk.CTkLabel:
        label = ctk.CTkLabel(self.parent, text=text, font=ui_font(12), text_color=Theme.MUTED,
                             anchor="w")
        label.grid(row=self.row, column=0, sticky="ew", padx=16, pady=(10, 0))
        self.row += 1
        return label

    def entry(self, label: str, placeholder: str = "", show: str = "") -> ctk.CTkEntry:
        self._label(label)
        widget = ctk.CTkEntry(self.parent, placeholder_text=placeholder, height=34,
                              font=ui_font(13), show=show)
        widget.grid(row=self.row, column=0, sticky="ew", padx=16, pady=(2, 0))
        self.row += 1
        return widget

    def choice(self, label: str, placeholder: str = "Select...",
               command: Callable | None = None) -> ChoiceBox:
        self._label(label)
        box = ChoiceBox(self.parent, placeholder, command=command)
        box.grid(row=self.row, column=0, sticky="ew", padx=16, pady=(2, 0))
        self.row += 1
        return box

    def info(self, text: str = "", color: str = Theme.MUTED) -> ctk.CTkLabel:
        label = ctk.CTkLabel(self.parent, text=text, font=ui_font(12), text_color=color,
                             anchor="w", justify="left", wraplength=260)
        label.grid(row=self.row, column=0, sticky="ew", padx=16, pady=(6, 0))
        self.row += 1
        return label

    def buttons(self, specs: Sequence[tuple[str, Callable, str]]) -> None:
        frame = ctk.CTkFrame(self.parent, fg_color="transparent")
        frame.grid(row=self.row, column=0, sticky="ew", padx=16, pady=(16, 16))
        frame.grid_columnconfigure((0, 1), weight=1, uniform="btn")
        for index, (text, command, kind) in enumerate(specs):
            make_button(frame, text, command, kind, width=100).grid(
                row=index // 2, column=index % 2, sticky="ew", padx=3, pady=3)
        self.row += 1


class DateRange(ctk.CTkFrame):
    """From/To date filter. Blank means unlimited."""

    def __init__(self, master, on_apply: Callable) -> None:
        super().__init__(master, fg_color="transparent")
        self.on_apply = on_apply
        ctk.CTkLabel(self, text="From", font=ui_font(12), text_color=Theme.MUTED).pack(
            side="left", padx=(0, 4))
        self.start_entry = ctk.CTkEntry(self, width=108, height=32, placeholder_text="YYYY-MM-DD")
        self.start_entry.pack(side="left")
        ctk.CTkLabel(self, text="To", font=ui_font(12), text_color=Theme.MUTED).pack(
            side="left", padx=(10, 4))
        self.end_entry = ctk.CTkEntry(self, width=108, height=32, placeholder_text="YYYY-MM-DD")
        self.end_entry.pack(side="left")
        make_button(self, "Apply", self.on_apply, width=70).pack(side="left", padx=(10, 4))
        make_button(self, "Clear", self.clear, "secondary", width=70).pack(side="left")

    def values(self) -> tuple[str | None, str | None]:
        start = Validate.date(self.start_entry.get(), "From date")
        end = Validate.date(self.end_entry.get(), "To date")
        if start and end and start > end:
            raise ValidationError("From date cannot be after To date.")
        return start, end

    def clear(self) -> None:
        self.start_entry.delete(0, "end")
        self.end_entry.delete(0, "end")
        self.on_apply()


class ItemsDialog(ctk.CTkToplevel):
    """Popup listing the line items of one purchase or sale."""

    def __init__(self, master, title: str, rows: Sequence[sqlite3.Row]) -> None:
        super().__init__(master)
        self.title(title)
        self.configure(fg_color=Theme.BG)
        center_window(self, 600, 360)
        self.transient(master)
        table = DataTable(self, [("Product", 260), ("Qty", 70, "e"),
                                 ("Price", 110, "e"), ("Subtotal", 120, "e")], height=10)
        table.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        table.load([(None, (r["name"], r["quantity"], fmt_money(r["price"]),
                            fmt_money(r["subtotal"]))) for r in rows])
        total = sum(r["subtotal"] for r in rows)
        ctk.CTkLabel(self, text=f"Total: {fmt_money(total)}", font=ui_font(14, "bold"),
                     text_color=Theme.TEXT).pack(anchor="e", padx=20, pady=(0, 12))
        self.after(150, self._focus)

    def _focus(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass


# =============================================================================
# GUI PAGES
# =============================================================================
class BasePage(ctk.CTkFrame):
    """Every page gets the app (and so all managers) and a refresh() hook."""
    PAGE_TITLE = ""

    def __init__(self, master, app: "MainApplication") -> None:
        super().__init__(master, fg_color=Theme.BG)
        self.app = app

    def refresh(self) -> None:
        """Reload data from the database (called whenever the page is shown)."""


class DashboardPage(BasePage):
    PAGE_TITLE = "Dashboard Overview"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="ew", padx=14, pady=(18, 10))
        specs = [("products", "📦", "Total Products", Theme.PRIMARY),
                 ("stock", "🧮", "Total Stock", "#7c3aed"),
                 ("categories", "🗂", "Total Categories", "#0891b2"),
                 ("low", "⚠", "Low Stock Products", Theme.DANGER),
                 ("sales", "💰", "Total Sales", Theme.SUCCESS)]
        self.cards: dict[str, StatCard] = {}
        for index, (key, icon, title, color) in enumerate(specs):
            cards.grid_columnconfigure(index, weight=1, uniform="card")
            card = StatCard(cards, icon, title, color)
            card.grid(row=0, column=index, sticky="ew", padx=6)
            self.cards[key] = card

        self.banner = ctk.CTkLabel(self, text="", fg_color="#fef3c7", text_color="#92400e",
                                   corner_radius=6, anchor="w", height=36,
                                   font=ui_font(13, "bold"))
        self.banner.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 16))
        body.grid_columnconfigure(0, weight=3, uniform="body")
        body.grid_columnconfigure(1, weight=2, uniform="body")
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=6)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure((0, 1), weight=1, uniform="left")

        recent = Panel(left, "Recent Transactions")
        recent.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self.recent_table = DataTable(recent.body, [
            ("Date", 130), ("Type", 90), ("Product", 170), ("Change", 60, "e"),
            ("Balance", 70, "e"), ("Reference", 90)], height=5)
        self.recent_table.grid(row=0, column=0, sticky="nsew", padx=12, pady=(0, 12))

        low = Panel(left, "Low Stock Alerts")
        low.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.low_table = DataTable(low.body, [
            ("Product", 200), ("Category", 120), ("In Stock", 80, "e"),
            ("Reorder Level", 100, "e")], height=4)
        self.low_table.grid(row=0, column=0, sticky="nsew", padx=12, pady=(0, 12))

        chart_panel = Panel(body, "Inventory Statistics")
        chart_panel.grid(row=0, column=1, sticky="nsew", padx=6)
        self.figure = Figure(figsize=(4.4, 5.0), dpi=96, constrained_layout=True)
        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_panel.body)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=8, pady=(0, 8))

    def refresh(self) -> None:
        stats = self.app.reports.dashboard_stats()
        self.cards["products"].set_value(f"{stats['products']:,}")
        self.cards["stock"].set_value(f"{stats['stock']:,}")
        self.cards["categories"].set_value(f"{stats['categories']:,}")
        self.cards["low"].set_value(f"{stats['low']:,}")
        self.cards["sales"].set_value(fmt_money(stats["sales"]))

        if stats["low"]:
            self.banner.configure(
                text=f"  ⚠  {stats['low']} product(s) are at or below their reorder level. "
                     f"Stock value: {fmt_money(stats['stock_value'])}")
            self.banner.grid()
        else:
            self.banner.grid_remove()

        self.recent_table.load([
            (r["id"], (r["created_at"], r["movement_type"].replace("_", " "), r["product"],
                       f"{r['quantity_change']:+d}", r["quantity_after"], r["reference"]))
            for r in self.app.reports.recent_transactions(10)])
        self.low_table.load([
            (r["id"], (r["name"], r["category"], r["quantity"], r["reorder_level"]), "low")
            for r in self.app.inventory.low_stock_products()])
        self._draw_charts()

    def _draw_charts(self) -> None:
        fig = self.figure
        fig.clear()
        fig.patch.set_facecolor("white")
        data_sets = [
            ("Stock by Category", self.app.reports.stock_by_category(), Theme.PRIMARY),
            ("Sales - Last 7 Days", self.app.reports.sales_last_days(7), Theme.SUCCESS),
        ]
        for index, (title, data, color) in enumerate(data_sets, start=1):
            ax = fig.add_subplot(2, 1, index)
            ax.set_title(title, fontsize=10, fontweight="bold", color=Theme.TEXT)
            if data and any(value for _, value in data):
                ax.bar([name for name, _ in data], [value for _, value in data], color=color)
            else:
                ax.text(0.5, 0.5, "No data yet", ha="center", va="center",
                        transform=ax.transAxes, color=Theme.MUTED)
            ax.tick_params(axis="x", labelrotation=25, labelsize=8)
            ax.tick_params(axis="y", labelsize=8)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        self.canvas.draw_idle()


class ProductsPage(BasePage):
    PAGE_TITLE = "Product Management"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        form_panel = ctk.CTkScrollableFrame(self, width=290, fg_color=Theme.CARD,
                                            corner_radius=10, border_width=1,
                                            border_color=Theme.BORDER)
        form_panel.grid(row=0, column=0, sticky="ns", padx=(18, 8), pady=18)
        form = FormBuilder(form_panel)
        form.title("Product Details")
        self.name_entry = form.entry("Product name", "e.g. Wireless Mouse")
        self.category_box = form.choice("Category", "Select category")
        self.supplier_box = form.choice("Supplier", "Select supplier")
        self.cost_entry = form.entry("Cost price", "0.00")
        self.price_entry = form.entry("Sale price", "0.00")
        self.qty_label = form._label("Opening quantity")
        self.qty_entry = ctk.CTkEntry(form_panel, height=34, font=ui_font(13))
        self.qty_entry.grid(row=form.row, column=0, sticky="ew", padx=16, pady=(2, 0))
        form.row += 1
        self.reorder_entry = form.entry("Reorder level (low-stock alert)", "10")
        form.buttons([("Add", self.add, "primary"), ("Update", self.update, "success"),
                      ("Delete", self.delete, "danger"), ("Clear", self.clear, "secondary")])

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 18), pady=18)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(right, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.search_entry = ctk.CTkEntry(toolbar, width=260, height=34,
                                         placeholder_text="🔍  Search name, ID, category, supplier")
        self.search_entry.pack(side="left")
        self.search_entry.bind("<KeyRelease>", lambda _e: self.load_table())
        self.filter_box = ChoiceBox(toolbar, "All Categories", width=180,
                                    command=lambda _v: self.load_table())
        self.filter_box.pack(side="left", padx=10)
        self.low_check = ctk.CTkCheckBox(toolbar, text="Low stock only", font=ui_font(13),
                                         command=self.load_table)
        self.low_check.pack(side="left", padx=6)

        self.table = DataTable(right, [
            ("ID", 80), ("Name", 190), ("Category", 110), ("Supplier", 120),
            ("Cost", 80, "e"), ("Price", 80, "e"), ("Qty", 60, "e"),
            ("Reorder", 70, "e"), ("Status", 70, "center")], on_select=self.on_select)
        self.table.grid(row=1, column=0, sticky="nsew")
        self.count_label = ctk.CTkLabel(right, text="", font=ui_font(12), text_color=Theme.MUTED,
                                        anchor="w")
        self.count_label.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._set_quantity_mode(editing=False)

    def refresh(self) -> None:
        categories = [(c["id"], c["name"]) for c in self.app.categories.list()]
        suppliers = [(None, "— None —")] + [(s["id"], s["name"]) for s in self.app.suppliers.list()]
        self.category_box.set_choices(categories)
        self.supplier_box.set_choices(suppliers, default_first=True)
        self.filter_box.set_choices([(None, "All Categories")] + categories, default_first=True)
        self.load_table()

    @guarded
    def load_table(self) -> None:
        rows = self.app.products.search(self.search_entry.get(), self.filter_box.selected_id(),
                                        bool(self.low_check.get()))
        self.table.load([
            (r["id"], (short_id(r["id"]), r["name"], r["category"], r["supplier"] or "—",
                       f"{r['cost_price']:,.2f}", f"{r['sale_price']:,.2f}", r["quantity"],
                       r["reorder_level"],
                       "LOW" if r["quantity"] <= r["reorder_level"] else "OK"),
             "low" if r["quantity"] <= r["reorder_level"] else None) for r in rows])
        self.count_label.configure(text=f"{len(rows)} product(s) shown")

    def _set_quantity_mode(self, editing: bool, value: object = "0") -> None:
        self.qty_entry.configure(state="normal")
        set_entry(self.qty_entry, value)
        if editing:
            self.qty_entry.configure(state="disabled")
        self.qty_label.configure(
            text="Current quantity (change it in Inventory)" if editing else "Opening quantity")

    @guarded
    def on_select(self, product_id: str | None) -> None:
        if not product_id:
            return
        p = self.app.products.get(product_id)
        set_entry(self.name_entry, p["name"])
        self.category_box.select_id(p["category_id"])
        self.supplier_box.select_id(p["supplier_id"], fallback_first=True)
        set_entry(self.cost_entry, f"{p['cost_price']:.2f}")
        set_entry(self.price_entry, f"{p['sale_price']:.2f}")
        set_entry(self.reorder_entry, p["reorder_level"])
        self._set_quantity_mode(True, p["quantity"])

    def clear(self) -> None:
        for entry in (self.name_entry, self.cost_entry, self.price_entry):
            set_entry(entry, "")
        set_entry(self.reorder_entry, "10")
        self.category_box.reset()
        self.supplier_box.reset(fallback_first=True)
        self._set_quantity_mode(False, "0")
        self.table.clear_selection()

    @guarded
    def add(self) -> None:
        self.app.products.add(
            self.name_entry.get(), self.category_box.selected_id(),
            self.supplier_box.selected_id(), self.cost_entry.get(), self.price_entry.get(),
            self.qty_entry.get(), self.reorder_entry.get())
        messagebox.showinfo("Product added", "The product was saved successfully.")
        self.clear()
        self.load_table()

    @guarded
    def update(self) -> None:
        product_id = self.table.selected()
        if not product_id:
            raise ValidationError("Select a product in the table first.")
        self.app.products.update(
            product_id, self.name_entry.get(), self.category_box.selected_id(),
            self.supplier_box.selected_id(), self.cost_entry.get(), self.price_entry.get(),
            self.reorder_entry.get())
        messagebox.showinfo("Product updated", "The product was updated successfully.")
        self.load_table()

    @guarded
    def delete(self) -> None:
        product_id = self.table.selected()
        if not product_id:
            raise ValidationError("Select a product in the table first.")
        name = self.app.products.get(product_id)["name"]
        if messagebox.askyesno("Confirm delete",
                               f"Delete '{name}' permanently?\nThis cannot be undone."):
            self.app.products.delete(product_id)
            self.clear()
            self.load_table()


class CategoriesPage(BasePage):
    PAGE_TITLE = "Product Categories"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        panel = Panel(self, "")
        panel.grid(row=0, column=0, sticky="n", padx=(18, 8), pady=18)
        panel.configure(width=290)
        form = FormBuilder(panel.body)
        form.title("Category Details")
        self.name_entry = form.entry("Category name", "e.g. Electronics")
        self.desc_entry = form.entry("Description", "Optional")
        form.buttons([("Add", self.add, "primary"), ("Update", self.update, "success"),
                      ("Delete", self.delete, "danger"), ("Clear", self.clear, "secondary")])
        self.table = DataTable(self, [("ID", 60), ("Name", 200), ("Description", 320),
                                      ("Products", 80, "e")], on_select=self.on_select)
        self.table.grid(row=0, column=1, sticky="nsew", padx=(8, 18), pady=18)

    def refresh(self) -> None:
        self.table.load([(r["id"], (r["id"], r["name"], r["description"], r["products"]))
                         for r in self.app.categories.list()])

    @guarded
    def on_select(self, category_id: str | None) -> None:
        if category_id:
            row = self.app.categories.get(int(category_id))
            set_entry(self.name_entry, row["name"])
            set_entry(self.desc_entry, row["description"])

    def clear(self) -> None:
        set_entry(self.name_entry, "")
        set_entry(self.desc_entry, "")
        self.table.clear_selection()

    @guarded
    def add(self) -> None:
        self.app.categories.add(self.name_entry.get(), self.desc_entry.get())
        self.clear()
        self.refresh()

    @guarded
    def update(self) -> None:
        selected = self.table.selected()
        if not selected:
            raise ValidationError("Select a category in the table first.")
        self.app.categories.update(int(selected), self.name_entry.get(), self.desc_entry.get())
        self.refresh()

    @guarded
    def delete(self) -> None:
        selected = self.table.selected()
        if not selected:
            raise ValidationError("Select a category in the table first.")
        name = self.app.categories.get(int(selected))["name"]
        if messagebox.askyesno("Confirm delete", f"Delete category '{name}'?"):
            self.app.categories.delete(int(selected))
            self.clear()
            self.refresh()


class SuppliersPage(BasePage):
    PAGE_TITLE = "Supplier Management"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        panel = ctk.CTkScrollableFrame(self, width=290, fg_color=Theme.CARD, corner_radius=10,
                                       border_width=1, border_color=Theme.BORDER)
        panel.grid(row=0, column=0, sticky="ns", padx=(18, 8), pady=18)
        form = FormBuilder(panel)
        form.title("Supplier Details")
        self.name_entry = form.entry("Supplier name")
        self.contact_entry = form.entry("Contact person")
        self.phone_entry = form.entry("Phone", "+1 555 0100")
        self.email_entry = form.entry("Email", "name@company.com")
        self.address_entry = form.entry("Address")
        form.buttons([("Add", self.add, "primary"), ("Update", self.update, "success"),
                      ("Delete", self.delete, "danger"), ("Clear", self.clear, "secondary")])

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 18), pady=18)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        self.search_entry = ctk.CTkEntry(right, width=300, height=34,
                                         placeholder_text="🔍  Search suppliers")
        self.search_entry.grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.search_entry.bind("<KeyRelease>", lambda _e: self.refresh())
        self.table = DataTable(right, [
            ("ID", 50), ("Name", 160), ("Contact", 120), ("Phone", 110), ("Email", 160),
            ("Address", 180), ("Products", 70, "e")], on_select=self.on_select)
        self.table.grid(row=1, column=0, sticky="nsew")

    @guarded
    def refresh(self) -> None:
        self.table.load([
            (r["id"], (r["id"], r["name"], r["contact_person"], r["phone"], r["email"],
                       r["address"], r["products"]))
            for r in self.app.suppliers.list(self.search_entry.get())])

    def _fields(self) -> tuple:
        return (self.name_entry.get(), self.contact_entry.get(), self.phone_entry.get(),
                self.email_entry.get(), self.address_entry.get())

    @guarded
    def on_select(self, supplier_id: str | None) -> None:
        if supplier_id:
            s = self.app.suppliers.get(int(supplier_id))
            for entry, key in ((self.name_entry, "name"), (self.contact_entry, "contact_person"),
                               (self.phone_entry, "phone"), (self.email_entry, "email"),
                               (self.address_entry, "address")):
                set_entry(entry, s[key])

    def clear(self) -> None:
        for entry in (self.name_entry, self.contact_entry, self.phone_entry,
                      self.email_entry, self.address_entry):
            set_entry(entry, "")
        self.table.clear_selection()

    @guarded
    def add(self) -> None:
        self.app.suppliers.add(*self._fields())
        self.clear()
        self.refresh()

    @guarded
    def update(self) -> None:
        selected = self.table.selected()
        if not selected:
            raise ValidationError("Select a supplier in the table first.")
        self.app.suppliers.update(int(selected), *self._fields())
        self.refresh()

    @guarded
    def delete(self) -> None:
        selected = self.table.selected()
        if not selected:
            raise ValidationError("Select a supplier in the table first.")
        name = self.app.suppliers.get(int(selected))["name"]
        if messagebox.askyesno("Confirm delete", f"Delete supplier '{name}'?"):
            self.app.suppliers.delete(int(selected))
            self.clear()
            self.refresh()


class TradePage(BasePage):
    """Shared screen for purchases and sales: build a cart, save, view history."""
    MODE = "sale"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.is_sale = self.MODE == "sale"
        self.manager = app.sales if self.is_sale else app.purchases
        self.cart: list[dict] = []
        self.grid_columnconfigure(0, weight=4, uniform="trade")
        self.grid_columnconfigure(1, weight=5, uniform="trade")
        self.grid_rowconfigure(0, weight=1)

        left = Panel(self, "New Sale" if self.is_sale else "New Purchase")
        left.grid(row=0, column=0, sticky="nsew", padx=(18, 8), pady=18)
        body = left.body
        body.grid_columnconfigure((0, 1), weight=1)
        body.grid_rowconfigure(5, weight=1)

        self._caption(body, "Customer name (optional)" if self.is_sale else "Supplier", 0)
        if self.is_sale:
            self.party_entry = ctk.CTkEntry(body, height=34, placeholder_text="Walk-in customer")
            self.party_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16)
        else:
            self.party_box = ChoiceBox(body, "Select supplier")
            self.party_box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16)

        self._caption(body, "Product", 2)
        self.product_box = ChoiceBox(body, "Select product", command=lambda _v: self.on_product())
        self.product_box.grid(row=3, column=0, columnspan=2, sticky="ew", padx=16)

        line = ctk.CTkFrame(body, fg_color="transparent")
        line.grid(row=4, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 8))
        line.grid_columnconfigure((0, 1), weight=1)
        self.info_label = ctk.CTkLabel(line, text="", font=ui_font(12), text_color=Theme.MUTED,
                                       anchor="w")
        self.info_label.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.qty_entry = ctk.CTkEntry(line, height=34, placeholder_text="Qty")
        self.qty_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        self.price_entry = ctk.CTkEntry(line, height=34,
                                        placeholder_text="Unit price" if self.is_sale else "Unit cost")
        self.price_entry.grid(row=1, column=1, sticky="ew", padx=(0, 6))
        make_button(line, "Add", self.add_item, width=70).grid(row=1, column=2)

        self.cart_table = DataTable(body, [("Product", 170), ("Qty", 50, "e"),
                                           ("Price", 80, "e"), ("Subtotal", 90, "e")], height=6)
        self.cart_table.grid(row=5, column=0, columnspan=2, sticky="nsew", padx=16)

        bar = ctk.CTkFrame(body, fg_color="transparent")
        bar.grid(row=6, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 0))
        make_button(bar, "Remove selected", self.remove_item, "secondary", width=130).pack(side="left")
        self.total_label = ctk.CTkLabel(bar, text=f"Total: {fmt_money(0)}",
                                        font=ui_font(16, "bold"), text_color=Theme.TEXT)
        self.total_label.pack(side="right")

        self.notes_entry = ctk.CTkEntry(body, height=34, placeholder_text="Notes (optional)")
        self.notes_entry.grid(row=7, column=0, columnspan=2, sticky="ew", padx=16, pady=(10, 0))
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.grid(row=8, column=0, columnspan=2, sticky="ew", padx=16, pady=(10, 16))
        actions.grid_columnconfigure((0, 1), weight=1, uniform="a")
        make_button(actions, "Complete Sale" if self.is_sale else "Save Purchase", self.save,
                    "success").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        make_button(actions, "Clear cart", self.clear_cart, "secondary").grid(
            row=0, column=1, sticky="ew", padx=(4, 0))

        right = Panel(self, "Sales History" if self.is_sale else "Purchase History")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 18), pady=18)
        right.body.grid_rowconfigure(1, weight=1)
        self.date_range = DateRange(right.body, self.load_history)
        self.date_range.grid(row=0, column=0, sticky="w", padx=12, pady=(0, 8))
        party = "Customer" if self.is_sale else "Supplier"
        self.history_table = DataTable(right.body, [
            ("#", 50), ("Date", 140), (party, 140), ("Items", 55, "e"),
            ("Total", 90, "e"), ("Notes", 140)], on_double_click=self.view_items)
        self.history_table.grid(row=1, column=0, sticky="nsew", padx=12)
        footer = ctk.CTkFrame(right.body, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=12, pady=10)
        make_button(footer, "View items", lambda: self.view_items(self.history_table.selected()),
                    "primary", width=110).pack(side="left")
        self.history_total = ctk.CTkLabel(footer, text="", font=ui_font(13, "bold"),
                                          text_color=Theme.TEXT)
        self.history_total.pack(side="right")

    @staticmethod
    def _caption(parent, text: str, row: int) -> None:
        ctk.CTkLabel(parent, text=text, font=ui_font(12), text_color=Theme.MUTED,
                     anchor="w").grid(row=row, column=0, columnspan=2, sticky="ew",
                                      padx=16, pady=(4, 0))

    def refresh(self) -> None:
        products = [(p["id"], f"{p['name']} ({short_id(p['id'])})")
                    for p in self.app.products.search()]
        self.product_box.set_choices(products)
        if not self.is_sale:
            self.party_box.set_choices([(s["id"], s["name"]) for s in self.app.suppliers.list()])
        self.on_product()
        self.load_history()

    @guarded
    def on_product(self) -> None:
        product_id = self.product_box.selected_id()
        if not product_id:
            self.info_label.configure(text="")
            return
        p = self.app.products.get(product_id)
        price = p["sale_price"] if self.is_sale else p["cost_price"]
        set_entry(self.price_entry, f"{price:.2f}")
        if not self.qty_entry.get():
            set_entry(self.qty_entry, "1")
        self.info_label.configure(text=f"In stock: {p['quantity']}   |   "
                                       f"Reorder level: {p['reorder_level']}")

    @guarded
    def add_item(self) -> None:
        product_id = self.product_box.selected_id()
        if not product_id:
            raise ValidationError("Please select a product.")
        qty = Validate.integer(self.qty_entry.get(), "Quantity", 1)
        price = Validate.money(self.price_entry.get(), "Price")
        product = self.app.products.get(product_id)
        existing = next((i for i in self.cart if i["product_id"] == product_id), None)
        total_qty = qty + (existing["quantity"] if existing else 0)
        if self.is_sale and total_qty > product["quantity"]:
            raise InsufficientStockError(
                f"Only {product['quantity']} unit(s) of '{product['name']}' are in stock.")
        if existing:
            existing["quantity"], existing["price"] = total_qty, price
        else:
            self.cart.append({"product_id": product_id, "name": product["name"],
                              "quantity": qty, "price": price})
        set_entry(self.qty_entry, "1")
        self._redraw_cart()

    @guarded
    def remove_item(self) -> None:
        selected = self.cart_table.selected()
        if not selected:
            raise ValidationError("Select a cart row first.")
        self.cart = [i for i in self.cart if i["product_id"] != selected]
        self._redraw_cart()

    def clear_cart(self) -> None:
        self.cart.clear()
        set_entry(self.notes_entry, "")
        self._redraw_cart()

    def _redraw_cart(self) -> None:
        self.cart_table.load([
            (i["product_id"], (i["name"], i["quantity"], f"{i['price']:,.2f}",
                               f"{i['quantity'] * i['price']:,.2f}")) for i in self.cart])
        total = sum(i["quantity"] * i["price"] for i in self.cart)
        self.total_label.configure(text=f"Total: {fmt_money(total)}")

    @guarded
    def save(self) -> None:
        items = [LineItem(i["product_id"], i["quantity"], i["price"]) for i in self.cart]
        notes = self.notes_entry.get()
        if self.is_sale:
            result = self.app.sales.create_sale(self.party_entry.get(), items, notes)
            messagebox.showinfo("Sale completed",
                                f"Sale #{result.sale_id} saved.\nTotal: {fmt_money(result.total)}")
            if result.low_stock:
                messagebox.showwarning("Low stock alert",
                                       "These products need restocking:\n\n• "
                                       + "\n• ".join(result.low_stock))
            set_entry(self.party_entry, "")
        else:
            purchase_id = self.app.purchases.create_purchase(self.party_box.selected_id(),
                                                             items, notes)
            messagebox.showinfo("Purchase saved",
                                f"Purchase #{purchase_id} saved and stock updated.")
        self.clear_cart()
        self.refresh()

    @guarded
    def load_history(self) -> None:
        start, end = self.date_range.values()
        rows = self.manager.list(start, end)
        self.history_table.load([
            (r["id"], (r["id"], r["date"], r["party"], r["items"], f"{r['total']:,.2f}",
                       r["notes"])) for r in rows])
        self.history_total.configure(
            text=f"{len(rows)} record(s)  |  Total: {fmt_money(sum(r['total'] for r in rows))}")

    @guarded
    def view_items(self, trade_id: str | None) -> None:
        if not trade_id:
            raise ValidationError("Select a record in the history table first.")
        kind = "Sale" if self.is_sale else "Purchase"
        ItemsDialog(self.app, f"{kind} #{trade_id} items", self.manager.items_for(int(trade_id)))


class PurchasesPage(TradePage):
    PAGE_TITLE = "Purchase Management"
    MODE = "purchase"


class SalesPage(TradePage):
    PAGE_TITLE = "Sales Management"
    MODE = "sale"


class InventoryPage(BasePage):
    PAGE_TITLE = "Inventory & Stock Movements"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        panel = Panel(self, "")
        panel.grid(row=0, column=0, sticky="n", padx=(18, 8), pady=18)
        form = FormBuilder(panel.body)
        form.title("Manual Stock Adjustment")
        self.product_box = form.choice("Product", "Select product",
                                       command=lambda _v: self.on_product())
        self.info_label = form.info("")
        self.qty_entry = form.entry("Quantity", "e.g. 25")
        self.note_entry = form.entry("Note / reason", "e.g. Returned item, damaged goods")
        form.buttons([("Stock In", lambda: self.move(True), "success"),
                      ("Stock Out", lambda: self.move(False), "danger")])

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 18), pady=18)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        toolbar = ctk.CTkFrame(right, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.filter_box = ChoiceBox(toolbar, "All Products", width=220,
                                    command=lambda _v: self.load_history())
        self.filter_box.pack(side="left", padx=(0, 12))
        self.date_range = DateRange(toolbar, self.load_history)
        self.date_range.pack(side="left")
        self.table = DataTable(right, [
            ("Date", 140), ("Product", 170), ("Type", 90), ("Change", 65, "e"),
            ("Balance", 70, "e"), ("Reference", 90), ("Note", 160)])
        self.table.grid(row=1, column=0, sticky="nsew")

    def refresh(self) -> None:
        products = [(p["id"], f"{p['name']} ({short_id(p['id'])})")
                    for p in self.app.products.search()]
        self.product_box.set_choices(products)
        self.filter_box.set_choices([(None, "All Products")] + products, default_first=True)
        self.on_product()
        self.load_history()

    @guarded
    def on_product(self) -> None:
        product_id = self.product_box.selected_id()
        if product_id:
            p = self.app.products.get(product_id)
            self.info_label.configure(text=f"Current stock: {p['quantity']}  "
                                           f"(reorder level {p['reorder_level']})")
        else:
            self.info_label.configure(text="")

    @guarded
    def move(self, incoming: bool) -> None:
        product_id = self.product_box.selected_id()
        manager = self.app.inventory
        name = self.app.products.get(product_id)["name"] if product_id else ""
        if incoming:
            new_qty = manager.stock_in(product_id, self.qty_entry.get(), self.note_entry.get())
        else:
            new_qty = manager.stock_out(product_id, self.qty_entry.get(), self.note_entry.get())
        set_entry(self.qty_entry, "")
        set_entry(self.note_entry, "")
        self.on_product()
        self.load_history()
        messagebox.showinfo("Stock updated", f"'{name}' now has {new_qty} unit(s) in stock.")
        if not incoming and manager.is_low(product_id):
            messagebox.showwarning("Low stock alert",
                                   f"'{name}' is at or below its reorder level ({new_qty} left).")

    @guarded
    def load_history(self) -> None:
        start, end = self.date_range.values()
        rows = self.app.inventory.history(self.filter_box.selected_id(), start, end)
        self.table.load([
            (r["id"], (r["created_at"], r["product"], r["movement_type"].replace("_", " "),
                       f"{r['quantity_change']:+d}", r["quantity_after"], r["reference"],
                       r["note"])) for r in rows])


class ReportsPage(BasePage):
    PAGE_TITLE = "Reports"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.headers: list[str] = []
        self.rows: list[tuple] = []

        toolbar = Panel(self, "")
        toolbar.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 10))
        inner = ctk.CTkFrame(toolbar.body, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="w", padx=14, pady=(0, 14))
        self.type_box = ctk.CTkComboBox(inner, values=ReportManager.REPORT_TYPES,
                                        state="readonly", width=200, height=34)
        self.type_box.set(ReportManager.REPORT_TYPES[0])
        self.type_box.pack(side="left", padx=(0, 14))
        self.date_range = DateRange(inner, self.generate)
        self.date_range.pack(side="left", padx=(0, 14))
        make_button(inner, "Generate", self.generate, "primary", 100).pack(side="left", padx=4)
        make_button(inner, "Export CSV", self.export, "success", 110).pack(side="left", padx=4)

        table_panel = Panel(self, "Report Preview")
        table_panel.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))
        table_panel.body.grid_rowconfigure(0, weight=1)
        self.table = DataTable(table_panel.body, [("Click Generate to build a report", 400)])
        self.table.grid(row=0, column=0, sticky="nsew", padx=12, pady=(0, 6))
        self.count_label = ctk.CTkLabel(table_panel.body, text="", font=ui_font(12),
                                        text_color=Theme.MUTED, anchor="w")
        self.count_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))

    def refresh(self) -> None:
        self.generate()

    @guarded
    def generate(self) -> None:
        start, end = self.date_range.values()
        self.headers, self.rows = self.app.reports.build(self.type_box.get(), start, end)
        self.table.set_columns([(h, max(90, len(h) * 11)) for h in self.headers])
        self.table.load([
            (None, tuple(f"{v:,.2f}" if isinstance(v, float) else v for v in row))
            for row in self.rows])
        self.count_label.configure(text=f"{len(self.rows)} row(s)")

    @guarded
    def export(self) -> None:
        if not self.headers:
            self.generate()
        if not self.rows:
            raise ValidationError("There is no data to export for this report.")
        default = f"{self.type_box.get().replace(' ', '_').lower()}_{datetime.now():%Y%m%d}.csv"
        path = filedialog.asksaveasfilename(
            title="Export report", defaultextension=".csv", initialfile=default,
            filetypes=[("CSV file", "*.csv")])
        if path:
            count = self.app.reports.export_csv(path, self.headers, self.rows)
            messagebox.showinfo("Export complete", f"{count} row(s) saved to:\n{path}")


class SettingsPage(BasePage):
    PAGE_TITLE = "Backup & Settings"

    def __init__(self, master, app) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure((0, 1), weight=1, uniform="s")
        backup = Panel(self, "Backup & Restore")
        backup.grid(row=0, column=0, sticky="new", padx=(18, 8), pady=18)
        form = FormBuilder(backup.body)
        form.info("Create a copy of all your data, or restore a previous copy. "
                  "A safety backup is made automatically before every restore.")
        self.path_label = form.info(f"Database: {app.db.path}")
        form.buttons([("Backup now", self.backup_now, "primary"),
                      ("Backup as...", self.backup_as, "secondary"),
                      ("Restore...", self.restore, "danger")])

        security = Panel(self, "Change Password")
        security.grid(row=0, column=1, sticky="new", padx=(8, 18), pady=18)
        sform = FormBuilder(security.body)
        self.old_entry = sform.entry("Current password", show="•")
        self.new_entry = sform.entry("New password (min 8 characters)", show="•")
        self.confirm_entry = sform.entry("Confirm new password", show="•")
        sform.buttons([("Update password", self.change_password, "success")])

        about = Panel(self, "About")
        about.grid(row=1, column=0, columnspan=2, sticky="new", padx=18, pady=(0, 18))
        FormBuilder(about.body).info(
            f"{APP_NAME}  v{APP_VERSION}\nLog file: {LOG_DIR / 'inventory.log'}\n"
            f"Backups folder: {BACKUP_DIR}")

    @guarded
    def backup_now(self) -> None:
        path = self.app.db.backup()
        messagebox.showinfo("Backup complete", f"Backup saved to:\n{path}")

    @guarded
    def backup_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save backup as", defaultextension=".db",
            initialfile=f"inventory_backup_{datetime.now():%Y%m%d_%H%M%S}.db",
            filetypes=[("SQLite database", "*.db")])
        if path:
            self.app.db.backup(path)
            messagebox.showinfo("Backup complete", f"Backup saved to:\n{path}")

    @guarded
    def restore(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a backup file",
            filetypes=[("SQLite database", "*.db *.sqlite *.sqlite3"), ("All files", "*.*")])
        if not path:
            return
        if messagebox.askyesno("Confirm restore",
                               "This replaces ALL current data with the backup.\n"
                               "A safety copy of the current data will be made first.\n\n"
                               "Continue?"):
            self.app.db.restore(path)
            self.app.refresh_all()
            messagebox.showinfo("Restore complete", "Your data was restored successfully.")

    @guarded
    def change_password(self) -> None:
        self.app.auth.change_password(self.old_entry.get(), self.new_entry.get(),
                                      self.confirm_entry.get())
        for entry in (self.old_entry, self.new_entry, self.confirm_entry):
            set_entry(entry, "")
        messagebox.showinfo("Password updated", "Your password was changed successfully.")


class LoginFrame(ctk.CTkFrame):
    """Split-screen login card."""

    def __init__(self, master, auth: AuthenticationManager, on_success: Callable) -> None:
        super().__init__(master, fg_color=Theme.BG)
        self.auth = auth
        self.on_success = on_success

        card = ctk.CTkFrame(self, width=860, height=500, corner_radius=16, fg_color="white",
                            border_width=1, border_color=Theme.BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")
        card.grid_propagate(False)
        card.grid_columnconfigure((0, 1), weight=1, uniform="half")
        card.grid_rowconfigure(0, weight=1)

        brand = ctk.CTkFrame(card, corner_radius=16, fg_color=Theme.SIDEBAR)
        brand.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        brand.grid_rowconfigure((0, 4), weight=1)
        brand.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(brand, text="📦", font=ui_font(56)).grid(row=1, column=0, pady=(0, 8))
        ctk.CTkLabel(brand, text="Advanced\nInventory System", font=ui_font(26, "bold"),
                     text_color="white", justify="center").grid(row=2, column=0)
        ctk.CTkLabel(brand, text="Track stock, sales and purchases\nin one professional dashboard.",
                     font=ui_font(13), text_color="#94a3b8", justify="center").grid(
            row=3, column=0, pady=(12, 0))

        form = ctk.CTkFrame(card, fg_color="transparent")
        form.grid(row=0, column=1, sticky="nsew", padx=40)
        form.grid_columnconfigure(0, weight=1)
        form.grid_rowconfigure((0, 9), weight=1)
        ctk.CTkLabel(form, text="Welcome back", font=ui_font(26, "bold"),
                     text_color=Theme.TEXT, anchor="w").grid(row=1, column=0, sticky="ew")
        ctk.CTkLabel(form, text="Sign in to your admin account", font=ui_font(13),
                     text_color=Theme.MUTED, anchor="w").grid(row=2, column=0, sticky="ew",
                                                              pady=(0, 20))
        self.username_entry = ctk.CTkEntry(form, height=40, placeholder_text="Username",
                                           font=ui_font(14))
        self.username_entry.grid(row=3, column=0, sticky="ew", pady=6)
        self.password_entry = ctk.CTkEntry(form, height=40, placeholder_text="Password",
                                           show="•", font=ui_font(14))
        self.password_entry.grid(row=4, column=0, sticky="ew", pady=6)
        self.error_label = ctk.CTkLabel(form, text="", text_color=Theme.DANGER,
                                        font=ui_font(12), anchor="w", wraplength=330)
        self.error_label.grid(row=5, column=0, sticky="ew")
        make_button(form, "Sign In", self.login, "primary", 120).grid(
            row=6, column=0, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(form, text="First run? Use  admin / Admin@123  and change the\n"
                                "password afterwards in Backup & Settings.",
                     font=ui_font(11), text_color=Theme.MUTED, justify="left",
                     anchor="w").grid(row=7, column=0, sticky="ew", pady=(16, 0))
        self.username_entry.bind("<Return>", lambda _e: self.password_entry.focus())
        self.password_entry.bind("<Return>", lambda _e: self.login())
        self.after(250, self.username_entry.focus)

    def login(self) -> None:
        try:
            self.auth.login(self.username_entry.get(), self.password_entry.get())
        except InventoryError as exc:
            self.error_label.configure(text=str(exc))
            return
        except Exception:
            log.exception("Login failed unexpectedly")
            self.error_label.configure(text="Unexpected error. See logs/inventory.log.")
            return
        self.password_entry.delete(0, "end")
        self.error_label.configure(text="")
        self.on_success()


# =============================================================================
# 6. MAIN APPLICATION
# =============================================================================
class MainApplication(ctk.CTk):
    """Creates the managers, the window, the sidebar/top bar and switches pages."""

    NAV_ITEMS = [
        ("dashboard", "📊  Dashboard", DashboardPage),
        ("products", "📦  Products", ProductsPage),
        ("categories", "🗂  Categories", CategoriesPage),
        ("suppliers", "🚚  Suppliers", SuppliersPage),
        ("purchases", "🛒  Purchases", PurchasesPage),
        ("sales", "💳  Sales", SalesPage),
        ("inventory", "🏬  Inventory", InventoryPage),
        ("reports", "📑  Reports", ReportsPage),
        ("settings", "⚙  Backup & Settings", SettingsPage),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.configure(fg_color=Theme.BG)
        self.minsize(1180, 700)
        center_window(self, 1320, 780)
        self._style_tables()

        # Managers (the business-logic layer) shared by every page
        self.db = DatabaseManager()
        self.auth = AuthenticationManager(self.db)
        self.categories = CategoryManager(self.db)
        self.suppliers = SupplierManager(self.db)
        self.inventory = InventoryManager(self.db)
        self.products = ProductManager(self.db, self.inventory)
        self.purchases = PurchaseManager(self.db, self.inventory)
        self.sales = SalesManager(self.db, self.inventory)
        self.reports = ReportManager(self.db, self.inventory)

        self.pages: dict[str, BasePage] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.current_page: BasePage | None = None
        self.shell: ctk.CTkFrame | None = None
        self.clock_job: str | None = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_login()

    def _style_tables(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background="white", fieldbackground="white",
                        foreground=Theme.TEXT, rowheight=28, borderwidth=0,
                        font=(Theme.FONT, 10))
        style.configure("Treeview.Heading", background="#e2e8f0", foreground=Theme.TEXT,
                        font=(Theme.FONT, 10, "bold"), relief="flat", padding=6)
        style.map("Treeview", background=[("selected", Theme.PRIMARY)],
                  foreground=[("selected", "white")])
        style.map("Treeview.Heading", background=[("active", "#cbd5e1")])

    # ---- screens -----------------------------------------------------------
    def _clear_shell(self) -> None:
        if self.clock_job:
            try:
                self.after_cancel(self.clock_job)
            except tk.TclError:
                pass
            self.clock_job = None
        if self.shell is not None:
            self.shell.destroy()
        self.shell = None
        self.pages.clear()
        self.nav_buttons.clear()
        self.current_page = None

    def show_login(self) -> None:
        self._clear_shell()
        self.shell = LoginFrame(self, self.auth, self.show_main)
        self.shell.pack(fill="both", expand=True)

    def show_main(self) -> None:
        self._clear_shell()
        shell = ctk.CTkFrame(self, fg_color=Theme.BG)
        shell.pack(fill="both", expand=True)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(0, weight=1)
        self.shell = shell

        sidebar = ctk.CTkFrame(shell, width=230, corner_radius=0, fg_color=Theme.SIDEBAR)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(20, weight=1)
        ctk.CTkLabel(sidebar, text="📦  InvenTrack", font=ui_font(20, "bold"),
                     text_color="white", anchor="w").grid(row=0, column=0, sticky="ew",
                                                         padx=20, pady=(24, 24))
        for index, (key, label, _page) in enumerate(self.NAV_ITEMS, start=1):
            button = ctk.CTkButton(
                sidebar, text=label, anchor="w", height=42, corner_radius=8,
                fg_color="transparent", hover_color=Theme.SIDEBAR_HOVER,
                text_color="#e2e8f0", font=ui_font(14),
                command=lambda k=key: self.show_page(k))
            button.grid(row=index, column=0, sticky="ew", padx=12, pady=2)
            self.nav_buttons[key] = button
        ctk.CTkLabel(sidebar, text=f"v{APP_VERSION}", font=ui_font(11),
                     text_color="#64748b").grid(row=21, column=0, pady=(0, 16))

        right = ctk.CTkFrame(shell, fg_color=Theme.BG, corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        topbar = ctk.CTkFrame(right, height=64, corner_radius=0, fg_color="white",
                              border_width=1, border_color=Theme.BORDER)
        topbar.grid(row=0, column=0, sticky="ew")
        topbar.grid_columnconfigure(1, weight=1)
        topbar.grid_propagate(False)
        self.page_title = ctk.CTkLabel(topbar, text="", font=ui_font(20, "bold"),
                                       text_color=Theme.TEXT)
        self.page_title.grid(row=0, column=0, padx=24, pady=16, sticky="w")
        self.clock_label = ctk.CTkLabel(topbar, text="", font=ui_font(12), text_color=Theme.MUTED)
        self.clock_label.grid(row=0, column=1, padx=10, sticky="e")
        username = self.auth.current_user["username"] if self.auth.current_user else "admin"
        ctk.CTkLabel(topbar, text=f"👤  {username}", font=ui_font(13, "bold"),
                     text_color=Theme.TEXT).grid(row=0, column=2, padx=10)
        make_button(topbar, "Logout", self.logout, "danger", 90).grid(row=0, column=3, padx=(0, 20))

        container = ctk.CTkFrame(right, fg_color=Theme.BG, corner_radius=0)
        container.grid(row=1, column=0, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        for key, _label, page_class in self.NAV_ITEMS:
            self.pages[key] = page_class(container, self)

        self._tick()
        self.show_page("dashboard")

    def show_page(self, key: str) -> None:
        if self.current_page is not None:
            self.current_page.grid_remove()
        page = self.pages[key]
        page.grid(row=0, column=0, sticky="nsew")
        self.current_page = page
        self.page_title.configure(text=page.PAGE_TITLE)
        for name, button in self.nav_buttons.items():
            button.configure(fg_color=Theme.PRIMARY if name == key else "transparent")
        try:
            page.refresh()
        except Exception:
            log.exception("Could not refresh page '%s'", key)
            messagebox.showerror("Error", "This page could not load its data. "
                                          "See logs/inventory.log.")

    def refresh_all(self) -> None:
        if self.current_page is not None:
            self.current_page.refresh()

    def _tick(self) -> None:
        self.clock_label.configure(text=datetime.now().strftime("%A, %d %B %Y   %I:%M:%S %p"))
        self.clock_job = self.after(1000, self._tick)

    def logout(self) -> None:
        if messagebox.askyesno("Logout", "Do you want to log out?"):
            self.auth.logout()
            self.show_login()

    def on_close(self) -> None:
        log.info("Application closed.")
        try:
            self.db.close()
        finally:
            self.destroy()


def main() -> None:
    setup_logging()
    log.info("Starting %s v%s", APP_NAME, APP_VERSION)
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    try:
        MainApplication().mainloop()
    except Exception:
        log.exception("Fatal error")
        raise


if __name__ == "__main__":
    main()
