# This file demonstrates the key learning goal:
# Python subclasses of Cython-bound C++ classes.
#
# Two patterns:
# 1. Subclassing concrete Cat/Dog — Python-only override (no C++ trampoline).
#    `class LoudCat(Cat): def speak():` works in Python but C++ `describe(loudcat)`
#    still sees the original "meow". Good for pure-Python extensions.
# 2. Subclassing abstract Animal — trampoline (PyAnimal) makes Python overrides
#    visible to C++. `class Spider(Animal):` + `describe(spider)` goes through
#    C++ virtual dispatch and sees the Python impl. This is in
#    fake_library/animal.pyx:22-71.

from fake_library import Animal, Cat, Dog, describe


class LoudCat(Cat):
    """A Python subclass that overrides speak()."""

    def speak(self) -> str:
        # Call the C++ implementation via super(), then modify
        base = super().speak()
        return base.upper() + "!!!"

    def describe(self) -> str:
        return f"{self.name} the LoudCat says {self.speak()} with {self.legs()} legs"


class SilentDog(Dog):
    """Another subclass — shows we can replace behavior entirely."""

    def speak(self) -> str:
        return "..."

    def describe(self) -> str:
        return f"{self.name} the SilentDog says {self.speak()} with {self.legs()} legs"


class Spider(Animal):
    """Trampoline demo: Python subclass of abstract Animal is visible to C++."""

    def speak(self) -> str:
        return "hisss"

    def legs(self) -> int:
        return 8


class Ant(Animal):
    def speak(self) -> str:
        return "..."

    def legs(self) -> int:
        return 6


def demo():
    cat = Cat("Whiskers")
    dog = Dog("Rex")
    loud = LoudCat("Thunder")
    silent = SilentDog("Shy")

    print(f"Cat: {cat} name={cat.name} speak={cat.speak()} legs={cat.legs()}")
    print(f"Dog: {dog} name={dog.name} speak={dog.speak()} legs={dog.legs()}")
    print(f"LoudCat: {loud} name={loud.name} speak={loud.speak()} legs={loud.legs()}")
    print(f"  describe: {loud.describe()}")
    print(f"SilentDog: {silent} name={silent.name} speak={silent.speak()} legs={silent.legs()}")
    print(f"  describe: {silent.describe()}")

    # Show property setter and isinstance checks
    loud.name = "Boomer"
    print(f"Renamed LoudCat: {loud.name}")

    print(f"isinstance(loud, Cat): {isinstance(loud, Cat)}")
    print(f"isinstance(loud, LoudCat): {isinstance(loud, LoudCat)}")
    print(f"issubclass(LoudCat, Cat): {issubclass(LoudCat, Cat)}")

    print("\n--- Trampoline: Animal subclass visible to C++ ---")
    spider = Spider("Shelob")
    ant = Ant("Tiny")
    # Python-level
    print(f"Spider Python: {spider} speak={spider.speak()} legs={spider.legs()}")
    # C++-level via describe() — goes through C++ virtual dispatch + PyAnimal trampoline
    print(f"Spider C++ describe: {describe(spider)}")
    print(f"Ant C++ describe: {describe(ant)}")

    print("\n--- Limitation demo: Cat subclass NOT visible to C++ ---")
    # describe() calls C++ describe_animal which does NOT go through trampoline for Cat
    print(f"LoudCat Python speak: {loud.speak()}")
    print(f"LoudCat C++ describe: {describe(loud)}  # still 'meow', not 'MEOW!!!'")
    print("-> Cat/Dog have no trampoline; override is Python-only. Animal has trampoline.")

    print("\n--- Abstract check ---")
    try:
        Animal("fail")
    except TypeError as e:
        print(f"Animal() correctly raises: {e}")


if __name__ == "__main__":
    demo()
