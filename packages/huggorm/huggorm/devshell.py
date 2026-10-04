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
`Store`, the async one over a local `AsyncStore`. The document half
crosses RPC - `read_derivation` answers a handle and `to_json`
reads through it - but the build half does not follow it across:
`build_paths` would build on the server's store and `real_path`
answers the server's filesystem, while the environment has to be
built where it will be sourced. The script text stays a parameter
in both, because the bytes follow the caller's Nix.

The second half is here too: build the rewrite, read the JSON file
`get-env.sh` wrote, and render it. `get_build_environment` is Nix's
`getBuildEnvironment` minus the installable and the profile: it
builds, keeps the answer alive with a temporary root, and returns
the environment beside the path it read, the same pair upstream
returns. `print_dev_env` renders that environment as shell code,
which is what the command prints without `--json`. With `--json`
the command prints the document itself, and that is `json.dumps`
over `to_dict` here.

What is NOT ported needs an installable to mean anything: output
redirects resolve installables, and the interactive shell, its
prompt and its `bashInteractive` lookup belong to `nix develop`
rather than to this command.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Union

from huggorm_bindings import (
    ContentAddressMethod,
    DerivedPathBuilt,
    HashAlgorithm,
    OutputsSpec,
)
from huggorm_generated import AsyncStore

from .errors import NixError

if TYPE_CHECKING:
    from huggorm_bindings import Store, StorePath

#: What `to_bash` leaves out. `Common::ignoreVars` in `develop.cc`,
#: verbatim: the caller already has a terminal, a shell and a temp
#: dir, so the build's copies would only break them.
IGNORE_VARS: Final[frozenset[str]] = frozenset({
    "BASHOPTS",
    "HOME",
    "NIX_BUILD_TOP",
    "NIX_ENFORCE_PURITY",
    "NIX_LOG_FD",
    "NIX_REMOTE",
    "PPID",
    "SHELLOPTS",
    "SSL_CERT_FILE",
    "TEMP",
    "TEMPDIR",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "UID",
})

#: Kept usable by prepending rather than overwriting. Upstream keeps
#: the list minimal to avoid impurities, and so does this.
SAVED_VARS: Final[tuple[str, ...]] = ("PATH", "XDG_DATA_DIRS")

#: A bash variable's value: a plain or exported string, an indexed
#: array, or an associative one. The three arms of Nix's
#: `BuildEnvironment::Value`, in the same order.
EnvValue = Union["Var", list[str], dict[str, str]]


@dataclass(frozen=True)
class Var:
    """A string variable, and whether the shell exports it.

    `get-env.sh` tells the two apart from `declare -p`: `-x` is
    exported, `--` is not. Anything else is an array, an
    associative, or an `unknown` that `from_json` drops the way
    upstream's unmatched branch does.
    """

    exported: bool
    value: str


