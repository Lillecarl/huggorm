"""The second half of `nix print-dev-env`: build, read, render.

The pure half - parsing the JSON `get-env.sh` prints and rendering
it back as shell - is tested exactly, because Nix's `develop.cc` is
the reference and a rendering that drifts is a bug. The
orchestration - build, first non-empty output, temporary root,
parse - runs against a stub store, which is honest about what it
proves: the call sequence and the failure modes are ours, while the
store calls themselves are the generated surface the other suites
cover. The canned environment still travels through real files and
the real parser, so only the store is fake.

A real end-to-end build belongs to the `live` half with the other
builder runs (huggorm#37): it needs a real `bash` and a store that
runs builders, and neither holds in the hermetic suite.
"""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from huggorm.devshell import (
    BuildEnvironment,
    Var,
    _escape_shell_arg,
    _rewrite_strings,
)

if TYPE_CHECKING:
    from huggorm import DerivedPath, Store, StorePath

CANNED = {
    "variables": {
        "HOME": {"type": "var", "value": "/root"},
        "out": {"type": "exported", "value": "/nix/store/abc-out"},
        "outputs": {"type": "var", "value": "out"},
    },
    "bashFunctions": {"hello": "echo hi\n"},
}


def test_from_json_sorts_nothing_and_drops_the_unknown() -> None:
    """Each JSON type lands in its arm, and `unknown` lands nowhere.

    The script writes `unknown` for integer, readonly and nameref
    flags; upstream's `fromJSON` matches no arm for it, so neither
    does this.
    """
    env = BuildEnvironment.from_json({
        "variables": {
            "plain": {"type": "var", "value": "x"},
            "kept": {"type": "exported", "value": "y"},
            "list": {"type": "array", "value": ["b", "a"]},
            "map": {"type": "associative",
                    "value": {"k": "v"}},
            "flagged": {"type": "unknown"},
        },
        "bashFunctions": {"f": "true\n"},
        "structuredAttrs": {".attrs.sh": "SH", ".attrs.json": "{}"},
    })
    assert env.vars["plain"] == Var(exported=False, value="x")
    assert env.vars["kept"] == Var(exported=True, value="y")
    assert env.vars["list"] == ["b", "a"]
    assert env.vars["map"] == {"k": "v"}
    assert "flagged" not in env.vars
    assert env.bash_functions == {"f": "true\n"}
    assert env.structured_attrs == ("{}", "SH")


def test_parse_reads_bytes_and_refuses_both_breakages() -> None:
    """UTF-8 first, then JSON: either failure raises, naming nothing,
    because the caller names the file it failed on."""
    env = BuildEnvironment.parse(json.dumps(CANNED).encode())
    assert env.vars["out"] == Var(exported=True,
                                  value="/nix/store/abc-out")
    with pytest.raises(UnicodeDecodeError):
        BuildEnvironment.parse(b"\xff\xfe")
    with pytest.raises(json.JSONDecodeError):
        BuildEnvironment.parse(b"{nope")


def test_to_dict_round_trips_through_from_json() -> None:
    """The document `--json` prints, and upstream's own self-check."""
    env = BuildEnvironment.from_json(CANNED)
    assert BuildEnvironment.from_json(env.to_dict()) == env
    assert env.to_dict()["variables"]["out"] == {
        "type": "exported", "value": "/nix/store/abc-out"}


def test_to_bash_renders_every_arm_sorted_and_quoted() -> None:
    """The exact shell for every value kind.

    Sorted - upstream iterates a `std::map` - with document order
    scrambled on purpose, a quote inside a value, and associative
    keys out of order.
    """
    env = BuildEnvironment(
        vars={
            "zebra": Var(exported=False, value="stripes"),
            "map": {"k2": "v2", "k1": "v'1"},
            "PATH": Var(exported=True, value="/bin"),
            "arr": ["b", "a"],
        },
        bash_functions={"zzz": "echo hi\n", "aaa": "true\n"},
    )
    assert env.to_bash(ignore_vars=frozenset()) == (
        "PATH='/bin'\n"
        "export PATH\n"
        "declare -a arr=('b' 'a' )\n"
        "declare -A map=(['k1']='v'\\''1' ['k2']='v2' )\n"
        "zebra='stripes'\n"
        "aaa ()\n{\ntrue\n}\n"
        "zzz ()\n{\necho hi\n}\n"
    )


