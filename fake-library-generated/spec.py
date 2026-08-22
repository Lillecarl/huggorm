"""
Source of truth for RPC services.

These are Python subclasses of Cython types from fake_library.
At Nix build time, generator/generate.py introspects them and emits
Python code via `ast` (not string templating) into fake_library_generated/.

When you write code in fake-library-python (or any downstream package),
you import the generated stubs from fake_library_generated — they are
already present via Nix propagatedBuildInputs.
"""

from fake_library import Animal, Cat

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


@rpc_service(threading="affine")
class RemoteCat(Cat):
    """Cat service exposed over RPC. Not thread-safe -> affine."""

    @rpc
    def greet(self, whom: str) -> str:
        """Greet someone."""
        return f"{self.speak()} to {whom}"

    @rpc
    def lives_remaining(self) -> int:
        """Hypothetical RPC method."""
        return 9


@rpc_service(threading="pool")
class RemoteSpider(Animal):
    """Animal subclass — trampoline for C++ *and* RPC for wire. Thread-safe -> pool."""

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


# Registry of services to codegen. Add new services here.
SERVICES = [RemoteCat, RemoteSpider]
