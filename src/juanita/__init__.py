# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn YouTube cooking videos into Mealie recipes."""
from __future__ import annotations

from juanita.cli import (
    Ingredient,
    Mealie,
    Recipe,
    extract_recipe,
    fetch_source,
    fetch_video,
    fetch_webpage,
    load_text_file,
    main,
    push_to_mealie,
    text_to_source_record,
)

__version__ = "0.2.0"

__all__ = [
    "Ingredient",
    "Mealie",
    "Recipe",
    "extract_recipe",
    "fetch_source",
    "fetch_video",
    "fetch_webpage",
    "load_text_file",
    "main",
    "push_to_mealie",
    "text_to_source_record",
    "__version__",
]
