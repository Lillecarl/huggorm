"""Stand-in for cythonix_bindings.errors."""


class ProbeError(Exception):
    def __init__(self, message, colored=""):
        super().__init__(message)
        self.colored = colored
