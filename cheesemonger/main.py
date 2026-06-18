from .config import get_settings
from .startup import create_app

app = create_app(get_settings())
