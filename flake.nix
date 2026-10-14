# A wrapper: every output is an attribute of `default.nix`. Work on huggorm
# happens in nixidae, which this repository is exported from. `flake.lock`
# names the nixidae commit; lock-subtrees writes it.
{
  inputs.nixidae = {
    url = "github:nixidae/nixidae";
    flake = false;
  };

  outputs =
    { nixidae, ... }:
    let
      sources = import "${nixidae}/nix/wire.nix" { };
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems =
        f:
        builtins.listToAttrs (
          map (system: {
            name = system;
            value = f system;
          }) systems
        );
      each = forAllSystems (
        system:
        import ./. {
          inherit sources;
          pkgs = import sources.nixpkgs {
            inherit system;
            config = { };
            overlays = [ ];
          };
        }
      );
    in
    {
      packages = builtins.mapAttrs (_: p: {
        default = p.huggorm;
        inherit (p) huggorm huggorm-bindings;
      }) each;
    };
}
