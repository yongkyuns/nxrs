"""Small shared checks for NuttX shell qualification."""


def verify_identity(command):
    """Query the running shell; an optional startup greeting is not identity.

    The caller sends one fresh command and returns only its response through
    the next prompt. The command echo itself cannot satisfy the exact line.
    """
    response = command("uname")
    if response.replace("\r", "").splitlines().count("NuttX") != 1:
        raise ValueError("missing or duplicate NuttX uname response")
    return response
