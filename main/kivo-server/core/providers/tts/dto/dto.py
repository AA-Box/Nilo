from enum import Enum
from typing import Union, Optional


class SentenceType(Enum):
    # Speech stage
    FIRST = "FIRST"  # first sentence
    MIDDLE = "MIDDLE"  # mid-speech
    LAST = "LAST"  # last sentence


class ContentType(Enum):
    # Content type
    TEXT = "TEXT"  # text content
    FILE = "FILE"  # file content
    ACTION = "ACTION"  # action content


class InterfaceType(Enum):
    # Interface type
    DUAL_STREAM = "DUAL_STREAM"  # dual-stream
    SINGLE_STREAM = "SINGLE_STREAM"  # single-stream
    NON_STREAM = "NON_STREAM"  # non-streaming


class TTSMessageDTO:
    def __init__(
        self,
        sentence_id: str,
        # Speech stage
        sentence_type: SentenceType,
        # Content type
        content_type: ContentType,
        # Content details, usually the text to synthesize or the audio's lyrics
        content_detail: Optional[str] = None,
        # File path, required when the content type is FILE
        content_file: Optional[str] = None,
    ):
        self.sentence_id = sentence_id
        self.sentence_type = sentence_type
        self.content_type = content_type
        self.content_detail = content_detail
        self.content_file = content_file
