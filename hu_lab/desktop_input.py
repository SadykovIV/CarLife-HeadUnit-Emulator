"""Live desktop contacts in video pixels, independent of Qt event classes.

Qt callers map logical widget positions through ViewportTransform first. A
contact update contains the complete current set (released points omitted).
Native trackpad zoom and Alt+drag synthesize two live contacts; touchscreen
events retain their own independent positions. No USB or protocol negotiation
occurs here, and this helper never changes the selected touch profile.
"""
import math
import time

from .geometry import Rect, SourceDisplayTransform
from .protocol import ProtocolError
from .touch import (DOWN, MOVE, POINTER_DOWN, POINTER_UP, UP,
                    Pointer, TouchFrame, profile_name)


class DesktopTouchInput:
    """Translate live contact sets and continuous pinch input to Android actions.

    ``contacts(client, [(external_id, x, y), ...], generation)`` handles a Qt
    touchscreen. ``begin_pinch`` followed by ``drag_pinch``/``scale_pinch`` and
    ``finish`` handles Alt+mouse, trackpad zoom, or Ctrl+wheel. Coordinates are
    video pixels; generation is the generation of the displayed frame. Call
    ``cancel`` on focus, layout, session and decoder changes.
    """
    def __init__(self):
        self._client = None
        self._generation = None
        self._points = {}  # external ID -> Pointer, insertion order is wire order
        self._pinch = None
        self._source = "desktop_touch"

    @property
    def active(self):
        return bool(self._points)

    @property
    def pinching(self):
        return self._pinch is not None and self.active

    @property
    def pointers(self):
        return tuple(self._points.values())

    def _reset(self):
        self._client = self._generation = self._pinch = None
        self._points.clear()

    def cancel(self, reason="desktop_input_cancel"):
        client = self._client
        try:
            if client is not None and client.pending_touch:
                return client.cancel_touch(reason)
            return False
        finally:
            self._reset()

    def _cancel_after_error(self):
        try:
            self.cancel("desktop_input_error")
        except Exception:
            # Preserve the original failure. The session owner handles transport
            # cleanup; never fabricate successful delivery of a cancel packet.
            pass

    def _send(self, action, pointers, action_index=0):
        self._client.send_touch_frame(TouchFrame(
            action, tuple(pointers), action_index, self._generation,
            event_time_ns=time.monotonic_ns(), source=self._source))

    def _validate(self, client, values, generation):
        if client is None or client.session.state != "STREAMING":
            raise ProtocolError("touch requires a streaming session")
        if generation != client.decoder_generation:
            raise ProtocolError("touch generation differs from displayed video")
        profile = profile_name(client.touch_profile)
        maximum = 1 if profile == "LEGACY_SINGLE" else 2
        if len(values) > maximum:
            raise ProtocolError("two contacts require Legacy dual or Action3 multi")
        ids = [item[0] for item in values]
        if any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
            raise ProtocolError("touch contact IDs must be unique integers")
        for _, x, y in values:
            if not math.isfinite(x) or not math.isfinite(y) or not client.input_content_contains(x, y):
                raise ProtocolError("touch outside active content")

    def contacts(self, client, values, generation, source="qt_touch", released=()):
        """Apply the complete current contact set; release disappeared IDs first.

        Wire IDs 0/1 survive Qt list reordering and first-finger release. This
        also makes the sequence reconstructable by the legacy dual decoder.
        A contact replaced in the same event is released before its ID is reused.
        Optional ``released`` contains final positions for lifted Qt contacts;
        they are never turned into new DOWN events.
        """
        values = tuple(values)
        released = tuple(released)
        try:
            self._validate(client, values, generation)
            self._validate(client, released, generation)
            if {p[0] for p in values} & {p[0] for p in released}:
                raise ProtocolError("contact cannot be active and released together")
            if self._client is not None and (
                    self._client is not client or self._generation != generation):
                self.cancel("desktop_input_session_changed")
            if not self.active and not values:
                return False
            if self._client is None:
                if client.pending_touch:
                    client.cancel_touch("desktop_input_begin")
                self._client, self._generation = client, generation
            self._source = source
            wanted = {external_id: (x, y) for external_id, x, y in values}
            final = {external_id: (x, y) for external_id, x, y in released}
            before = self.pointers
            # POINTER_UP carries all old contacts, including final coordinates
            # of surviving contacts, then the remaining array is compacted.
            for external_id, pointer in tuple(self._points.items()):
                if external_id in wanted:
                    self._points[external_id] = Pointer(pointer.id, *wanted[external_id])
                elif external_id in final:
                    self._points[external_id] = Pointer(pointer.id, *final[external_id])
            changed = False
            for external_id in tuple(self._points):
                if external_id not in wanted:
                    index = list(self._points).index(external_id)
                    self._send(UP if len(self._points) == 1 else POINTER_UP,
                               self.pointers, 0 if len(self._points) == 1 else index)
                    del self._points[external_id]
                    changed = True
            for external_id, x, y in values:
                if external_id not in self._points:
                    used = {p.id for p in self._points.values()}
                    wire_id = next(i for i in (0, 1) if i not in used)
                    pointer = Pointer(wire_id, x, y)
                    points = self.pointers + (pointer,)
                    self._send(DOWN if not self._points else POINTER_DOWN, points,
                               0 if not self._points else len(points) - 1)
                    self._points[external_id] = pointer
                    changed = True
            if self._points and not changed and self.pointers != before:
                self._send(MOVE, self.pointers)
            if not self._points:
                self._reset()
            return True
        except Exception:
            self._cancel_after_error()
            raise

    @staticmethod
    def _content(client):
        source = getattr(client, "source_size", None)
        if source:
            if getattr(client, "source_policy", "fit") != "fit":
                raise ProtocolError("unsupported source content mapping")
            return SourceDisplayTransform(*source, client.session.width,
                                          client.session.height).content
        return Rect(0, 0, client.session.width, client.session.height)

    def begin_pinch(self, client, x, y, generation, source="alt_mouse", bounds=None):
        """Begin a pair around the cursor, moving its centre inward at edges.

        Optional ``bounds`` is a Rect in video pixels for a displayed crop; it
        is intersected with active MD content, never used to extend it.
        """
        try:
            self._validate(client, ((0, x, y), (1, x, y)), generation)
            self.cancel("desktop_pinch_restart")
            content = self._content(client)
            if bounds is not None:
                x1, y1 = max(content.x, bounds.x), max(content.y, bounds.y)
                x2 = min(content.x + content.width, bounds.x + bounds.width)
                y2 = min(content.y + content.height, bounds.y + bounds.height)
                bounds = Rect(x1, y1, x2 - x1, y2 - y1)
                if not bounds.contains(x, y):
                    raise ProtocolError("touch outside displayed crop")
            else:
                bounds = content
            radius = min(bounds.width - 1, bounds.height - 1) * .12
            if radius < 1:
                raise ProtocolError("active content is too small for two contacts")
            cx = min(max(x, bounds.x + radius), bounds.x + bounds.width - 1 - radius)
            cy = min(max(y, bounds.y), bounds.y + bounds.height - 1)
            self.contacts(client, ((0, cx - radius, cy), (1, cx + radius, cy)),
                          generation, source)
            self._pinch = dict(center=(cx, cy), anchor=(x, y), radius=radius,
                               vector=(radius, 0.0), bounds=bounds)
            return True
        except Exception:
            self._cancel_after_error()
            raise

    def _move_pinch(self, dx, dy):
        if not self.pinching:
            return False
        try:
            if not math.isfinite(dx) or not math.isfinite(dy):
                raise ProtocolError("pinch coordinates must be finite")
            state = self._pinch
            cx, cy = state["center"]
            bounds = state["bounds"]
            if math.hypot(dx, dy) < 1:
                # Keep distinct contacts even when the pointer crosses centre.
                dx, dy = state["vector"]
                norm = math.hypot(dx, dy)
                dx, dy = dx / norm, dy / norm
            scale = 1.0
            if dx:
                scale = min(scale, (cx - bounds.x) / abs(dx),
                            (bounds.x + bounds.width - 1 - cx) / abs(dx))
            if dy:
                scale = min(scale, (cy - bounds.y) / abs(dy),
                            (bounds.y + bounds.height - 1 - cy) / abs(dy))
            dx, dy = dx * scale, dy * scale
            if math.hypot(dx, dy) < 1:
                return False  # At the content edge, keep the last valid pair.
            self.contacts(self._client, ((0, cx - dx, cy - dy), (1, cx + dx, cy + dy)),
                          self._generation, self._source)
            state["vector"] = (dx, dy)
            return True
        except Exception:
            self._cancel_after_error()
            raise

    def drag_pinch(self, x, y):
        """Alt+drag expands/contracts horizontally and rotates vertically."""
        if not self.pinching:
            return False
        anchor_x, anchor_y = self._pinch["anchor"]
        return self._move_pinch(self._pinch["radius"] + x - anchor_x, y - anchor_y)

    def scale_pinch(self, factor):
        """Apply an incremental native-zoom or wheel scale (>0)."""
        if not self.pinching:
            return False
        if not math.isfinite(factor) or factor <= 0:
            self._cancel_after_error()
            raise ProtocolError("pinch scale must be positive and finite")
        dx, dy = self._pinch["vector"]
        return self._move_pinch(dx * factor, dy * factor)

    def finish(self):
        """Release active contacts in order, including both contacts of a pinch."""
        if not self.active:
            return False
        return self.contacts(self._client, (), self._generation, self._source)
