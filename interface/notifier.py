class CaptureNotifier:
    """Collects outgoing messages instead of sending them anywhere.

    Lets the interface backend read back what the orchestrator replied,
    the same way WhatsAppNotifier would send it to Meta.
    """

    def __init__(self):
        self.outbox: list[str] = []

    def send(self, to_number: str, text: str):
        self.outbox.append(text)

    def drain(self) -> list[str]:
        messages, self.outbox = self.outbox, []
        return messages
