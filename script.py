
import sys
from libka import libka, Script
from libka.tools import sequential_dataclass
from libka.logs import log
import xbmcgui
from xbmcgui import (
    ACTION_PREVIOUS_MENU, ACTION_NAV_BACK, ACTION_BACKSPACE,
)
from time import time  # DEBUG

#
# XXX --------------------- XXX
#
# NOTE:
#   There is "libka" experimental code.
#   Should NOT be used.
#
# (rysson)
#


BACK_ACTIONS = [ACTION_PREVIOUS_MENU, ACTION_NAV_BACK, ACTION_BACKSPACE]

from typing import Optional, Union, List                      # noqa: E402
from dataclasses import dataclass, field as data_field        # noqa: E402
from contextlib import contextmanager                         # noqa: E402
from collections.abc import Sequence                          # noqa: E402
from enum import Enum, Flag, auto as auto_enum                # noqa: E402


def white():
    """Returns path to 1x1 white image. Lazy executing."""
    return libka.media.white


class invalidate_property:
    """Read-write property. Wtite invalidate widget."""

    def __init__(self):
        self._name = None

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return getattr(instance, self._name)

    def __set__(self, instance, value):
        if getattr(instance, self._name) != value:
            setattr(instance, self._name, value)
            instance.invalidate()

    def __set_name__(self, owner, name):
        self._name = f'_{name}'


class Direction(Enum):
    LeftToRight = 0
    RightToLeft = 1
    TopToBottom = 2
    BottomToTop = 3
    Horizontal = LeftToRight
    Vertical = TopToBottom

    @classmethod
    def horizontal(cls, d):
        return d == cls.LeftToRight or d == cls.RightToLeft

    @classmethod
    def vertical(cls, d):
        return d == cls.TopToBottom or d == cls.BottomToTop


@sequential_dataclass
@dataclass
class Point:
    x: int = 0
    y: int = 0

    def move(self, dx, dy=None):
        if dy is None:
            dx, dy = dx
        self.x += dx
        self.y += dy
        return self

    def moved(self, dx, dy=None):
        if dy is None:
            dx, dy = dx
        return Point(self.x + dx, self.y + dy)


@sequential_dataclass
@dataclass
class Size:
    width: int = 0
    height: int = 0

    def set(self, width: int, height: int = None):
        if height is None:
            self.width, self.height = width
        else:
            self.width = width
            self.height = height

    def pad_out(self, *margins: List['Margin']) -> 'Size':
        width, height = self.width, self.height
        for margin in margins:
            width += margin.horizontal
            height += margin.vertical
        return Size(width, height)

    def pad_in(self, *margins: List['Margin']) -> 'Size':
        width, height = self.width, self.height
        for margin in margins:
            width -= margin.horizontal
            height -= margin.vertical
        return Size(width, height)


@sequential_dataclass
@dataclass
class Rect:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def top_left(self):
        return Point(self.x, self.y)

    @top_left.setter
    def top_left(self, pos):
        self.x, self.y = pos

    @property
    def size(self):
        return Size(self.width, self.height)

    @size.setter
    def size(self, size):
        self.width, self.height = size

    def adjust(self, dx1, dy1, dx2, dy2):
        self.x += dx1
        self.y += dy1
        self.width += dx2 - dx1
        self.height += dy2 - dy1
        return self

    def shrink(self, dx, dy=None):
        if dy is None:
            dy = dx
        self.x += dx
        self.y += dy
        self.width -= 2 * dx
        self.height -= 2 * dy
        return self

    @classmethod
    def new(self, rect):
        if rect is None:
            return Rect()
        elif isinstance(rect, Rect):
            return rect
        elif isinstance(rect, Size):
            return Rect(0, 0, rect.width, rect.height)
        elif len(rect) == 2:
            return Rect(0, 0, *rect)
        elif len(rect) == 4:
            return Rect(*rect)
        else:
            raise ValueError(f'Unsported rect {rect!r} type of {type(rect)}')


