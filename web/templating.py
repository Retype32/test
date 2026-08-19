import os
from fastapi.templating import Jinja2Templates
from backend.core.config import settings

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.globals["settings"] = settings
