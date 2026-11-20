"""Python function dispatch across local processes and recursive SSH workers."""

import logging

from .config import localprocess, networkprocess, proxyprocess, tupleprocess
from .futures import RPC_Future
from .pool import Pool
from .runners import NetworkRunner, ProcessRunner, ProxyRunner
from .scheduler import Recursive_RPC
from .setup import SetupScript
from .ssh import ShellAwareMachine, SSHConnection, shell_aware

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "Recursive_RPC",
    "RPC_Future",
    "Pool",
    "ProcessRunner",
    "NetworkRunner",
    "ProxyRunner",
    "tupleprocess",
    "localprocess",
    "networkprocess",
    "proxyprocess",
    "SSHConnection",
    "ShellAwareMachine",
    "shell_aware",
    "SetupScript",
    "hello",
]


def hello() -> str:
    """Return the original greeting retained for compatibility."""
    return "Hello from recursive_rpc!"
