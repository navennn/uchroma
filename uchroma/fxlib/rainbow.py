import asyncio

from grapefruit import Color

from uchroma.anim import Renderer


DEFAULT_SPEED = 20


class Rainbow(Renderer):

    def __init__(self, *args, **kwargs):
        self._stagger = None
        self._gradient = None
        self._offset = 0
        self._speed = DEFAULT_SPEED


    @staticmethod
    def _hue_gradient(start, length):
        step = 360 / length
        return [Color.NewFromHsv((start + (step * x)) % 360, 1, 1) for x in range(0, length)]


    def init(self, frame, stagger: int=4, speed: int=DEFAULT_SPEED, *args, **kwargs):
        self._offset = 0
        self._stagger = stagger

        length = speed
        self._gradient = Rainbow._hue_gradient(0, speed * frame.width + (frame.height * stagger))
        return True


    @asyncio.coroutine
    def draw(self, frame, timestamp):
        data = []
        for row in range(0, frame.height):
            data.append( \
                [self._gradient[(self._offset + (row * self._stagger) + col) % len(self._gradient)] \
                for col in range(0, frame.width)])

        frame.put_all(data)
        self._offset = (self._offset + 1) % len(self._gradient)

        return True
