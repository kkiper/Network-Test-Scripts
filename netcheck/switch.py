"""SSH sessions to the test switch and (read-only) production switches, via Netmiko."""

from __future__ import annotations

import socket
from typing import Optional


class SwitchError(Exception):
    """Raised for problems talking to the test switch."""


class NetmikoSession:
    """Minimal command interface used by port verification.

    Anything with the same ``send``/``run``/``privileged``/``close`` members can
    stand in for it (the tests use a fake).
    """

    def __init__(self, host: str, username: str, password: str, secret: str = "",
                 device_type: str = "cisco_xe", port: int = 22, timeout: float = 15,
                 source: Optional[str] = None):
        try:
            import netmiko
        except ImportError as exc:
            raise SwitchError(
                "The 'netmiko' package is needed to talk to the switches.\n"
                "Install it with:  pip install netmiko") from exc
        self.host = host
        self.hostname = ""
        sock = None
        if source:
            # Open the TCP connection from the wired interface's address, so the
            # SSH session can't leave through Wi-Fi.
            try:
                sock = socket.create_connection((host, port), timeout=timeout,
                                                source_address=(source, 0))
            except OSError as exc:
                raise SwitchError(
                    f"Could not reach {host} on SSH port {port} from the wired interface "
                    f"({source}) - check the cabling, the IP addresses, and "
                    f"that SSH is enabled. ({exc})") from exc
        try:
            self.conn = netmiko.ConnectHandler(
                device_type=device_type, host=host, username=username, password=password,
                secret=secret or "", port=port, conn_timeout=timeout,
                **({"sock": sock} if sock is not None else {}))
            if secret and not self.conn.check_enable_mode():
                self.conn.enable()
            self.hostname = self.conn.find_prompt().rstrip("#>").strip()
        except Exception as exc:  # netmiko raises a variety of errors
            if sock is not None:
                sock.close()
            if isinstance(exc, netmiko.NetmikoAuthenticationException):
                raise SwitchError(
                    f"Login to {host} failed - check the username and password.") from exc
            if isinstance(exc, netmiko.NetmikoTimeoutException):
                raise SwitchError(
                    f"Could not reach {host} on SSH port {port} - check the IP address, your "
                    "cabling, and that SSH is enabled.") from exc
            raise SwitchError(f"Could not connect to {host}: {exc}") from exc

    @property
    def privileged(self) -> bool:
        return bool(self.conn.check_enable_mode())

    def enable(self, secret: str) -> None:
        """Enter privileged EXEC mode with ``secret``."""
        try:
            self.conn.secret = secret
            self.conn.enable()
        except Exception as exc:
            raise SwitchError(f"Could not enter enable mode on {self.host}: {exc}") from exc

    def send(self, command: str, read_timeout: float = 60) -> str:
        """Run a command that returns to the prompt (show commands)."""
        try:
            return str(self.conn.send_command(command, read_timeout=read_timeout))
        except Exception as exc:
            raise SwitchError(f"'{command}' failed on {self.host}: {exc}") from exc

    def run(self, command: str, read_timeout: float = 30) -> str:
        """Run an exec command that may ask for confirmation (clear/test commands)."""
        try:
            output = str(self.conn.send_command_timing(command, read_timeout=read_timeout))
            if "[confirm]" in output or "[yes/no]" in output.lower():
                reply = "\n" if "[confirm]" in output else "yes"
                output += str(self.conn.send_command_timing(reply, read_timeout=read_timeout))
            return output
        except Exception as exc:
            raise SwitchError(f"'{command}' failed on {self.host}: {exc}") from exc

    def close(self) -> None:
        try:
            self.conn.disconnect()
        except Exception:
            pass


def connect(settings, password: str, secret: str = "", iface=None) -> NetmikoSession:
    """Open a session to the test switch described by ``settings``.

    With ``iface`` (a netif.Interface) the session goes out through that interface.
    """
    if not settings.host:
        raise SwitchError("No test switch address set.")
    if not settings.username:
        raise SwitchError("No test switch username set.")
    return NetmikoSession(settings.host, settings.username, password, secret,
                          device_type=settings.device_type, port=settings.ssh_port,
                          source=iface.address if iface is not None else None)



def connect_to(host: str, username: str, password: str, device_type: str = "cisco_xe",
               ssh_port: int = 22, iface=None) -> NetmikoSession:
    """Open a session to any switch (e.g. a production switch for a read-only audit)."""
    if not host:
        raise SwitchError("No switch address set.")
    if not username:
        raise SwitchError(f"No username set for {host}.")
    return NetmikoSession(host, username, password, device_type=device_type, port=ssh_port,
                          source=iface.address if iface is not None else None)
