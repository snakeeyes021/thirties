"""Which build of Thirties is running: stamped on stored messages, so a problem can be placed by version."""

BUILD = "dev"  # the app sets this at startup ("0.1 (216ebce)"); tests and tools leave it


def set_build(text: str) -> None:
    global BUILD
    BUILD = text or BUILD
