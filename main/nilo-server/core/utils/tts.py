import os
import re
import sys
import importlib

from config.logger import setup_logging
from core.utils.textUtils import check_emoji

logger = setup_logging()

punctuation_set = {
    "，",
    ",",  # Chinese + English comma
    "。",
    ".",  # Chinese + English period
    "！",
    "!",  # Chinese + English exclamation mark
    "“",
    "”",
    '"',  # Chinese + English double quotes
    "：",
    ":",  # Chinese + English colon
    "-",
    "－",  # ASCII hyphen + full-width dash
    "、",  # Chinese enumeration comma
    "[",
    "]",  # Square brackets
    "【",
    "】",  # Chinese square brackets
    "~",  # Tilde
}

def create_instance(class_name, *args, **kwargs):
    # Create a TTS instance
    if os.path.exists(os.path.join('core', 'providers', 'tts', f'{class_name}.py')):
        lib_name = f'core.providers.tts.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].TTSProvider(*args, **kwargs)

    raise ValueError(f"Unsupported TTS type: {class_name} - check the 'type' field of that config block")


class MarkdownCleaner:
    """
    Encapsulates Markdown cleanup logic: just call MarkdownCleaner.clean_markdown(text)
    """
    # Formula characters
    NORMAL_FORMULA_CHARS = re.compile(r'[a-zA-Z\\^_{}\+\-\(\)\[\]=]')

    @staticmethod
    def _replace_inline_dollar(m: re.Match) -> str:
        """
        Whenever a complete "$...$" is captured:
          - if it contains typical formula characters => strip the surrounding $
          - otherwise (plain numbers/currency etc.) => keep "$...$"
        """
        content = m.group(1)
        if MarkdownCleaner.NORMAL_FORMULA_CHARS.search(content):
            return content
        else:
            return m.group(0)

    @staticmethod
    def _replace_table_block(match: re.Match) -> str:
        """
        Callback invoked when a whole table block is matched.
        """
        block_text = match.group('table_block')
        lines = block_text.strip('\n').split('\n')

        parsed_table = []
        for line in lines:
            line_stripped = line.strip()
            if re.match(r'^\|\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?$', line_stripped):
                continue
            columns = [col.strip() for col in line_stripped.split('|') if col.strip() != '']
            if columns:
                parsed_table.append(columns)

        if not parsed_table:
            return ""

        headers = parsed_table[0]
        data_rows = parsed_table[1:] if len(parsed_table) > 1 else []

        lines_for_tts = []
        if len(parsed_table) == 1:
            # Only one row
            only_line_str = ", ".join(parsed_table[0])
            lines_for_tts.append(f"Single-row table: {only_line_str}")
        else:
            lines_for_tts.append(f"Headers: {', '.join(headers)}")
            for i, row in enumerate(data_rows, start=1):
                row_str_list = []
                for col_index, cell_val in enumerate(row):
                    if col_index < len(headers):
                        row_str_list.append(f"{headers[col_index]} = {cell_val}")
                    else:
                        row_str_list.append(cell_val)
                lines_for_tts.append(f"Row {i}: {', '.join(row_str_list)}")

        return "\n".join(lines_for_tts) + "\n"

    # Pre-compile all regexes (ordered by execution frequency)
    # The replace_xxx static methods must be defined above so the list can reference them.
    REGEXES = [
        (re.compile(r'```.*?```', re.DOTALL), ''),  # Code blocks
        (re.compile(r'^#+\s*', re.MULTILINE), ''),  # Headings
        (re.compile(r'(\*\*|__)(.*?)\1'), r'\2'),  # Bold
        (re.compile(r'(\*|_)(?=\S)(.*?)(?<=\S)\1'), r'\2'),  # Italic
        (re.compile(r'!\[.*?\]\(.*?\)'), ''),  # Images
        (re.compile(r'\[(.*?)\]\(.*?\)'), r'\1'),  # Links
        (re.compile(r'^\s*>+\s*', re.MULTILINE), ''),  # Blockquotes
        (
            re.compile(r'(?P<table_block>(?:^[^\n]*\|[^\n]*\n)+)', re.MULTILINE),
            _replace_table_block
        ),
        (re.compile(r'^\s*[*+-]\s*', re.MULTILINE), '- '),  # Lists
        (re.compile(r'\$\$.*?\$\$', re.DOTALL), ''),  # Block formulas
        (
            re.compile(r'(?<![A-Za-z0-9])\$([^\n$]+)\$(?![A-Za-z0-9])'),
            _replace_inline_dollar
        ),
        (re.compile(r'\n{2,}'), '\n'),  # Extra blank lines
    ]

    @staticmethod
    def clean_markdown(text: str) -> str:
        """
        Main entry point: run all regexes in order, removing or replacing Markdown elements
        """
        for regex, replacement in MarkdownCleaner.REGEXES:
            text = regex.sub(replacement, text)

        # Strip emoji
        text = check_emoji(text)

        # Check whether the text is entirely ASCII and basic punctuation
        if text and all((c.isascii() or c.isspace() or c in punctuation_set) for c in text):
            # Keep original whitespace and return as is
            return text

        return text.strip()

def convert_percentage_to_range(percentage, min_val, max_val, base_val=None):
    """
    Convert a percentage (-100~100) to a value in the given range

    Args:
        percentage: percentage value (-100 to 100)
        min_val: minimum of the target range
        max_val: maximum of the target range
        base_val: base value (optional, defaults to the range midpoint)

    Returns:
        The converted value
    """
    percentage, min_val, max_val = float(percentage), float(min_val), float(max_val)
    base_val = float(base_val) if base_val is not None else (min_val + max_val) / 2

    if percentage < 0:
        # Negative percentage: interpolate linearly from base_val towards min_val
        result = base_val + (base_val - min_val) * (percentage / 100)
    else:
        # Positive percentage: interpolate linearly from base_val towards max_val
        result = base_val + (max_val - base_val) * (percentage / 100)

    # Clamp the result to the valid range
    return max(min_val, min(max_val, result))