@dataclass(frozen=True)
class BuildEnvironment:
    """The build environment of a derivation, as dumped by `get-env.sh`.

    Nix's `BuildEnvironment` struct in `develop.cc`, minus the one
    member that only serves `nix develop`: `getSystem` answers which
    platform an interactive shell would run on, and nothing here runs
    one. `structured_attrs` is the `.attrs.json` beside the
    `.attrs.sh`, in that order, or nothing when the derivation has no
    structured attributes.
    """

    vars: dict[str, EnvValue]
    bash_functions: dict[str, str]
    structured_attrs: tuple[str, str] | None = None

    @classmethod
    def from_json(cls, document: dict[str, Any]) -> BuildEnvironment:
        """Parse what `get-env.sh` printed.

        A variable whose type is none of `var`, `exported`, `array`
        or `associative` matches no arm upstream either - the script
        writes `unknown` for integer, readonly and nameref flags -
        so it is dropped here the same way.
        """
        vars: dict[str, EnvValue] = {}
        for name, info in document["variables"].items():
            kind = info["type"]
            if kind == "var" or kind == "exported":
                vars[name] = Var(exported=kind == "exported",
                                 value=info["value"])
            elif kind == "array":
                vars[name] = list(info["value"])
            elif kind == "associative":
                vars[name] = dict(info["value"])
        functions = dict(document["bashFunctions"])
        structured = None
        if "structuredAttrs" in document:
            attrs = document["structuredAttrs"]
            structured = (attrs[".attrs.json"], attrs[".attrs.sh"])
        return cls(vars=vars, bash_functions=functions,
                   structured_attrs=structured)

    @classmethod
    def parse(cls, data: bytes) -> BuildEnvironment:
        """Parse the file `get-env.sh` wrote.

        Strict throughout, like the `nlohmann::json` parse upstream:
        the bytes decode as UTF-8 and the document parses as JSON, or
        this raises rather than guessing. The caller names the file
        it failed on.
        """
        return cls.from_json(json.loads(data.decode("utf-8")))

    def to_dict(self) -> dict[str, Any]:
        """The document the command prints with `--json`.

        Upstream asserts the round trip in `toJSON`; an `assert`
        compiles out under `python -O`, so the same check raises
        here instead.
        """
        variables: dict[str, Any] = {}
        for name, value in self.vars.items():
            if isinstance(value, Var):
                variables[name] = {
                    "type": "exported" if value.exported else "var",
                    "value": value.value,
                }
            elif isinstance(value, list):
                variables[name] = {"type": "array", "value": list(value)}
            else:
                variables[name] = {
                    "type": "associative", "value": dict(value)}
        document: dict[str, Any] = {
            "variables": variables,
            "bashFunctions": dict(self.bash_functions),
        }
        if self.structured_attrs is not None:
            attrs_json, attrs_sh = self.structured_attrs
            document["structuredAttrs"] = {
                ".attrs.sh": attrs_sh, ".attrs.json": attrs_json}
        if BuildEnvironment.from_json(document) != self:
            raise NixError(
                "a build environment that does not survive its own "
                "JSON round trip")
        return document

    def to_bash(self, ignore_vars: frozenset[str] = IGNORE_VARS) -> str:
        """The variables and functions as shell code.

        Nix's `toBash`: sorted, because upstream iterates a
        `std::map` and document order is whatever `declare -p`
        printed. Exported strings assign and export, arrays declare
        `-a`, associatives declare `-A` with sorted keys for the same
        reason, functions keep the `name ()` shape `type` printed.
        """
        chunks: list[str] = []
        for name in sorted(self.vars):
            if name in ignore_vars:
                continue
            value = self.vars[name]
            if isinstance(value, Var):
                chunks.append(f"{name}={_escape_shell_arg(value.value)}\n")
                if value.exported:
                    chunks.append(f"export {name}\n")
            elif isinstance(value, list):
                chunks.append(
                    "declare -a " + name + "=("
                    + "".join(_escape_shell_arg(item) + " "
                              for item in value) + ")\n")
            else:
                chunks.append(
                    "declare -A " + name + "=("
                    + "".join("[" + _escape_shell_arg(key) + "]="
                              + _escape_shell_arg(value[key]) + " "
                              for key in sorted(value)) + ")\n")
        for name in sorted(self.bash_functions):
            chunks.append(
                name + " ()\n{\n" + self.bash_functions[name] + "}\n")
        return "".join(chunks)

    def _require_string(self, name: str) -> str:
        """The string behind this variable, or a refusal.

        Upstream asserts presence and throws `getString`'s error for
        the wrong kind; both are crashes there, and both are
        `NixError` here.
        """
        value = self.vars.get(name)
        if value is None:
            raise NixError(
                f"variable '{name}' is not set in this environment")
        if not isinstance(value, Var):
            raise NixError("bash variable is not a string")
        return value.value

    def to_rc_script(self, outputs_dir: str | None = None,
                     tmp_dir: str | None = None) -> str:
        """Shell code that reproduces this environment when sourced.

        Nix's `makeRcScript` minus the CLI: the saved-variable dance,
        the ignored variables, a fresh build top, the `shellHook`,
        and the output paths rewritten from the store to a directory
        of real paths - `cwd/"outputs"`, as upstream defaults, unless
        told otherwise. Structured attributes move from `/build` to
        files under `tmp_dir`, because the build directory is gone by
        the time anyone sources this; without one a directory is made
        and stays, and cleaning it is the caller's.
        """
        chunks = ["unset shellHook\n"]
        for var in SAVED_VARS:
            chunks.append(f"{var}=${{{var}:-}}\n")
            chunks.append(f'nix_saved_{var}="${var}"\n')
        chunks.append(self.to_bash())
        for var in SAVED_VARS:
            chunks.append(var + '="$' + var + '${nix_saved_' + var
                           + ':+:$nix_saved_' + var + '}"\n')
        chunks.append('export NIX_BUILD_TOP="$(mktemp -d -t nix-shell.XXXXXX)"\n')
        for var in ("TMP", "TMPDIR", "TEMP", "TEMPDIR"):
            chunks.append(f'export {var}="$NIX_BUILD_TOP"\n')
        chunks.append('eval "${shellHook:-}"\n')
        script = "".join(chunks)

        outputs = self.vars.get("outputs")
        if outputs is None:
            raise NixError(
                "derivation does not have an 'outputs' attribute")
        base = outputs_dir or os.path.join(os.getcwd(), "outputs")
        # ONE map and one pass, as upstream builds them: the output
        # rewrites and the two attrs files go in together, and
        # `rewriteStrings` walks the map in key order.
        rewrites: dict[str, str] = {}
        # Branched on structured attrs, as upstream branches, and not
        # on the shape of `outputs`: without them an associative array
        # names its outputs by its KEYS (`getStrings`).
        if self.structured_attrs is not None:
            if not isinstance(outputs, dict):
                raise NixError("bash variable is not an associative array")
            for name, path in outputs.items():
                rewrites[path] = os.path.join(base, name)
        else:
            if isinstance(outputs, Var):
                names = [n for n in re.split(r"[ \t\n\r]+", outputs.value) if n]
            elif isinstance(outputs, dict):
                names = list(outputs)
            else:
                names = outputs
            for name in names:
                rewrites[self._require_string(name)] = os.path.join(
                    base, name)

        if self.structured_attrs is not None:
            attrs_json, attrs_sh = self.structured_attrs
            own = tmp_dir or tempfile.mkdtemp(prefix="nix-dev-env-")
            sh_file = os.path.join(own, ".attrs.sh")
            json_file = os.path.join(own, ".attrs.json")
            with open(sh_file, "w", encoding="utf-8") as handle:
                handle.write(attrs_sh)
            with open(json_file, "w", encoding="utf-8") as handle:
                handle.write(attrs_json)
            rewrites[self._require_string("NIX_ATTRS_SH_FILE")] = sh_file
            rewrites[self._require_string("NIX_ATTRS_JSON_FILE")] = json_file
        return _rewrite_strings(script, rewrites)


