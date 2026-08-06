import asyncio

from viam.module.module import Module

# Imported for its side effect: subclassing EasyResource registers the model in
# the global registry, which is what run_from_registry() then serves.
from models.camera import Camera as SonyCameraModel  # noqa: F401

if __name__ == "__main__":
    asyncio.run(Module.run_from_registry())
