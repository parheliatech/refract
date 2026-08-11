"""Making the pointer's path agree with what the wearer sees.

Mutter parks new virtual monitors at the far right of the desktop. Measured
on this machine with Desk running:

    eDP-1@0   DP-2@1920   Meta-0@5760   Meta-1@7680

but Desk draws them as  [Meta-0] [eDP-1] [Meta-1], mirror in the centre, as
the concept spec requires. Those two orders disagree, so pushing the pointer
off the left of the centre screen does not arrive on the left screen -- it
goes nowhere, and the virtual monitors are reachable only by travelling
right THROUGH the glasses output. "Cursor continuity comes free from Mutter"
is only true if the logical layout is arranged to match.

`plan_positions` is pure so it can be tested without touching the session;
applying it is a separate, opt-in step (Desk's "Match desktop layout"
setting) because rearranging someone's monitors mid-session is not
something to do behind their back.
"""


def plan_positions(layout, order, park=()):
    """Lay the desk monitors in a row and push `park` onto a SECOND row.

    layout: [(connector, x, y, w, h)] as from displaymode.logical_layout()
    order:  connectors in the order Desk draws them, left to right
    park:   connectors to keep out of the way -- the glasses output, which
            displays Refract itself. A window dragged onto it lands in front
            of your eyes and hides the very monitors you were aiming for.

    Mutter REJECTS any layout whose logical monitors are not adjacent
    ("Logical monitors not adjacent"), so the glasses output cannot simply be
    banished to a gap -- it has to touch the rest somewhere. It does not have
    to touch them SIDEWAYS though: on a second row, dragging left and right
    between the desk monitors never crosses it, and only a deliberate
    downward drag can reach it. Verified accepted by Mutter.

    Returns {connector: (x, y)}. Monitors absent from `layout` are ignored.
    """
    size = {conn: (w, h) for conn, _x, _y, w, h in layout}
    known = [c for c in order if c in size]
    parked = [c for c in park if c in size]
    rest = [conn for conn, _x, _y, _w, _h in layout
            if conn not in known and conn not in parked]

    positions = {}
    cursor = 0
    last_x = 0
    for conn in known + rest:
        positions[conn] = (cursor, 0)
        last_x = cursor
        cursor += size[conn][0]
    if not positions:                       # nothing but parked monitors
        for i, conn in enumerate(parked):
            positions[conn] = (i * size[conn][0], 0)
        return positions

    # second row, starting under the RIGHTMOST desk monitor: the pointer can
    # only fall onto it by dragging down off the least-used screen
    row_h = max(size[c][1] for c in positions)
    x = last_x
    for conn in parked:
        positions[conn] = (x, row_h)
        x += size[conn][0]
    return positions


def positions_of(layout):
    """{connector: (x, y)} -- a snapshot to restore later."""
    return {conn: (x, y) for conn, x, y, _w, _h in layout}


def pointer_order(layout):
    """Connectors in the order the pointer crosses them."""
    return [conn for conn, _x, _y, _w, _h in
            sorted(layout, key=lambda r: r[1])]
