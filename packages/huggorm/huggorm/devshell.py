"""A development shell derivation, rewritten from another derivation.

The first half of what `nix print-dev-env` does: take a derivation,
replace its builder with a script that dumps the build environment,
and write the rewrite back. Building the answer prints the
environment as JSON.

`getDerivationEnvironment` in Nix's `develop.cc` is the reference,
and this follows it: refuse a non-`bash` builder the way `nix
develop` does, drop the reference checks a shell never answers,
and invalidate only the outputs that name a path or a hash. The
document surgery is JSON throughout, so no hash is computed here;
`add_derivation` fills in the deferred output paths.

Two flavours, like the session's: the sync one over a local
`Store`, the async one over a local `AsyncStore`. A remote session
cannot rewrite: `read_derivation` answers a C++-backed object that
does not cross RPC, so there is nothing there to read the document
from. The script text stays a parameter in both, because the bytes
follow the caller's Nix.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from huggorm_bindings import ContentAddressMethod, HashAlgorithm
from huggorm_generated import AsyncStore

from .errors import NixError

if TYPE_CHECKING:
    from huggorm_bindings import Store, StorePath

_CHECKS = (
    "allowedReferences",
    "allowedRequisites",
    "disallowedReferences",
    "disallowedRequisites",
)


def _rewrite(document: dict[str, Any], args_path: str, srcs_path: str) -> dict[str, Any]:
    """The document surgery, with no store in it.

    Pure so the rules are testable without one: the refusal, the
    stripped checks and the selective invalidation each fail here
    rather than in a store.
    """
    if os.path.basename(document["builder"]) != "bash":
        raise NixError(
            "'develop' only works on derivations that use 'bash' as their builder")
    document["args"] = [args_path]
    # A dev shell is not the build, so the build's reference checks
    # do not apply.
    if document.get("structuredAttrs") is not None:
        document["structuredAttrs"].pop("outputChecks", None)
    else:
        for check in _CHECKS:
            document["env"].pop(check, None)
    document["name"] += "-env"
    document["env"]["name"] = document["name"]
    document["inputs"]["srcs"].append(srcs_path)
    for name, output in document["outputs"].items():
        # Input-addressed and fixed outputs have a path to
        # invalidate; the other kinds have none.
        if "path" in output or "hash" in output:
            document["outputs"][name] = {}
            document["env"][name] = ""
    return document


def write_dev_shell_derivation(
    store: Store, drv_path: StorePath, get_env_script: str,
) -> StorePath:
    """Store a rewrite of `drv_path` whose builder dumps its environment.

    `get_env_script` is the text of the dumping script, and the
    caller owns it: Nix keeps its own copy inside the `nix` binary,
    where no library can reach it. The script has to enter the store
    before the derivation is hashed, which is why the text is the
    argument.

    Raises `NixError` when the builder of `drv_path` is not `bash`,
    which is the same refusal `nix develop` makes.
    """
    document = json.loads(store.read_derivation(drv_path).to_json())
    script = store.add_to_store(
        "get-env.sh", get_env_script.encode(), ContentAddressMethod.TEXT,
        HashAlgorithm.SHA256)
    rewritten = _rewrite(
        document, store.print_store_path(script), script.to_string())
    return store.add_derivation(json.dumps(rewritten))


async def awrite_dev_shell_derivation(
    store: AsyncStore, drv_path: StorePath, get_env_script: str,
) -> StorePath:
    """The async flavour, over a local `AsyncStore`.

    Same rewrite, awaited: every store call here crosses into the
    pool. Not for remote sessions, which have no `read_derivation`
    to read the document from.
    """
    document = json.loads((await store.read_derivation(drv_path)).to_json())
    script = await store.add_to_store(
        "get-env.sh", get_env_script.encode(), ContentAddressMethod.TEXT,
        HashAlgorithm.SHA256)
    rewritten = _rewrite(
        document, await store.print_store_path(script), script.to_string())
    return await store.add_derivation(json.dumps(rewritten))
