import asyncio

from viam.module.module import Module

import sdk_log_channel

# Imported for its side effect: subclassing EasyResource registers the model in
# the global registry, which is what run_from_registry() then serves.
from models.camera import Camera as SonyCameraModel  # noqa: F401

if __name__ == "__main__":
    # Before the module connects to viam-server: module logs get their own
    # connection instead of sharing (and corrupting) the module's main one.
    # CONSULT-2296 / CONSULT-2298; see sdk_log_channel.py.
    sdk_log_channel.install()
    asyncio.run(Module.run_from_registry())
