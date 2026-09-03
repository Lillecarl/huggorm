"""
The evaluation binding: EvalState is the affine SERVICE exemplar.

`nix::EvalState` is documented as not thread-safe, one per thread;
here that fact becomes `threading="affine"`. Values live on the
state's thread: they are produced by state methods and attach to its
runner in the async layer. `force` mutates a value in place, which is
the affine-value-as-parameter case.

Two facts about libexpr shape everything below, and both are LIFETIME.

A value lives in the collector's heap and belongs to nobody: it dies
when the collector can no longer see a pointer to it, even while its
EvalState lives on. Python's heap is not scanned, so a wrapper
holding a bare pointer roots nothing.

And a value is not self-describing. An attribute name is a `Symbol`,
a `uint32_t` index into the PRODUCING state's symbol table, so
rendering one needs that state in hand.

`huggorm::Bridge` answers both: it holds an upstream `RootValue` and
a share of the state that made it, so the state cannot die under a
value that still points into its memory. That is producer pinning as
a C++ fact, beside the server's `parents=[self]`. It is the only C++
in this binding a declaration could not have written, and
`cpp/eval.hpp` says why line by line.
"""

from huggorm_dsl.declare import (
    I64,
    Bint,
    Cxx,
    PyFunc,
    Str,
    StrView,
    binding,
    binds,
    blocks,
    cxx_name,
    fills,
    guard,
    header,
    instant,
    names,
    needs,
    produced,
    produces,
    reads,
    startup,
    tagged,
    threading,
    tree,
    wire_value,
)