def test_to_bash_ignores_the_caller_s_shell() -> None:
    """`HOME` and friends never cross: the build's copies would only
    break the shell being entered."""
    env = BuildEnvironment(
        vars={"HOME": Var(exported=True, value="/root"),
              "out": Var(exported=True, value="x")},
        bash_functions={})
    assert "HOME" not in env.to_bash()
    assert "out='x'\n" in env.to_bash()


def test_an_ignored_name_still_renders_when_asked() -> None:
    """The ignore set is a default, not a rule: passing none keeps
    everything."""
    env = BuildEnvironment(
        vars={"HOME": Var(exported=True, value="/root")},
        bash_functions={})
    assert "HOME='/root'\n" in env.to_bash(ignore_vars=frozenset())


def test_to_rc_script_rewrites_outputs_into_a_directory(
        tmp_path: pathlib.Path) -> None:
    """The full sourced script: the saved-variable dance, a fresh
    build top, the hook, and the store path rewritten out."""
    from huggorm.devshell import BuildEnvironment

    env = BuildEnvironment.from_json(CANNED)
    script = env.to_rc_script(outputs_dir=str(tmp_path / "outputs"))
    assert script.startswith("unset shellHook\n")
    assert 'PATH=${PATH:-}\n' in script
    assert 'nix_saved_PATH="$PATH"\n' in script
    assert 'PATH="$PATH${nix_saved_PATH:+:$nix_saved_PATH}"\n' in script
    assert 'export NIX_BUILD_TOP="$(mktemp -d -t nix-shell.XXXXXX)"\n' \
        in script
    assert 'export TMP="$NIX_BUILD_TOP"\n' in script
    assert 'eval "${shellHook:-}"\n' in script
    assert f"out='{tmp_path / 'outputs' / 'out'}'\n" in script
    assert "/nix/store/abc-out" not in script
    assert "HOME" not in script


