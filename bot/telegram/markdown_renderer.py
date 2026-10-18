"""Render the bridge's small Markdown dialect as safe Telegram HTML."""

from dataclasses import dataclass
import html
import re


MAX_MESSAGE_UNITS = 4000
_LANGUAGE = re.compile(r"^[A-Za-z0-9_+#.-]{1,32}$")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_ESCAPABLE = set("\\`*_{}[]()#+-.!|>~&<>")


@dataclass(frozen=True)
class RenderedMessage:
    """One independently valid Telegram message and its plain-text fallback."""

    html: str
    plain: str


def _units(text):
    """Count UTF-16 code units, the unit Telegram uses for message entities."""
    return len(text.encode("utf-16-le")) // 2


def _find_unescaped(text, marker, start):
    index = start
    while True:
        index = text.find(marker, index)
        if index < 0:
            return -1
        backslashes = 0
        previous = index - 1
        while previous >= 0 and text[previous] == "\\":
            backslashes += 1
            previous -= 1
        if backslashes % 2 == 0:
            return index
        index += len(marker)


def _text(value):
    return ("text", value, "")


def _tag(kind, name, markup=""):
    return (kind, name, markup)


def _inline_events(source):
    events = []
    plain = []

    def emit_text(value):
        if value:
            events.append(_text(value))
            plain.append(value)

    index = 0
    while index < len(source):
        if source[index] == "\\" and index + 1 < len(source) and source[index + 1] in _ESCAPABLE:
            emit_text(source[index + 1])
            index += 2
            continue

        if source[index] == "`":
            end = _find_unescaped(source, "`", index + 1)
            if end > index + 1:
                events.append(_tag("open", "code", "<code>"))
                events.append(_text(source[index + 1 : end]))
                events.append(_tag("close", "code", ""))
                plain.append(source[index + 1 : end])
                index = end + 1
                continue

        if source[index] == "[":
            match = _LINK.match(source, index)
            if match:
                label, url = match.groups()
                # Telegram automatically links HTTP(S) URLs. Showing the target
                # explicitly also keeps it available in the plain-text fallback.
                visible = f"{label} ({url})"
                emit_text(visible)
                index = match.end()
                continue

        marker = next(
            (candidate for candidate in ("**", "~~", "*") if source.startswith(candidate, index)),
            None,
        )
        if marker:
            end = _find_unescaped(source, marker, index + len(marker))
            if end > index + len(marker):
                content = source[index + len(marker) : end]
                if content[0].isspace() or content[-1].isspace():
                    emit_text(source[index])
                    index += 1
                    continue
                tag_name = {"**": "b", "~~": "s", "*": "i"}[marker]
                events.append(_tag("open", tag_name, f"<{tag_name}>"))
                inner_events, inner_plain = _inline_events(content)
                events.extend(inner_events)
                events.append(_tag("close", tag_name, ""))
                plain.append(inner_plain)
                index = end + len(marker)
                continue

        emit_text(source[index])
        index += 1

    return events, "".join(plain)


def _is_fence(line):
    match = re.match(r"^ {0,3}```([A-Za-z0-9_+#.-]{0,32})\s*$", line)
    return match.group(1) if match else None


def _is_special_line(line):
    return (
        _is_fence(line) is not None
        or re.match(r"^ {0,3}#{1,6}\s+", line) is not None
        or re.match(r"^\s*>\s?", line) is not None
        or re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line) is not None
    )


def _append_inline(events, source):
    inline, _plain = _inline_events(source)
    events.extend(inline)