@produced(by="EvalState")
@header("huggorm_decl/cpp/eval.hpp")
@binding(
    # One state per thread, and its values belong to that thread. The
    # async layer inherits the runner rather than making a new one -
    # a value operation touches the state's own memory, so running it
    # anywhere else is a data race.
    threading="affine",
    # Reading a forced value is a memory read. Forcing is what waits,
    # and it says so for itself.
    blocking=False,
    cxx="huggorm::Bridge",
)
# Bridge is a HANDLE over a TAGGED UNION. Reach the value with
# `get()`, ask which arm it holds with `type<true>()`, and here is
# every arm nix::ValueType has.
#
# ONE table, read two ways. `type_name` answers a caller with the
# name; every `@guard` below checks against the enumerator. Adding an
# arm is one line here rather than a switch case and a guard that
# have to agree.
@tagged(
    "get()", "type<true>()",
    thunk="nix::nThunk",
    int="nix::nInt",
    float="nix::nFloat",
    bool="nix::nBool",
    string="nix::nString",
    path="nix::nPath",
    null="nix::nNull",
    attrs="nix::nAttrs",
    list="nix::nList",
    function="nix::nFunction",
    external="nix::nExternal",
    failed="nix::nFailed",
)
# How a value TREE is walked, read by the RPC layer so that no layer
# above this declaration knows what a Value is or which of its methods
# do what (tasks/030). `kind` names the accessor that says what this
# node is; its answer selects one of the branches below. A kind named
# nowhere here crosses as a proxy - and real Nix has five of those:
# thunk, function, external, failed and path. Laziness the wire cannot
# serialize, plus three things that are not data at all.
@tree(
    kind="type_name",
    # What makes two nodes THE SAME node. A fresh wrapper is built for
    # every access, so Python identity says nothing: two wrappers over
    # one value differ, and a wrapper that dies hands its id() to the
    # next one. The underlying value's address is the identity.
    identity="_identity",
    # kind reported by `kind` -> [wire type, accessor]. The wire type
    # is what picks the arm, so the layer above reads a declared type
    # name rather than a label this file invented.
    scalars={"int": ["int", "integer"],
             "string": ["str", "string_value"],
             "bool": ["bool", "boolean"]},
    list={"size": "size", "item": "at"},
    attrs={"size": "size", "name": "name_at", "value": "value_at"},
)
class Value:
    """One GC-resident nix::Value, rooted for as long as Python holds
    it.

    Wire-proxy despite being "just data": a thunk must force on its
    home thread and forcing mutates in place. A future refinement may
    serialize forced scalars; until then, proxy.

    Produced, never constructed. A value comes from an EvalState -
    parsed, evaluated or built - and there is nothing a caller could
    correctly make one from.

    EVERY ACCESSOR GUARDS, and that is not politeness. `nix::Value` is
    a tagged union whose readers are `noexcept` and undefined on the
    wrong tag: reading `integer` off a string is not an error, it is a
    reinterpretation of the payload. So no method here binds a
    pointer-to-member on `nix::Value`; each one is a Bridge method
    that checks the tag first."""

    @cxx_name("identity")
    def _identity(self) -> I64:
        """The underlying value's address, as a number.

        Private: it is not surface, so the codegen leaves it out of
        every generated form. The tree walk uses it to visit a shared
        value once - values are immutable and shared freely, so
        without it a diamond is copied and a cycle never ends."""

    def is_gc_managed(self) -> Bint:
        """True when this value lives inside a GC-allocated block.

        Bound straight from gc.h: a no-op integration cannot fake
        it."""

    @names
    def type_name(self) -> Str:
        """What this value is: "thunk", "int", "float", "bool",
        "string", "path", "null", "attrs", "list", "function",
        "external" or "failed".

        The full `nix::ValueType`, not a subset. A kind the `@tree`
        map above does not name crosses as a proxy, so naming all of
        them here costs nothing and hides nothing."""

    @guard("int")
    def integer(self) -> I64:
        """This value as an integer. Raises on a thunk, or on a value
        of another kind.

        nix::Value::integer answers a NixInt, which is a checked
        int64 with an explicit conversion - so the cast the emitter
        already writes for an I64 return is the whole of it."""

    @guard("string")
    @cxx_name("string_view")
    def string_value(self) -> StrView:
        """This value as a string. Raises as `integer` does.

        The string CONTEXT is dropped. A Nix string can carry store
        paths it depends on, and nothing above this layer can act on
        them yet; a declaration that carried them would be inventing
        a surface rather than binding one."""

    @guard("bool")
    def boolean(self) -> Bint:
        """This value as a bool. Raises as `integer` does."""

    # Collections. Reading is by index, which is also how the
    # alphabetical order of an attribute set reaches Python.
    #
    # That order is not free. nix::Bindings is sorted by Symbol ID,
    # which is INTERNING order - the order a name was first seen
    # anywhere in the process - so an attribute set comes back in
    # whatever order its names happened to be interned.
    # `lexicographicOrder` is the accessor that hides it, and the
    # Bridge caches the result because the walk reads every index
    # against one object.
    #
    # A list[Value] or dict[str, Value] accessor is deliberately
    # absent. It needs a collection of PROXIES, which is the recursive
    # value message (tasks/030), not another loop here.

    def size(self) -> I64:
        """Elements in a list, or attributes in an attribute set.

        No `@guard`: it takes ONE arm and this accepts two."""
        Cxx("""
if (self.get()->type<true>() == nix::nList)
    return static_cast<std::int64_t>(self.get()->listSize());
if (self.get()->type<true>() == nix::nAttrs)
    return static_cast<std::int64_t>(self.get()->attrs()->size());
throw std::runtime_error("value is not a list or an attribute set");
        """)

    @guard("list")
    def at(self, index: I64) -> "Value":
        """One element of a list.

        It may still be a thunk: forcing a list forces the list, not
        what is in it."""
        Cxx("""
auto items = self.get()->listView();
if (index < 0 || static_cast<std::size_t>(index) >= items.size())
    throw std::runtime_error("list index out of range");
return self.wrap(items[static_cast<std::size_t>(index)]);
        """)

    @guard("attrs")
    def name_at(self, index: I64) -> Str:
        """One attribute name, in alphabetical order."""
        Cxx("""
const auto & by_name = self.sorted();
if (index < 0 || static_cast<std::size_t>(index) >= by_name.size())
    throw std::runtime_error("attribute index out of range");
return self.symbol(by_name[static_cast<std::size_t>(index)]->name);
        """)

    @guard("attrs")
    def value_at(self, index: I64) -> "Value":
        """One attribute value, in alphabetical order of name."""
        Cxx("""
const auto & by_name = self.sorted();
if (index < 0 || static_cast<std::size_t>(index) >= by_name.size())
    throw std::runtime_error("attribute index out of range");
return self.wrap(by_name[static_cast<std::size_t>(index)]->value);
        """)

    @guard("attrs")
    def has(self, name: Str) -> Bint:
        """Whether this attribute set carries that name."""
        Cxx("return self.get()->attrs()->get(self.intern(name)) != nullptr;")

    @guard("attrs")
    def get(self, name: Str) -> "Value":
        """One attribute by name. Raises when it is missing."""
        Cxx("""
const auto * attr = self.get()->attrs()->get(self.intern(name));
if (attr == nullptr)
    throw std::runtime_error("attribute '" + name + "' is missing");
return self.wrap(attr->value);
        """)


