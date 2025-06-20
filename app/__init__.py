import os
from flask import Flask
from dotenv import load_dotenv
from datetime import timedelta

def create_app():
    load_dotenv()
    
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY")
    app.permanent_session_lifetime = timedelta(days=7)

    if not app.debug:
        import logging
        from logging import FileHandler
        file_handler = FileHandler('error.log')
        file_handler.setLevel(logging.WARNING)
        app.logger.addHandler(file_handler)

    with app.app_context():
        from . import routes
        app.register_blueprint(routes.bp)

    return app