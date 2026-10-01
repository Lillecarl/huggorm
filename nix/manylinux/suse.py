"""huggorm from its wheels, on openSUSE Leap's own Python.

A venv on the distribution's python3, and pip with no index and the
wheelhouse as its only source: what a host without Nix does, without
the network. pip resolves every dependency from the wheels' metadata,
so a dependency the metadata forgets fails the import here. The
control is the same venv before the install.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    suse, settings = vms.suse, vms.settings
    await suse.add_closure(settings["closure"])

    version = (await suse.succeed("python3 -c 'import sys; print(sys.version.split()[0])'")).strip()
    await suse.succeed("python3 -m venv /tmp/venv")
    status, _ = await suse.execute("/tmp/venv/bin/python -c 'import huggorm'")
    if status == 0:
        raise AssertionError("huggorm imports in a venv nothing was installed into")
    print(f"[test] SUSE's python3 is {version}, and a fresh venv on it has no huggorm")

    out = await suse.succeed(
        f"/tmp/venv/bin/pip install --no-index --find-links {settings['wheelhouse']} huggorm",
        timeout=300,
    )
    installed = next((line for line in out.splitlines() if line.startswith("Successfully")), out)
    print(f"[test] {installed}")

    said = await suse.succeed(
        f"/tmp/venv/bin/python {settings['smoke']} {settings['modules']}", timeout=300
    )
    print(f"[test] {said.strip()}")
