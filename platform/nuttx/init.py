"""Direct NuttX application entry from a typed product description."""
import json
import re


def application_config(platform: dict, command: str, priority: int, stack: int) -> dict[str, str]:
    init = platform.get("init")
    if init is None:
        return {}
    if (not isinstance(init, dict) or set(init) != {"command", "args"}
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", command)
            or init["command"] != command):
        raise ValueError("init must name the built application command and its args")
    args = init["args"]
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
    if command == "stream_test" and args[:1] == ["boot-tls"]:
        hal = platform.get("hal", {})
        if ("mcuboot" not in platform or hal.get("boot") != "nuttx"
                or hal.get("watchdog") != "nuttx"):
            raise ValueError("boot-tls init requires MCUboot, boot control and inherited watchdog")
        values["NETINIT_THREAD"] = "n"
        values["NETUTILS_NETINIT"] = "y"
    return values
