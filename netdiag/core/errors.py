"""The exception type for bad user input."""


class InputError(Exception):
    """Bad user input: an option value, a check name, an input or output file.

    Raise it with a message already in the user's language (``tr()``). The CLI
    prints the message and exits with ``ExitCode.USAGE`` (4). Subclass it for a
    specific source, such as a device list.
    """