@header("huggorm_decl/cpp/eval.hpp")
@binding(
    cxx="huggorm::LogField",
    threading="pool",
    blocking=False,
)
@produced(by="LogRecord.fields")
@wire_value()
class LogField:
    """One field of one log record: an integer or a string.

    Nix's own `Logger::Field` is a hand-rolled variant - a two-valued
    enum, a `uint64_t` and a `std::string` - with upstream's FIXME
    asking for a `std::variant` (logging.hh:76). This mirrors it
    rather than picking one of the two, because a field that carried
    only its rendering would lose the difference between the number
    42 and the string "42", and a progress result is made of numbers.
    """

    @reads("is_int")
    def is_int(self) -> Bint:
        """Which of the two `integer` and `text` this field is."""

    @reads("integer")
    def integer(self) -> I64:
        """The number, when `is_int`. Zero otherwise."""

    @reads("text")
    def text(self) -> Str:
        """The string, when not `is_int`. Empty otherwise."""


@header("huggorm_decl/cpp/eval.hpp")
@binding(
    cxx="huggorm::LogRecord",
    threading="pool",
    blocking=False,
)
@produced(by="LogStream.drain")
@wire_value()
class LogRecord:
    """One thing Nix said while it worked.

    NOT a line of text. `nix::Logger` is a tree of ACTIVITIES carrying
    typed progress results, and the progress bar every Nix user sees
    is built from that tree rather than from messages. A binding that
    handed back only strings would throw most of it away.

    The names are Nix's own, from the `internal-json` log format
    (`logging.cc:272`), so a client that already reads that format
    reads this one. The mechanism is different and `tasks/032` says
    why.

    `level`, `type` and `id` are integers because that is what they
    are: `Verbosity`, `ActivityType` and `ResultType` are int-valued
    C++ enums and nothing upstream parses one from a string.
    `decl/words.py` is for vocabularies whose member IS the string
    libstore parses, and these are not that.
    """

    @reads("action")
    def action(self) -> Str:
        """Which of the four kinds this is.

        `"msg"` is a message, and the only kind `level` filters.
        `"start"` and `"stop"` open and close an activity. `"result"`
        reports progress inside one.
        """

    @reads("level")
    def level(self) -> I64:
        """`nix::Verbosity`: 0 error, 1 warn, 2 notice, 3 info, 4
        talkative, 5 chatty, 6 debug, 7 vomit.

        A field of the record, not a gate. `Activity::Activity` calls
        `startActivity` with no test (`logging.cc:196`), so a `start`
        arrives whatever its level says."""

    @reads("id")
    def id(self) -> I64:
        """The activity this belongs to, or 0 for a plain message."""

    @reads("parent")
    def parent(self) -> I64:
        """The activity this one runs inside, for a `"start"`.

        Nix tracks it per THREAD - `curActivity` is a `thread_local`
        (`logging.cc:23`) - so the tree is already per-thread before
        this binding sees it."""

    @reads("type")
    def type(self) -> I64:
        """`nix::ActivityType` for a `"start"`, `nix::ResultType` for
        a `"result"`, and 0 otherwise."""

    @reads("text")
    def text(self) -> Str:
        """The message, or the activity's description.

        An error arrives RENDERED, the way `JSONLogger` renders one
        (`logging.cc:283`). An `ErrorInfo` carries a trace of
        positions, and crossing those parts would be a second error
        shape beside the one a failed call already crosses with
        (`tasks/036`). Those two should agree; `tasks/032` holds the
        question open rather than answering it twice."""

    @reads("fields")
    def fields(self) -> "list[LogField]":
        """The typed payload, for a `"start"` or a `"result"`.

        What it MEANS depends on `type`. A `resProgress` carries done,
        expected, running and failed; a `resBuildLogLine` carries the
        line. Upstream documents the pairing in `logging.hh` and this
        binding does not restate it."""


