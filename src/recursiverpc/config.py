"""Descriptors for local processes, SSH workers, and proxy groups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from plumbum import SshMachine
from plumbum.machines.paramiko_machine import ParamikoMachine

from .ssh import RemoteShell, SSHConnection


@dataclass
class tupleprocess:
    number: int


@dataclass
class localprocess(tupleprocess):
    number: int


@dataclass
class networkprocess(tupleprocess):
    number: int
    connection: (
        SshMachine | ParamikoMachine | SSHConnection | RemoteShell | tuple[str, int]
    )


@dataclass
class proxyprocess(tupleprocess):
    number: int
    host: str | None
    port: int | None
    client: list[localprocess | networkprocess | "proxyprocess"]
    ssh_login: Tuple
    ssh_remote: Dict[str, Tuple]
