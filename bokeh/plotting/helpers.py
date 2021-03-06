from __future__ import absolute_import

from collections import Iterable, Sequence
import difflib
import itertools
import re
import warnings

import numpy as np
from six import string_types

from ..models import (
    BoxSelectTool, BoxZoomTool, CategoricalAxis,
    TapTool, CrosshairTool, DataRange1d, DatetimeAxis,
    FactorRange, Grid, HelpTool, HoverTool, LassoSelectTool, Legend, LinearAxis,
    LogAxis, PanTool, PolySelectTool, ContinuousTicker,
    SaveTool, Range, Range1d, UndoTool, RedoTool, ResetTool, ResizeTool, Tool,
    WheelPanTool, WheelZoomTool, ColumnDataSource, GlyphRenderer)

from ..core.properties import ColorSpec, Datetime, value
from ..util.deprecate import BokehDeprecationWarning
from ..util.string import nice_join

DEFAULT_PALETTE = ["#f22c40", "#5ab738", "#407ee7", "#df5320", "#00ad9c", "#c33ff3"]


def get_default_color(plot=None):
    colors = [
      "#1f77b4",
      "#ff7f0e", "#ffbb78",
      "#2ca02c", "#98df8a",
      "#d62728", "#ff9896",
      "#9467bd", "#c5b0d5",
      "#8c564b", "#c49c94",
      "#e377c2", "#f7b6d2",
      "#7f7f7f",
      "#bcbd22", "#dbdb8d",
      "#17becf", "#9edae5"
    ]
    if plot:
        renderers = plot.renderers
        renderers = [x for x in renderers if x.__view_model__ == "GlyphRenderer"]
        num_renderers = len(renderers)
        return colors[num_renderers]
    else:
        return colors[0]

def get_default_alpha(plot=None):
    return 1.0

def _glyph_doc(args, props, desc):
    params_tuple =tuple(itertools.chain.from_iterable(sorted(list(args.items()))))
    params = "\t%s : %s\n" * len(args) % params_tuple

    return """%s

    Parameters
    ----------
    %s
    Additionally, the following properties are accepted as keyword arguments: %s

    Returns
    -------
    plot : :py:class:`Plot <bokeh.models.Plot>`
    """ % (desc, params, props)

def _pop_renderer_args(kwargs):
    result = dict(data_source=kwargs.pop('source', ColumnDataSource()))
    for attr in ['name', 'x_range_name', 'y_range_name', 'level']:
        val = kwargs.pop(attr, None)
        if val: result[attr] = val
    return result

def _pop_colors_and_alpha(glyphclass, kwargs, prefix="", default_alpha=1.0):
    """
    Given a kwargs dict, a prefix, and a default value, looks for different
    color and alpha fields of the given prefix, and fills in the default value
    if it doesn't exist.
    """
    result = dict()

    # TODO: The need to do this and the complexity of managing this kind of
    # thing throughout the codebase really suggests that we need to have
    # a real stylesheet class, where defaults and Types can declaratively
    # substitute for this kind of imperative logic.
    color = kwargs.pop(prefix+"color", get_default_color())
    for argname in ("fill_color", "line_color"):
        if argname not in glyphclass.properties(): continue
        result[argname] = kwargs.pop(prefix + argname, color)

    # NOTE: text fill color should really always default to black, hard coding
    # this here now until the stylesheet solution exists
    if "text_color" in glyphclass.properties():
        result["text_color"] = kwargs.pop(prefix + "text_color", "black")

    alpha = kwargs.pop(prefix+"alpha", default_alpha)
    for argname in ("fill_alpha", "line_alpha", "text_alpha"):
        if argname not in glyphclass.properties(): continue
        result[argname] = kwargs.pop(prefix + argname, alpha)

    return result


def _process_legend_kwargs(kwargs):
    if kwargs.get('legend') and kwargs.get('label'):
        raise RuntimeError("Cannot set both legend and label at the same time.")
    legend = kwargs.pop('legend', None)
    source = kwargs.get('source')
    if legend:
        if isinstance(legend, string_types):
            # Try and do something intelligent with a legend string:
            # * value if it's not in column data source
            # * field if it is
            if source and hasattr(source, 'column_names'):
                if legend in source.column_names:
                    kwargs['label'] = legend
                else:
                    kwargs['label'] = value(legend)
        else:
            # Otherwise just accept folks were being intentional
            kwargs['label'] = legend

_GLYPH_SOURCE_MSG = """
Supplying a user-defined data source AND iterable values to glyph methods is deprecated.

See https://github.com/bokeh/bokeh/issues/2056 for more information.
"""

