"""
nix::fetchers::Registry: `nix/fetchers/registry.hh`.

The flake registry: what `nixpkgs` means when a flake reference says
only that. Four layers, consulted in order, and each a JSON file.

EVERY CALL TAKES ITS OWN SETTINGS. They are the process's fetcher
settings (`set_setting`, nix.conf), then `settings` over them, spelled
as in nix.conf. No evaluator is needed, so none is asked for.

A WRITE READS THE FILE IT NAMES, every time. `getUserRegistry` and
`getCustomRegistry` keep their answer in a function-local static, so in
a long-lived process the first read decides for every later one, and a
second write would build on the first read. `Registry::read` takes a
path and reads it.

A reference is parsed against a BASE directory, None for the working
directory, as `nix` parses one. Without one, a git working tree parses
as `path:` rather than `git+file:`, and a relative path does not parse
at all.
"""

from typing import Annotated

from huggorm_decl.decl.store import Store
from huggorm_decl.decl.words import RegistryType
from huggorm_dsl.declare import (
    U64,
    Bint,
    Cxx,
    Str,
    Variant,
    Wrap,
    binding,
    blocks,
    needs,
    produced,
    threading,
    wire_value,
)

Attr = Annotated[
    Str | U64 | Bint,
    Variant(
        "nix::fetchers::Attr",
        header="nix/fetchers/attrs.hh",
        # Upstream keeps a bool as `Explicit<bool>`, so that a bool
        # never converts to the integer arm by accident. Python's
        # `bool` is already its own type.
        wraps={"bool": Wrap("nix::Explicit<bool>", holds="t")},
    ),
]
"""One fetcher attribute: a string, a count, or a flag."""


@produced(by="registry_entries")
@binding(threading="pool", blocking=False)
@wire_value()
class RegistryEntry:
    """One entry of one registry layer."""

    def layer(self) -> RegistryType:
        """The layer that holds it."""

    def source(self) -> Str:
        """The reference it matches, such as `flake:nixpkgs`."""

    def target(self) -> Str:
        """The reference it resolves to."""

    def extra_attrs(self) -> dict[str, Attr]:
        """What the entry adds to the target. `dir` is the subdirectory
        of a flake that is not at the top of its tree."""

    def exact(self) -> Bint:
        """Whether only the whole reference matches. An entry that is
        not exact also matches `nixpkgs/nixos-unstable`, and passes
        the branch on."""


@produced(by="registry_add")
@binding(threading="pool", blocking=False)
@wire_value()
class RegistryWrite:
    """What one change to a registry file did."""

    def path(self) -> Str:
        """The file it wrote."""

    def removed(self) -> U64:
        """How many entries for the same source it dropped first.

        Nix's own command says nothing when it removes nothing, so
        this is the only way to tell the two apart."""

    def target(self) -> Str | None:
        """The reference the source now resolves to. None for a
        removal."""

    def locked(self) -> Bint | None:
        """Whether that target is locked. Only a pin answers: an entry
        that is not locked still moves when its branch does."""


@needs("huggorm_decl/cpp/fetch.hpp", "nix/fetchers/registry.hh")
@threading("pool")
@blocks
def registry_entries(store: Store,
                     settings: dict[str, Str] | None = None,
                     ) -> list[RegistryEntry]:
    """Every entry of every layer, in the order Nix consults them.

    THIS CAN DOWNLOAD. The global layer is the file `flake-registry`
    names, and its default is a URL; Nix fetches it into `store` and
    roots it. `{"flake-registry": ""}` drops the layer.

    NIX CACHES EACH LAYER for the life of the process, so the first
    call decides what the user, system and global layers hold for
    every later one. A write through this module does not reach a
    later listing; read the file."""
    Cxx("""
auto fetch = huggorm::fetch_settings(settings.value_or(std::map<std::string, std::string>{}));
std::vector<huggorm::RegistryEntry> out;
for (auto & registry : nix::fetchers::getRegistries(*fetch, store))
    for (auto & entry : registry->entries)
        out.push_back(huggorm::RegistryEntry{
            huggorm::as_word(registry->type), entry.from.to_string(), entry.to.to_string(),
            entry.extraAttrs, entry.exact});
return out;
    """)


@needs("nix/fetchers/registry.hh")
def user_registry_path() -> Str:
    """The user's registry file, which a write with no path changes."""
    Cxx("""
return nix::fetchers::getUserRegistryPath().string();
    """)


@needs("huggorm_decl/cpp/fetch.hpp", "nix/fetchers/registry.hh",
       "nix/flake/flakeref.hh", "nix/util/source-path.hh")