@sequential_dataclass
@dataclass
class Margin:
    top: int = 0
    right: int = None
    bottom: int = None
    left: int = None

    def __post_init__(self):
        if self.bottom is None:
            self.bottom = self.top
        if self.right is None:
            self.right = self.top
        if self.left is None:
            self.left = self.right

    @property
    def horizontal(self):
        return self.left + self.right

    @property
    def vertical(self):
        return self.top + self.bottom

    def size(self, width: Union[int, Size], height: Optional[int] = None) -> Size:
        if height is None:
            width, height = width
        return Size(width + self.left + self.right, height + self.top + self.bottom)

    @classmethod
    def new(cls, value: Union[None, int, 'Margin'], default: int = 0) -> 'Margin':
        if value is None:
            value = default
        if isinstance(value, int):
            return cls(value)
        return cls(*value)


@dataclass
class Border:
    color: str = None
    width: Margin = 0

    def __post_init__(self):
        if type(self.width) is not Margin:
            if isinstance(self.width, Sequence):
                self.width = Margin(*self.width)
            else:
                self.width = Margin(self.width)

    @classmethod
    def new(cls, border):
        if border is None:
            return Border()
        if type(border) is Border:
            return Border(border.color, border.width)
        if type(border) is int:
            return cls('FF000000', border)
        if type(border) is str:
            return cls(border, 1)
        return cls(*border)


class SizePolicy(Flag):
    """Size policy, see Qt5 `QSizePolicy::Policy` for more infomration."""
    # Qt5 documetation is used here, see https://doc.qt.io/qt-5/qsizepolicy.html#Policy-enum.

    #: The widget can grow beyond its size hint if necessary.
    GrowFlag = auto_enum()
    #: The widget should get as much space as possible.
    ExpandFlag = auto_enum()
    #: The widget can shrink below its size hint if necessary.
    ShrinkFlag = auto_enum()
    #: The widget's size hint is ignored. The widget will get as much space as possible.
    IgnoreFlag = auto_enum()

    #: The KWidget.size_hint() is the only acceptable alternative, so the widget can never grow or shrink
    #: (e.g. the vertical direction of a push button).
    Fixed = 0
    #: The size_hint() is minimal, and sufficient. The widget can be expanded, but there is no advantage
    #: to it being larger (e.g. the horizontal direction of a push button). It cannot be smaller
    #: than the size provided by size_hint().
    Minimum = GrowFlag
    #: The size_hint() is a maximum. The widget can be shrunk any amount without detriment if other widgets need
    #: the space (e.g. a separator line). It cannot be larger than the size provided by sizeHint().
    Maximum = ShrinkFlag
    #: The size_hint() is best, but the widget can be shrunk and still be useful. The widget can be expanded,
    #: but there is no advantage to it being larger than sizeHint() (the default KWidget policy).
    Preferred = GrowFlag | ShrinkFlag
    #: The size_hint() is a sensible size, but the widget can be shrunk and still be useful. The widget can make use of
    #: extra space, so it should get as much space as possible (e.g. the horizontal direction of a horizontal slider).
    Expanding = GrowFlag | ShrinkFlag | ExpandFlag
    #: The size_hint() is minimal, and sufficient. The widget can make use of extra space, so it should get as much
    #: space as possible (e.g. the horizontal direction of a horizontal slider).
    MinimumExpanding = GrowFlag | ExpandFlag
    #: The size_hint() is ignored. The widget will get as much space as possible.
    Ignored = GrowFlag | ShrinkFlag | IgnoreFlag


@sequential_dataclass
@dataclass
class WidgetSizePolicy:
    horizontal: SizePolicy = SizePolicy.Preferred
    vertical: SizePolicy = SizePolicy.Preferred


