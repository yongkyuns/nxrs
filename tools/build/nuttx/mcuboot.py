"""Qualification compatibility; Rust owns boot configuration and validation."""
from platform_config import _query
MCUBOOT_REVISION = '6d3b3d2c38ab20c242e5b9abb04d050086383eb2'
HAL_REVISION = '911dbec8e4a92e70056b58a3d2b0d965b8b7bcc9'


def protected(platform):
    return _query('protected', platform)


def application_config(platform):
    return _query('boot-application', platform)


def watchdog_timeout_ms(platform):
    return _query('watchdog', platform)


def bootloader_config(platform, root):
    return _query('bootloader', platform, **{'key-root': str(root)})
