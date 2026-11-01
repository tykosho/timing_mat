from abc import ABC, abstractmethod
from typing import Callable
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
