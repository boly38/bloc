"""Select correlated assistant text that is known not to be the final reply."""


class IntermediateReplyTracker:
    """Deduplicate intermediate assistant messages and hold back the newest message."""

    def __init__(self, skip_existing=False):
        self._seen_ids = set()
        self._primed = not skip_existing

    def collect(self, messages):
        """Return newly observed text from all but the newest correlated message."""
        messages = messages if isinstance(messages, list) else []
        if not self._primed:
            self._seen_ids.update(
                message_id
                for message in messages
                if (message_id := self._message_id(message)) is not None
            )
            self._primed = True
            return []

        texts = []
        for message in messages[:-1]:
            message_id = self._message_id(message)
            if message_id is None or message_id in self._seen_ids:
                continue
            self._seen_ids.add(message_id)
            text = self._text(message)
            if text:
                texts.append(text)
        return texts

    @staticmethod
    def _message_id(message):
        if not isinstance(message, dict):
            return None
        info = message.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            return None
        message_id = info.get("id")
        return message_id if isinstance(message_id, str) and message_id else None

    @staticmethod
    def _text(message):
        parts = message.get("parts")
        if not isinstance(parts, list):
            return ""
        return "\n".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and part["text"]
        ).strip()
