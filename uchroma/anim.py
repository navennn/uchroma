# pylint: disable=unused-argument, protected-access, invalid-name
import asyncio
import importlib
import types

from collections import OrderedDict
from concurrent import futures
from typing import NamedTuple

from traitlets import Bool, HasTraits, List, observe

from uchroma.frame import Frame
from uchroma.renderer import MAX_FPS, NUM_BUFFERS, Renderer, RendererMeta
from uchroma.traits import FrozenDict, get_args_dict, is_trait_writable
from uchroma.util import ensure_future, Signal, Ticker, LOG_TRACE


class LayerHolder(HasTraits):

    def __init__(self, renderer: Renderer, frame: Frame,
                 blend_mode=None, *args, **kwargs):
        super(LayerHolder, self).__init__(*args, **kwargs)

        self._renderer = renderer
        self._frame = frame
        self._blend_mode = blend_mode

        self.waiter = None
        self.active_buf = None
        self.task = None

        self.traits_changed = Signal()
        self._renderer.observe(self._traits_changed, names=['all'])

        self._renderer._flush()

        for buf in range(0, NUM_BUFFERS):
            layer = self._frame.create_layer()
            layer.blend_mode = self._blend_mode
            self._renderer._free_layer(layer)


    @property
    def type_string(self):
        cls = self._renderer.__class__
        return '%s.%s' % (cls.__module__, cls.__name__)


    @property
    def trait_values(self):
        return get_args_dict(self._renderer)


    def _traits_changed(self, change):
        if not self.renderer.running:
            return

        self.traits_changed.fire(self.zindex, self.trait_values, change.name, change.old)


    @property
    def zindex(self):
        return self._renderer.zindex


    @property
    def renderer(self):
        return self._renderer


    def start(self):
        if not self.renderer.running:
            self.task = ensure_future(self.renderer._run())


    @asyncio.coroutine
    def stop(self):
        if self.renderer.running:

            tasks = []
            if self.task is not None and not self.task.done():
                self.task.cancel()
                tasks.append(self.task)

            if self.waiter is not None and not self.waiter.done():
                self.waiter.cancel()
                tasks.append(self.waiter)

            yield from self.renderer._stop()

            if len(tasks) > 0:
                yield from asyncio.wait(tasks, return_when=futures.ALL_COMPLETED)

            self.renderer.finish(self._frame)


