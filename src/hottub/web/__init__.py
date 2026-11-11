"""Web: the HTTP face of the application.

The pages people use (``pages.py``, ``views.py``, ``templates/``,
``static/``) and the JSON API programs use (``api.py``, ``schemas.py``).
Both offer the same actions. Handlers hold no business logic and call
``hottub.control``, ``hottub.telemetry``, ``hottub.scheduler`` and
``hottub.watchdog``.
"""

from hottub.web.server import Lifespan, create_web_app

__all__ = ["Lifespan", "create_web_app"]