class Dialog(xbmcgui.WindowDialog):

    def __init__(self, bg='0xC0000000'):
        super().__init__()
        width, height = self.getWidth(), self.getHeight()
        self.background = xbmcgui.ControlImage(0, 0, width, height, filename=white(), colorDiffuse=bg)
        self.addControl(self.background)  # omit Wdiget.add() it's a dialog background
        rect = Rect(0, 0, width, height)
        rect.shrink(100)
        self.frame = KFrame(size=rect.size, bg='0x40FFCC00')
        self.frame.window = self
        self.frame.abs.move(100, 100)
        self._mapping_controls = None

    def map(self) -> None:
        self.frame.map()

    def add(self, widget: 'KWidget') -> None:
        self.frame.add(widget)

    # def adjust_size(self) -> None:
    #     """Adjusts the size of the widget to fit its contents."""
    #     self.frame.adjust_size()

    def onAction(self, action):
        aid = action.getId()
        acts = ', '.join(a for a in dir(xbmcgui) if a.startswith('ACTION_') and getattr(xbmcgui, a) == aid)
        log(f'ACTION {action.getId()} ({acts})  {action}')
        if action.getId() in BACK_ACTIONS:
            self.close()
        super().onAction(action)

    def add_control(self, item):
        if self._mapping_controls is None:
            self.window.addControl(item)
        else:
            self._mapping_controls.append(item)

    @contextmanager
    def mapping(self):
        if self._mapping_controls is not None:
            yield self
        else:
            try:
                self._mapping_controls = []
                yield self
            finally:
                self._mapping_controls, controls = None, self._mapping_controls
                if controls:
                    t1 = time()
                    self.addControls(controls)
                    t2 = time()
                    log(f'MAP: add {len(controls)} items in {t2-t1:.3f} s')


class KLayoutItem:

    def __init__(self):
        #: Position relatve to parent.
        self.pos: Point = Point()
        #: Size ocuppated in layout item.
        self.size: Size = Size()


