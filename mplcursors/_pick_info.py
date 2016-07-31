from collections import namedtuple
from functools import singledispatch
import re
import warnings

from matplotlib import cbook
from matplotlib.collections import PathCollection
from matplotlib.image import AxesImage
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.path import Path as MPath
from matplotlib.text import Text
import numpy as np


class AttrArray(np.ndarray):
    """An array subclass that can store additional attributes.
    """

    def __new__(cls, array):
        return np.asarray(array).view(cls)


Selection = namedtuple("Selection", "artist target dist annotation extras")
# Override equality to identity: Selections should be considered immutable
# (with mutable fields though) and we don't want to trigger casts of array
# equality checks to booleans.  We don't need to override comparisons because
# artists are already non-comparable.
Selection.__eq__ = lambda self, other: self is other
Selection.__ne__ = lambda self, other: self is not other
Selection.artist.__doc__ = (
    "The selected artist.")
Selection.target.__doc__ = (
    "The point picked within the artist, in data coordinates.")
Selection.dist.__doc__ = (
    "The distance from the click to the target, in pixels.")
Selection.annotation.__doc__ = (
    "The instantiated `matplotlib.text.Annotation`.")
Selection.extras.__doc__ = (
    "An additional list of artists (e.g., highlighters) that will be cleared "
    "at the same time as the annotation.")


@singledispatch
def compute_pick(artist, event):
    """Find whether ``artist`` has been picked by ``event``.

    If it has, return the appropriate `Selection`; otherwise return ``None``.

    This is a single-dispatch function; implementations for various artist
    classes follow.
    """
    warnings.warn("Support for {} is missing.".format(type(artist)))


class Index:
    def __init__(self, i, x, y):
        self.int = i
        self.x = x
        self.y = y

    def floor(self):
        return self.int

    def ceil(self):
        return self.int if max(self.x, self.y) == 0 else self.int + 1

    def __format__(self, fmt):
        return "{0.int}.(x={0.x:{1}}, y={0.y:{1}})".format(self, fmt)

    def __str__(self):
        return format(self, "")

    @classmethod
    def pre_index(cls, n_pts, raw_index, frac):
        i, odd = divmod(raw_index, 2)
        x, y = (0, frac) if not odd else (frac, 1)
        return cls(i, x, y)

    @classmethod
    def post_index(cls, n_pts, raw_index, frac):
        i, odd = divmod(raw_index, 2)
        x, y = (frac, 0) if not odd else (1, frac)
        return cls(i, x, y)

    @classmethod
    def mid_index(cls, n_pts, raw_index, frac):
        if raw_index == 0:
            frac = .5 + frac / 2
        elif raw_index == n_pts - 2:  # One less line than points.
            frac = frac / 2
        quot, odd = divmod(raw_index, 2)
        if not odd:
            if frac < .5:
                i = quot - 1
                x, y = frac + .5, 1
            else:
                i = quot
                x, y = frac - .5, 0
        else:
            i = quot
            x, y = .5, frac
        return cls(i, x, y)


@compute_pick.register(Line2D)
def _(artist, event):
    # No need to call `line.contains` because we're going to redo
    # the work anyways, and it was broken for step plots up to
    # matplotlib/matplotlib#6645.

    # Always work in screen coordinates, as this is how we need to compute
    # distances.  Note that the artist transform may be different from the axes
    # transform (e.g., for axvline).
    xy = event.x, event.y
    sels = []
    # If markers are visible, find the closest vertex.
    if artist.get_marker() not in ["None", "none", " ", "", None]:
        artist_data_xys = artist.get_xydata()
        artist_xys = artist.get_transform().transform(artist_data_xys)
        d2s = ((xy - artist_xys) ** 2).sum(-1)
        argmin = np.nanargmin(d2s)
        dmin = np.sqrt(d2s[argmin])
        # More precise than transforming back.
        target = AttrArray(artist.get_xydata()[argmin])
        target.index = argmin
        sels.append(Selection(artist, target, dmin, None, None))
    # If lines are visible, find the closest projection.
    if (artist.get_linestyle() not in ["None", "none", " ", "", None]
            and len(artist.get_xydata()) > 1):
        drawstyle = artist.drawStyles[artist.get_drawstyle()]
        drawstyle_conv = {
            "_draw_lines": lambda xs, ys: (xs, ys),
            "_draw_steps_pre": cbook.pts_to_prestep,
            "_draw_steps_mid": cbook.pts_to_midstep,
            "_draw_steps_post": cbook.pts_to_poststep}[drawstyle]
        artist_data_xys = np.asarray(drawstyle_conv(*artist.get_xydata().T)).T
        transform = artist.get_transform()
        artist_xys = (
            transform.transform(artist_data_xys) if transform.is_affine
            # Only construct Paths if we need to follow a curved projection.
            else transform.transform_path(MPath(artist_data_xys)).vertices)
        # Unit vectors for each segment.
        us = artist_xys[1:] - artist_xys[:-1]
        ds = np.sqrt((us ** 2).sum(-1))
        us /= ds[:, None]
        # Vectors from each vertex to the event.
        vs = xy - artist_xys[:-1]
        # Clipped dot products.
        dot = np.clip((vs * us).sum(-1), 0, ds)
        # Projections.
        projs = artist_xys[:-1] + dot[:, None] * us
        d2s = ((xy - projs) ** 2).sum(-1)
        argmin = np.nanargmin(d2s)
        dmin = np.sqrt(d2s[argmin])
        target = AttrArray(
            artist.axes.transData.inverted().transform_point(projs[argmin]))
        if transform.is_affine:  # Otherwise, all bets are off.
            target.index = {
                "_draw_lines": lambda _, x, y: x + y,
                "_draw_steps_pre": Index.pre_index,
                "_draw_steps_mid": Index.mid_index,
                "_draw_steps_post": Index.post_index}[drawstyle](
                    len(artist_xys), argmin, dot[argmin] / ds[argmin])
        sels.append(Selection(artist, target, dmin, None, None))
    sel = min(sels, key=lambda sel: sel.dist, default=None)
    return sel if sel and sel.dist < artist.pickradius else None


