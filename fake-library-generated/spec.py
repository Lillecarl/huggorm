"""
Source of truth for the async service surface.

These are Python subclasses of Cython types from fake_library.
At Nix build time, the codegen merges their own methods with the pxd
declaration surface and emits async wrappers via `ast` (not string
templating) into fake_library_generated/.

When you write code in fake-library-python (or any downstream package),
you import the generated wrappers from fake_library_generated — they are
already present via Nix propagatedBuildInputs.
"""

from fake_library import Animal
from fake_library import Cat as _Cat


def exposed(func):
    """Mark a method as part of the generated async surface."""
    func._exposed = True  # type: ignore[attr-defined]
    return func


def service(threading: str = "affine"):
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


# --- Typed errors (the IDL's exception vocabulary) ---
# The runtime recognizes errors by duck-typing: anything with to_dict()
# passes through untouched, so callers always get structured errors.


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


@service(threading="affine")
class Cat(_Cat):
    """
    Cat service. Not thread-safe -> affine.

    Binding surface (fetch/poop/toy/wait_ms/speak/legs/name) is inherited
    from the pxd declarations by the codegen - only genuine service logic
    lives here.
    """

    @exposed
    def greet(self, whom: str) -> str:
        """Greet someone. Raises NameRequiredError on empty whom."""
        if not whom:
            raise NameRequiredError("whom must not be empty")
        return f"{self.speak()} to {whom}"

    @exposed
    def lives_remaining(self) -> int:
        """Hypothetical extra accessor."""
        return 9


@service(threading="pool")
class Spider(Animal):
    """
    Spider service. Thread-safe -> pool.

    _hide: poop() returns an affine value; an affine value produced on a
    pool service has no home thread, so it is removed from the surface.
    """

    _hide = frozenset({"poop"})

    def speak(self) -> str:
        return "hisss"

    def legs(self) -> int:
        return 8

    @exposed
    def crawl(self, meters: float) -> str:
        """Ask the spider to crawl."""
        return f"{self.name} crawls {meters}m"

    @exposed
    def bite(self, target: str) -> bool:
        return target == "fly"


# Registry of services to codegen. Add new services here.
SERVICES = [Cat, Spider]

# Error vocabulary exposed in the manifest for tooling/codegen.
ERRORS = [ServiceError, FetchError, NameRequiredError]