def test_to_rc_script_defaults_outputs_beside_the_work(
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No directory given means `cwd/"outputs"`, as upstream."""
    from huggorm.devshell import BuildEnvironment

    monkeypatch.chdir(tmp_path)
    script = BuildEnvironment.from_json(CANNED).to_rc_script()
    assert f"out='{tmp_path / 'outputs' / 'out'}'\n" in script


def test_to_rc_script_without_outputs_is_upstream_s_error() -> None:
    """No `outputs` variable names nothing to rewrite, and upstream
    refuses with exactly this."""
    from huggorm.devshell import BuildEnvironment
    from huggorm.errors import NixError

    env = BuildEnvironment(vars={}, bash_functions={})
    with pytest.raises(
            NixError,
            match="derivation does not have an 'outputs' attribute"):
        env.to_rc_script()


def test_to_rc_script_moves_structured_attrs_out_of_the_build(
        tmp_path: pathlib.Path) -> None:
    """`/build/.attrs.*` is gone once sourced, so the contents move
    to files that outlive the call."""
    from huggorm.devshell import BuildEnvironment

    env = BuildEnvironment(
        vars={
            "outputs": Var(exported=False, value="out"),
            "out": Var(exported=True, value="/nix/store/abc-out"),
            "NIX_ATTRS_SH_FILE": Var(exported=True,
                                     value="/build/.attrs.sh"),
            "NIX_ATTRS_JSON_FILE": Var(exported=True,
                                       value="/build/.attrs.json"),
        },
        bash_functions={},
        structured_attrs=('{"out": 1}', "out=1\n"))
    owned = tmp_path / "attrs"
    owned.mkdir()
    script = env.to_rc_script(outputs_dir=str(tmp_path / "o"),
                              tmp_dir=str(owned))
    assert "/build/.attrs.sh" not in script
    assert (owned / ".attrs.sh").read_text() == "out=1\n"
    assert (owned / ".attrs.json").read_text() == '{"out": 1}'


def test_escape_shell_arg_quotes_like_nix() -> None:
    """Always single-quoted; an inner quote closes, escapes, reopens."""
    assert _escape_shell_arg("plain") == "'plain'"
    assert _escape_shell_arg("") == "''"
    assert _escape_shell_arg("v'1") == "'v'\\''1'"
    assert _escape_shell_arg("$HOME `x`") == "'$HOME `x`'"


def test_rewrite_strings_replaces_every_occurrence_sorted() -> None:
    """Sorted application, the identity skipped, all occurrences."""
    assert _rewrite_strings("ab ab", {"ab": "c"}) == "c c"
    assert _rewrite_strings("x", {"x": "x", "y": "z"}) == "x"


def test_rewrite_strings_terminates_where_upstream_hangs() -> None:
    """Upstream scans from the replacement, so a target containing
    its source loops; continuing past it answers the same whenever
    upstream answers at all."""
    assert _rewrite_strings("ab", {"ab": "xab"}) == "xab"


class FakeStore:
    """The orchestration's store, with a canned derivation to read.

    Only the calls `get_build_environment` makes exist here; each
    records itself, and the output files are real files the real
    parser reads. `chroot` lends real path objects and real
    formatting, so the recorded build target asserts as one. The
    signatures match `Store`'s, which is what makes the production
    calls check; the tests hold this as `Any`, because it is not a
    store anyone should mistake for one.
    """

    def __init__(self, chroot: Store, files: dict[str, pathlib.Path],
                 build_error: str | None = None,
                 empty: bool = False) -> None:
        self.chroot = chroot
        self.files = files
        self.build_error = build_error
        self.built: list[DerivedPath] = []
        self.rooted: list[StorePath] = []
        names = ["empty", "full"] if empty else ["full"]
        self.outputs: dict[str, StorePath] = {
            name: chroot.parse_store_path(
                f"/nix/store/00000000000000000000000000000000-{name}")
            for name in names
        }

    def read_derivation(self, path: StorePath) -> Any:
        document = {
            "name": "leaf",
            "outputs": {"out": {}},
            "inputs": {"drvs": [], "srcs": []},
            "builder": "/bin/bash",
            "args": ["-c", "echo"],
            "env": {"name": "leaf"},
        }

        class Read:
            def to_json(self) -> str:
                return json.dumps(document)

        return Read()

    def add_to_store(self, *args: Any) -> StorePath:
        return self.chroot.parse_store_path(
            "/nix/store/00000000000000000000000000000000-get-env.sh")

    def print_store_path(self, path: StorePath) -> str:
        return "/nix/store/00000000000000000000000000000000-get-env.sh"

    def add_derivation(self, document: str) -> StorePath:
        rewritten = json.loads(document)
        assert rewritten["name"] == "leaf-env"
        assert rewritten["args"] == [
            "/nix/store/00000000000000000000000000000000-get-env.sh"]
        return self.chroot.parse_store_path(
            "/nix/store/00000000000000000000000000000000-leaf-env.drv")

    def build_paths(self, targets: list[DerivedPath], *args: Any) -> None:
        from huggorm_bindings.errors import NixError

        self.built = targets
        if self.build_error is not None:
            self.raised = NixError(self.build_error)
            raise self.raised

    def query_derivation_output_map(
            self, path: StorePath) -> dict[str, StorePath]:
        return dict(self.outputs)

    def real_path(self, path: StorePath) -> pathlib.Path:
        for name, candidate in self.outputs.items():
            if candidate.to_string() == path.to_string():
                return self.files[name]
        raise AssertionError(f"unknown output {path.to_string()}")

    def add_temp_root(self, path: StorePath) -> None:
        self.rooted.append(path)



def _fake(chroot: Store, files: dict[str, pathlib.Path],
          **kwargs: Any) -> tuple[Any, Any]:
    """A stub the checker reads as unwritten: `Any` in, `Any` out."""
    store: Any = FakeStore(chroot, files, **kwargs)
    drv: Any = chroot.parse_store_path(
        "/nix/store/00000000000000000000000000000000-leaf.drv")
    return store, drv


def _canned_files(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    full = tmp_path / "env.json"
    full.write_bytes(json.dumps(CANNED).encode())
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    return {"full": full, "empty": empty}


def test_get_build_environment_builds_reads_and_roots(
        tmp_path: pathlib.Path) -> None:
    """The whole orchestration: rewrite, build every output, first
    non-empty file, temporary root, parsed environment beside its
    path."""
    from huggorm.devshell import get_build_environment
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    store, drv = _fake(chroot, _canned_files(tmp_path))
    env, path = get_build_environment(store, drv, "script")
    assert env.vars["out"] == Var(exported=True,
                                  value="/nix/store/abc-out")
    assert env.bash_functions == {"hello": "echo hi\n"}
    assert path.to_string().endswith("-full")
    assert store.rooted == [path]
    [target] = store.built
    assert target.outputs().all() is True
    assert chroot.print_derived_path(target).endswith(".drv^*")


def test_get_build_environment_skips_the_empty_output(
        tmp_path: pathlib.Path) -> None:
    """Map order meets an empty first file: the search moves on, in
    the map's order."""
    from huggorm.devshell import get_build_environment
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    store, drv = _fake(chroot, _canned_files(tmp_path), empty=True)
    env, path = get_build_environment(store, drv, "script")
    assert path.to_string().endswith("-full")
    assert env.vars["outputs"] == Var(exported=False, value="out")


def test_get_build_environment_without_an_answer_is_upstream_s_error(
        tmp_path: pathlib.Path) -> None:
    """Nothing non-empty anywhere: upstream's own message."""
    from huggorm.devshell import get_build_environment
    from huggorm.errors import NixError
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    files = _canned_files(tmp_path)
    files["full"].write_bytes(b"")
    store, drv = _fake(chroot, files)
    with pytest.raises(
            NixError, match=r"get-env.sh failed to produce an environment"):
        get_build_environment(store, drv, "script")


def test_a_failed_build_raises_its_own_error(
        tmp_path: pathlib.Path) -> None:
    """The build's exception propagates as it is, as in Nix.

    `nix print-dev-env` lets a failed build's error through, and a
    build log reaches the user through the logger, which a session's
    `logs()` streams here. Rewrapping it in a `NixError` lost a
    `BuildError`'s class, `info` and `status`, and reading a strict
    UTF-8 log could replace the build error with `UnicodeDecodeError`
    (huggorm#112)."""
    from huggorm.devshell import get_build_environment
    from huggorm.errors import NixError
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    store, drv = _fake(chroot, _canned_files(tmp_path),
                       build_error="build of foo failed")
    with pytest.raises(NixError) as caught:
        get_build_environment(store, drv, "script")
    assert caught.value is store.raised


def test_a_broken_dump_names_the_file_it_broke_on(
        tmp_path: pathlib.Path) -> None:
    """Unparseable JSON raises with the path it came from."""
    from huggorm.devshell import get_build_environment
    from huggorm.errors import NixError
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    files = _canned_files(tmp_path)
    files["full"].write_bytes(b"{nope")
    store, drv = _fake(chroot, files)
    with pytest.raises(NixError, match=r"(?s)cannot parse.*-full"):
        get_build_environment(store, drv, "script")


def test_print_dev_env_renders_the_built_environment(
        tmp_path: pathlib.Path) -> None:
    """The command's stdout: the built environment as sourcable
    shell, with outputs rewritten where told."""
    from huggorm.devshell import print_dev_env
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    store, drv = _fake(chroot, _canned_files(tmp_path))
    script = print_dev_env(store, drv, "script",
                           outputs_dir=str(tmp_path / "o"))
    assert script.startswith("unset shellHook\n")
    assert f"out='{tmp_path / 'o' / 'out'}'\n" in script
    assert 'eval "${shellHook:-}"\n' in script


def test_first_env_output_needs_a_build(
        tmp_path: pathlib.Path) -> None:
    """The search over a real store, before anything built.

    The rewrite is written and its deferred outputs filled, but no
    builder ran, so no output file exists: the search finds nothing
    and says exactly what upstream says. What this proves without a
    builder is the store half of the orchestration - the query
    answers on filled-deferred outputs, and missing files are not
    answers - while the build itself stays where builder runs
    belong, the `live` half.
    """
    from huggorm.devshell import _first_env_output, write_dev_shell_derivation
    from huggorm.errors import NixError
    from huggorm_bindings import EvalState, Store

    store = Store(str(tmp_path))
    (tmp_path / "src").write_text("source\n")
    drv = EvalState(store).eval_expr(
        'derivation { name = "leaf"; system = "x86_64-linux"; '
        'builder = "/bin/bash"; outputs = [ "out" ]; }',
        str(tmp_path)).drv_path()
    shell = write_dev_shell_derivation(store, drv, "echo env\n")
    with pytest.raises(
            NixError, match=r"get-env.sh failed to produce an environment"):
        _first_env_output(store, shell)


async def test_afirst_env_output_needs_a_build(
        tmp_path: pathlib.Path) -> None:
    """The async search over the same unbuilt store."""
    from huggorm.devshell import (
        _afirst_env_output,
        awrite_dev_shell_derivation,
    )
    from huggorm.errors import NixError
    from huggorm_bindings import EvalState, Store
    from huggorm_generated import AsyncStore

    sync = Store(str(tmp_path))
    (tmp_path / "src").write_text("source\n")
    drv = EvalState(sync).eval_expr(
        'derivation { name = "leaf"; system = "x86_64-linux"; '
        'builder = "/bin/bash"; outputs = [ "out" ]; }',
        str(tmp_path)).drv_path()
    astore = AsyncStore(str(tmp_path))
    shell = await awrite_dev_shell_derivation(astore, drv, "echo env\n")
    with pytest.raises(
            NixError, match=r"get-env.sh failed to produce an environment"):
        await _afirst_env_output(astore, shell)


class AsyncFakeStore:
    """The same stub, awaited: every store call crosses into the pool
    upstream, so every one here is async too. A separate class
    rather than a subclass, because async methods never override
    sync ones. The files surface as real `anyio.Path` objects,
    which is also what the async `real_path` answers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.sync = FakeStore(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sync, name)

    async def read_derivation(self, path: StorePath) -> Any:
        read = self.sync.read_derivation(path)

        class AsyncRead:
            """`AsyncDerivation`'s shape: `to_json` is awaited."""

            async def to_json(self) -> str:
                return str(read.to_json())

        return AsyncRead()

    async def add_to_store(self, *args: Any) -> StorePath:
        return self.sync.add_to_store(*args)

    async def print_store_path(self, path: StorePath) -> str:
        return self.sync.print_store_path(path)

    async def add_derivation(self, document: str) -> StorePath:
        return self.sync.add_derivation(document)

    async def build_paths(self, targets: list[DerivedPath],
                          *args: Any) -> None:
        self.sync.build_paths(targets, *args)

    async def query_derivation_output_map(
            self, path: StorePath) -> dict[str, StorePath]:
        return self.sync.query_derivation_output_map(path)

    async def real_path(self, path: StorePath) -> anyio.Path:
        return anyio.Path(self.sync.real_path(path))

    async def add_temp_root(self, path: StorePath) -> None:
        self.sync.add_temp_root(path)



def _afake(chroot: Store, files: dict[str, pathlib.Path],
           **kwargs: Any) -> tuple[Any, Any]:
    """The async stub, likewise unread by the checker."""
    store: Any = AsyncFakeStore(chroot, files, **kwargs)
    drv: Any = chroot.parse_store_path(
        "/nix/store/00000000000000000000000000000000-leaf.drv")
    return store, drv


async def test_aget_build_environment_builds_reads_and_roots(
        tmp_path: pathlib.Path) -> None:
    """The async orchestration answers the same environment."""
    from huggorm.devshell import aget_build_environment
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    store, drv = _afake(chroot, _canned_files(tmp_path))
    env, path = await aget_build_environment(store, drv, "script")
    assert env.vars["out"] == Var(exported=True,
                                  value="/nix/store/abc-out")
    assert store.rooted == [path]
    [target] = store.built
    assert target.outputs().all() is True


async def test_aprint_dev_env_renders_the_built_environment(
        tmp_path: pathlib.Path) -> None:
    """And the async rendering matches the sync one."""
    from huggorm.devshell import aprint_dev_env, print_dev_env
    from huggorm_bindings import Store

    chroot = Store(str(tmp_path / "chroot"))
    files = _canned_files(tmp_path)
    sync, drv = _afake(chroot, files)
    script = await aprint_dev_env(sync, drv, "script",
                                  outputs_dir=str(tmp_path / "o"))
    assert script == print_dev_env(
        _fake(chroot, files)[0], drv, "script",
        outputs_dir=str(tmp_path / "o"))
