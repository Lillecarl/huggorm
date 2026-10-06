/**
  The plugin against a daemon on loopback TCP, inside the build sandbox.

  The daemon serves a chroot store that holds a path the sandbox's own
  /nix/store does not. So a read that succeeds came over the protocol,
  and the `unix://` case shows that the same read through a local-FS
  store fails.
*/
{
  runCommand,
  nix,
  nix-tcp-store,
  socat,
}:
runCommand "nix-tcp-store-check-${nix-tcp-store.version}"
  {
    nativeBuildInputs = [
      nix
      socat
    ];
  }
  ''
    export HOME=$TMPDIR NIX_STATE_DIR=$TMPDIR/state NIX_CONF_DIR=$TMPDIR/conf NIX_LOG_DIR=$TMPDIR/log
    export NIX_CONFIG="extra-experimental-features = nix-command"
    plugin="--option plugin-files ${nix-tcp-store}/lib/nix/plugins"
    remote="local?root=$TMPDIR/remote"

    echo over-tcp > f.txt
    path=$(nix store add-file --store "$remote" ./f.txt)
    if [ -e "$path" ]; then echo "precondition: $path must not be in the sandbox store"; exit 1; fi

    socat TCP-LISTEN:37001,bind=127.0.0.1,fork,reuseaddr EXEC:"nix daemon --stdio --store $remote" &
    socat UNIX-LISTEN:$TMPDIR/d.sock,fork EXEC:"nix daemon --stdio --store $remote" &
    for _ in $(seq 100); do
      nix $plugin store ping --store tcp://127.0.0.1:37001 >/dev/null 2>&1 && break
      sleep 0.1
    done

    echo "--- without the plugin, nix knows no tcp:// store"
    if nix path-info --store tcp://127.0.0.1:37001 "$path" 2>err.txt; then exit 1; fi
    grep tcp err.txt

    echo "--- path-info, store cat and copy --from, over tcp://"
    [ "$(nix $plugin path-info --store tcp://127.0.0.1:37001 "$path")" = "$path" ]
    [ "$(nix $plugin store cat --store tcp://127.0.0.1:37001 "$path")" = over-tcp ]
    nix $plugin copy --no-check-sigs --from tcp://127.0.0.1:37001 --to "local?root=$TMPDIR/pulled" "$path"
    [ "$(nix store cat --store "local?root=$TMPDIR/pulled" "$path")" = over-tcp ]

    echo "--- copy --to tcp://"
    echo pushed > g.txt
    pushed=$(nix store add-file --store "local?root=$TMPDIR/source" ./g.txt)
    nix $plugin copy --no-check-sigs --from "local?root=$TMPDIR/source" --to tcp://127.0.0.1:37001 "$pushed"
    [ "$(nix store cat --store "$remote" "$pushed")" = pushed ]

    echo "--- the same read through unix:// reads the local store, and fails"
    if nix store cat --store "unix://$TMPDIR/d.sock" "$path" 2>err.txt; then exit 1; fi
    # 2.34 says "does not exist" and 2.35 "No such file or directory"; both
    # name the path that the local filesystem lacks.
    grep -F "$path" err.txt

    touch $out
  ''