@header("huggorm_decl/cpp/eval.hpp")
@binding(
    cxx="huggorm::LogQueue",
    # A share, because the queue outlives the subscribe call and the
    # tap holds one too: a reader that drops its LogStream must not
    # leave the logger writing into freed memory.
    holder="shared_ptr",
    # NOT affine, and that is the whole point. Every EvalState method
    # runs on that state's own thread, one at a time, so a drain
    # declared there would queue BEHIND the evaluation whose progress
    # it wants to report - and the records would arrive only once the
    # work they describe had finished. A queue with its own mutex is
    # what makes the backchannel a backchannel.
    threading="pool",
    # It takes a mutex and moves a deque. Nothing waits.
    blocking=False,
)
@produced(by="EvalState.subscribe_logs")
class LogStream:
    """The records one subscriber has not read yet.

    BOUNDED, and the bound is the interesting part. A full queue
    refuses a `"msg"` and a `"result"` and never a `"start"` or a
    `"stop"`: a dropped stop leaves a node in the reader's activity
    tree that nothing later closes, and an unclosed node is worse than
    a missing log line. `dropped` counts what the bound refused.
    """

    def drain(self) -> "list[LogRecord]":
        """Everything waiting, and the queue is empty afterwards.

        Empty when nothing happened. It does not block and it does not
        wait for a record, because the thread that would wait is the
        one that has to keep draining."""

    def dropped(self) -> I64:
        """How many records the bound refused, over this queue's life.

        CUMULATIVE, so a reader that misses a drain still sees the
        number grow. A reader wanting the per-drain figure
        subtracts."""

    def close(self) -> None:
        """Stop recording and let go of what is waiting.

        The subscription on the state's thread is a separate fact:
        `EvalState.unsubscribe_logs` is what clears that. Closing here
        stops this queue filling; it does not stop the next `drain`
        from being callable."""