class AnimationLoop(HasTraits):
    layers = List(default_value=(), allow_none=False)
    running = Bool()

    """
    Collects the output of one or more Renderers and displays the
    composited image.

    The loop is a fully asynchronous design, and renderers may independently
    block or yield buffers at different rates. Each renderer has a pair of
    asyncio.Queue objects and will put buffers onto the "active" queue when
    their draw cycle is completed. The loop yields on these queues until
    at least one buffer is available. All new buffers are placed on the
    "active" list and the previous buffers are returned to the respective
    renderer on the "avail" queue. If a renderer doesn't produce any output
    during the round, the current buffer is kept. The active list is finally
    composed and sent to the hardware.

    The design of this loop intends to be as CPU-efficient as possible and
    does not wake up spuriously or otherwise consume cycles while inactive.
    """
    def __init__(self, frame: Frame, default_blend_mode: str=None,
                 *args, **kwargs):
        super(AnimationLoop, self).__init__(*args, **kwargs)

        self._frame = frame
        self._default_blend_mode = default_blend_mode

        self._anim_task = None

        self._pause_event = asyncio.Event()
        self._pause_event.set()

        self._logger = frame._driver.logger

        self.layers_changed = Signal()


    @observe('layers')
    def _start_stop(self, change):
        old = 0
        if isinstance(change.old, list):
            old = len(change.old)

        new = len(change.new)

        if old == 0 and new > 0 and not self.running:
            self.start()
        elif new == 0 and old > 0 and self.running:
            self.stop()


    @asyncio.coroutine
    def _dequeue(self, r_idx: int):
        """
        Gather completed layers from the renderers. If nothing
        is available, keep the last layer (in case the renderers
        are producing output at different rates). Yields until
        at least one layer is ready.
        """
        if not self.running or r_idx >= len(self.layers):
            return

        layer = self.layers[r_idx]
        renderer = layer.renderer

        # wait for a buffer
        buf = yield from renderer._active_q.get()

        # return the old buffer to the renderer
        if layer.active_buf is not None:
            renderer._free_layer(layer.active_buf)

        # put it on the active list
        layer.active_buf = buf


    def _dequeue_nowait(self, r_idx) -> bool:
        """
        Variation of _dequeue which does not yield.

        :return: True if any layers became active
        """
        if not self.running or r_idx >= len(self.layers):
            return False

        layer = self.layers[r_idx]
        renderer = layer.renderer

        # check if a buffer is ready
        if not renderer._active_q.empty():
            buf = renderer._active_q.get_nowait()
            if buf is not None:

                # return the last buffer
                if layer.active_buf is not None:
                    renderer._free_layer(layer.active_buf)

                # put it on the composition list
                layer.active_buf = buf
                return True

        return False


    @asyncio.coroutine
    def _get_layers(self):
        """
        Wait for renderers to produce new layers, yields until at least one
        layer is active.
        """
        # schedule tasks to wait on each renderer queue
        for r_idx in range(0, len(self.layers)):
            layer = self.layers[r_idx]

            if layer.waiter is None or layer.waiter.done():
                layer.waiter = ensure_future(self._dequeue(r_idx))

        # async wait for at least one completion
        waiters = [layer.waiter for layer in self.layers]
        if len(waiters) == 0:
            return

        yield from asyncio.wait(waiters, return_when=futures.FIRST_COMPLETED)

        # check the rest without waiting
        for r_idx in range(0, len(self.layers)):
            layer = self.layers[r_idx]

            if layer.waiter is not None and not layer.waiter.done():
                self._dequeue_nowait(r_idx)


    def _commit_layers(self):
        """
        Merge layers from all renderers and commit to the hardware
        """
        if self._logger.isEnabledFor(LOG_TRACE - 1):
            self._logger.debug("Layers: %s", self.layers)

        active_bufs = [layer.active_buf for layer in \
                sorted(self.layers, key=lambda z: z.zindex) \
                if layer is not None and layer.active_buf is not None]

        if len(active_bufs) > 0:
            self._frame.commit(active_bufs)


    @asyncio.coroutine
    def _animate(self):
        """
        Main loop

        Starts the renderers, waits for new layers to be drawn,
        composites the layers, sends them to the hardware, and
        finally syncs to achieve consistent frame rate. If no
        layers are ready, the loop yields to prevent spurious
        wakeups.
        """
        self._logger.info("AnimationLoop is starting..")

        # start the renderers
        for layer in self.layers:
            layer.start()

        tick = Ticker(1 / MAX_FPS)

        # loop forever, waiting for layers
        while self.running:
            yield from self._pause_event.wait()

            with tick:
                yield from self._get_layers()

                if not self.running:
                    break

                # compose and display the frame
                self._commit_layers()

            # FIXME: Use "async with" on Python 3.6+
            yield from tick.tick()


    def _renderer_done(self, future):
        """
        Invoked when the renderer exits
        """
        self._logger.info("AnimationLoop is cleaning up")

        self._anim_task = None


    def _update_z(self, tmp_list):
        if len(tmp_list) > 0:
            for layer_idx in range(0, len(tmp_list)):
                tmp_list[layer_idx].renderer.zindex = layer_idx

        # fires trait observer
        self.layers = tmp_list


    def _layer_traits_changed(self, *args):
        self.layers_changed.fire('modify', *args)


    def add_layer(self, renderer: Renderer, zindex: int=None) -> bool:
        with self.hold_trait_notifications():
            if zindex is None:
                zindex = len(self.layers)

            if not renderer.init(self._frame):
                self._logger.error('Renderer %s failed to initialize', renderer.name)
                return False

            layer = LayerHolder(renderer, self._frame, self._default_blend_mode)
            tmp = self.layers[:]
            tmp.insert(zindex, layer)
            self._update_z(tmp)

            layer.traits_changed.connect(self._layer_traits_changed)

            if self.running:
                layer.start()

            self.layers_changed.fire('add', zindex, layer.renderer)

            self._logger.info("Layer created, renderer=%s zindex=%d",
                              layer.renderer, zindex)
        return True


    @asyncio.coroutine
    def remove_layer(self, layer_like):
        with self.hold_trait_notifications():
            if isinstance(layer_like, LayerHolder):
                zindex = self.layers.index(layer_like)
            elif isinstance(layer_like, int):
                zindex = layer_like
            else:
                raise TypeError('Layer should be a holder or an index')

            if zindex >= 0 and zindex < len(self.layers):
                layer = self.layers[zindex]
                layer_id = id(self.layers[zindex])
                yield from layer.stop()

                tmp = self.layers[:]
                del tmp[zindex]
                self._update_z(tmp)

                self.layers_changed.fire('remove', zindex, layer_id)

                self._logger.info("Layer %d removed", zindex)


    @asyncio.coroutine
    def clear_layers(self):
        if len(self.layers) == 0:
            return False
        for layer in self.layers[::-1]:
            yield from self.remove_layer(layer)
        return True


    def start(self) -> bool:
        """
        Start the AnimationLoop

        Initializes the renderers, zeros the buffers, and starts the loop.

        Requires an active asyncio event loop.

        :return: True if the loop was started
        """
        if self.running:
            self._logger.error("Animation loop already running")
            return False

        if len(self.layers) == 0:
            self._logger.error("No renderers were configured")
            return False

        self.running = True

        self._anim_task = ensure_future(self._animate())
        self._anim_task.add_done_callback(self._renderer_done)

        return True


    @asyncio.coroutine
    def _stop(self):
        """
        Stop this AnimationLoop

        Shuts down the loop and triggers cleanup tasks.
        """
        if not self.running:
            return False

        self.running = False

        for layer in self.layers[::-1]:
            yield from self.remove_layer(layer)

        if self._anim_task is not None and not self._anim_task.done():
            self._anim_task.cancel()
            yield from asyncio.wait([self._anim_task], return_when=futures.ALL_COMPLETED)

        self._logger.info("AnimationLoop stopped")


    def stop(self, cb=None):
        if not self.running:
            return False

        task = ensure_future(self._stop())
        if cb is not None:
            task.add_done_callback(cb)
        return True


    def pause(self, paused):
        if paused != self._pause_event.is_set():
            return

        self._logger.debug("Loop paused: %s", paused)

        if paused:
            self._pause_event.clear()
        else:
            self._pause_event.set()


