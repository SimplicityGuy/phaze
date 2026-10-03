"""Installed Phaze release version shared by the API and operator shell."""

import importlib.metadata


APP_VERSION = importlib.metadata.version("phaze")