@header("huggorm_decl/cpp/eval.hpp")
@binding(
    cxx="huggorm::Evaluator",
    # Not thread-safe, one per thread. libexpr says so and this is
    # where that sentence becomes a policy.
    threading="affine",
    # Parsing and evaluating are slow and pure C++ after the string
    # crosses. The accessors are not, and say so.
    blocking=True,
)
class EvalState:
    """One evaluator, and the thread it belongs to."""

    def __init__(self, store_uri: Str) -> None:
        """Open a state against a store URI.

        REQUIRED, with no default. A state is bound to a store and a
        thread, and neither is a thing to guess at.

        `nix::EvalState` takes a `ref<Store>` and two settings objects
        that must outlive it, so `huggorm::Evaluator` owns all four
        and this parameter is the one a caller can answer."""

    def get_store_uri(self) -> Str:
        """The URI this state was opened with."""

    def parse_expr(self, expr: Str) -> "Value":
        """Parse without evaluating: the result is an unforced thunk.

        Not a `@produces`: that marker is for a value made by ONE
        initialiser, and this parses first and then builds a thunk
        over the state's base environment. Stretching the marker to
        cover it would make it a description of structure."""
        Cxx("""
if (expr.empty())
    throw std::invalid_argument("empty expression");
auto * e = self.state().parseExprFromString(expr, self.state().rootPath("."));
auto * made = self.alloc();
made->mkThunk(&self.state().baseEnv, e);
return self.wrap(made);
        """)

    def eval_expr(self, expr: Str) -> "Value":
        """Parse and evaluate: slow, fully forced result."""
        Cxx("""
if (expr.empty())
    throw std::invalid_argument("empty expression");
auto * e = self.state().parseExprFromString(expr, self.state().rootPath("."));
auto * made = self.alloc();
self.state().eval(e, *made);
self.state().forceValue(*made, nix::noPos);
return self.wrap(made);
        """)

    def eval_file(self, path: Str) -> "Value":
        """Evaluate a file, and remember it.

        The one call that is CHEAP the second time. `evalFile` keeps a
        `fileEvalCache` keyed by resolved path, and a hit forces the
        value it already has and copies it - no open, no parse, no
        evaluation (`eval.cc:1118`). `eval_expr` has no such cache: a
        string is not a key, so the same text evaluated twice is
        evaluated twice.

        That cache is what "warm" means for this project, and it is
        why the state has to outlive the client that filled it.

        A `str`, not a `pathlib.Path`, and the reason is
        `add_path_to_store`'s: the file is read on the machine the
        EVALUATOR runs on. In process that is here; over RPC it is the
        server's filesystem, and no client path crosses - the argument
        goes as the string it is.

        `rootPath` takes a relative path too, and resolves it against
        the process's own directory. Left to libexpr rather than
        refused here: a caller who passes one gets upstream's answer,
        which is the same answer `nix-instantiate` gives them.

        Forced to WHNF, like every other `evalFile` caller: upstream
        forces the thunk before it hands the value back. Not deeply -
        an attribute set comes back with its members unforced, which
        is what makes the call cheap enough to be worth caching."""
        Cxx("""
if (path.empty())
    throw std::invalid_argument("empty path");
auto * made = self.alloc();
self.state().evalFile(self.state().rootPath(path), *made);
return self.wrap(made);
        """)

    def cached_files(self) -> "list[Str]":
        """Every file whose evaluation this state has cached.

        What a watcher watches. `tasks/016` wants a change to a file an
        evaluation read to invalidate the warm state rather than throw
        it away, and this is the set that change would touch.

        libexpr keeps it and does not offer it: `fileEvalCache` is
        private, and the accessor every read goes through is built
        inside the constructor from the settings alone, so there is
        nothing to substitute either. `huggorm::cached_files` reaches
        it the one way the standard allows without patching nixpkgs -
        the whole argument is in `cpp/eval.hpp`, beside the code.

        `import` goes through `evalFile`, so a file reached from
        INSIDE an expression is here as surely as the one the caller
        named. That is the reason this reads libexpr's cache rather
        than counting what `eval_file` was handed: our own boundary
        sees one file and an evaluation reads many.

        Resolved paths, so `/foo` appears as `/foo/default.nix`. That
        is what the cache is keyed by and what a watch has to name.

        NOT every file read. `builtins.readFile` and `builtins.path`
        do not go through this cache, so a caller who wants those
        watched needs something else - and would find out here rather
        than from a stale answer.

        NOT every entry is a file either. libexpr evaluates its own
        `derivation-internal.nix` out of an in-memory accessor, and it
        renders as `«nix-internal»/derivation-internal.nix`. A watcher
        has to skip what it cannot stat; the list is what the cache
        holds, not a promise that each entry is on disk."""
        Cxx("return huggorm::cached_files(self.state());")

    def forget_file(self, path: Str) -> None:
        """Forget one cached file, so the next evaluation reads disk.

        The other half of `cached_files`. A watcher that sees a file
        change needs to drop that file's warm evaluation and KEEP the
        rest - `resetFileCache()` is the only public way to do it, and
        it also clears the fetched flake inputs, so one edited local
        file costs a re-download.

        FORGET THE CLOSURE, NOT THE FILE. Measured, and written up in
        `tasks/016`: an importer does not notice its import changing.
        `outer.nix` that says `import ./inner.nix` stays cached at its
        old answer after `inner.nix` is edited and forgotten, because
        the cache holds no edge between the two - both files are
        listed, and nothing says one read the other.

        The DIFF is not that closure, and this docstring said it was.
        `cached_files` before and after one `eval_file` differ by what
        the evaluation newly CACHED, not by what it read - so the
        second root to import a shared file gets a diff that does not
        mention it, and forgetting that diff leaves the root stale.
        Measured in `tasks/083`.

        What is sound is the SNAPSHOT: everything cached when a root
        finished is a superset of what that root read. `huggorm.Watcher`
        keeps one per root and does this bookkeeping, so a caller who
        wants live reloading should use it rather than pair this call
        with a diff of their own.

        Erases both spellings. The cache is keyed by the RESOLVED
        path, so forgetting `/foo` erases `/foo/default.nix` too - the
        argument is in `cpp/eval.hpp`, beside the code.

        A path that was never cached forgets nothing, which is what
        makes feeding a whole closure in safe."""
        Cxx("""
if (path.empty())
    throw std::invalid_argument("empty path");
huggorm::forget_file(self.state(), self.state().rootPath(path));
        """)

    @instant
    def register_primop(self, name: Str, arity: I64,
                        fn: PyFunc) -> None:
        """Publish a Python callable as `builtins.<name>`.

        The direction every other method here runs the other way.
        Everything else is Python calling the evaluator; this hands
        the evaluator a function it calls back, in the middle of an
        evaluation, on this state's own thread.

        `fn` takes `arity` Values and returns a Value. Its arguments
        arrive FORCED - a primop receives thunks, and a Python
        function given an unforced one could do nothing with it and
        had no way to say so.

        SYNCHRONOUS, and there is no way to make it otherwise. It runs
        inside evaluation, so it must not await and must not hop
        threads. That is the exact inverse of every wrapper this repo
        emits, which exist to get OFF the calling thread - and it is
        why `tasks/034`, a Nix function called FROM Python, cannot
        borrow this shape.

        IN-PROCESS ONLY. No rpc surface exists and the manifest
        refuses to build one, because a remote registration would make
        the evaluator call back over the socket once per invocation,
        on its evaluation thread. A decision, in `tasks/033`, rather
        than something not written yet.

        PERMANENT, and it keeps what it CLOSES OVER. Upstream stores
        a primop with `new PrimOp(...)` in GC memory and the collector
        runs no destructors, so the registration and its callable last
        as long as the process. There is no unregister to add later;
        upstream has nowhere to put one.

        So a callable that captures this state PINS it forever. The
        C++ side takes a weak reference for exactly this reason, and a
        Python closure puts the cycle back:

            state.register_primop(
                "f", 1, lambda v: state.make_int(1))   # state leaks

        Measured, not feared: adding these gates made nanobind report
        two leaked instances and the `EvalState` type at shutdown, and
        removing them made the report go away. Capture what the
        callable needs and not the state, or accept that the evaluator
        lives as long as the process.

        The name is not sanitised. Upstream treats a `__` prefix
        specially - it strips it for the `builtins` attribute and
        keeps it in the base environment - and this passes the name
        through so a caller gets Nix's own behaviour rather than
        ours.

        A base environment holds a fixed number of names, and this
        build patches that number from 128 to 512
        (`nix/patches/nix-base-env-size.patch`). Registering past it
        raises rather than corrupting the heap, which stock Nix does
        not: `addPrimOp` tests no bound at all."""
        Cxx("""
if (name.empty())
    throw std::invalid_argument("empty name");
if (arity < 1)
    // Upstream turns a ZERO-arity primop into a lazy constant: it
    // sets arity to 1 and registers an application of the primop to
    // itself (eval.cc:523). A caller asking for 0 would get something
    // other than what they asked for, and nothing would say so.
    throw std::invalid_argument(
        "arity must be at least 1: nix turns a zero-arity primop into "
        "a lazy constant");
self.register_primop(name, static_cast<std::size_t>(arity), fn);
        """)

    def subscribe_logs(self, capacity: I64 = 1024,
                       level: I64 = 3) -> "LogStream":
        """Record what Nix says on THIS state's thread.

        The direction `register_primop` runs, without a call: nix
        tells Python what it is doing, in the middle of work Python
        asked for.

        The subscription belongs to the THREAD, because an `EvalState`
        is affine and this Nix evaluates on one thread - nothing under
        `src/libexpr` names `eval-cores`, so 2.34.8 has no parallel
        evaluation. So the thread that owns a state is the thread its
        records are raised on, and the queue this returns holds that
        state's records and no other state's.

        Two things it does NOT see, and both are named rather than
        hidden.

        The global `nix::verbosity` filters BEFORE any logger runs
        (`logging.hh:314`), so `level` can only narrow. Asking for
        more than the global gets nothing, and raising the global
        would flood every other logger in the process too.

        A record raised on a fetcher or a file-transfer thread reaches
        no queue, because that thread subscribed to none. A
        process-wide subscriber is what covers those, and the rpc is
        what needs one.

        REPLACES any subscription this thread had, and closes it. Two
        live subscriptions on one thread would each get an arbitrary
        half of the records, which is worse than either getting none.
        """
        Cxx("""
if (capacity < 1)
    throw std::invalid_argument("capacity must be at least 1");
if (level < 0)
    throw std::invalid_argument("level must not be negative");
(void) self;
return huggorm::subscribe_logs(static_cast<std::size_t>(capacity),
                               static_cast<std::uint64_t>(level));
        """)

    def unsubscribe_logs(self) -> None:
        """Stop recording on this state's thread.

        A queue already handed out still drains what it holds. This
        says only that nothing more goes into it.

        It CROSSES the wire and `subscribe_logs` does not, which looks
        like an accident and is not. `subscribe_logs` answers a
        `LogStream`, and a LogStream is a proxy with no service - so
        the answer would be a handle no later call could use, and the
        schema refuses it for that reason. This answers nothing, so
        the reason does not apply.

        What a remote caller can do with it is stop a subscription
        somebody else made on that state's thread. That is the same
        power every shared handle already grants - a second connection
        holding an EvalState can `forget_file` on it too - so it is
        within the sharing model rather than a new hole in it."""
        Cxx("""
(void) self;
huggorm::unsubscribe_logs();
        """)

    def force(self, v: "Value") -> None:
        """Force a value in place. Idempotent.

        Mutates GC-resident memory, and the async layer routes the
        call to this state's OWN thread - which is why a Value is
        affine and travels as a proxy."""
        Cxx("return self.state().forceValue(*v.get(), nix::noPos);")

    # Builders. A caller builds a list or an attribute set one element
    # at a time, and that shape is the WIRE's, not libexpr's: a Nix
    # collection is immutable and sized when it is built, so each call
    # here rebuilds it.
    #
    # The alternative is worse. `make_list(items: list[Value])` would
    # need a container of PROXIES to cross, and one lease per element
    # is not something anything grants in bulk - so it would have no
    # RPC surface at all. One element per call is what crosses.

    @produces("mkInt")
    def make_int(self, value: I64) -> "Value":
        """A forced integer value."""

    def make_string(self, value: Str) -> "Value":
        """A forced string value, with no string context.

        Not a `@produces`: `mkString` takes the state's allocator as a
        second argument, so the call is not "the initialiser with the
        declared arguments"."""
        Cxx("""
auto * made = self.alloc();
made->mkString(value, self.state().mem);
return self.wrap(made);
        """)

    @produces("mkBool")
    def make_bool(self, value: Bint) -> "Value":
        """A forced boolean value."""

    def make_list(self) -> "Value":
        """An empty list. Fill it with `list_append`."""
        Cxx("""
auto * made = self.alloc();
made->mkList(self.state().buildList(0));
return self.wrap_builder(made);
        """)

    @fills("make_list", "list")
    def list_append(self, target: "Value", item: "Value") -> None:
        """Add one element to a list, in place."""
        Cxx("return target.stage(item.get());")

    def make_attrs(self) -> "Value":
        """An empty attribute set. Fill it with `attrs_set`."""
        Cxx("""
auto * made = self.alloc();
auto builder = self.state().buildBindings(0);
made->mkAttrs(builder);
return self.wrap_builder(made);
        """)

    @fills("make_attrs", "attrs")
    def attrs_set(self, target: "Value", name: Str, item: "Value") -> None:
        """Set one attribute, in place.

        Setting a name twice replaces its value, matching an attribute
        set built by assignment."""
        Cxx("return target.stage_attr(name, item.get());")


