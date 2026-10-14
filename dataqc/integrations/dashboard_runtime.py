"""Attach hourly dashboard synchronization to an existing workbench lifespan."""
from contextlib import asynccontextmanager

from dataqc.collection_dashboard_api import inventory
from dataqc.dashboard_sync import RemoteInventory


def install_runtime(app):
    original=app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            index=inventory()
            if isinstance(index,RemoteInventory):index.start()
            try:yield
            finally:
                if isinstance(index,RemoteInventory):index.stop()

    app.router.lifespan_context=lifespan
