from enum import Enum
from typing import Union, Optional


class InterfaceType(Enum):
    # Interface type
    STREAM = "STREAM"  # streaming API
    NON_STREAM = "NON_STREAM"  # non-streaming API
    LOCAL = "LOCAL"  # local service
