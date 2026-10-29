"""Errors returned to clients as JSON."""


class RouterError(Exception):
    """A failure we can explain to the caller. Subclasses stay structured."""

    def __init__(self, status: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code

    def body(self) -> dict:
        # OpenAI-shaped so Open WebUI and similar clients surface `message`.
        return {
            "error": {
                "message": self.message,
                "type": "kobold_router_error",
                "code": self.code,
            }
        }


class ReloadRejected(RouterError):
    """KoboldCPP refused the reload, or it never started.

    The previously loaded model, if any, is still the one we advertise.
    """


class ReloadFailed(RouterError):
    """The reload started but the backend did not become ready.

    The loaded-model record is cleared because the process may be mid-restart.
    """