# --- free functions ------------------------------------------------


@needs("huggorm_decl/cpp/eval.hpp")
@threading("pool")
def gc_stats() -> "dict[str, I64]":
    """Live collector counters, bound straight from gc.h.

    These prove the collector is ACTIVE: a no-op integration cannot
    fake them."""
    Cxx("""
return {
    {"heap_size", static_cast<std::int64_t>(GC_get_heap_size())},
    {"total_bytes", static_cast<std::int64_t>(GC_get_total_bytes())},
    {"bytes_since_gc", static_cast<std::int64_t>(GC_get_bytes_since_gc())},
    {"collections", static_cast<std::int64_t>(GC_get_gc_no())},
    {"used_bytes",
     static_cast<std::int64_t>(GC_get_heap_size() - GC_get_free_bytes())},
    // OURS, not the collector's: how many roots this process holds. A
    // root that is never dropped keeps its value alive forever, and no
    // heap counter can tell that from a heap that simply grew.
    {"live_roots", huggorm::live_roots().load()},
};
    """)


@needs("huggorm_decl/cpp/eval.hpp")
@threading("pool")
@blocks
@binds("huggorm::gc_collect")
def collect_garbage() -> None:
    """Run a full stop-the-world collection (twice).

    Global process state, mirroring libgc: not a method on EvalState.
    Blocking - dispatch it to a thread from async code.

    It registers the calling thread first. Boehm stops the world by
    signalling every registered thread, and a thread it does not know
    cannot answer - the collection aborts the process with "Collecting
    from unknown thread"."""