class KWidget:

    def __init__(self, *, size: Optional[Size] = None,
                 bg: Optional[str] = None,
                 content_bg: Optional[str] = '10FFFFFF',  # XXX: Should be `None`
                 margin: Optional[Union[int, Margin]] = None,
                 border: Optional[Border] = None,
                 padding: Optional[Union[int, Margin]] = None,
                 size_policy: Optional[WidgetSizePolicy] = None):

        if size_policy is None:
            size_policy = WidgetSizePolicy()
        elif type(size_policy) is SizePolicy:
            size_policy = WidgetSizePolicy(size_policy, size_policy)
        elif type(size_policy) is not WidgetSizePolicy:
            raise TypeError(f'Incorrect policy_size type {type(size_policy)}')

        #: Kodi window.
        self.window: xbmcgui.Window = None
        #: Parent widget.
        self.parent: KWidget = None
        #: List of children widgets (subcontrols).
        self.children: List[KWidget] = []
        #: Absolute position (in the window). It's equal self.pos + all parents self.abs.
        #: Updated by parent only.
        self.abs: Point = Point()
        #: Updated by layout only.
        self.li: KLayoutItem = KLayoutItem()
        #: Size.
        self.size: Size = Size() if size is None else Size(*size)
        #: Margins (outside the border and background).
        self.margin = Margin.new(margin, 0)
        #: Box border (inside background - non-solid border case).
        self.border = Border.new(border)
        #: Paddings (inside the border and background).
        self.padding = Margin.new(padding, 8)
        #: Size policy
        self.size_policy = size_policy
        #: True if widget is mapped (Kodi controls are created and added to the window).
        self._mapped = False
        #: Background color (AARRGGBB).
        self.bg: str = bg
        #: Background color of content area (padding inside) (AARRGGBB).
        self.content_bg: str = content_bg
        # Background image
        self._bg_image: xbmcgui.ControlImage = None
        #: KWidget need refresh / remap / repositioning.
        self._dirty: bool = True
        #: Last found size-hint.
        self._cache_size_hint = None

    @property
    def rect(self) -> Rect:
        #: Rectangle (position and size) relatve to parent.
        return Rect(*self.li.pos, *self.size)

    @property
    def content_abs(self):
        return Point(self.abs.x + self.margin.left + self.border.width.left + self.padding.left,
                     self.abs.y + self.margin.top + self.border.width.top + self.padding.top)

    @property
    def content_pos(self):
        return Point(self.li.pos.x + self.margin.left + self.border.width.left + self.padding.left,
                     self.li.pos.y + self.margin.top + self.border.width.top + self.padding.top)

    @property
    def content_size(self):
        return Size(self.size.width - self.margin.horizontal - self.border.width.horizontal - self.padding.horizontal,
                    self.size.height - self.margin.vertical - self.border.width.vertical - self.padding.vertical)

    @property
    def mapped(self):
        return self._mapped

    def map(self) -> None:
        with self.window.mapping():
            if self.window:
                if not self._mapped:
                    if self._dirty:
                        self.adjust_size(self.size)
                    log(f'map({self.__class__.__name__}), abs={self.abs}, parent={self.parent and self.parent.abs}, rect={self.rect}, bg={self.bg!r}')
                    self._box_map()
                    self._map()
                    self._mapped = True
                    self._dirty = False
                for w in self.children:
                    w.window = self.window
                    w.abs = self.abs.moved(w.li.pos)
                    w.map()

    def _box_map(self):
        if self.bg is not None:
            self._bg_image = xbmcgui.ControlImage(self.abs.x + self.margin.left, self.abs.y + self.margin.top,
                                                  self.size.width - self.margin.horizontal,
                                                  self.size.height - self.margin.vertical,
                                                  filename=white(), colorDiffuse=self.bg)
            self.window.add_control(self._bg_image)
            if self.border.color:
                w = self.size.width - self.margin.horizontal                              # full width
                h = self.size.height - self.margin.vertical - self.border.width.vertical  # height w/o horiz. borders
                x1, y1 = self.abs.x + self.margin.left, self.abs.y + self.margin.top
                x2 = x1 + self.size.width - self.margin.right - self.border.width.right
                y2 = y1 + self.size.height - self.margin.bottom - self.border.width.bottom
                if self.border.width.top:
                    img = xbmcgui.ControlImage(x1, y1, w, self.border.width.top,
                                               filename=white(), colorDiffuse=self.border.color)
                    self.window.add_control(img)
                if self.border.width.bottom:
                    img = xbmcgui.ControlImage(x1, y2, w, self.border.width.bottom,
                                               filename=white(), colorDiffuse=self.border.color)
                    self.window.add_control(img)
                if self.border.width.left:
                    img = xbmcgui.ControlImage(x1, y1 + self.margin.top + self.border.width.top,
                                               self.border.width.left, h,
                                               filename=white(), colorDiffuse=self.border.color)
                    self.window.add_control(img)
                if self.border.width.right:
                    img = xbmcgui.ControlImage(x2, y1 + self.margin.top + self.border.width.top,
                                               self.border.width.right, h,
                                               filename=white(), colorDiffuse=self.border.color)
                    self.window.add_control(img)
            if self.content_bg:
                img = xbmcgui.ControlImage(*self.content_abs, *self.content_size,
                                           filename=white(), colorDiffuse=self.content_bg)
                self.window.add_control(img)

    def _map(self) -> None:
        pass

    def add(self, widget: 'KWidget') -> None:
        if not isinstance(widget, KWidget):
            widget = KWidgetItem(item=widget)
        if widget.parent is not None:
            try:
                widget.parent.children.remove(widget)
            except ValueError:
                pass
        widget.parent = self
        if widget not in self.children:
            self.children.append(widget)
            if self._mapped:
                widget.map()
            return widget

    def invalidate(self) -> None:
        """Invalidates any cached information in this layout item."""
        self._dirty = True
        self._cache_size_hint = None
        for w in self.children:
            w.invalidate()

    def size_hint(self) -> Size:
        """Returns the preferred size of this item."""
        if self._cache_size_hint is None:
            width = height = 0
            for w in self.children:
                size = w.size_hint()
                width = max(width, size.width)
                height = max(height, size.height)
            self._cache_size_hint = self.margin.size(self.border.width.size(self.padding.size(width, height)))
        return Size(*self._cache_size_hint)

    def adjust_size(self, place: Size) -> Size:
        """Adjusts the size of the widget to fit its contents."""
        pos = self.content_pos
        size = self.content_size
        for w in self.children:
            w.adjust_size(size)
            w.li.pos = pos
        size = self.size_hint()
        content_place = place.pad_in(self.margin, self.border.width, self.padding)
        log(f'adjust_size({self.__class__.__name__}): {self.size=}, {size=}, {place=}, {content_place=}')
        pw, ph = self.size_policy.horizontal, self.size_policy.vertical
        if ((pw & SizePolicy.IgnoreFlag)
                or (size.width > place.width and (pw & SizePolicy.ShrinkFlag))
                or (size.width < place.width and (pw & SizePolicy.GrowFlag))):
            size.width = place.width
        if ((ph & SizePolicy.IgnoreFlag)
                or (size.height > place.height and (ph & SizePolicy.ShrinkFlag))
                or (size.height < place.height and (ph & SizePolicy.GrowFlag))):
            size.height = place.height
        self.size.set(size)
        self._arrange()
        self._dirty = False
        return size

    # def move(self, x: int, y: int) -> None:
    #     self.pos.x = x
    #     self.pos.y = y
    #     self._arrange()

    def _arrange(self) -> None:
        pass


