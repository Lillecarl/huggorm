"""
Nix profiles: `nix/store/profiles.hh`.

A profile is a symlink to its current generation, `<profile>-<N>-link`,
and each generation link points at a store path. A generation link
lives beside the profile, so a profile under a directory the collector
treats as roots keeps every generation alive until it is deleted.
"""

from huggorm_decl.decl.path import StorePath
from huggorm_decl.decl.store import Store
from huggorm_dsl.declare import (
    I64,
    U64,
    Bint,
    Cxx,
    Path,
    Str,
    binding,
    blocks,
    header,
    needs,
    produced,
    reads,
    threading,
    wire_value,
)


@produced
@header("nix/store/profiles.hh")
@binding(cxx="nix::Generation", threading="pool", blocking=False)
@wire_value(compare="parts")
class Generation:
    """One generation of a profile."""

    # WIRE ORDER: the members, in the order the aggregate holds them.

    @reads("number")
    def number(self) -> U64:
        """Its number, which grows with each new generation."""

    @reads("path")
    def path(self) -> Path:
        """The generation link, `<profile>-<number>-link`."""

    @reads("creationTime")
    def creation_time(self) -> I64:
        """When the link was made, as a Unix time."""

    @staticmethod
    def _from_parts() -> Generation:
        """Rebuild one from the parts that crossed."""
        Cxx("return nix::Generation{number, path, creation_time};")


@needs("nix/store/profiles.hh")
@threading("pool")
@blocks
def find_generations(profile: Path) -> list[Generation]:
    """Every generation of `profile`, oldest first. Empty for a profile
    that does not exist."""
    Cxx("""
auto gens = nix::findGenerations(profile).first;
return std::vector<nix::Generation>(gens.begin(), gens.end());
    """)


@needs("nix/store/profiles.hh")
@threading("pool")
@blocks
def current_generation(profile: Path) -> U64 | None:
    """The number of the generation `profile` points at, or None."""
    Cxx("return nix::findGenerations(profile).second;")


@needs("nix/store/profiles.hh", "nix/store/local-fs-store.hh", "filesystem")
@threading("pool")
@blocks
def set_profile(store: Store, profile: Path, path: StorePath) -> Path:
    """Point `profile` at `path`, through a new generation, and return
    the generation link.

    What `nix ... --profile` does (`MixProfile::updateProfile`): no new
    generation when the newest one already points at `path`, then a
    switch of the profile link. The generation link is a permanent
    root. Nix takes no lock here, and neither does this.

    `store` must be a local store, as Nix requires: the generation is a
    root, and only a local store keeps roots on this file system."""
    Cxx("""
auto * local = dynamic_cast<nix::LocalFSStore *>(&*store);
if (local == nullptr)
    throw nix::Error("a profile needs a local store");
auto absolute = std::filesystem::absolute(std::filesystem::path(profile));
auto generation = nix::createGeneration(*local, absolute, path);
nix::switchLink(absolute, generation);
return generation.string();
    """)


@needs("nix/store/profiles.hh")
@threading("pool")
@blocks
def delete_old_generations(profile: Path, dry_run: Bint = False) -> None:
    """Delete every generation but the current one. `nix-env
    --delete-generations old`."""
    Cxx("nix::deleteOldGenerations(profile, dry_run);")


@needs("nix/store/profiles.hh")
@threading("pool")
@blocks
def delete_generations_older_than(profile: Path, older_than: Str,
                                  dry_run: Bint = False) -> None:
    """Delete the generations older than `older_than`, such as `30d`, but
    never the current one, nor the newest one older than the cutoff.
    `nix-env --delete-generations 30d`."""
    Cxx("""
nix::deleteGenerationsOlderThan(profile, nix::parseOlderThanTimeSpec(older_than), dry_run);
    """)
