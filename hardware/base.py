from abc import ABC, abstractmethod
from typing import Any, Callable, Dict
from core.models import TagRead

class BaseRFIDReader(ABC):
    def __init__(self, on_tag_read: Callable[[TagRead], None]):
        self.on_tag_read = on_tag_read
        self.is_running = False

    @abstractmethod
    def start(self):
        """Start listening / reading tags."""
        pass

    @abstractmethod
    def stop(self):
        """Stop reader."""
        pass

    def health(self) -> Dict[str, Any]:
        """Connection details for the admin page's reader strip (issue #48)."""
        return {"kind": type(self).__name__, "running": self.is_running, "connected": self.is_running}
