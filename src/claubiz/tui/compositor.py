import textual
from textual._compositor import ChopsUpdate, Compositor
from textual.geometry import Region
from textual.strip import Strip

SUPPORTED_MAJOR = "8."
_EMPTY = Strip([], 0)
_state = {"installed": False}

_original_partial = Compositor.render_partial_update
_original_chops = Compositor._render_chops


def _owners(compositor: Compositor) -> list[dict[int, tuple[Region, Region] | None]]:
    cuts = compositor.cuts
    cached = getattr(compositor, "_claubiz_owners", None)
    if cached is not None and cached[0] is cuts:
        return cached[1]
    owners: list[dict[int, tuple[Region, Region] | None]] = [dict.fromkeys(line[:-1]) for line in cuts]
    for region, clip in compositor.visible_widgets.values():
        x, y, width, height = Region.intersection(region, clip)
        if not (width and height):
            continue
        right = x + width
        key = (region, clip)
        for line in owners[y : y + height]:
            for cut in line:
                if x <= cut < right and line[cut] is None:
                    line[cut] = key
    compositor._claubiz_owners = (cuts, owners)
    return owners


def _render_chops(self: Compositor, crop: Region, is_rendered_line) -> list[dict[int, Strip | None]]:
    cuts = self.cuts
    owners = _owners(self)
    chops: list[dict[int, Strip | None]] = [dict.fromkeys(cut_set[:-1]) for cut_set in cuts]
    for region, clip, strips in self._get_renders(crop):
        render_region = region.intersection(clip)
        render_x = render_region.x
        first_cut, last_cut = render_region.column_span
        key = (region, clip)
        for y, strip in zip(render_region.line_range, strips):
            if not is_rendered_line(y):
                continue
            chops_line = chops[y]
            owner_line = owners[y]
            final_cuts = [cut for cut in cuts[y] if last_cut >= cut >= first_cut]
            keys = final_cuts[:-1]
            if not keys:
                continue
            starts: list[int] = []
            previous = False
            for index, cut in enumerate(keys):
                mergeable = chops_line[cut] is None and owner_line.get(cut) == key
                if not (mergeable and previous):
                    starts.append(index)
                previous = mergeable
            bounds = [keys[i] for i in starts[1:]] + [final_cuts[-1]]
            pieces = strip.divide([bound - render_x for bound in bounds])
            ends = [*starts[1:], len(keys)]
            for start, end, piece in zip(starts, ends, pieces):
                if chops_line[keys[start]] is None:
                    chops_line[keys[start]] = piece
                for index in range(start + 1, end):
                    chops_line[keys[index]] = _EMPTY
    return chops


def _render_partial_update(self: Compositor) -> ChopsUpdate | None:
    update = _original_partial(self)
    if update is None:
        return None
    update.chop_ends = [
        [end if strip is None else x + strip.cell_length for (x, strip), end in zip(line.items(), ends)]
        for line, ends in zip(update.chops, update.chop_ends)
    ]
    return update


def install() -> bool:
    if _state["installed"]:
        return True
    if not textual.__version__.startswith(SUPPORTED_MAJOR):
        return False
    Compositor._render_chops = _render_chops
    Compositor.render_partial_update = _render_partial_update
    _state["installed"] = True
    return True
