# Copyright Modal Labs 2022
import asyncio
import contextlib
import inspect
import os
import platform
import signal
import sys
import warnings
from enum import Enum
from typing import AsyncGenerator, Collection, Dict, List, Optional, Union

from rich.tree import Tree

from modal_proto import api_pb2
from modal_utils.app_utils import is_valid_app_name
from modal_utils.async_utils import TaskContext, synchronize_apis
from modal_utils.decorator_utils import decorator_with_options

from . import _pty
from ._function_utils import FunctionInfo
from ._ipython import is_notebook
from ._live_reload import MODAL_AUTORELOAD_ENV, restart_serve
from ._output import OutputManager, step_completed, step_progress
from ._pty import exec_cmd
from .app import _App, _container_app, is_local
from .client import HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT, _Client
from .config import config, logger
from .exception import InvalidError
from .functions import _Function, _FunctionHandle
from .gpu import GPU_T
from .image import _Image, _ImageHandle
from .mount import _get_client_mount, _Mount
from .object import Provider
from .proxy import _Proxy
from .queue import _Queue, _QueueHandle
from .rate_limit import RateLimit
from .schedule import Schedule
from .secret import _Secret
from .shared_volume import _SharedVolume

_default_image = _Image.debian_slim()


class StubRunMode(Enum):
    RUN = "run"
    DEPLOY = "deploy"
    DETACH = "detach"
    SERVE = "serve"


class LocalEntrypoint:
    def __init__(self, raw_f, stub):
        self.raw_f = raw_f
        self._stub = stub

    def __call__(self, *args, **kwargs):
        return self.raw_f(*args, **kwargs)


async def _heartbeat(client, app_id):
    request = api_pb2.AppHeartbeatRequest(app_id=app_id)
    # TODO(erikbern): we should capture exceptions here
    # * if request fails: destroy the client
    # * if server says the app is gone: print a helpful warning about detaching
    await client.stub.AppHeartbeat(request, timeout=HEARTBEAT_TIMEOUT)


