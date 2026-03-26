"""
Entry point for running the backend as a module.
=================================================

Usage:
    python -m backend

This starts the FastAPI web server on the host/port defined in config.py
(defaults to localhost:8000). The server provides the web UI alternative
to the Jupyter notebook for processing CLO emails.

After starting, open http://localhost:8000 in your browser.
For API docs, open http://localhost:8000/docs.
"""
from backend.app import app
from backend import config
import uvicorn

if __name__ == "__main__":
    uvicorn.run("backend.app:app", host=config.HOST, port=config.PORT, reload=False)