def _markdown_events(markdown):
    lines = markdown.splitlines()
    events = []
    index = 0
    has_block = False

    def separate_block():
        nonlocal has_block
        if has_block:
            events.append(_text("\n\n"))
        has_block = True

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        language = _is_fence(line)
        if language is not None:
            separate_block()
            index += 1
            code_lines = []
            while index < len(lines) and not re.match(r"^ {0,3}```\s*$", lines[index]):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            safe_language = language if _LANGUAGE.fullmatch(language) else ""
            code_tag = (
                f'<code class="language-{safe_language}">'
                if safe_language
                else "<code>"
            )
            events.append(_tag("open", "pre", "<pre>"))
            events.append(_tag("open", "code", code_tag))
            events.append(_text("\n".join(code_lines)))
            events.append(_tag("close", "code", ""))
            events.append(_tag("close", "pre", ""))
            continue

        heading = re.match(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading:
            separate_block()
            events.append(_tag("open", "b", "<b>"))
            _append_inline(events, heading.group(1))
            events.append(_tag("close", "b", ""))
            index += 1
            continue

        if re.match(r"^\s*>", line):
            separate_block()
            quote_lines = []
            while index < len(lines) and re.match(r"^\s*>", lines[index]):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[index], count=1))
                index += 1
            for quote_index, quote_line in enumerate(quote_lines):
                if quote_index:
                    events.append(_text("\n"))
                events.append(_text("│ "))
                _append_inline(events, quote_line)
            continue

        list_match = re.match(r"^\s*(?:([-+*])|(\d+)[.)])\s+(.*)$", line)
        if list_match:
            separate_block()
            list_item_index = 0
            while index < len(lines):
                item = re.match(r"^\s*(?:([-+*])|(\d+)[.)])\s+(.*)$", lines[index])
                if not item:
                    break
                if list_item_index:
                    events.append(_text("\n"))
                prefix = f"{item.group(2)}. " if item.group(2) else "• "
                events.append(_text(prefix))
                _append_inline(events, item.group(3))
                index += 1
                list_item_index += 1
            continue

        separate_block()
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not _is_special_line(lines[index]):
            paragraph.append(lines[index])
            index += 1
        for paragraph_index, paragraph_line in enumerate(paragraph):
            if paragraph_index:
                events.append(_text("\n"))
            _append_inline(events, paragraph_line)

    return events


def _split_text(text, capacity):
    """Split at a nearby whitespace where possible, without losing characters."""
    used = 0
    cut = 0
    for character in text:
        size = _units(character)
        if used + size > capacity:
            break
        used += size
        cut += 1
    if cut == 0:
        return 0
    if cut < len(text):
        boundary = max(text.rfind(" ", 0, cut), text.rfind("\n", 0, cut))
        if boundary >= cut // 2:
            cut = boundary + 1
    return cut


def _chunk_events(events, max_units):
    chunks = []
    html_parts = []
    plain_parts = []
    open_tags = []
    visible_units = 0

    def flush():
        nonlocal html_parts, plain_parts, visible_units
        if not plain_parts:
            return
        closing = "".join(f"</{name}>" for name, _markup in reversed(open_tags))
        chunks.append(RenderedMessage("".join(html_parts) + closing, "".join(plain_parts)))
        html_parts = [markup for _name, markup in open_tags]
        plain_parts = []
        visible_units = 0

    for kind, value, markup in events:
        if kind == "open":
            html_parts.append(markup)
            open_tags.append((value, markup))
        elif kind == "close":
            html_parts.append(f"</{value}>")
            if open_tags and open_tags[-1][0] == value:
                open_tags.pop()
        else:
            remaining = value
            while remaining:
                capacity = max_units - visible_units
                if capacity <= 0:
                    flush()
                    capacity = max_units
                cut = _split_text(remaining, capacity)
                if cut == 0:
                    flush()
                    continue
                piece = remaining[:cut]
                html_parts.append(html.escape(piece, quote=False))
                plain_parts.append(piece)
                visible_units += _units(piece)
                remaining = remaining[cut:]
                if remaining:
                    flush()

    flush()
    return chunks


def render_markdown(markdown, max_units=MAX_MESSAGE_UNITS):
    """Render the supported Markdown subset into balanced Telegram HTML chunks."""
    if max_units < 2:
        raise ValueError("max_units must be at least 2 UTF-16 code units")
    return _chunk_events(_markdown_events(markdown), max_units)