def _escape_shell_arg(value: str) -> str:
    """Quote one shell word, always. Nix's `escapeShellArgAlways`,
    exactly: single quotes around, and each inner quote closes,
    escapes and reopens.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _rewrite_strings(script: str, rewrites: dict[str, str]) -> str:
    """Replace every occurrence of each source with its target.

    Nix's `rewriteStrings` (`util.cc`): the map in key order, the
    identity skipped, and the next search starting AT the last
    replacement rather than past it. So text a replacement forms with
    what follows it is replaced too: `ab -> a` over `abb` answers `a`.

    A target that contains its source makes upstream loop forever.
    That raises here instead, before any rewrite runs.
    """
    for old, new in rewrites.items():
        if old != new and old in new:
            raise ValueError(
                f"rewrite of {old!r} to {new!r} never ends: the target "
                f"contains its source")
    for old, new in sorted(rewrites.items()):
        if old == new:
            continue
        start = 0
        while (at := script.find(old, start)) != -1:
            script = script[:at] + new + script[at + len(old):]
            start = at
    return script


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
            "'nix develop' only works on derivations that use 'bash' as their builder")
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
    pool, and the derivation handle answers `to_json` the same way.
    Remote stores read the document the same way; only the build
    stays where the environment will be sourced.
    """
    document = json.loads(await (await store.read_derivation(drv_path)).to_json())
    script = await store.add_to_store(
        "get-env.sh", get_env_script.encode(), ContentAddressMethod.TEXT,
        HashAlgorithm.SHA256)
    rewritten = _rewrite(
        document, await store.print_store_path(script), script.to_string())
    return await store.add_derivation(json.dumps(rewritten))


