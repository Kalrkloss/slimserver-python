"""Web interface for Lyrion Music Server."""
from .server import WebServer
from .api import JSONRPCAPI, WebAPIHandler

__all__ = ["WebServer", "JSONRPCAPI", "WebAPIHandler"]