def _process_sequence_literals(glyphclass, kwargs, source, is_user_source):
    dataspecs = glyphclass.dataspecs_with_props()
    for var, val in kwargs.items():

        # ignore things that are not iterable
        if not isinstance(val, Iterable): continue

        # pass dicts (i.e., values or fields) on as-is
        if isinstance(val, dict): continue

        # let any non-dataspecs do their own validation (e.g., line_dash properties)
        if var not in dataspecs: continue

        # strings sequences are handled by the dataspec as-is
        if isinstance(val, string_types): continue

        # similarly colorspecs handle color tuple sequences as-is
        if (isinstance(dataspecs[var].descriptor, ColorSpec) and ColorSpec.is_color_tuple(val)):
            continue

        if isinstance(val, np.ndarray) and val.ndim != 1:
            raise RuntimeError("Columns need to be 1D (%s is not)" % var)

        if is_user_source:
            warnings.warn(_GLYPH_SOURCE_MSG, BokehDeprecationWarning, stacklevel=2)

        source.add(val, name=var)
        kwargs[var] = var

def _make_glyph(glyphclass, kws, extra):
        if extra is None: return None
        kws = kws.copy()
        kws.update(extra)
        return glyphclass(**kws)

def _update_legend(plot, glyph_renderer):
    legends = plot.select(type=Legend)
    if not legends:
        legend = Legend()
        plot.add_layout(legend)
    elif len(legends) == 1:
        legend = legends[0]
    else:
        raise RuntimeError("Plot %s configured with more than one legend renderer" % plot)
    legend.legends.append(glyph_renderer)

def _get_range(range_input):
    if range_input is None:
        return DataRange1d()
    if isinstance(range_input, Range):
        return range_input
    if isinstance(range_input, Sequence):
        if all(isinstance(x, string_types) for x in range_input):
            return FactorRange(factors=range_input)
        if len(range_input) == 2:
            try:
                return Range1d(start=range_input[0], end=range_input[1])
            except ValueError: # @mattpap suggests ValidationError instead
                pass
    raise ValueError("Unrecognized range input: '%s'" % str(range_input))

def _get_axis_class(axis_type, range_input):
    if axis_type is None:
        return None
    elif axis_type == "linear":
        return LinearAxis
    elif axis_type == "log":
        return LogAxis
    elif axis_type == "datetime":
        return DatetimeAxis
    elif axis_type == "auto":
        if isinstance(range_input, FactorRange):
            return CategoricalAxis
        elif isinstance(range_input, Range1d):
            try:
                # Easier way to validate type of Range1d parameters
                Datetime.validate(Datetime(), range_input.start)
                return DatetimeAxis
            except ValueError:
                pass
        return LinearAxis
    else:
        raise ValueError("Unrecognized axis_type: '%r'" % axis_type)

def _get_num_minor_ticks(axis_class, num_minor_ticks):
    if isinstance(num_minor_ticks, int):
        if num_minor_ticks <= 1:
            raise ValueError("num_minor_ticks must be > 1")
        return num_minor_ticks
    if num_minor_ticks is None:
        return 0
    if num_minor_ticks == 'auto':
        if axis_class is LogAxis:
            return 10
        return 5

_known_tools = {
    "pan": lambda: PanTool(dimensions=["width", "height"]),
    "xpan": lambda: PanTool(dimensions=["width"]),
    "ypan": lambda: PanTool(dimensions=["height"]),
    "wheel_zoom": lambda: WheelZoomTool(dimensions=["width", "height"]),
    "xwheel_zoom": lambda: WheelZoomTool(dimensions=["width"]),
    "ywheel_zoom": lambda: WheelZoomTool(dimensions=["height"]),
    "xwheel_pan": lambda: WheelPanTool(dimension="width"),
    "ywheel_pan": lambda: WheelPanTool(dimension="height"),
    "resize": lambda: ResizeTool(),
    "click": lambda: TapTool(behavior="inspect"),
    "tap": lambda: TapTool(),
    "crosshair": lambda: CrosshairTool(),
    "box_select": lambda: BoxSelectTool(),
    "xbox_select": lambda: BoxSelectTool(dimensions=['width']),
    "ybox_select": lambda: BoxSelectTool(dimensions=['height']),
    "poly_select": lambda: PolySelectTool(),
    "lasso_select": lambda: LassoSelectTool(),
    "box_zoom": lambda: BoxZoomTool(dimensions=['width', 'height']),
    "xbox_zoom": lambda: BoxZoomTool(dimensions=['width']),
    "ybox_zoom": lambda: BoxZoomTool(dimensions=['height']),
    "hover": lambda: HoverTool(tooltips=[
        ("index", "$index"),
        ("data (x, y)", "($x, $y)"),
        ("canvas (x, y)", "($sx, $sy)"),
    ]),
    "save": lambda: SaveTool(),
    "previewsave": "save",
    "undo": lambda: UndoTool(),
    "redo": lambda: RedoTool(),
    "reset": lambda: ResetTool(),
    "help": lambda: HelpTool(),
}

