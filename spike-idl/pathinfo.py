"""
What a store knows about one path it holds (nix::ValidPathInfo).

The second shape, and the one that shows where the line falls. A
StorePath declares completely: every accessor is derivable, and the
custom hatch stays at zero. This one does not, and the reason is
worth naming rather than hiding.

`nix::ValidPathInfo` holds a store DIRECTORY and a store PATH, and
what Python wants is the two joined. That join is a decision - which
separator, which of the three path-shaped fields get rendered, what
an unset registration time means - and a decision is not a binding.
So four accessors derive and seven come through `@cxx_body`, counted.

The count is the point. A hatch nobody measures becomes the place the
real code lives; a hatch that reports seven lines against four
derived accessors is telling the truth about a hard type.
"""

from declare import binding, cxx_body, header, produced, reads, wire_value


@produced(by="Store.query_path_info")
@header("nix/store/path-info.hh")
@binding(
    cxx="nix::ValidPathInfo",
    # Every accessor reads memory the object already owns. Nothing
    # blocks, so nothing needs a thread to hop to.
    threading="pool",
    blocking=False,
)
@wire_value(shown="store_path")
class ValidPathInfo:
    """What a store knows about one path it holds.

    A VALUE: it is what the store said at the moment it was asked, so
    nothing about it can go stale in a way a caller could act on.

    Produced, never constructed. Every field comes from the store's
    own database, so there is nothing a caller could correctly build
    one from."""

    @reads("storeDir")
    def store_dir(self) -> str:
        """The store directory this path lives under."""

    @reads("path")
    def store_path(self) -> "StorePath":
        """The path itself, for a caller that wants to compare or hash
        one and never needs the text."""

    @reads("narSize")
    def nar_size(self) -> int:
        """The size of the NAR in bytes. Not the size on disk."""

    @reads("ultimate")
    def ultimate(self) -> bool:
        """Whether this store built it itself, as opposed to receiving
        it from a substituter or an import."""

    # -- the seven that do not derive ------------------------------------
    #
    # Each one JOINS the store directory to something, or decides what
    # an absent value means. Both are choices about the Python surface
    # rather than facts about nix::ValidPathInfo.

    @cxx_body('return (vpi.storeDir + "/").append(vpi.path.to_string());')
    def path(self) -> str:
        """The full path, store directory included."""

    @cxx_body("""nb::list refs;
for (auto &r : vpi.references)
    refs.append((vpi.storeDir + "/").append(r.to_string()));
return refs;""")
    def references(self) -> "list[str]":
        """The store paths this one points at, its own included when
        it does.

        This is what makes a store path a graph rather than a name: a
        closure is the transitive reading of this field."""

    @cxx_body("""if (!vpi.deriver)
    return std::nullopt;
return (vpi.storeDir + "/").append(vpi.deriver->to_string());""")
    def deriver(self) -> "str | None":
        """The .drv that built this, or None.

        None is a real answer, not a gap: a path added straight to the
        store was not built by anything."""

    @cxx_body("return vpi.narHash.to_string(nix::HashFormat::SRI, true);")
    def nar_hash(self) -> str:
        """The hash of the path's NAR serialisation, algorithm first:
        `sha256:<base32>`, the same spelling `nix path-info` prints."""

    @cxx_body("""if (!vpi.registrationTime)
    return std::nullopt;
return static_cast<std::int64_t>(vpi.registrationTime);""")
    def registration_time(self) -> "int | None":
        """When the store learnt about this path, as a Unix time.

        The C++ field is 0 when unset, and 0 is a real Unix time, so
        the binding answers None rather than 1970."""

    @cxx_body("""if (!vpi.ca)
    return std::nullopt;
return nix::renderContentAddress(*vpi.ca);""")
    def ca(self) -> "str | None":
        """How this path's content addresses itself, or None.

        None for a path that was BUILT: an input-addressed output is
        named after the derivation that made it, not after its own
        bytes, so there is nothing to address by."""

    @cxx_body("""nb::list sigs;
for (auto &sig : nix::Signature::toStrings(vpi.sigs))
    sigs.append(sig);
return sigs;""")
    def sigs(self) -> "list[str]":
        """Who vouched for this path, as `<key-name>:<base64>`."""