class KWidgetItem(KWidget):
    """KWidget wrapper for single xbmcgui.Control."""

    def __init__(self, item: Optional[xbmcgui.Control] = None, **kwargs):
        super().__init__(**kwargs)
        self.item: xbmcgui.Control = item
        self.item_size: Size = Size()
        if self.item:
            self.item_size = Size(self.item.getWidth(), self.item.getHeight())
            self.size = self.size_hint()

    def _map(self) -> None:
        if self.item:
            self.item.setPosition(*self.content_abs)
            size = self.content_size
            self.item.setWidth(size.width)
            self.item.setHeight(size.height)
            self.window.add_control(self.item)

    def size_hint(self) -> Size:
        """Returns the preferred size of this item."""
        if self._cache_size_hint is None:
            width, height = self.item_size
            for w in self.children:
                size = w.size_hint()
                width = max(width, size.width)
                height = max(height, size.height)
            self._cache_size_hint = self.margin.size(self.border.width.size(self.padding.size(width, height)))
            # self._cache_size_hint = self.padding.size(width, height)
        return Size(*self._cache_size_hint)

    def _arrange(self) -> None:
        if self._mapped and self.item:
            self.item.setPosition(*self.content_abs)
        if self.item:
            size = self.content_size
            self.item.setWidth(size.width)
            self.item.setHeight(size.height)


class KFrame(KWidget):

    def __init__(self, *, size=None, bg=None):
        super().__init__(size=size, bg=bg)


class KBox(KWidget):

    def __init__(self, direction: Direction = Direction.Horizontal, *, spacing: int = 16,
                 padding: Union[int, Margin] = 0, margin: Union[int, Margin] = 0, **kwargs):
        super().__init__(padding=padding, margin=margin, **kwargs)
        self._direction = direction
        self._spacing = spacing
        self._stretch = []

    direction = invalidate_property()
    spacing = invalidate_property()

    def size_hint(self) -> Size:
        """Returns the preferred size of this item."""
        if self._cache_size_hint is None:
            # width, height = self.getWidth(), self.getHeight()
            width = height = 0
            for w in self.children:
                size = w.size_hint()
                if Direction.horizontal(self.direction):
                    width += size.width
                    height = max(height, size.height)
                elif Direction.vertical(self.direction):
                    width = max(width, size.width)
                    height += size.height
            if self.children:
                if Direction.horizontal(self.direction):
                    width += self.spacing * (len(self.children) - 1)
                elif Direction.vertical(self.direction):
                    height += self.spacing * (len(self.children) - 1)
            self._cache_size_hint = self.margin.size(self.border.width.size(self.padding.size(width, height)))
            # self._cache_size_hint = self.padding.size(width, height)
        return self._cache_size_hint

    def adjust_size(self, place: Size) -> Size:
        """Adjusts the size of the widget to fit its contents."""
        if not self.children:
            return self.size_hint()
        content_place = place.pad_in(self.margin, self.border.width, self.padding)
        off = 1 if Direction.vertical(self.direction) else 0
        # count item space
        pos = list(self.content_pos)
        for i, w in enumerate(self.children):
            if i:
                pos[off] += self.spacing
            w.li.size.set(w.size_hint())
            w.li.pos = Point(*pos)
            log(f'  >> box[1].adjust_size: {w=}, {w.li.pos=}, {w.li.size=}, {w.size=}, {w.size_hint()=}')
            pos[off] += w.li.size[off]
        # recount space
        n = len(self.children)
        used = sum((w.li.size[off] for w in self.children))
        space = content_place[off] - self.spacing * (n - 1)
        if used < space and (self.size_policy[off] & SizePolicy.GrowFlag):
            space -= used
            s = sum(self._stretch)
            if s:
                n = s
            step = space // n
            for i, w in enumerate(self.children):
                if not s or self._stretch[i]:
                    a = self._stretch[i] or 1
                    d = space - (n - a) * step
                    w.li.size[off] += d
                    space -= d
                    n -= a
        # _|_ axes
        dim = max((w.size[1 - off] for w in self.children), default=0)
        if dim < content_place[1 - off] and (self.size_policy[1 - off] & SizePolicy.GrowFlag):
            dim = content_place[1 - off]
        if self.size[1 - off] < place[1 - off] and (self.size_policy[1 - off] & SizePolicy.GrowFlag):
            self.size[1 - off] = place[1 - off]
        # adjust items
        pos = list(self.content_pos)
        for i, w in enumerate(self.children):
            if i:
                pos[off] += self.spacing
            w.li.pos = Point(*pos)
            w.li.size[1 - off] = dim
            w.adjust_size(w.li.size)
            log(f'  >> box[2].adjust_size: {w=}, {w.li.pos=}, {w.li.size=}, {w.size=}, {w.size_hint()=}')
            pos[off] += w.li.size[off]

        pos[1 - off] += dim
        self.size = size = Size(pos[0] + self.padding.right + self.border.width.right + self.margin.right,
                                pos[1] + self.padding.bottom + self.border.width.bottom + self.margin.bottom)
        self._arrange()
        self._dirty = False

        size_hint = self.size_hint()
        log(f'box.adjust_size({self.__class__.__name__}): {size=}, {size_hint=}, {place=}, {content_place=}')
        return Size(*size)

    def add(self, widget: 'KWidget', *, stretch: int = 0) -> None:
        w = super().add(widget)
        self._stretch.append(stretch)
        return w


