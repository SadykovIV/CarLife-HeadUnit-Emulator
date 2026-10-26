"""Small native-binding probes; never bypass normal process finalization."""
import gc
import json
import sys


def refs():
    return [sys.getrefcount(True), sys.getrefcount(False)]


def run(label, callback):
    # Warm up imports and lazy converters before measuring a repeated operation.
    callback()
    gc.collect()
    before = refs()
    for _ in range(200):
        callback()
    gc.collect()
    after = refs()
    print("NATIVE_PROBE", json.dumps({"label": label, "before": before, "after": after,
                                     "delta": [end - start for start, end in zip(before, after)]}), flush=True)


def qt():
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QInputMethodQueryEvent
    from PySide6.QtWidgets import QApplication, QCheckBox, QWidget
    import shiboken6

    class Surface(QWidget):
        def event(self, event):
            if event.type() == QEvent.User:
                return True
            return super().event(event)

        def inputMethodQuery(self, query):
            if query == Qt.ImEnabled:
                return True
            return super().inputMethodQuery(query)

    app = QApplication([])
    surface = Surface()
    surface.show()
    app.processEvents()
    run("qt_virtual_event_true", lambda: QApplication.sendEvent(surface, QEvent(QEvent.User)))
    run("qt_virtual_event_super", lambda: QApplication.sendEvent(surface, QEvent(QEvent.None_)))
    run("qt_virtual_query_variant_bool", lambda: QApplication.sendEvent(surface, QInputMethodQueryEvent(Qt.ImEnabled)))
    run("qt_widget_bool_property", lambda: surface.isEnabled())
    checkbox = QCheckBox()
    checkbox.toggled.connect(lambda checked: None)
    run("qt_signal_bool", lambda: (checkbox.setChecked(True), checkbox.setChecked(False)))
    surface.close()
    shiboken6.delete(surface)
    shiboken6.delete(checkbox)
    return 0


def video():
    from fractions import Fraction
    import av

    frame = av.VideoFrame(32, 32, "yuv420p")
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = encoder.height = 32
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
    encoded = bytes(encoder.encode(frame)[0])
    decoder = av.CodecContext.create("h264", "r")
    run("av_frame_bool_properties", lambda: (frame.key_frame, frame.is_corrupt))
    run("av_codec_bool_property", lambda: encoder.is_open)
    run("av_decode", lambda: decoder.decode(av.Packet(encoded)))
    run("av_to_image", lambda: frame.to_image())
    return 0


if __name__ == "__main__":
    raise SystemExit(qt() if sys.argv[1] == "qt" else video())