@compute_pick.register(PathCollection)
def _(artist, event):
    contains, info = artist.contains(event)
    if not contains:
        return
    # Snapping, really only works for scatter plots (for example,
    # `PathCollection`s created through `matplotlib.tri` are unsupported).
    if len(artist.get_paths()) != 1:
        warnings.warn("Only PathCollections created through `plt.scatter` are "
                      "supported.")
        return
    ax = artist.axes
    idxs = info["ind"]
    offsets = artist.get_offsets()[idxs]
    d2 = ((ax.transData.transform(offsets) -
           [event.x, event.y]) ** 2).sum(axis=1)
    argmin = d2.argmin()
    target = AttrArray(offsets[argmin])
    target.index = idxs[argmin]
    return Selection(artist, target, np.sqrt(d2[argmin]), None, None)


@compute_pick.register(AxesImage)
@compute_pick.register(Patch)
def _(artist, event):
    contains, _ = artist.contains(event)
    if not contains:
        return
    return Selection(artist, (event.xdata, event.ydata), 0, None, None)


@compute_pick.register(Text)
def _(artist, event):
    return


@singledispatch
def get_ann_text(*args):
    """Compute an annotating text for a `Selection` (unpacked as ``*args``).

    This is a single-dispatch function; implementations for various artist
    classes follow.
    """
    sel = Selection(*args)
    warnings.warn("Support for {} is missing".format(type(sel.artist)))
    return ""


@get_ann_text.register(Line2D)
@get_ann_text.register(PathCollection)
@get_ann_text.register(Patch)
def _(*args):
    sel = Selection(*args)
    ax = sel.artist.axes
    x, y = sel.target
    label = sel.artist.get_label() or ""
    # Un-space-pad the output of `format_{x,y}data`.
    text = re.sub("[ ,] +", "\n", ax.format_coord(x, y)).strip()
    if re.match("[^_]", label):
        text = "{}\n{}".format(label, text)
    return text


@get_ann_text.register(AxesImage)
def _(*args):
    sel = Selection(*args)
    artist = sel.artist
    ax = artist.axes
    x, y = sel.target
    # Un-space-pad the output of `format_{x,y}data`.
    text = re.sub("[ ,] +", "\n", ax.format_coord(x, y)).strip()
    event = namedtuple("event", "xdata ydata")(x, y)
    text += "\n[{}]".format(
        artist.format_cursor_data(artist.get_cursor_data(event)))
    return text


@singledispatch
def move(*args, key):
    """"Move" a `Selection` following a keypress.

    This function is used to implement annotation displacement through the
    keyboard.

    This is a single-dispatch function; implementations for various artist
    classes follow.
    """
    return Selection(*args)


@move.register(Line2D)
def _(*args, key):
    sel = Selection(*args)
    if not hasattr(sel.target, "index"):
        return sel
    new_idx = (int(np.ceil(sel.target.index) - 1) if key == "left"
               else int(np.floor(sel.target.index) + 1) if key == "right"
               else sel.target.index)
    artist_xys = sel.artist.get_xydata()
    target = AttrArray(artist_xys[new_idx % len(artist_xys)])
    target.index = new_idx
    return sel._replace(target=target, dist=0)


@move.register(AxesImage)
def _(*args, key):
    sel = Selection(*args)
    if type(sel.artist) != AxesImage:
        # All bets are off with subclasses such as NonUniformImage.
        return sel
    low, high = np.reshape(sel.artist.get_extent(), (2, 2)).T
    ns = np.asarray(sel.artist.get_array().shape)[::-1]  # (y, x) -> (x, y)
    idxs = ((sel.target - low) / (high - low) * ns).astype(int)
    idxs += {
        "left": [-1, 0], "right": [1, 0], "up": [0, 1], "down": [0, -1]}[key]
    idxs %= ns
    target = (idxs + .5) / ns * (high - low) + low
    return sel._replace(target=target)
