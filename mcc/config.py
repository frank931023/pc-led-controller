"""Every setting the control center reads from the environment (.env in Docker), in one place."""

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # The whole data/ directory is mounted in Docker, not the single file: bind-mounting a
    # file that doesn't exist yet makes Docker create a *directory* with that name.
    config_file: str
    # Alert rules sit next to the device list (same data/ directory, same Docker mount).
    alerts_file: str

    # Seconds a single command may take. Each Pi reports its own COMMAND_TIMEOUT and
    # MAX_COMMAND_TIMEOUT in /api/status and those win; these are only used before the
    # first successful status check (or with a Pi too old to report them).
    default_command_timeout: float
    max_command_timeout: float

    # Optional shared secret. When set, every request that changes something (and the
    # agent settings, which contain the Beszel registration token) needs
    # "Authorization: Bearer <API_TOKEN>". The web UI asks for it once and remembers it.
    api_token: str

    # Beszel hub (health monitoring). Empty beszel_url turns monitoring off.
    beszel_url: str            # how *this server* reaches the hub
    beszel_email: str
    beszel_password: str
    beszel_port: int           # hub port published on the PC
    beszel_public_url: str     # how browsers / Pis reach the hub; empty = same host as this page


def load_settings():
    config_file = os.getenv('CONFIG_FILE', os.path.join('data', 'pi_config.json'))
    return Settings(
        config_file=config_file,
        alerts_file=os.getenv('ALERTS_FILE', os.path.join(os.path.dirname(config_file) or '.', 'alerts.json')),
        default_command_timeout=float(os.getenv('DEFAULT_COMMAND_TIMEOUT', '130')),
        max_command_timeout=float(os.getenv('MAX_COMMAND_TIMEOUT', '300')),
        api_token=os.getenv('API_TOKEN', ''),
        beszel_url=os.getenv('BESZEL_URL', ''),
        beszel_email=os.getenv('BESZEL_EMAIL', ''),
        beszel_password=os.getenv('BESZEL_PASSWORD', ''),
        beszel_port=int(os.getenv('BESZEL_PORT', '8090')),
        beszel_public_url=os.getenv('BESZEL_PUBLIC_URL', '').rstrip('/'),
    )