def _tool_from_string(name):
    """ Takes a string and returns a corresponding `Tool` instance. """
    known_tools = sorted(_known_tools.keys())

    if name in known_tools:
        tool_fn = _known_tools[name]

        if isinstance(tool_fn, string_types):
            tool_fn = _known_tools[tool_fn]

        return tool_fn()
    else:
        matches, text = difflib.get_close_matches(name.lower(), known_tools), "similar"

        if not matches:
            matches, text = known_tools, "possible"

        raise ValueError("unexpected tool name '%s', %s tools are %s" % (name, text, nice_join(matches)))


def _process_axis_and_grid(plot, axis_type, axis_location, minor_ticks, axis_label, rng, dim):
    axiscls = _get_axis_class(axis_type, rng)
    if axiscls:

        if axiscls is LogAxis:
            # TODO (bev) this mapper type hinting is ugly
            if dim == 0:
                plot.x_mapper_type = 'log'
            elif dim == 1:
                plot.y_mapper_type = 'log'
            else:
                raise ValueError("received invalid dimension value: %r" % dim)

        # this is so we can get a ticker off the axis, even if we discard it
        axis = axiscls(plot=plot if axis_location else None)

        if isinstance(axis.ticker, ContinuousTicker):
            axis.ticker.num_minor_ticks = _get_num_minor_ticks(axiscls, minor_ticks)

        axis_label = axis_label
        if axis_label:
            axis.axis_label = axis_label

        grid = Grid(plot=plot, dimension=dim, ticker=axis.ticker); grid

        if axis_location is not None:
            getattr(plot, axis_location).append(axis)


def _process_tools_arg(plot, tools):
    """ Adds tools to the plot object

    Args:
        plot (Plot): instance of a plot object
        tools (seq[Tool or str]|str): list of tool types or string listing the
            tool names. Those are converted using the _tool_from_string
            function. I.e.: `wheel_zoom,box_zoom,reset`.

    Returns:
        list of Tools objects added to plot, map of supplied string names to tools
    """
    tool_objs = []
    tool_map = {}
    temp_tool_str = ""
    repeated_tools = []

    if isinstance(tools, (list, tuple)):
        for tool in tools:
            if isinstance(tool, Tool):
                tool_objs.append(tool)
            elif isinstance(tool, string_types):
                temp_tool_str += tool + ','
            else:
                raise ValueError("tool should be a string or an instance of Tool class")
        tools = temp_tool_str

    for tool in re.split(r"\s*,\s*", tools.strip()):
        # re.split will return empty strings; ignore them.
        if tool == "":
            continue

        tool_obj = _tool_from_string(tool)
        tool_objs.append(tool_obj)
        tool_map[tool] = tool_obj

    for typename, group in itertools.groupby(
            sorted([tool.__class__.__name__ for tool in tool_objs])):
        if len(list(group)) > 1:
            repeated_tools.append(typename)

    if repeated_tools:
        warnings.warn("%s are being repeated" % ",".join(repeated_tools))

    return tool_objs, tool_map


def _process_active_tools(toolbar, tool_map, active_drag, active_scroll, active_tap):
    """ Adds tools to the plot object

    Args:
        toolbar (Toolbar): instance of a Toolbar object
        tools_map (dict[str]|Tool): tool_map from _process_tools_arg
        active_drag (str or Tool): the tool to set active for drag
        active_scroll (str or Tool): the tool to set active for scroll
        active_tap (str or Tool): the tool to set active for tap

    Returns:
        None

    Note:
        This function sets properties on Toolbar
    """
    if active_drag in ['auto', None] or isinstance(active_drag, Tool):
        toolbar.active_drag = active_drag
    elif active_drag in tool_map:
        toolbar.active_drag = tool_map[active_drag]
    else:
        raise ValueError("Got unknown %r for 'active_drag', which was not a string supplied in 'tools' argument" % active_drag)

    if active_scroll in ['auto', None] or isinstance(active_scroll, Tool):
        toolbar.active_scroll = active_scroll
    elif active_scroll in tool_map:
        toolbar.active_scroll = tool_map[active_scroll]
    else:
        raise ValueError("Got unknown %r for 'active_scroll', which was not a string supplied in 'tools' argument" % active_scroll)

    if active_tap in ['auto', None] or isinstance(active_tap, Tool):
        toolbar.active_tap = active_tap
    elif active_tap in tool_map:
        toolbar.active_tap = tool_map[active_tap]
    else:
        raise ValueError("Got unknown %r for 'active_tap', which was not a string supplied in 'tools' argument" % active_tap)