class _Stub:
    """A `Stub` is a description of how to create a Modal application.

    The stub object principally describes Modal objects (`Function`, `Image`,
    `Secret`, etc.) associated with the application. It has three responsibilities:

    * Syncing of identities across processes (your local Python interpreter and
      every Modal worker active in your application).
    * Making Objects stay alive and not be garbage collected for as long as the
      app lives (see App lifetime below).
    * Manage log collection for everything that happens inside your code.

    **Registering functions with an app**

    The most common way to explicitly register an Object with an app is through the
    `@stub.function` decorator. It both registers the annotated function itself and
    other passed objects, like schedules and secrets, with the app:

    ```python
    import modal

    stub = modal.Stub()

    @stub.function(
        secret=modal.Secret.from_name("some_secret"),
        schedule=modal.Period(days=1),
    )
    def foo():
        pass
    ```

    In this example, the secret and schedule are registered with the app.
    """

    _name: str
    _description: str
    _app_id: str
    _blueprint: Dict[str, Provider]
    _client_mount: Optional[_Mount]
    _function_mounts: Dict[str, _Mount]
    _mounts: Collection[_Mount]
    _secrets: Collection[_Secret]
    _function_handles: Dict[str, _FunctionHandle]
    _web_endpoints: List[str]  # Used by the CLI
    _local_entrypoints: Dict[str, LocalEntrypoint]
    _local_mounts: List[_Mount]
    _app: Optional[_App]

    def __init__(
        self,
        name: str = None,
        *,
        mounts: Collection[_Mount] = [],
        secrets: Collection[_Secret] = [],
        **blueprint,
    ) -> None:
        """Construct a new app stub, optionally with default mounts."""

        self._name = name
        if name is not None:
            self._description = name
        else:
            self._description = self._infer_app_desc()
        self._blueprint = blueprint
        self._client_mount = None
        self._function_mounts = {}
        self._mounts = mounts
        self._secrets = secrets
        self._function_handles = {}
        self._local_entrypoints = {}
        self._local_mounts = []
        self._web_endpoints = []

        self._app = None
        if not is_local():
            # TODO(erikbern): in theory there could be multiple stubs defined.
            # We should try to determine whether this is in fact the "right" one.
            # We could probably do this by looking at the app's name.
            self._app = _container_app

    @property
    def name(self) -> str:
        """The user-provided name of the Stub."""
        return self._name

    @property
    def description(self) -> str:
        """The Stub's `name`, if available, or a fallback descriptive identifier."""
        return self._description

    def _infer_app_desc(self):
        if is_notebook():
            # when running from a notebook the sys.argv for the kernel will
            # be really long an not very helpful
            return "Notebook"  # TODO: use actual name of notebook

        script_filename = os.path.split(sys.argv[0])[-1]
        args = [script_filename] + sys.argv[1:]
        return " ".join(args)

    def __getitem__(self, tag: str):
        # Deprecated? Note: this is currently the only way to refer to lifecycled methods on the stub, since they have . in the tag
        return self._blueprint[tag]

    def __setitem__(self, tag: str, obj: Provider):
        # Deprecated ?
        self._blueprint[tag] = obj

    def __getattr__(self, tag: str) -> Provider:
        assert isinstance(tag, str)
        if tag.startswith("__"):
            # Hacky way to avoid certain issues, e.g. pickle will try to look this up
            raise AttributeError(f"Stub has no member {tag}")
        # Return a reference to an object that will be created in the future
        return self._blueprint[tag]

    def __setattr__(self, tag: str, obj: Provider):
        # Note that only attributes defined in __annotations__ are set on the object itself,
        # everything else is registered on the blueprint
        if tag in self.__annotations__:
            object.__setattr__(self, tag, obj)
        else:
            self._blueprint[tag] = obj

    def is_inside(self, image: Optional[_Image] = None) -> bool:
        """Returns if the program is currently running inside a container for this app."""
        if self._app is None:
            return False
        elif self._app != _container_app:
            return False
        elif image is None:
            # stub.app is set, which means we're inside this stub (no specific image)
            return True

        # We need to look up the image handle from the image provider
        assert isinstance(image, _Image)
        for tag, provider in self._blueprint.items():
            if provider == image:
                image_handle = self._app[tag]
                break
        else:
            raise InvalidError(
                inspect.cleandoc(
                    """`is_inside` only works for an image associated with an App. For instance:
                    stub.image = DebianSlim()
                    if stub.is_inside(stub.image):
                    print("I'm inside!")"""
                )
            )

        assert isinstance(image_handle, _ImageHandle)
        return image_handle._is_inside()

    @contextlib.asynccontextmanager
    async def _run(
        self,
        client,
        output_mgr: OutputManager,
        existing_app_id: Optional[str],
        last_log_entry_id: Optional[str] = None,
        name: Optional[str] = None,
        mode: StubRunMode = StubRunMode.RUN,
    ) -> AsyncGenerator[_App, None]:
        app_name = name if name is not None else self.description
        detach = mode == StubRunMode.DETACH
        if mode == StubRunMode.DETACH:
            post_init_state = api_pb2.APP_STATE_DETACHED
        elif mode == StubRunMode.DEPLOY:
            post_init_state = (
                api_pb2.APP_STATE_UNSPECIFIED
            )  # don't change the app state - deploy state is set by AppDeploy
        else:
            post_init_state = api_pb2.APP_STATE_EPHEMERAL

        if existing_app_id is not None:
            app = await _App._init_existing(client, existing_app_id)
        else:
            app = await _App._init_new(client, app_name, deploying=(mode == StubRunMode.DEPLOY), detach=detach)
        self._app = app

        aborted = False
        # Start tracking logs and yield context
        async with TaskContext(grace=config["logs_timeout"]) as tc:
            # Start heartbeats loop to keep the client alive
            tc.infinite_loop(lambda: _heartbeat(client, app.app_id), sleep=HEARTBEAT_INTERVAL)

            status_spinner = step_progress("Running app...")
            with output_mgr.ctx_if_visible(output_mgr.make_live(step_progress("Initializing..."))):
                logs_loop = tc.create_task(
                    output_mgr.get_logs_loop(app.app_id, client, status_spinner, last_log_entry_id or "")
                )
            if MODAL_AUTORELOAD_ENV not in os.environ:
                initialized_msg = (
                    f"Initialized. [grey70]View app at [underline]{app._app_page_url}[/underline][/grey70]"
                )
                output_mgr.print_if_visible(step_completed(initialized_msg))

            try:
                # Create all members
                create_progress = Tree(step_progress("Creating objects..."), guide_style="gray50")
                with output_mgr.ctx_if_visible(output_mgr.make_live(create_progress)):
                    await app._create_all_objects(self._blueprint, create_progress, post_init_state)
                create_progress.label = step_completed("Created objects.")
                output_mgr.print_if_visible(create_progress)

                # Update all functions client-side to have the output mgr
                for tag, obj in self._function_handles.items():
                    obj._set_output_mgr(output_mgr)

                # Cancel logs loop after creating objects for a deployment.
                # TODO: we can get rid of this once we have 1) a way to separate builder
                # logs from runner logs and 2) a termination signal that's sent after object
                # creation is complete, that is also triggered on exceptions (`app.disconnect()`)
                if mode == StubRunMode.DEPLOY:
                    logs_loop.cancel()

                if self._pty_input_stream:
                    output_mgr._visible_progress = False
                    handle = app._pty_input_stream
                    assert isinstance(handle, _QueueHandle)
                    async with _pty.write_stdin_to_pty_stream(handle):
                        yield app
                    output_mgr._visible_progress = True
                else:
                    # Yield to context
                    with output_mgr.ctx_if_visible(output_mgr.make_live(status_spinner)):
                        yield app
            except KeyboardInterrupt:
                aborted = True
                # mute cancellation errors on all function handles to prevent exception spam
                for tag, obj in self._function_handles.items():
                    obj._set_mute_cancellation(True)

                if detach:
                    logs_loop.cancel()
                else:
                    print("Disconnecting from Modal - This will terminate your Modal app in a few seconds.\n")
            finally:
                if mode == StubRunMode.SERVE:
                    # Cancel logs loop since we're going to start another one.
                    logs_loop.cancel()
                else:
                    await app.disconnect()

        if mode == StubRunMode.DEPLOY:
            output_mgr.print_if_visible(step_completed("App deployed! 🎉"))
        elif aborted:
            if detach:
                output_mgr.print_if_visible(step_completed("Shutting down Modal client."))
                output_mgr.print_if_visible(
                    f"""The detached app keeps running. You can track its progress at: [magenta]{app.log_url()}[/magenta]"""
                )
            else:
                output_mgr.print_if_visible(step_completed("App aborted."))
        elif mode != StubRunMode.SERVE:
            output_mgr.print_if_visible(step_completed("App completed."))
        self._app = None

    @contextlib.asynccontextmanager
    async def run(self, client=None, stdout=None, show_progress=None, detach=False) -> AsyncGenerator[_App, None]:
        """Context manager that runs an app on Modal.

        Use this as the main entry point for your Modal application. All calls
        to Modal functions should be made within the scope of this context
        manager, and they will correspond to the current app.

        See the documentation for the [`App`](modal.App) class for more details.
        """
        if not is_local():
            raise InvalidError(
                "Can not run an app from within a container."
                " Are you calling stub.run() directly?"
                " Consider using the `modal run` shell command."
            )
        if client is None:
            client = await _Client.from_env()
        output_mgr = OutputManager(stdout, show_progress)
        mode = StubRunMode.DETACH if detach else StubRunMode.RUN
        async with self._run(client, output_mgr, existing_app_id=None, mode=mode) as app:
            yield app

    async def serve(self, client=None, stdout=None, show_progress=None, timeout=None) -> None:
        """Run an app until the program is interrupted. Modal watches source files
        and mounts for the app, and live updates the app when any changes are detected.

        This function is useful for developing and testing cron schedules, job queues, and webhooks,
        since they will run until the program is interrupted with `Ctrl + C` or other means.
        Any changes made to webhook handlers will show up almost immediately the next time the route is hit.

        **Note:** live-reloading is not supported on Python 3.7. Please upgrade to Python 3.8+.
        """
        from ._watcher import watch

        if self._app is not None:
            raise InvalidError(
                "The stub already has an app running."
                " Are you calling stub.serve() directly?"
                " Consider using the `modal serve` shell command."
            )

        if client is None:
            client = await _Client.from_env()

        if timeout is None:
            timeout = config["serve_timeout"]

        output_mgr = OutputManager(stdout, show_progress)

        if MODAL_AUTORELOAD_ENV in os.environ:
            existing_app_id = os.environ[MODAL_AUTORELOAD_ENV]
            output_mgr.print_if_visible(f"⚡️ Updating app {existing_app_id}...")
            try:
                async with self._run(client, output_mgr, existing_app_id, mode=StubRunMode.SERVE) as app:
                    await asyncio.sleep(1e10)  # never awake except for exceptions
            except asyncio.exceptions.CancelledError:
                return
        else:
            unsupported_msg = None
            if platform.system() == "Windows":
                unsupported_msg = "Live-reload skipped. This feature is currently unsupported on Windows"
                " This can hopefully be fixed in a future version of Modal."
            elif sys.version_info < (3, 8):
                unsupported_msg = (
                    "Live-reload skipped. This feature is unsupported below Python 3.8."
                    " Upgrade to Python 3.8+ to enable live-reloading."
                )

            if unsupported_msg:
                async with self._run(client, output_mgr, None, mode=StubRunMode.SERVE) as app:
                    client.set_pre_stop(app.disconnect)
                    async for _ in watch(self._local_mounts, output_mgr, timeout):
                        output_mgr.print_if_visible(unsupported_msg)
            else:
                app = await _App._init_new(client, self.description, deploying=False, detach=False)
                curr_proc = None
                try:
                    async for _ in watch(self._local_mounts, output_mgr, timeout):
                        curr_proc = await restart_serve(
                            existing_app_id=app.app_id, prev_proc=curr_proc, output_mgr=output_mgr
                        )
                finally:
                    if curr_proc:
                        try:
                            curr_proc.send_signal(signal.SIGINT)
                        except ProcessLookupError:
                            logger.warning("Could not interrupt app serve. Supervised process already terminated.")

    async def deploy(
        self,
        name: str = None,  # Unique name of the deployment. Subsequent deploys with the same name overwrites previous ones. Falls back to the app name
        namespace=api_pb2.DEPLOYMENT_NAMESPACE_WORKSPACE,
        client=None,
        stdout=None,
        show_progress=None,
        object_entity="ap",
    ):
        """Deploy an app and export its objects persistently.

        Typically, using the command-line tool `modal deploy <module or script>`
        should be used, instead of this method.

        **Usage:**

        ```python
        if __name__ == "__main__":
            stub.deploy()
        ```

        Deployment has two primary purposes:

        * Persists all of the objects in the app, allowing them to live past the
          current app run. For schedules this enables headless "cron"-like
          functionality where scheduled functions continue to be invoked after
          the client has disconnected.
        * Allows for certain kinds of these objects, _deployment objects_, to be
          referred to and used by other apps.
        """
        if not is_local():
            raise InvalidError("Cannot run a deploy from within a container.")
        if name is None:
            name = self.name
        if name is None:
            raise InvalidError(
                "You need to either supply an explicit deployment name to the deploy command, or have a name set on the app.\n"
                "\n"
                "Examples:\n"
                'stub.deploy("some_name")\n\n'
                "or\n"
                'stub = Stub("some-name")'
            )

        if not is_valid_app_name(name):
            raise InvalidError(
                f"Invalid app name {name}. App names may only contain alphanumeric characters, dashes, periods, and underscores, and must be less than 64 characters in length. "
            )

        if client is None:
            client = await _Client.from_env()

        # Look up any existing deployment
        app_req = api_pb2.AppGetByDeploymentNameRequest(name=name, namespace=namespace)
        app_resp = await client.stub.AppGetByDeploymentName(app_req)
        existing_app_id = app_resp.app_id or None
        last_log_entry_id = app_resp.last_log_entry_id

        # The `_run` method contains the logic for starting and running an app
        output_mgr = OutputManager(stdout, show_progress)
        async with self._run(
            client, output_mgr, existing_app_id, last_log_entry_id, name=name, mode=StubRunMode.DEPLOY
        ) as app:
            deploy_req = api_pb2.AppDeployRequest(
                app_id=app._app_id,
                name=name,
                namespace=namespace,
                object_entity=object_entity,
            )
            deploy_response = await client.stub.AppDeploy(deploy_req)
        output_mgr.print_if_visible(f"\nView Deployment: [magenta]{deploy_response.url}[/magenta]")
        return app

    def _get_default_image(self):
        if "image" in self._blueprint:
            return self._blueprint["image"]
        else:
            return _default_image

    @property
    def _pty_input_stream(self):
        return self._blueprint.get("_pty_input_stream", None)

    def _get_function_mounts(self, function_info: FunctionInfo):
        # Get the common mounts for the stub.
        mounts = list(self._mounts)

        # Create client mount
        if self._client_mount is None:
            self._client_mount = _get_client_mount()
        mounts.append(self._client_mount)

        # Create function mounts
        for key, mount in function_info.get_mounts().items():
            if key not in self._function_mounts:
                self._function_mounts[key] = mount
            mounts.append(self._function_mounts[key])

        return mounts

    def _get_function_handle(self, info: FunctionInfo) -> _FunctionHandle:
        tag = info.get_tag()
        function_handle: Optional[_FunctionHandle] = None
        if self._app:
            # Grab the existing function handle from the running app
            # TODO: handle missing items, or wrong types
            try:
                handle = self._app[tag]
                if isinstance(handle, _FunctionHandle):
                    function_handle = handle
                else:
                    logger.warning(f"Object {tag} has wrong type {type(handle)}")
            except KeyError:
                logger.warning(f"Could not find app function {tag}")

        if function_handle is None:
            function_handle = _FunctionHandle._new()

        function_handle._set_info(info)
        function_handle._set_stub(self)
        self._function_handles[tag] = function_handle
        return function_handle

    def _add_function(self, function: _Function, mounts: List[_Mount]):
        if function.tag in self._blueprint:
            old_function = self._blueprint[function.tag]
            if isinstance(old_function, _Function):
                if not is_notebook():
                    logger.warning(
                        f"Warning: Tag '{function.tag}' collision!"
                        f" Overriding existing function [{old_function._info.module_name}].{old_function._info.function_name}"
                        f" with new function [{function._info.module_name}].{function._info.function_name}"
                    )
            else:
                logger.warning(f"Warning: tag {function.tag} exists but is overridden by function")
        self._blueprint[function.tag] = function

        # Track all mounts. This is needed for file watching
        for mount in mounts:
            if mount.is_local():
                self._local_mounts.append(mount)

    @property
    def registered_functions(self) -> Dict[str, _FunctionHandle]:
        """All modal.Function objects registered on the stub."""
        return self._function_handles

    @property
    def registered_entrypoints(self) -> Dict[str, LocalEntrypoint]:
        """All local CLI entrypoints registered on the stub."""
        return self._local_entrypoints

    @property
    def registered_web_endpoints(self) -> List[str]:
        """Names of web endpoint (ie. webhook) functions registered on the stub."""
        return self._web_endpoints

    @decorator_with_options
    def local_entrypoint(self, raw_f=None, name: Optional[str] = None):
        """Decorate a function to be used as a CLI entrypoint for a Modal App.

        These functions can be used to do initialization of apps using local
        assets. Note that regular Modal functions can also be used as CLI entrypoints,
        but unlike `local_entrypoint` Modal function are executed remotely.

        **Example**

        ```python
        @stub.local_entrypoint
        def main():
            some_modal_function.call()
        ```

        You can call the entrypoint function within a Modal run context
        directly from the CLI:

        ```shell
        modal run stub_module.py
        ```

        If you have multiple `local_entrypoint` functions, you can qualify the name of your stub and function:

        ```shell
        modal run stub_module.py::stub.some_other_function
        ```

        """
        info = FunctionInfo(raw_f, False, name_override=name)
        entrypoint = self._local_entrypoints[info.get_tag()] = LocalEntrypoint(raw_f, self)
        return entrypoint

    @decorator_with_options
    def function(
        self,
        raw_f=None,  # The decorated function
        *,
        image: _Image = None,  # The image to run as the container for the function
        schedule: Optional[Schedule] = None,  # An optional Modal Schedule for the function
        secret: Optional[_Secret] = None,  # An optional Modal Secret with environment variables for the container
        secrets: Collection[_Secret] = (),  # Plural version of `secret` when multiple secrets are needed
        gpu: GPU_T = None,  # GPU specification as string ("any", "T4", "A10G", ...) or object (`modal.GPU.A100()`, ...)
        rate_limit: Optional[RateLimit] = None,  # Optional RateLimit for the function
        serialized: bool = False,  # Whether to send the function over using cloudpickle.
        mounts: Collection[_Mount] = (),
        shared_volumes: Dict[str, _SharedVolume] = {},
        cpu: Optional[float] = None,  # How many CPU cores to request. This is a soft limit.
        memory: Optional[int] = None,  # How much memory to request, in MB. This is a soft limit.
        proxy: Optional[_Proxy] = None,  # Reference to a Modal Proxy to use in front of this function.
        retries: Optional[int] = None,  # Number of times to retry each input in case of failure.
        concurrency_limit: Optional[int] = None,  # Limit for max concurrent containers running the function.
        container_idle_timeout: Optional[int] = None,  # Timeout for idle containers waiting for inputs to shut down.
        timeout: Optional[int] = None,  # Maximum execution time of the function in seconds.
        interactive: bool = False,  # Whether to run the function in interactive mode.
        keep_warm: Union[bool, int] = False,  # Toggles an adaptively-sized warm pool for latency-sensitive apps.
        name: Optional[str] = None,  # Sets the Modal name of the function within the stub
        is_generator: Optional[bool] = None,  # If not set, it's inferred from the function signature
        cloud: Optional[str] = None,  # Cloud provider to run the function on. Possible values are aws, gcp, auto.
    ) -> _FunctionHandle:  # Function object - callable as a regular function within a Modal app
        """Decorator to register a new Modal function with this stub."""
        if image is None:
            image = self._get_default_image()
        info = FunctionInfo(raw_f, serialized=serialized, name_override=name)
        function_handle = self._get_function_handle(info)
        base_mounts = self._get_function_mounts(info)
        secrets = [*self._secrets, *secrets]

        if interactive:
            if self._pty_input_stream:
                warnings.warn(
                    "Running multiple interactive functions at the same time is not fully supported, and could lead to unexpected behavior."
                )
            else:
                self._blueprint["_pty_input_stream"] = _Queue()

        if is_generator is None:
            is_generator = inspect.isgeneratorfunction(raw_f) or inspect.isasyncgenfunction(raw_f)

        function = _Function(
            function_handle,
            info,
            _stub=self,
            image=image,
            secret=secret,
            secrets=secrets,
            schedule=schedule,
            is_generator=is_generator,
            gpu=gpu,
            rate_limit=rate_limit,
            serialized=serialized,
            base_mounts=base_mounts,
            mounts=mounts,
            shared_volumes=shared_volumes,
            memory=memory,
            proxy=proxy,
            retries=retries,
            concurrency_limit=concurrency_limit,
            container_idle_timeout=container_idle_timeout,
            timeout=timeout,
            cpu=cpu,
            interactive=interactive,
            keep_warm=keep_warm,
            name=name,
            cloud=cloud,
        )

        self._add_function(function, [*base_mounts, *mounts])
        return function_handle

    @decorator_with_options
    def webhook(
        self,
        raw_f,
        *,
        method: str = "GET",  # REST method for the created endpoint.
        label: str = None,  # Label for created endpoint. Final subdomain will be <workspace>--<label>.modal.run.
        wait_for_response: bool = True,  # Whether requests should wait for and return the function response.
        image: _Image = None,  # The image to run as the container for the function
        secret: Optional[_Secret] = None,  # An optional Modal Secret with environment variables for the container
        secrets: Collection[_Secret] = (),  # Plural version of `secret` when multiple secrets are needed
        gpu: GPU_T = None,  # GPU specification as string ("any", "T4", "A10G", ...) or object (`modal.GPU.A100()`, ...)
        mounts: Collection[_Mount] = (),
        shared_volumes: Dict[str, _SharedVolume] = {},
        cpu: Optional[float] = None,  # How many CPU cores to request. This is a soft limit.
        memory: Optional[int] = None,  # How much memory to request, in MB. This is a soft limit.
        proxy: Optional[_Proxy] = None,  # Reference to a Modal Proxy to use in front of this function.
        retries: Optional[int] = None,  # Number of times to retry each input in case of failure.
        concurrency_limit: Optional[int] = None,  # Limit for max concurrent containers running the function.
        container_idle_timeout: Optional[int] = None,  # Timeout for idle containers waiting for inputs to shut down.
        timeout: Optional[int] = None,  # Maximum execution time of the function in seconds.
        keep_warm: Union[bool, int] = False,  # Toggles an adaptively-sized warm pool for latency-sensitive apps.
        cloud: Optional[str] = None,  # Cloud provider to run the function on. Possible values are aws, gcp, auto.
    ):
        """Register a basic web endpoint with this application.

        This is the simple way to create a web endpoint on Modal. The function
        behaves as a [FastAPI](https://fastapi.tiangolo.com/) handler and should
        return a response object to the caller.

        Endpoints created with `@stub.webhook` are meant to be simple, single
        request handlers and automatically have
        [CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS) enabled.
        For more flexibility, use `@stub.asgi`.

        To learn how to use Modal with popular web frameworks, see the
        [guide on web endpoints](https://modal.com/docs/guide/webhooks).

        All webhook requests have a 150s maximum request time for the HTTP request itself. However, the underlying functions can
        run for longer and return results to the caller on completion.

        The two `wait_for_response` modes for webhooks are as follows:
        * `wait_for_response=True` - tries to fulfill the request on the original URL, but returns a 302 redirect after ~150s to a result URL (original URL with an added `__modal_function_id=...` query parameter)
        * `wait_for_response=False` - immediately returns a 202 ACCEPTED response with a JSON payload: `{"result_url": "..."}` containing the result "redirect" URL from above (which in turn redirects to itself every ~150s)
        """
        if image is None:
            image = self._get_default_image()
        info = FunctionInfo(raw_f)
        function_handle = self._get_function_handle(info)
        self._web_endpoints.append(info.get_tag())
        base_mounts = self._get_function_mounts(info)
        secrets = [*self._secrets, *secrets]

        if not wait_for_response:
            _response_mode = api_pb2.WEBHOOK_ASYNC_MODE_TRIGGER
        else:
            _response_mode = api_pb2.WEBHOOK_ASYNC_MODE_AUTO  # the default

        function = _Function(
            function_handle,
            info,
            _stub=self,
            image=image,
            secret=secret,
            secrets=secrets,
            is_generator=True,
            gpu=gpu,
            base_mounts=base_mounts,
            mounts=mounts,
            shared_volumes=shared_volumes,
            webhook_config=api_pb2.WebhookConfig(
                type=api_pb2.WEBHOOK_TYPE_FUNCTION,
                method=method,
                requested_suffix=label,
                async_mode=_response_mode,
            ),
            cpu=cpu,
            memory=memory,
            proxy=proxy,
            retries=retries,
            concurrency_limit=concurrency_limit,
            container_idle_timeout=container_idle_timeout,
            timeout=timeout,
            keep_warm=keep_warm,
            cloud=cloud,
        )
        self._add_function(function, [*base_mounts, *mounts])
        return function_handle

    @decorator_with_options
    def asgi(
        self,
        asgi_app,  # The asgi app
        *,
        label: str = None,  # Label for created endpoint. Final subdomain will be <workspace>--<label>.modal.run.
        wait_for_response: bool = True,  # Whether requests should wait for and return the function response.
        image: _Image = None,  # The image to run as the container for the function
        secret: Optional[_Secret] = None,  # An optional Modal Secret with environment variables for the container
        secrets: Collection[_Secret] = (),  # Plural version of `secret` when multiple secrets are needed
        gpu: GPU_T = None,  # GPU specification as string ("any", "T4", "A10G", ...) or object (`modal.GPU.A100()`, ...)
        mounts: Collection[_Mount] = (),
        shared_volumes: Dict[str, _SharedVolume] = {},
        cpu: Optional[float] = None,  # How many CPU cores to request. This is a soft limit.
        memory: Optional[int] = None,  # How much memory to request, in MB. This is a soft limit.
        proxy: Optional[_Proxy] = None,  # Reference to a Modal Proxy to use in front of this function.
        retries: Optional[int] = None,  # Number of times to retry each input in case of failure.
        concurrency_limit: Optional[int] = None,  # Limit for max concurrent containers running the function.
        container_idle_timeout: Optional[int] = None,  # Timeout for idle containers waiting for inputs to shut down.
        timeout: Optional[int] = None,  # Maximum execution time of the function in seconds.
        keep_warm: Union[bool, int] = False,  # Toggles an adaptively-sized warm pool for latency-sensitive apps.
        cloud: Optional[str] = None,  # Cloud provider to run the function on. Possible values are aws, gcp, auto.
        _webhook_type=api_pb2.WEBHOOK_TYPE_ASGI_APP,
    ):
        """Register an ASGI app with this application.

        Asynchronous Server Gateway Interface (ASGI) is a standard for Python
        synchronous and asynchronous apps, supported by all popular Python web
        libraries. This is an advanced decorator that gives full flexibility in
        defining one or more web endpoints on Modal.

        To learn how to use Modal with popular web frameworks, see the
        [guide on web endpoints](https://modal.com/docs/guide/webhooks).

        The two `wait_for_response` modes for webhooks are as follows:
        * wait_for_response=True - tries to fulfill the request on the original URL, but returns a 302 redirect after ~150s to a result URL (original URL with an added `__modal_function_id=fc-1234abcd` query parameter)
        * wait_for_response=False - immediately returns a 202 ACCEPTED response with a json payload: `{"result_url": "..."}` containing the result "redirect" url from above (which in turn redirects to itself every 150s)
        """
        if image is None:
            image = self._get_default_image()
        info = FunctionInfo(asgi_app)
        function_handle = self._get_function_handle(info)
        self._web_endpoints.append(info.get_tag())
        base_mounts = self._get_function_mounts(info)
        secrets = [*self._secrets, *secrets]

        if not wait_for_response:
            _response_mode = api_pb2.WEBHOOK_ASYNC_MODE_TRIGGER
        else:
            _response_mode = api_pb2.WEBHOOK_ASYNC_MODE_AUTO  # the default

        function = _Function(
            function_handle,
            info,
            _stub=self,
            image=image,
            secret=secret,
            secrets=secrets,
            is_generator=True,
            gpu=gpu,
            base_mounts=base_mounts,
            mounts=mounts,
            shared_volumes=shared_volumes,
            webhook_config=api_pb2.WebhookConfig(type=_webhook_type, requested_suffix=label, async_mode=_response_mode),
            cpu=cpu,
            memory=memory,
            proxy=proxy,
            retries=retries,
            concurrency_limit=concurrency_limit,
            container_idle_timeout=container_idle_timeout,
            timeout=timeout,
            keep_warm=keep_warm,
            cloud=cloud,
        )
        self._add_function(function, [*base_mounts, *mounts])
        return function_handle

    @decorator_with_options
    def wsgi(
        self,
        wsgi_app,
        **kwargs,
    ):
        """Exposes a WSGI app. For a list of arguments, see the documentation for `asgi`."""
        asgi_decorator = self.asgi(_webhook_type=api_pb2.WEBHOOK_TYPE_WSGI_APP, **kwargs)
        return asgi_decorator(wsgi_app)

    async def interactive_shell(self, cmd=None, image=None, **kwargs):
        """Run an interactive shell (like `bash`) within the image for this app.

        This is useful for online debugging and interactive exploration of the
        contents of this image. If `cmd` is optionally provided, it will be run
        instead of the default shell inside this image.

        **Example**

        ```python
        import modal

        stub = modal.Stub(image=modal.Image.debian_slim().apt_install("vim"))

        if __name__ == "__main__":
            stub.interactive_shell("/bin/bash")
        ```

        Or alternatively:

        ```python
        import modal

        stub = modal.Stub()
        app_image = modal.Image.debian_slim().apt_install("vim")

        if __name__ == "__main__":
            stub.interactive_shell(cmd="/bin/bash", image=app_image)
        ```
        """
        # TODO(erikbern): rewrite the docstring above to point the user towards `modal shell`
        wrapped_fn = self.function(interactive=True, timeout=86400, image=image or self._get_default_image(), **kwargs)(
            exec_cmd
        )

        async with self.run():
            await wrapped_fn.call(cmd)


Stub, AioStub = synchronize_apis(_Stub)
