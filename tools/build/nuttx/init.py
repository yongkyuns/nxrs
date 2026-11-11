"""Qualification compatibility for the Rust entry configuration."""
from platform_config import _query


def application_config(platform, command, priority, stack):
    return _query('init', platform, command=command, priority=priority, stack=stack)