@threading("pool")
@blocks
def registry_add(path: Str | None, source: Str, target: Str,
                 base: Str | None = None,
                 settings: dict[str, Str] | None = None) -> RegistryWrite:
    """`nix registry add`: resolve `source` to `target` from now on.

    `path` is the file, None for the user's. Both references parse as
    FLAKE references, because only a flake reference carries a
    subdirectory, and Nix keeps that as the `dir` extra attribute. An
    entry replaces every earlier one for the same source."""
    Cxx("""
auto fetch = huggorm::fetch_settings(settings.value_or(std::map<std::string, std::string>{}));
auto dir = base ? std::filesystem::path(*base) : std::filesystem::current_path();
auto file = path ? std::filesystem::path(*path) : nix::fetchers::getUserRegistryPath();
auto from = nix::parseFlakeRef(*fetch, source, dir);
auto to = nix::parseFlakeRef(*fetch, target, dir);
auto registry = nix::fetchers::Registry::read(
    *fetch,
    nix::SourcePath{nix::getFSSourceAccessor(), nix::CanonPath{file.string()}}.resolveSymlinks(),
    nix::fetchers::Registry::User);
nix::fetchers::Attrs extra;
if (!to.subdir.empty())
    extra["dir"] = to.subdir;
auto before = registry->entries.size();
registry->remove(from.input);
auto removed = before - registry->entries.size();
registry->add(from.input, to.input, extra);
registry->write(file);
return huggorm::RegistryWrite{file.string(), removed, to.input.to_string(), std::nullopt};
    """)


@needs("huggorm_decl/cpp/fetch.hpp", "nix/fetchers/registry.hh",
       "nix/flake/flakeref.hh", "nix/util/source-path.hh")
@threading("pool")
@blocks
def registry_remove(path: Str | None, source: Str,
                    base: Str | None = None,
                    settings: dict[str, Str] | None = None) -> RegistryWrite:
    """`nix registry remove`: drop every entry for `source`.

    Whole references compare, so `nixpkgs` does not remove an entry
    written for `nixpkgs/nixos-unstable`."""
    Cxx("""
auto fetch = huggorm::fetch_settings(settings.value_or(std::map<std::string, std::string>{}));
auto dir = base ? std::filesystem::path(*base) : std::filesystem::current_path();
auto file = path ? std::filesystem::path(*path) : nix::fetchers::getUserRegistryPath();
auto from = nix::parseFlakeRef(*fetch, source, dir);
auto registry = nix::fetchers::Registry::read(
    *fetch,
    nix::SourcePath{nix::getFSSourceAccessor(), nix::CanonPath{file.string()}}.resolveSymlinks(),
    nix::fetchers::Registry::User);
auto before = registry->entries.size();
registry->remove(from.input);
auto removed = before - registry->entries.size();
registry->write(file);
return huggorm::RegistryWrite{file.string(), removed, std::nullopt, std::nullopt};
    """)


@needs("huggorm_decl/cpp/fetch.hpp", "nix/fetchers/registry.hh",
       "nix/flake/flakeref.hh", "nix/util/source-path.hh")
@threading("pool")
@blocks
def registry_pin(store: Store, path: Str | None, source: Str,
                 target: Str | None = None,
                 base: Str | None = None,
                 settings: dict[str, Str] | None = None) -> RegistryWrite:
    """`nix registry pin`: resolve `source` to what `target` is NOW.

    `target` None pins `source` to itself. THIS FETCHES: resolving
    through the registry names a branch, and only the fetch turns it
    into a revision."""
    Cxx("""
auto fetch = huggorm::fetch_settings(settings.value_or(std::map<std::string, std::string>{}));
auto dir = base ? std::filesystem::path(*base) : std::filesystem::current_path();
auto file = path ? std::filesystem::path(*path) : nix::fetchers::getUserRegistryPath();
auto from = nix::parseFlakeRef(*fetch, source, dir);
auto to = nix::parseFlakeRef(*fetch, target ? *target : source, dir);
auto resolved = to.resolve(*fetch, store).input.getAccessor(*fetch, store).second;
auto registry = nix::fetchers::Registry::read(
    *fetch,
    nix::SourcePath{nix::getFSSourceAccessor(), nix::CanonPath{file.string()}}.resolveSymlinks(),
    nix::fetchers::Registry::User);
nix::fetchers::Attrs extra;
if (!from.subdir.empty())
    extra["dir"] = from.subdir;
auto before = registry->entries.size();
registry->remove(from.input);
auto removed = before - registry->entries.size();
registry->add(from.input, resolved, extra);
registry->write(file);
return huggorm::RegistryWrite{
    file.string(), removed, resolved.to_string(), resolved.isLocked(*fetch)};
    """)