@needs("huggorm_decl/cpp/eval.hpp")
@binds("huggorm::gc_unregister_thread")
def gc_release_thread() -> None:
    """Take the CURRENT thread off the collector's list.

    Runtime plumbing, not domain surface: it carries no threading
    policy, so the codegen leaves it alone and it has no async or RPC
    form. The absence is the declaration.

    A thread that registered must call this as its last GC action
    before it exits. Boehm stops the world by signalling every
    registered thread and waiting for each to answer. A thread that
    exits while still registered never answers, and the next
    collection aborts the process. That is what a dedicated affine
    executor does when its wrapper is closed."""


@needs("huggorm_decl/cpp/eval.hpp")
@binds("huggorm::install_log_tap")
@startup
def _log_tap_init() -> None:
    """Put the log tap behind the logger already installed, once.

    At import rather than on the first `subscribe_logs`, because
    `nix::logger` is a plain global `unique_ptr` (`logging.hh:258`)
    and replacing it while another thread reads it is a race with no
    lock to take.

    A TEE: the logger that was there stays the MAIN one, so it keeps
    writing to stdout and keeps being the one Nix asks a question
    with. Subscribing to nothing therefore changes nothing."""


@needs("huggorm_decl/cpp/eval.hpp")
@binds("nix::initGC")
@startup
def _gc_init() -> None:
    """Start the collector, once, before any value can exist.

    Bound straight from libexpr. It also calls
    `GC_allow_register_threads`, so the permission is upstream's and
    only the per-thread registration is ours."""