RendererInfo = NamedTuple('RendererInfo', [('module', types.ModuleType),
                                           ('clazz', type),
                                           ('key', str),
                                           ('meta', RendererMeta),
                                           ('traits', dict)])

class AnimationManager(HasTraits):
    """
    Configures and manages animations of one or more renderers
    """

    _renderer_info = FrozenDict()
    paused = Bool(False)

    def __init__(self, driver):
        super(AnimationManager, self).__init__()

        self._driver = driver
        self._loop = None
        self._logger = driver.logger

        self.layers_changed = Signal()
        self.state_changed = Signal()

        driver.power_state_changed.connect(self._power_state_changed)

        self._fxlib = importlib.import_module('uchroma.fxlib')
        self._renderer_info = self._discover_renderers()

        self._shutting_down = False


    @observe('paused')
    def _state_changed(self, change):
        # aggregate the trait notifications to a single signal
        value = 'stopped'
        if change.name == 'paused' and change.new and self.running:
            value = 'paused'
        elif change.name == 'running' and change.new and not self.paused:
            value = 'running'

        self.state_changed.fire(value)


    def _loop_running_changed(self, change):
        self._driver.reset()
        self._state_changed(change)


    def _loop_layers_changed(self, *args):
        self.layers_changed.fire(*args)
        self._update_prefs()


    def _power_state_changed(self, brightness, suspended):
        if self.running and self.paused != suspended:
            self.pause(suspended)


    def _create_loop(self):
        if self._loop is None:
            self._loop = AnimationLoop(self._driver.frame_control)
            self._loop.observe(self._loop_running_changed, names=['running'])
            self._loop.layers_changed.connect(self._loop_layers_changed)


    def _update_prefs(self):
        if self._loop is None or self._shutting_down:
            return

        prefs = OrderedDict()
        for layer in self._loop.layers:
            prefs[layer.type_string] = layer.trait_values

        if len(prefs) > 0:
            self._driver.preferences.layers = prefs
        else:
            self._driver.preferences.layers = None


    def _discover_renderers(self):
        infos = OrderedDict()

        for item in self._fxlib.__dir__():
            obj = getattr(self._fxlib, item)
            if isinstance(obj, type) and issubclass(obj, Renderer):
                if obj.meta.display_name == '_unknown_':
                    self._logger.error("Renderer %s did not set metadata, skipping",
                                       obj.__name__)
                    continue

                key = '%s.%s' % (obj.__module__, obj.__name__)
                infos[key] = RendererInfo(obj.__module__, obj, key,
                                          obj.meta, obj.class_traits())

        self._logger.debug("Loaded renderers: %s", ', '.join(infos.keys()))
        return infos


    def _get_renderer(self, name, zindex: int=None, **traits) -> Renderer:
        """
        Instantiate a renderer

        :param name: Name of the discovered renderer

        :return: The renderer object
        """
        info = self._renderer_info[name]

        try:
            return info.clazz(self._driver, **traits)

        except ImportError as err:
            self._logger.exception('Invalid renderer: %s', name, exc_info=err)

        return None


    def add_renderer(self, name, traits: dict, zindex: int=None) -> int:
        """
        Adds a renderer which will produce a layer of this animation.
        Any number of renderers may be added and the output will be
        composited together. The z-order of the layers corresponds to
        the order renderers were added, with the first producing the
        base layer and the last producing the topmost layer.

        Renderers may be loaded from any valid Python package, the
        default is "uchroma.fxlib".

        The loop must not be running when this is called.

        :param renderer: Key name of a discovered renderer

        :return: Z-position of the new renderer or -1 on error
        """
        self._create_loop()

        if zindex is not None and zindex > len(self._loop.layers):
            raise ValueError("Z-index out of range (requested %d max %d)" % \
                    (zindex, len(self._loop.layers)))

        renderer = self._get_renderer(name, **traits)
        if renderer is None:
            self._logger.error('Renderer %s failed to load', renderer)
            return -1

        if not self._loop.add_layer(renderer, zindex):
            self._logger.error('Renderer %s failed to initialize', name)
            return -1

        return renderer.zindex


    def remove_renderer(self, zindex: int) -> bool:
        if self._loop is None:
            return False

        if zindex is None or zindex < 0 or zindex > len(self._loop.layers):
            self._logger.error("Z-index out of range (requested %d max %d)",
                               zindex, len(self._loop.layers))
            return False

        ensure_future(self._loop.remove_layer(zindex))
        return True


    def pause(self, state=None):
        if self._loop is not None:
            if state is None:
                state = not self.paused
            if state != self.paused:
                self._loop.pause(state)

            self.paused = state
            self._logger.info("Animation paused: %s", self.paused)

        return self.paused


    def stop(self, cb=None):
        if self._loop is not None:
            return self._loop.stop(cb=cb)

        return False


    @asyncio.coroutine
    def shutdown(self):
        """
        Stop and remove all renderers
        """
        self._shutting_down = True

        if self._loop is None:
            return

        yield from self._loop.clear_layers()


    def restore_prefs(self, prefs):
        self._logger.debug('Restoring layers: %s', prefs.layers)

        if prefs.layers is not None and len(prefs.layers) > 0:
            try:
                for name, args in prefs.layers.items():
                    self.add_renderer(name, args)

            except Exception as err:
                self._logger.exception('Failed to add renderers, clearing! [%s]',
                                       prefs.layers, exc_info=err)
                self.stop()


    @property
    def renderer_info(self):
        return self._renderer_info


    @property
    def running(self):
        return self._loop is not None and self._loop.running


    def __del__(self):
        if hasattr(self, '_loop') and self._loop is not None:
            self._loop.stop()
