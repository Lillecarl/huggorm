"""
Source of truth for RPC services.

These are Python subclasses of Cython types from fake_library.
At Nix build time, generator/generate.py introspects them and emits
Python code via `ast` (not string templating) into fake_library_generated/.

When you write code in fake-library-python (or any downstream package),
you import the generated stubs from fake_library_generated — they are
already present via Nix propagatedBuildInputs.
"""

from fake_library import Animal, Ball, Poop
from fake_library import Cat as _Cat

# Simple marker — replicated from fake_library_python.rpc.rpc but kept
# local to avoid a Nix cycle (generated cannot depend on fake-library-python).


def rpc(func):
    func._is_rpc = True  # type: ignore[attr-defined]
    return func


def rpc_service(threading: str = "affine"):
    """
    Declare the threading model for a service.

    - "affine": the C++ object is not thread-safe. It is constructed on a
      dedicated thread and every operation executes there.
    - "pool": the C++ object is thread-safe. Operations run on a shared
      thread pool.
    """
    def deco(cls):
        cls._threading = threading  # type: ignore[attr-defined]
        return cls

    return deco


# --- Typed, serializable errors (the IDL's exception vocabulary) ---
# The runtime recognizes errors by duck-typing: anything with to_dict()
# passes through untouched. These will serialize over RPC later.


class ServiceError(Exception):
    code = "service_error"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class FetchError(ServiceError):
    """Raised for unknown fetch items."""

    code = "fetch_unknown"


class NameRequiredError(ServiceError):
    """Raised when a name argument is empty."""

    code = "name_required"


@rpc_service(threading="affine")
class Cat(_Cat):
    """Cat service. Not thread-safe -> affine."""

    @rpc
    def greet(self, whom: str) -> str:
        """Greet someone. Raises NameRequiredError on empty whom."""
        if not whom:
            raise NameRequiredError("whom must not be empty")
        return f"{self.speak()} to {whom}"

    @rpc
    def fetch(self, item: str) -> str:
        """Fetch an item via C++. C++ throws std::invalid_argument on unknown items."""
        return super().fetch(item)

    @rpc
    def lives_remaining(self) -> int:
        """Hypothetical RPC method."""
        return 9

    @rpc
    def poop(self) -> Poop:
        """Produce poop. Returned type is affine: its ops run on this cat's thread."""
        return super().poop()

    @rpc
    def toy(self) -> Ball:
        """Produce a toy. Returned type is thread-safe (pool)."""
        return super().toy()

    @rpc
    def wait(self, ms: int) -> None:
        """Slow C++ sleep; releases the GIL while running."""
        super().wait_ms(ms)


@rpc_service(threading="pool")
class Spider(Animal):
    """Spider service. Thread-safe -> pool."""

    def speak(self) -> str:
        return "hisss"

    def legs(self) -> int:
        return 8

    @rpc
    def crawl(self, meters: float) -> str:
        """Ask the spider to crawl."""
        return f"{self.name} crawls {meters}m"

    @rpc
    def bite(self, target: str) -> bool:
        return target == "fly"

    @rpc
    def wait(self, ms: int) -> None:
        """Slow C++ sleep; releases the GIL while running."""
        super().wait_ms(ms)


# Registry of services to codegen. Add new services here.
SERVICES = [Cat, Spider]

# Error vocabulary exposed in the manifest for tooling/codegen.
ERRORS = [ServiceError, FetchError, NameRequiredError]
