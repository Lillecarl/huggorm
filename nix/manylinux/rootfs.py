"""Unpack a docker-archive into a directory that bwrap can use as `/`.

The layers apply in manifest order, with OCI whiteouts. A store path
cannot hold a setuid bit or a device node, so the bits go and the
nodes are skipped: bwrap mounts its own /dev over the rootfs anyway.
"""

import json
import os
import shutil
import stat
import sys
import tarfile


def remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def apply_layer(layer: tarfile.TarFile, root: str) -> None:
    for member in layer:
        parent = os.path.join(root, os.path.dirname(member.name))
        base = os.path.basename(member.name)
        if base == ".wh..wh..opq":
            for entry in os.listdir(parent):
                remove(os.path.join(parent, entry))
            continue
        if base.startswith(".wh."):
            remove(os.path.join(parent, base.removeprefix(".wh.")))
            continue
        target = os.path.join(root, member.name)
        if not (member.isdir() and os.path.isdir(target)):
            remove(target)
        if member.ischr() or member.isblk() or member.isfifo():
            continue
        member.mode = (member.mode & 0o777) | stat.S_IWUSR
        layer.extract(member, root, set_attrs=not member.issym(), filter="fully_trusted")


def main(archive: str, root: str) -> None:
    with tarfile.open(archive) as image:
        manifest = image.extractfile("manifest.json")
        if manifest is None:
            raise RuntimeError(f"{archive} has no manifest.json")
        os.makedirs(root)
        for name in json.load(manifest)[0]["Layers"]:
            blob = image.extractfile(name)
            if blob is None:
                raise RuntimeError(f"{archive} names a layer {name} it does not hold")
            with tarfile.open(fileobj=blob) as layer:
                apply_layer(layer, root)
    # Mount points: bwrap cannot create one on a read-only root. /build is
    # the sandbox's build directory.
    for mount in ("nix/store", "build"):
        os.makedirs(os.path.join(root, mount), exist_ok=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