_arg_template = "    %s (%s) : %s (default %r)"
_doc_template = """ Configure and add %s glyphs to this Figure.

Args:
%s

Keyword Args:
%s

Other Parameters:
    alpha (float) : an alias to set all alpha keyword args at once
    color (Color) : an alias to set all color keyword args at once
    source (ColumnDataSource) : a user supplied data source
    legend (str) : a legend tag for this glyph
    x_range_name (str) : name an extra range to use for mapping x-coordinates
    y_range_name (str) : name an extra range to use for mapping y-coordinates
    level (Enum) : control the render level order for this glyph

It is also possible to set the color and alpha parameters of a "nonselection"
glyph. To do so, prefix any visual parameter with ``'nonselection_'``.
For example, pass ``nonselection_alpha`` or ``nonselection_fill_alpha``.

Returns:
    GlyphRenderer
"""

def _glyph_function(glyphclass, extra_docs=None):

    def func(self, *args, **kwargs):

        _process_legend_kwargs(kwargs)
        # Need to check if user source is present before _pop_renderer_args
        is_user_source = kwargs.get('source', None) is not None
        renderer_kws = _pop_renderer_args(kwargs)
        source = renderer_kws['data_source']

        # add the positional arguments as kwargs
        attributes = dict(zip(glyphclass._args, args))
        kwargs.update(attributes)

        # handle the main glyph, need to process literals
        glyph_ca = _pop_colors_and_alpha(glyphclass, kwargs)
        _process_sequence_literals(glyphclass, kwargs, source, is_user_source)
        _process_sequence_literals(glyphclass, glyph_ca, source, is_user_source)

        # handle the nonselection glyph, we always set one
        nsglyph_ca = _pop_colors_and_alpha(glyphclass, kwargs, prefix='nonselection_', default_alpha=0.1)

        # handle the selection glyph, if any properties were given
        if any(x.startswith('selection_') for x in kwargs):
            sglyph_ca = _pop_colors_and_alpha(glyphclass, kwargs, prefix='selection_')
        else:
            sglyph_ca = None

        # handle the hover glyph, if any properties were given
        if any(x.startswith('hover_') for x in kwargs):
            hglyph_ca = _pop_colors_and_alpha(glyphclass, kwargs, prefix='hover_')
        else:
            hglyph_ca = None

        glyph = _make_glyph(glyphclass, kwargs, glyph_ca)
        nsglyph = _make_glyph(glyphclass, kwargs, nsglyph_ca)
        hglyph = _make_glyph(glyphclass, kwargs, hglyph_ca)
        sglyph = _make_glyph(glyphclass, kwargs, sglyph_ca)

        glyph_renderer = GlyphRenderer(glyph=glyph,
                                       nonselection_glyph=nsglyph,
                                       selection_glyph=sglyph,
                                       hover_glyph=hglyph,
                                       **renderer_kws)

        if glyph.label:
            _update_legend(self, glyph_renderer)

        for tool in self.select(type=BoxSelectTool):
            # this awkward syntax is needed to go through Property.__set__ and
            # therefore trigger a change event. With improvements to Property
            # we might be able to use a more natural append() or +=
            tool.renderers = tool.renderers + [glyph_renderer]

        # awkward syntax for same reason mentioned above
        self.renderers = self.renderers + [glyph_renderer]
        return glyph_renderer

    func.__name__ = glyphclass.__view_model__

    arglines = []
    for arg in glyphclass._args:
        spec = getattr(glyphclass, arg)
        desc = " ".join(x.strip() for x in spec.__doc__.strip().split("\n\n")[0].split('\n'))
        arglines.append(_arg_template % (arg, spec.__class__.__name__, desc, spec.class_default(glyphclass)))

    kwlines = []
    kws = glyphclass.properties() - set(glyphclass._args)
    for kw in sorted(kws):
        if kw == "session": continue # TODO (bev) improve or remove
        spec = getattr(glyphclass, kw)
        if spec.__doc__:
            typ = spec.__class__.__name__
            desc = " ".join(x.strip() for x in spec.__doc__.strip().split("\n\n")[0].split('\n'))
        else:
            typ = str(spec)
            desc = ""
        kwlines.append(_arg_template % (kw, typ, desc, spec.class_default(glyphclass)))

    func.__doc__ = _doc_template  % (glyphclass.__name__, "\n".join(arglines), "\n".join(kwlines))

    if extra_docs:
        func.__doc__ += extra_docs
    return func
