"""Direct NuttX application entry from a typed product description."""
import json
import re


def application_config(platform: dict, command: str, priority: int, stack: int) -> dict[str, str]:
    init = platform.get("init")
    if init is None:
        return {}
    if (not isinstance(init, dict) or set(init) - {"args", "console", "network", "priority", "stack-bytes"}
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", command)):
        raise ValueError("init must contain only target startup settings")
    args = init.get("args", [])
    priority = init.get("priority", priority)
    stack = init.get("stack-bytes", stack)
    if (not isinstance(args, list) or len(args) > 16
            or any(not isinstance(arg, str) or len(arg) > 128
                   or any(not 32 <= ord(char) < 127 or char in '\\"' for char in arg) for arg in args)
            or type(priority) is not int or not 1 <= priority <= 255
            or type(stack) is not int or stack <= 0):
        raise ValueError("invalid bounded init arguments, priority or stack")
    values = {
        "INIT_ENTRY": "y",
        "INIT_ENTRYPOINT": json.dumps(command + "_main"),
        "INIT_ARGS": json.dumps(",".join(json.dumps(arg) for arg in args)),
        "INIT_PRIORITY": str(priority),
        "INIT_STACKSIZE": str(stack),
    }
    if "console" in init:
        if init["console"] != "uart0":
            raise ValueError("unsupported init console")
        values.update(UART0_SERIAL_CONSOLE="y", OTHER_SERIAL_CONSOLE="n")
    if platform.get("boot-watchdog-ms") is not None:
        hal = platform.get("hal", {})
        if "mcuboot" not in platform or hal.get("boot") != "nuttx" or hal.get("watchdog") != "nuttx":
            raise ValueError("inherited boot watchdog requires MCUboot and boot/watchdog control")
    if "network" in init:
        if init["network"] not in ("external", "on-acquire"):
            raise ValueError("unsupported network initialization policy")
        if init["network"] == "on-acquire":
            values["NETINIT_THREAD"] = "n"
            values["NETUTILS_NETINIT"] = "y"
    return values
