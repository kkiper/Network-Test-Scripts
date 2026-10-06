"""SSH session to the test switch (via Netmiko, imported only when needed)."""

from __future__ import annotations


class SwitchError(Exception):
    """Raised for problems talking to the test switch."""


class NetmikoSession:
    """Minimal command interface used by port verification.

    Anything with the same ``send``/``run``/``privileged``/``close`` members can
    stand in for it (the tests use a fake).
    """

    def __init__(self, host: str, username: str, password: str, secret: str = "",
                 device_type: str = "cisco_xe", port: int = 22, timeout: float = 15):
        try:
            import netmiko
        except ImportError as exc:
            raise SwitchError(
                "The 'netmiko' package is needed to talk to the test switch.\n"
                "Install it with:  pip install netmiko") from exc
        self.host = host
        self.hostname = ""
        try:
            self.conn = netmiko.ConnectHandler(
                device_type=device_type, host=host, username=username, password=password,
                secret=secret or "", port=port, conn_timeout=timeout)
            if secret and not self.conn.check_enable_mode():
                self.conn.enable()
            self.hostname = self.conn.find_prompt().rstrip("#>").strip()
        except netmiko.NetmikoAuthenticationException as exc:
            raise SwitchError(f"Login to {host} failed - check the username and password.") from exc
        except netmiko.NetmikoTimeoutException as exc:
            raise SwitchError(
                f"Could not reach {host} on SSH port {port} - check the IP address, your "
                "cabling to the test switch's management port, and that SSH is enabled.") from exc
        except Exception as exc:  # netmiko raises a variety of errors
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


def connect(settings, password: str, secret: str = "") -> NetmikoSession:
    """Open a session to the test switch described by ``settings``."""
    if not settings.host:
        raise SwitchError("No test switch address set.")
    if not settings.username:
        raise SwitchError("No test switch username set.")
    return NetmikoSession(settings.host, settings.username, password, secret,
                          device_type=settings.device_type, port=settings.ssh_port)

