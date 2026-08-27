import os
from fastapi.templating import Jinja2Templates
from backend.core.config import settings
from backend.core.security import generate_csrf_token

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.globals["settings"] = settings
# S-03: makes `{{ csrf_token(request) }}` available in every template
# without every GET route having to thread it through its own context dict
# -- `request` is already injected into every TemplateResponse's context by
# Jinja2Templates itself.
templates.env.globals["csrf_token"] = generate_csrf_token
