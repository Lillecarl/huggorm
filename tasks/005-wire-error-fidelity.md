# Wire error fidelity

Review finding 5. remote.py `raise ... from None` clobbers the cause
chain from_dict just rebuilt (cause_type becomes NoneType). Server
guard() lets non-WrapperError exceptions vanish into anonymous
UNKNOWN 'Internal Server Error' - unknown-handle KeyErrors included.

Fix: drop `from None` (keep the decoded cause); guard() wraps ALL
handler exceptions as InternalError(e) before serialization.
