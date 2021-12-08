import asyncio
import functools
import inspect
import json
import os
import sys
from typing import Dict

from .async_utils import retry
from .config import config, logger
from .exception import RemoteError
from .grpc_utils import BLOCKING_REQUEST_TIMEOUT, GRPC_REQUEST_TIMEOUT
from .mount import get_sha256_hex_from_content  # TODO: maybe not
from .object import Object, requires_create
from .proto import api_pb2


def _make_bytes(s):
    assert type(s) in (str, bytes)
    return s.encode("ascii") if type(s) is str else s


def get_python_version():
    return config["image_python_version"] or "%d.%d.%d" % sys.version_info[:3]


class Image(Object):
    def __init__(self, session, tag):
        super().__init__(tag=tag, session=session)

    def is_inside(self):
        # This is used from inside of containers to know whether this container is active or not
        env_image_id = os.getenv("POLYESTER_IMAGE_ID")
        image_id = self.object_id
        logger.debug(f"Is image inside? env {env_image_id} image {image_id}")
        return image_id is not None and env_image_id == image_id


class CustomImage(Image):
    """This might be a temporary thing until we can simplify other code.

    Needed to rewrite all the other subclasses to use composition instead of inheritance."""

    def __init__(self, base_images={}, context_files={}, dockerfile_commands=[], must_create=False):
        self._base_images = base_images
        self._context_files = context_files
        self._dockerfile_commands = dockerfile_commands
        self._must_create = must_create
        # Note that these objects have neither sessions nor tags
        # They rely on the factories for this
        super().__init__(session=None, tag=None)

    async def _create_impl(self, session):
        # Recursively build base images
        base_image_ids = await asyncio.gather(*(session.create_object(image) for image in self._base_images.values()))
        base_images_pb2s = [
            api_pb2.BaseImage(docker_tag=docker_tag, image_id=image_id)
            for docker_tag, image_id in zip(self._base_images.keys(), base_image_ids)
        ]

        context_file_pb2s = [
            api_pb2.ImageContextFile(filename=filename, data=data) for filename, data in self._context_files.items()
        ]

        dockerfile_commands = [_make_bytes(s) for s in self._dockerfile_commands]
        image_definition = api_pb2.Image(
            base_images=base_images_pb2s,
            dockerfile_commands=dockerfile_commands,
            context_files=context_file_pb2s,
        )

        req = api_pb2.ImageGetOrCreateRequest(
            session_id=session.session_id,
            image=image_definition,
            must_create=self._must_create,
        )
        resp = await session.client.stub.ImageGetOrCreate(req)
        image_id = resp.image_id

        logger.debug("Waiting for image %s" % image_id)
        while True:
            request = api_pb2.ImageJoinRequest(
                image_id=image_id,
                timeout=BLOCKING_REQUEST_TIMEOUT,
                session_id=session.session_id,
            )
            response = await retry(session.client.stub.ImageJoin)(request, timeout=GRPC_REQUEST_TIMEOUT)
            if not response.result.status:
                continue
            elif response.result.status == api_pb2.GenericResult.Status.FAILURE:
                raise RemoteError(response.result.exception)
            elif response.result.status == api_pb2.GenericResult.Status.SUCCESS:
                break
            else:
                raise RemoteError("Unknown status %s!" % response.result.status)

        return image_id


class ImageFactory(Image):
    """Acts as a wrapper for a transient Image object.

    Puts a tag and optionally a session on it. Otherwise just "steals" the image id from the
    underlying image at construction time.
    """

    def __init__(self, fun, args=None, kwargs=None):  # TODO: session?
        self._fun = fun
        self._args = args
        self._kwargs = kwargs
        # TODO: merge code with FunctionInfo, get module name too
        # TODO: break this out into a utility function
        if self._args is not None:
            args = inspect.signature(fun).bind(*self._args, **self._kwargs)
            args.apply_defaults()
            args = list(args.arguments.values())
            args = json.dumps(args)
            args = "(" + args[1:-1] + ")"  # replace the outer [] with ()
            tag = fun.__name__ + args
        else:
            tag = fun.__name__
        super().__init__(session=None, tag=tag)

    async def _create_impl(self, session):
        if self._args is not None:
            image = self._fun(*self._args, **self._kwargs)
        else:
            image = self._fun()
        image_id = await session.create_object(image)
        # Note that we can "steal" the image id from the other image
        # and set it on this image. This is a general trick we can do
        # to other objects too.
        return image_id

    def __call__(self, *args, **kwargs):
        """Binds arguments to this image."""
        assert self._args is None
        assert self._kwargs is None
        return ImageFactory(self._fun, args=args, kwargs=kwargs)


image_factory = ImageFactory  # Make it look nice as a decorator


class LocalImage(Image):
    # TODO: merge this into CustomImage
    def __init__(self, session, python_executable):
        super().__init__(tag="local", session=session)
        self.python_executable = python_executable

    async def _create_impl(self, session):
        image_definition = api_pb2.Image(
            local_image_python_executable=self.python_executable,
        )
        req = api_pb2.ImageGetOrCreateRequest(
            session_id=session.session_id,
            image=image_definition,
        )
        resp = await session.client.stub.ImageGetOrCreate(req)
        return resp.image_id


@image_factory
def debian_slim(extra_commands=None, python_packages=None, python_version=None):
    if python_version is None:
        python_version = get_python_version()

    base_image = Image.use(None, f"python-{python_version}-slim-buster-base")
    builder_image = Image.use(None, f"python-{python_version}-slim-buster-builder")

    if extra_commands is None and python_packages is None:
        return base_image

    dockerfile_commands = ["FROM base as target"]
    base_images = {"base": base_image}
    if extra_commands is not None:
        dockerfile_commands += ["RUN {cmd}" for cmd in extra_commands]

    if python_packages is not None:
        base_images["builder"] = builder_image
        dockerfile_commands += [
            "FROM builder as builder-vehicle",
            f"RUN pip wheel {' '.join(python_packages)} -w /tmp/wheels",
            "FROM target",
            "COPY --from=builder-vehicle /tmp/wheels /tmp/wheels",
            "RUN pip install /tmp/wheels/*",
            "RUN rm -rf /tmp/wheels",
        ]

    return CustomImage(
        dockerfile_commands=dockerfile_commands,
        base_images=base_images,
    )


def extend_image(base_image, extra_dockerfile_commands):
    return CustomImage(base_images={"base": base_image}, dockerfile_commands=["FROM base"] + extra_dockerfile_commands)
