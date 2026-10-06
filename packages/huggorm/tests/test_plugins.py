"""`init_plugins` loads what `plugin-files` names, once per process.

Each case runs in a child, because loading a plugin changes the process
for good: the store types it registers cannot be taken back.
`HUGGORM_TCP_STORE_PLUGINS` is the lane's `nix-tcp-store`, which the
package build and `nix run --file . test` both set.
"""

import os
import subprocess
import sys

PLUGINS = os.environ["HUGGORM_TCP_STORE_PLUGINS"]

CHILD = """
import json, sys
import huggorm_bindings as h

def attempt(action):
    try:
        action()
        print("ok")
    except Exception as e:
        print(type(e).__name__, e)

if sys.argv[1] == "load":
    h.set_setting("plugin-files", sys.argv[2])
    h.init_plugins()
print("TCP Daemon Store" in json.loads(h.store_types_json()))
attempt(lambda: h.Store("tcp://127.0.0.1:1").is_valid_path(
    h.StorePath("7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1")))
attempt(h.init_plugins)
"""


def run(*args: str) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", CHILD, *args], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    return done.stdout.splitlines()


def test_a_loaded_plugin_registers_its_store_and_connects() -> None:
    registered, connect, second = run("load", PLUGINS)
    assert registered == "True"
    # The plugin's own message: the store opened, and its connection
    # code ran inside this process.
    assert "cannot connect to the Nix daemon at '127.0.0.1:1'" in connect, connect
    assert second.startswith("UsageError") and "already loaded" in second, second


def test_without_init_plugins_the_scheme_is_unknown() -> None:
    registered, connect, _ = run("none")
    assert registered == "False"
    assert "don't know how to open Nix store with scheme 'tcp'" in connect, connect