def _first_env_output(store: Store, shell_drv: StorePath) -> StorePath:
    """The file `get-env.sh` wrote its JSON to.

    Upstream returns the first non-empty output path, in map order,
    and errors when the script produced nothing. The outputs are
    plain files - that is all the script writes - so a directory or
    an empty file is not an answer here either.
    """
    for path in store.query_derivation_output_map(shell_drv).values():
        # A missing file is not an answer, and the search moves on.
        # Upstream's `maybeLstat` tolerates ENOENT and ENOTDIR only,
        # and any other failure raises.
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            info = os.lstat(store.real_path(path))
            if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                return path
    raise NixError("get-env.sh failed to produce an environment")


async def _afirst_env_output(store: AsyncStore,
                             shell_drv: StorePath) -> StorePath:
    """The async half of the file search above."""
    for path in (await store.query_derivation_output_map(shell_drv)).values():
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            info = await (await store.real_path(path)).stat()
            if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                return path
    raise NixError("get-env.sh failed to produce an environment")


def get_build_environment(
    store: Store, drv_path: StorePath, get_env_script: str,
) -> tuple[BuildEnvironment, StorePath]:
    """The build environment of this derivation, built and read.

    The rest of `getDerivationEnvironment`: rewrite the derivation,
    build every output the normal way, and parse the JSON file the
    script wrote. The environment comes back beside the path it was
    read from, which is the pair upstream returns, and that path
    carries a temporary root - the command's profile only roots when
    asked, and a library keeps its answer alive for the process
    instead.

    A failed build raises its own error, as in Nix: the build log
    reaches a caller through the logger, which `logs()` streams.
    """
    shell_drv = write_dev_shell_derivation(store, drv_path, get_env_script)
    store.build_paths([DerivedPathBuilt(shell_drv, OutputsSpec(all=True))])
    env_path = _first_env_output(store, shell_drv)
    store.add_temp_root(env_path)
    data = store.real_path(env_path).read_bytes()
    try:
        return BuildEnvironment.parse(data), env_path
    except (UnicodeDecodeError, json.JSONDecodeError) as broken:
        raise NixError(
            "cannot parse build environment at "
            f"{env_path.to_string()}: {broken}") from broken


async def aget_build_environment(
    store: AsyncStore, drv_path: StorePath, get_env_script: str,
) -> tuple[BuildEnvironment, StorePath]:
    """The async flavour, over a local `AsyncStore`.

    Same build, awaited: every store call here crosses into the
    pool. The parse is sync - bytes already in hand - and the file
    reads through `anyio.Path`, which is the async spelling of one.
    """
    shell_drv = await awrite_dev_shell_derivation(
        store, drv_path, get_env_script)
    await store.build_paths([DerivedPathBuilt(
        shell_drv, OutputsSpec(all=True))])
    env_path = await _afirst_env_output(store, shell_drv)
    await store.add_temp_root(env_path)
    data = await (await store.real_path(env_path)).read_bytes()
    try:
        return BuildEnvironment.parse(data), env_path
    except (UnicodeDecodeError, json.JSONDecodeError) as broken:
        raise NixError(
            "cannot parse build environment at "
            f"{env_path.to_string()}: {broken}") from broken


def print_dev_env(
    store: Store, drv_path: StorePath, get_env_script: str,
    outputs_dir: str | None = None, tmp_dir: str | None = None,
) -> str:
    """Shell code that reproduces this derivation's environment.

    What `nix print-dev-env` prints without `--json`, less the newline
    its logger writes after: build the environment and render it
    sourcable. The JSON half needs no
    function - it is `json.dumps` over `get_build_environment`'s
    `to_dict` - so this is the only rendering here.
    """
    environment, _ = get_build_environment(store, drv_path, get_env_script)
    return environment.to_rc_script(outputs_dir=outputs_dir, tmp_dir=tmp_dir)


async def aprint_dev_env(
    store: AsyncStore, drv_path: StorePath, get_env_script: str,
    outputs_dir: str | None = None, tmp_dir: str | None = None,
) -> str:
    """The async flavour, over a local `AsyncStore`."""
    environment, _ = await aget_build_environment(
        store, drv_path, get_env_script)
    return environment.to_rc_script(outputs_dir=outputs_dir, tmp_dir=tmp_dir)
