"""Local versioned HTTP control plane. Thin adapter over application services."""

from projectos.http.app import create_app

__all__ = ["create_app"]
