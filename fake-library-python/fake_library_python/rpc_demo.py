"""
Demo of the full pipeline:

    C++ fake-library
      -> Cython bindings (fake_library)
        -> spec.py subclasses (RPC IDL, source of truth)
          -> Nix build-time AST codegen (generator/generate.py)
            -> fake_library_generated (importable everywhere via Nix)
              -> this package consumes the generated clients

The generated clients are plain Python classes built with `ast`
(syntax-checked, unparseable, toolable) — no string concatenation.
"""

from fake_library_generated import RemoteCatClient, RemoteSpiderClient


class DictTransport:
    """Fake transport: dispatches to a live service object in-process."""

    def __init__(self, service):
        self.service = service

    def call(self, method: str, args: list):
        fn = getattr(self.service, method)
        return fn(*args)


def demo():
    # Server side: live instances of the Cython-backed service objects
    from fake_library_generated.spec import RemoteCat, RemoteSpider

    cat_server = RemoteCat("Whiskers")
    spider_server = RemoteSpider("Shelob")

    # Client side: generated stubs talk over the transport
    cat_client = RemoteCatClient(DictTransport(cat_server))
    spider_client = RemoteSpiderClient(DictTransport(spider_server))

    print("=== Generated client round-trip ===")
    print(f"cat_client.greet('you')       -> {cat_client.greet('you')}")
    print(f"cat_client.lives_remaining()  -> {cat_client.lives_remaining()}")
    print(f"spider_client.crawl(2.5)      -> {spider_client.crawl(2.5)}")
    print(f"spider_client.bite('fly')     -> {spider_client.bite('fly')}")

    # Note what just happened: greet() dispatched over 'RPC' to cat_server,
    # whose speak() went into the C++ library. Full circle:
    # generated Python -> transport -> Python service -> Cython -> C++.

    print("\n=== Generated source (from ast.unparse) ===")
    import inspect

    import fake_library_generated.remotecat_client as m

    print(inspect.getsource(m))


if __name__ == "__main__":
    demo()
