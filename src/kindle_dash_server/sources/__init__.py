"""Source types. Importing this package registers the built-in ones."""

from . import (  # noqa: F401  (registration)
    almanac,
    generative,
    json_api,
    lichess,
    local_folder,
    text_file,
)
from .base import SOURCE_TYPES, FilteredOut, Source, SourceConfig, SourceContext, SourceError

__all__ = ["SOURCE_TYPES", "FilteredOut", "Source", "SourceConfig", "SourceContext", "SourceError"]
