"""
Folder watcher / email queue for new .txt email files.
=======================================================

WHAT THIS FILE DOES:
    Manages a simple FIFO queue of email files from the inbox directory.
    It scans for .txt files, lets you process them one at a time, and
    moves each file to a "processed" folder when you're done with it.

HOW THE EMAIL PROCESSING FLOW WORKS:
    1. Drop .txt email files into `data/inbox/`
    2. Call queue.scan() — discovers new files and adds them to the queue
    3. Call queue.peek() — see the next file name without removing it
    4. Call queue.read_file(name) — read the email content
    5. Process the email (extract fields, review, accept)
    6. Call queue.move_to_processed(name) — move file to `data/processed/`
    7. Call queue.pop() — remove from the queue
    8. Repeat from step 3 until queue is empty

HOW TO ADAPT TO YOUR ENVIRONMENT:
    - If your emails come from Outlook or another source, you'd write a
      script that saves them as .txt files in the inbox directory.
    - The file format can be plain text or HTML — the extractor handles both.
    - If you want to watch for new files automatically (instead of manual scan),
      you could add a filesystem watcher using `watchdog` library.
    - If you want to support .eml or .msg files, add a converter in read_file().
"""

import os
import logging
import shutil
from collections import deque

from backend import config

logger = logging.getLogger(__name__)


class EmailQueue:
    """
    Manages a queue of .txt email files from the inbox directory.

    The queue is an in-memory FIFO (first-in, first-out). Files are
    sorted alphabetically so they process in a predictable order
    (tip: prefix filenames with numbers, e.g. email_001.txt).

    Attributes:
        _queue   — deque of filenames waiting to be processed
        _skipped — set of filenames the user chose to skip (won't be re-queued)
    """

    def __init__(self):
        self._queue: deque[str] = deque()
        self._skipped: set[str] = set()
        self.scan()

    def scan(self) -> None:
        """
        Scan the inbox directory for new .txt files.

        Only adds files that aren't already in the queue or previously skipped.
        Safe to call multiple times — won't duplicate entries.
        """
        os.makedirs(config.INBOX_DIR, exist_ok=True)
        existing = set(self._queue) | self._skipped
        files = sorted(
            f for f in os.listdir(config.INBOX_DIR)
            if f.endswith(".txt") and f not in existing
        )
        for f in files:
            self._queue.append(f)
        logger.info(f"Email queue scanned: {len(self._queue)} files queued")

    def peek(self) -> str | None:
        """Return the next filename without removing it from the queue."""
        return self._queue[0] if self._queue else None

    def pop(self) -> str | None:
        """Remove and return the next filename from the queue."""
        return self._queue.popleft() if self._queue else None

    def skip(self) -> str | None:
        """
        Skip the current email — removes it from the queue and adds it
        to the skipped set so it won't be re-queued on next scan().
        """
        if self._queue:
            name = self._queue.popleft()
            self._skipped.add(name)
            return name
        return None

    def remaining(self) -> list[str]:
        """Return all filenames still in the queue (in order)."""
        return list(self._queue)

    def size(self) -> int:
        """Number of emails remaining in the queue."""
        return len(self._queue)

    def read_file(self, filename: str) -> str:
        """Read the full text content of an email file from the inbox directory."""
        path = os.path.join(config.INBOX_DIR, filename)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def move_to_processed(self, filename: str) -> None:
        """
        Move a file from inbox/ to processed/.

        This is called after the user accepts an extraction. The file is
        preserved (not deleted) so you have a record of what was processed.
        """
        os.makedirs(config.PROCESSED_DIR, exist_ok=True)
        src = os.path.join(config.INBOX_DIR, filename)
        dst = os.path.join(config.PROCESSED_DIR, filename)
        if os.path.exists(src):
            shutil.move(src, dst)
            logger.info(f"Moved {filename} to processed/")