class KHBox(KBox):

    def __init__(self, **kwargs):
        super().__init__(direction=Direction.Horizontal, **kwargs)


class KVBox(KBox):

    def __init__(self, **kwargs):
        super().__init__(direction=Direction.Vertical, **kwargs)


class TvpScript(Script):

    def abc(self, a: int = 42, /, b: int = 44):
        log(f'TvpScript.abc(a={a!r}, b={b!r})')

    def ss(self, x):
        t1 = time()
        win = Dialog()
        width, height = win.getWidth(), win.getHeight()
        log(f'{x=!r}, win {width}, {height}')
        if 0:
            win.add(KFrame(size=[200, 100], bg='40FF0000'))
        if 0:
            box = KVBox(bg='20000000', margin=4)
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, 'Status', angle=45), bg='20FF0000',
                    border=Border('60660000', (5, 0))))
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, 'Second'), bg='2000FF00',
                    border=Border('60006600', (0, 5))), stretch=2)
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, '3rd'), bg='200000FF',
                    border=Border('60000066', 5), size_policy=SizePolicy.Fixed), stretch=1)
            win.add(box)
        if 1:
            vbox = KVBox(padding=4, border='FFFFFF00')
            box = KBox(bg='20000000', margin=4, border=2)
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, 'Status', angle=45), bg='20FF0000',
                    border=Border('60660000', (5, 0))))
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, 'Second'), bg='2000FF00',
                    border=Border('60006600', (0, 5))), stretch=2)
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, '3rd'), bg='200000FF',
                    border=Border('60000066', 5), size_policy=SizePolicy.Fixed), stretch=1)
            vbox.add(box)
            box = KBox(bg='20000000', margin=4)
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, 'First'), bg='20FF0000',
                    border=Border('60660000', 5)))
            box.add(KWidgetItem(xbmcgui.ControlLabel(0, 0, 100, 100, 'Second'), bg='2000FF00',
                    border=Border('60006600', 5)))
            vbox.add(box)
            win.add(vbox)
        win.map()
        t2 = time()
        log(f'CREATE: in {t2-t1:.3f} s')
        win.doModal()


log(f'TVP: {__file__}: {sys.argv}')
TvpScript().run()
