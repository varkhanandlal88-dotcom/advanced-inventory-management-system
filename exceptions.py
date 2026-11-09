"""Domain-specific exceptions."""


class InventoryError(Exception):
    """Base class for all inventory errors."""


class ValidationError(InventoryError):
    """Raised when input data is invalid."""


class EntityNotFoundError(InventoryError):
    """Raised when a product or warehouse does not exist."""


class DuplicateEntityError(InventoryError):
    """Raised when creating an entity whose unique key already exists."""


class InsufficientStockError(InventoryError):
    """Raised when an operation needs more stock than is available."""
