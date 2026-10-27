"""Narrow offline SDK commands for a boot build. No serial or network operations."""
import hashlib
from pathlib import Path
import os
import stat
import sys


def require(value):
    if not value:
        raise ValueError("offline SDK command refused")


def reject(*args, **kwargs):
    raise RuntimeError("serial and network access are disabled in this build")


def expected_operand(actual, expected):
    path = Path(expected)
    return actual == expected or (actual == path.name and Path.cwd().resolve() == path.parent.resolve())


def main(kind, expected_key, key_sha256, expected_elf, expected_image, arguments):
    if kind == "imgtool":
        require(arguments == ["getpub", "-k", expected_key])
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        handle = os.open(expected_key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(handle)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 4096)
            data = os.read(handle, 4097)
        finally:
            os.close(handle)
        require(len(data) <= 4096 and data.startswith(b"-----BEGIN PUBLIC KEY-----\n"))
        key = serialization.load_pem_public_key(data)
        require(isinstance(key, rsa.RSAPublicKey) and key.key_size == 3072)
        der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
        require(hashlib.sha256(der).hexdigest() == key_sha256)
    elif kind == "esptool":
        require(arguments[:3] == ["--chip", "esp32s3", "elf2image"])
        values = {}
        index = 3
        while index < len(arguments) - 1:
            option = arguments[index]
            require(option in ("--min-rev", "--flash_mode", "--flash_freq", "--flash_size", "-o")
                    and option not in values and index + 1 < len(arguments) - 1)
            values[option] = arguments[index + 1]
            index += 2
        require(bool(arguments) and expected_operand(arguments[-1], expected_elf))
        require("-o" in values and expected_operand(values.pop("-o"), expected_image))
        require(values == {"--min-rev": "0", "--flash_mode": "dio", "--flash_freq": "40m",
                           "--flash_size": "16MB"})
        require(Path(expected_elf).is_file() and not Path(expected_elf).is_symlink())
        require(not Path(expected_image).is_symlink())
        if Path(expected_image).exists():
            info = Path(expected_image).stat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid())
    else:
        require(False)
    # Patch before importing vendor command implementations. Selection above also
    # excludes alternate output paths, serial ports, flash/eFuse commands and servers.
    import serial
    import socket
    serial.Serial.open = reject
    serial.serial_for_url = reject
    socket.create_connection = reject
    socket.getaddrinfo = reject
    socket.socket.connect = reject
    socket.socket.connect_ex = reject
    socket.socket.bind = reject
    socket.socket.listen = reject
    socket.socket.send = reject
    socket.socket.sendall = reject
    socket.socket.sendto = reject
    sys.argv = [kind, *arguments]
    if kind == "imgtool":
        from imgtool.main import imgtool
        imgtool()
    else:
        import esptool
        esptool._main()
