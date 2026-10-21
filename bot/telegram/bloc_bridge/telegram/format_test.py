"""Send a static Telegram formatting fixture without calling OpenCode."""

import sys

from .. import bridge
from ..bridge_config import ALLOWED


FIXTURE = """# Telegram formatting smoke test

This fixture checks **bold**, *italic*, ~~strikethrough~~, `inline code`,
an [HTTP link](https://example.com/path?q=a&lang=en), and Unicode: café 🧪.

- bullet list item
- a second item with <HTML-like text> and an ampersand &
1. numbered item
2. another numbered item

> A quoted line with **emphasis**.

```python
def hello(name):
    print(f"Hello, {name}! <safe & escaped>")
```

## Long code block (the next block should span multiple Telegram messages)

```text
""" + "line with code & special <characters> and emoji 🧪\n" * 180 + "```\n"


def send_fixture(chat_id):
    if str(chat_id) not in ALLOWED:
        raise ValueError("The requested chat is not in TELEGRAM_ALLOWED_CHAT_IDS")
    bridge.send(int(chat_id), FIXTURE)


def main(argv):
    if len(argv) != 2:
        raise SystemExit("Usage: python -m bloc_bridge.telegram.format_test <allowed-chat-id>")
    try:
        chat_id = int(argv[1])
    except ValueError as error:
        raise SystemExit("Chat ID must be an integer") from error
    send_fixture(chat_id)
    print("Static formatting fixture sent; no OpenCode request was made.")


if __name__ == "__main__":
    main(sys.argv)
