/**
  The Nix versions huggorm builds, each with the nixpkgs component scope
  it starts from and every patch huggorm puts on it. Each patch file
  starts with its reason and its measurement.

  The order of a list is part of the derivation. A reordered list builds
  the same Nix under a different store path.
*/
{ pkgs }:
let
  patch = name: ./patches + "/${name}.patch";

  # A thread-local `Bindings::emptyBindings`: `ExprAttrs::eval` writes the
  # shared one from every evaluator thread. 2.36 makes it `constinit`.
  emptyBindings = patch "nix-thread-local-empty-bindings";
  # The base environment and `builtins` hold 512 slots, not 128, and a
  # write past the end throws (huggorm#33).
  baseEnvSize = patch "nix-base-env-size";
  # `gmtime_r` for `lastModified`. 2.36 changes the line it replaces.
  gmtime234 = patch "nix-2.34-gmtime-not-thread-safe";
  gmtime236 = patch "nix-2.36-gmtime-not-thread-safe";
  # Upstream 5c4f498d3, in 2.35: an interrupted thunk can be forced again
  # (huggorm#97). It applies after `baseEnvSize`.
  interruptedThunk234 = patch "nix-2.34-interrupted-thunk-recovers";
  # One temporary roots file per LocalStore, not per process.
  tempRoots = patch "nix-temp-roots-per-store";
  # `setOptions` sends a daemon a level other than `nix::verbosity`
  # (huggorm#102).
  remoteVerbosity = patch "nix-remote-verbosity";
  remoteVerbosity236 = patch "nix-2.36-remote-verbosity";
  # `statisticsJSON`, the `count-calls` setting, and concurrent call
  # counts.
  countCalls234 = patch "nix-2.34-count-calls";
  countCalls235 = patch "nix-2.35-count-calls";
  countCalls236 = patch "nix-2.36-count-calls";
  # A parsed path literal held its accessor, so the Store, for the life of
  # the process (huggorm#128).
  exprPath = patch "nix-expr-path-holds-no-accessor";
  # The same, for the `ExprParseFile` every `import` allocates (huggorm#128).
  parseFile = patch "nix-parse-file-holds-no-accessor";
  # The `open_tree` polyfill defines `AT_RECURSIVE`, which glibc 2.28
  # lacks: the manylinux build needs it. 2.34 does not call `open_tree`.
  atRecursive = patch "nix-open-tree-at-recursive";
  # A pure evaluation checks for an interrupt once per function call
  # (huggorm#158). On 2.34 it applies after `interruptedThunk234`.
  callChecksInterrupt = patch "nix-call-function-checks-interrupt";
in
{
  nix_2_34 = {
    components = pkgs.nixVersions.nixComponents_2_34;
    patches = [
      emptyBindings
      baseEnvSize
      gmtime234
      interruptedThunk234
      tempRoots
      remoteVerbosity
      countCalls234
      exprPath
      parseFile
      callChecksInterrupt
    ];
  };
  nix_2_35 = {
    components = pkgs.nixVersions.nixComponents_2_35;
    patches = [
      emptyBindings
      baseEnvSize
      gmtime234
      tempRoots
      remoteVerbosity
      countCalls235
      atRecursive
      exprPath
      parseFile
      callChecksInterrupt
    ];
  };
  # Unpinned on purpose: it follows nixpkgs' `nixComponents_git`, so a
  # patch that stops applying fails here first.
  git = {
    components = pkgs.nixVersions.nixComponents_git;
    patches = [
      baseEnvSize
      gmtime236
      tempRoots
      remoteVerbosity236
      countCalls236
      atRecursive
      exprPath
      parseFile
      callChecksInterrupt
    ];
    # Nix master uses `#embed`, which needs GCC 15. Every PyPA image
    # ships gcc-toolset-14 (huggorm#109).
    wheels = false;
  };

  # On every collector a lane links, after nixpkgs' own `nixDependencies`
  # configuration: the large config and a 1 MiB initial mark stack.
  boehmgcPatches = [
    # `pthread_kill` gives EINVAL, not ESRCH, for a thread that exits
    # during a stop-the-world.
    (patch "boehmgc-tolerate-suspend-thread-exit-race")
    # The first `%d` in `GC_LOG_FILE` is the process id.
    (patch "boehmgc-log-file-pid")
    # `GC_set_all_interior_pointers` after `GC_init` keeps offset 0
    # (huggorm#101).
    (patch "bdwgc-late-interior-pointers")
  ];
}
