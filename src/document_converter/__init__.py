"""Structure-aware, auditable document to Markdown conversion."""

from .converter import ConversionError, convert_document

__all__ = ["ConversionError", "convert_document"]
__version__ = "0.1.0"
