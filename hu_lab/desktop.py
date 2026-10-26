"""Cross-platform HeadUnit desktop application. UI and audio stay on the Qt main thread."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sys
import threading
import time

from PySide6.QtCore import Qt, QEvent, QPointF, QRectF, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QImage, QEventPoint, QKeySequence, QPainter, QPalette, QPixmap, QTransform
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSlider,
    QSpinBox, QSplitter, QTabBar, QStackedWidget, QVBoxLayout, QWidget)
from .desktop_audio import DesktopAudioPlayer
from .desktop_session import DesktopConfig, create_client, list_usb_devices
from .desktop_demo import DemoPeer
from .desktop_input import DesktopTouchInput
from .keyboard_input import HIDKeyboard, qt_keyboard_command
from .geometry import Rect, ViewportTransform
from .touch import two_pointer_gesture
from .frame_wake import FrameWake
from .macos_activity import UserInitiatedActivity

VERSION = "0.3.1"

from .desktop_theme import stylesheet



def label(text, name=None, wrap=False):
    result = QLabel(text)
    if name:
        result.setObjectName(name)
    result.setWordWrap(wrap)
    return result


def button(text, callback, primary=False):
    result = QPushButton(text)
    result.clicked.connect(callback)
    if primary:
        result.setObjectName('primary')
    return result


class AudioIngress:
    """Fence reader callbacks before disconnect/reset on the UI thread."""
    def __init__(self, audio):
        self.audio = audio
        self.lock = threading.Lock()
        self.enabled = True

    def submit(self, packet):
        with self.lock:
            if self.enabled:
                self.audio.submit(packet)

    def close(self):
        with self.lock:
            self.enabled = False


class VideoSurface(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.image = None
        self.transform = None
        self.index = 0
        self.generation = -1
        self.pressed = False
        self.touch_input = DesktopTouchInput()
        self.input_source = None
        self.wheel_finish = QTimer(self)
        self.wheel_finish.setSingleShot(True)
        self.wheel_finish.timeout.connect(self.finish_contacts)
        self.setAttribute(Qt.WA_AcceptTouchEvents)
        self.setAttribute(Qt.WA_InputMethodEnabled)
        self.setMinimumSize(480, 270)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def clear(self):
        self.cancel('video_cleared')
        self.image = self.transform = None
        self.index = 0
        self.generation = -1
        self.pressed = False
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        empty_background = '#edf2f8' if self.owner.theme_variant == 'light' else '#080e14'
        painter.fillRect(self.rect(), QColor('#080e14' if self.image is not None else empty_background))
        client = self.owner.client
        if self.image is not None and client and self.generation == client.decoder_generation:
            self.transform = ViewportTransform(self.image.width(), self.image.height(),
                Rect(0, 0, self.width(), self.height()), self.owner.fit.currentData(),
                self.owner.rotation.currentData(), backing_scale=self.devicePixelRatioF())
            painter.setClipRect(self.rect())
            painter.setTransform(QTransform(*self.transform.affine))
            painter.drawImage(QRectF(0, 0, self.image.width(), self.image.height()), self.image)
            points = self.touch_input.pointers
            if points:
                painter.setPen(QColor('#4da4ff'))
                painter.setBrush(QColor(77, 164, 255, 60))
                for point in points:
                    painter.drawEllipse(QPointF(point.x, point.y), 10, 10)
                if len(points) == 2:
                    painter.drawLine(QPointF(points[0].x, points[0].y), QPointF(points[1].x, points[1].y))
            painter.end()
            client.mark_presented(self.index, time.monotonic_ns())
        else:
            self.transform = None
            painter.setPen(QColor('#657c90'))
            painter.drawText(self.rect(), Qt.AlignCenter,
                'CarLife HeadUnit Emulator\n\nВыберите устройство сверху и нажмите «Подключить»\n\nЭкран · Звук · Управление')

    def cancel(self, reason):
        self.wheel_finish.stop()
        self.owner.gesture_frames.clear()
        if self.touch_input.active:
            self.owner.invoke(lambda: self.touch_input.cancel(reason), quiet=True)
        if self.owner.client and self.owner.client.pending_touch:
            self.owner.invoke(lambda: self.owner.client.cancel_touch(reason), quiet=True)
        self.pressed = False
        self.input_source = None
        self.update()

    def mapped_point(self, position):
        client = self.owner.client
        if not client or self.owner.busy or client.session.state != 'STREAMING' or not self.transform or self.generation != client.decoder_generation:
            return None
        result = self.transform.view_to_video(position.x(), position.y())
        return result if result and client.input_content_contains(*result) else None

    def point(self, event):
        return self.mapped_point(event.position())

    def visible_video_bounds(self):
        """Video-pixel bounds visible through fit/crop and all four rotations."""
        if self.transform is None:
            return None
        viewport, image = self.transform.viewport, self.transform.rectangle
        left, top = max(viewport.x, image.x), max(viewport.y, image.y)
        right = min(viewport.x + viewport.width, image.x + image.width)
        bottom = min(viewport.y + viewport.height, image.y + image.height)
        if right <= left or bottom <= top:
            return None
        # Right/bottom edges are exclusive in ViewportTransform. Use points
        # just inside them, then the same pixel quantization as pointer input.
        epsilon = min(1e-5, (right - left) / 4, (bottom - top) / 4)
        points = [self.transform.view_to_video(x, y)
                  for x in (left + epsilon, right - epsilon)
                  for y in (top + epsilon, bottom - epsilon)]
        if any(point is None for point in points):
            return None
        xs, ys = zip(*points)
        return Rect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)

    def finish_contacts(self):
        self.wheel_finish.stop()
        self.owner.invoke(self.touch_input.finish, quiet=True)
        self.input_source = None
        self.pressed = False
        self.update()

    def begin_pinch(self, point, source):
        self.owner.cancel_typing()
        self.cancel('new_' + source)
        success = self.owner.invoke(lambda: self.touch_input.begin_pinch(
            self.owner.client, *point, self.generation, source=source,
            bounds=self.visible_video_bounds()), quiet=True)
        if success: self.input_source = source
        self.update()
        return success

    def event(self, event):
        kind = event.type()
        if (kind == QEvent.ShortcutOverride and self.owner.keyboard_ready()
                and not event.matches(QKeySequence.Quit)):
            # Let the subsequent key press reach Android while the projection
            # owns focus, instead of triggering e.g. local disconnect/save.
            event.accept()
            return True
        if (kind == QEvent.KeyPress and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)
                and self.owner.keyboard_ready()):
            # QWidget normally consumes Tab for local focus traversal. When
            # typing into the projected device, it belongs to Android instead.
            if self.owner.keyboard_event(event):
                event.accept()
                return True
        if kind in (QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd, QEvent.TouchCancel):
            if kind == QEvent.TouchCancel:
                self.cancel('qt_touch_cancel')
            else:
                if kind == QEvent.TouchBegin:
                    self.setFocus()
                    self.owner.cancel_typing()
                    self.cancel('qt_touch_begin')
                    self.input_source = 'qt_touch'
                elif self.input_source != 'qt_touch':
                    # A canceled drag must not restart when the same fingers
                    # move back from a letterbox or an inactive tab.
                    event.accept()
                    return True
                values, released = [], []
                for point in event.points():
                    mapped = self.mapped_point(point.position())
                    if mapped is None:
                        self.cancel('touch_outside_video')
                        event.accept()
                        return True
                    target = released if kind == QEvent.TouchEnd or point.state() == QEventPoint.State.Released else values
                    target.append((point.id(), *mapped))
                self.owner.invoke(lambda: self.touch_input.contacts(
                    self.owner.client, values, self.generation, released=released), quiet=True)
                if not values: self.input_source = None
                self.update()
            event.accept()
            return True
        if kind == QEvent.NativeGesture:
            gesture = event.gestureType()
            if gesture == Qt.NativeGestureType.EndNativeGesture:
                if self.input_source == 'native_zoom': self.finish_contacts()
            elif gesture == Qt.NativeGestureType.ZoomNativeGesture:
                point = self.point(event)
                if point is not None:
                    self.setFocus()
                    if self.input_source != 'native_zoom' or not self.touch_input.pinching:
                        if not self.begin_pinch(point, 'native_zoom'):
                            event.accept()
                            return True
                    self.owner.invoke(lambda: self.touch_input.scale_pinch(max(.05, 1 + event.value())), quiet=True)
                    self.update()
                elif self.input_source == 'native_zoom':
                    self.cancel('native_zoom_outside_video')
            event.accept()
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.setFocus()
        self.owner.cancel_typing()
        self.cancel('new_mouse_press')
        point = self.point(event)
        if point:
            if event.modifiers() & Qt.AltModifier:
                self.pressed = self.begin_pinch(point, 'alt_mouse')
            else:
                self.pressed = self.owner.invoke(lambda: self.owner.client.touch(0, *point), quiet=True)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.pressed:
            point = self.point(event)
            if point:
                if self.input_source == 'alt_mouse':
                    self.owner.invoke(lambda: self.touch_input.drag_pinch(*point), quiet=True)
                    self.update()
                else:
                    self.owner.invoke(lambda: self.owner.client.touch(2, *point), quiet=True)
            else:
                self.cancel('pointer_outside_video')

    def mouseReleaseEvent(self, event):
        if self.pressed and event.button() == Qt.LeftButton:
            if self.input_source == 'alt_mouse':
                point = self.point(event)
                if point is None:
                    self.cancel('release_outside_video')
                else:
                    self.owner.invoke(lambda: self.touch_input.drag_pinch(*point), quiet=True)
                    self.finish_contacts()
                return
            point = self.point(event)
            if point:
                self.owner.invoke(lambda: self.owner.client.touch(1, *point), quiet=True)
                self.pressed = False
            else:
                self.cancel('release_outside_video')

    def wheelEvent(self, event):
        if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            point = self.point(event)
            if point:
                self.setFocus()
                if self.input_source != 'wheel_zoom' or not self.touch_input.pinching:
                    if not self.begin_pinch(point, 'wheel_zoom'):
                        event.accept()
                        return
                delta = event.angleDelta().y() / 120 if event.angleDelta().y() else event.pixelDelta().y() / 80
                factor = math.exp(max(-1, min(1, delta * .16)))
                self.owner.invoke(lambda: self.touch_input.scale_pinch(factor), quiet=True)
                self.wheel_finish.start(180)
                self.update()
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        if self.owner.keyboard_event(event):
            event.accept()
        else:
            super().keyPressEvent(event)

    def inputMethodEvent(self, event):
        if event.replacementStart() or event.replacementLength():
            self.owner.show_error('Замена ранее введённого текста через IME недоступна. Используйте выделение и клавишу удаления на устройстве.')
            event.accept()
            return
        if event.commitString():
            self.owner.send_text(event.commitString())
        event.accept()

    def inputMethodQuery(self, query):
        if query == Qt.ImEnabled:
            return self.owner.keyboard_ready()
        if query == Qt.ImSurroundingText:
            return ''  # The desktop cannot read the focused Android editor.
        return super().inputMethodQuery(query)

    def focusOutEvent(self, event):
        self.owner.cancel_typing()
        self.cancel('focus_lost')
        super().focusOutEvent(event)

    def resizeEvent(self, event):
        self.cancel('viewport_resize')
        super().resizeEvent(event)


class HeadUnitWindow(QMainWindow):
    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.client = self.demo = None
        self.keyboard = None
        self.keyboard_start_future = None
        self.keyboard_start_requested = False
        self.last_keyboard_status = None
        self.ingress = None
        self.last_run = None
        self.last_report = None
        self.history = []
        self.busy = False
        self.closing = False
        self.last_state = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='headunit-control')
        self.pending = []
        self.gesture_frames = []
        self.gesture_due = 0
        self.audio = DesktopAudioPlayer()
        self.activity = UserInitiatedActivity('CarLife video and input session')
        self.setWindowTitle(f'CarLife HeadUnit Emulator {VERSION}')
        self.resize(1360, 900)
        self.setMinimumSize(1060, 680)
        self.theme_variant = 'dark'
        self.setStyleSheet(stylesheet('dark'))
        icon_path = Path(__file__).parent / 'assets/carlife-icon.png'
        if icon_path.is_file(): self.setWindowIcon(QIcon(str(icon_path)))
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QWidget()
        self.header.setObjectName('header')
        bar = QHBoxLayout(self.header)
        bar.setContentsMargins(18, 14, 18, 14)
        bar.setSpacing(10)
        badge = label('CL', 'badge')
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(38, 38)
        if icon_path.is_file():
            badge.setObjectName('productIcon')
            badge.setPixmap(QPixmap(str(icon_path)))
            badge.setScaledContents(True)
        bar.addWidget(badge)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addWidget(label('CarLife HeadUnit Emulator', 'brand'))
        system = 'macOS arm64' if sys.platform == 'darwin' else 'Windows x64' if sys.platform == 'win32' else platform.system()
        titles.addWidget(label(f'{VERSION} · {system}', 'muted'))
        bar.addLayout(titles)
        self.transport = QComboBox()
        for text, value in [('USB', 'usb'), ('TCP', 'tcp'), ('Демо', 'demo')]:
            self.transport.addItem(text, value)
        self.transport.setFixedWidth(90)
        self.transport.setToolTip('Способ подключения')
        bar.addWidget(self.transport)
        self.usb_group = QWidget()
        usb_bar = QHBoxLayout(self.usb_group)
        usb_bar.setContentsMargins(0, 0, 0, 0)
        self.usb_devices = QComboBox()
        self.usb_devices.setMinimumWidth(240)
        self.usb_devices.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.usb_devices.setToolTip('Устройство CarLife. При подключении режим AOA включается автоматически.')
        usb_bar.addWidget(self.usb_devices, 1)
        self.scan_button = button('↻', self.refresh_usb)
        self.scan_button.setFixedWidth(38)
        self.scan_button.setToolTip('Обновить список USB-устройств')
        usb_bar.addWidget(self.scan_button)
        bar.addWidget(self.usb_group, 1)
        self.tcp_group = QWidget()
        tcp_bar = QHBoxLayout(self.tcp_group)
        tcp_bar.setContentsMargins(0, 0, 0, 0)
        self.host = QLineEdit('127.0.0.1')
        self.host.setPlaceholderText('IP или имя хоста')
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(7240)
        self.port.setFixedWidth(90)
        tcp_bar.addWidget(self.host, 1); tcp_bar.addWidget(self.port)
        bar.addWidget(self.tcp_group, 1)
        self.demo_header = label('Встроенное устройство · без USB', 'muted')
        bar.addWidget(self.demo_header, 1)
        self.connect_button = button('Подключить', self.connect_session, True)
        self.disconnect_button = button('Отключить', self.disconnect_session)
        bar.addWidget(self.connect_button); bar.addWidget(self.disconnect_button)
        layout.addWidget(self.header)

        navigation = QHBoxLayout()
        navigation.setContentsMargins(18, 10, 18, 10)
        navigation.setSpacing(12)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.addTab('Экран')
        navigation.addWidget(self.tabs)
        navigation.addStretch()
        self.sound_enabled = QCheckBox('Звук')
        self.sound_enabled.setChecked(True)
        self.sound_enabled.setFixedWidth(80)
        navigation.addWidget(self.sound_enabled)
        self.header_volume = QSlider(Qt.Horizontal)
        self.header_volume.setRange(0, 100); self.header_volume.setValue(70)
        self.header_volume.setFixedWidth(90)
        navigation.addWidget(self.header_volume)
        self.header_volume_label = label('70%', 'muted')
        self.header_volume_label.setFixedWidth(32)
        navigation.addWidget(self.header_volume_label)
        layout.addLayout(navigation)
        self.error_label = label('', 'error', True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.body = QStackedWidget()
        self.splitter = QSplitter()
        stage = QWidget()
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(24, 8, 24, 16)
        stage_layout.setSpacing(12)
        self.video = VideoSurface(self)
        self.video.setObjectName('video')
        stage_layout.addWidget(self.video, 1)
        controls = QHBoxLayout()
        self.stream_buttons = []
        for text, action in [('⌂  Домой','home'), ('←  Назад','back'), ('◀◀','previous'), ('▶','play'), ('■','stop'), ('▶▶','next')]:
            b = button(text, lambda checked=False, a=action: self.control(a))
            b.setToolTip({'play':'Воспроизведение','stop':'Остановка','previous':'Предыдущий трек','next':'Следующий трек'}.get(action, text))
            controls.addWidget(b)
            self.stream_buttons.append(b)
        controls.addStretch()
        self.pause_button = button('Пауза видео', self.toggle_pause)
        controls.addWidget(self.pause_button)
        controls.addWidget(button('Снимок', self.screenshot))
        controls.addWidget(button('⛶', self.fullscreen))
        stage_layout.addLayout(controls)
        self.stats = label('Подключите устройство в верхней панели', 'muted')
        self.stats.setAlignment(Qt.AlignCenter)
        stage_layout.addWidget(self.stats)
        self.splitter.addWidget(stage)
        self.inspector = QStackedWidget()
        self.inspector.setObjectName('inspector')
        self.inspector.setMinimumWidth(300)
        self.inspector.setMaximumWidth(420)
        self.splitter.addWidget(self.inspector)
        self.splitter.setSizes([1000, 340])
        self.connection_tab()
        self.display_tab()
        self.input_tab()
        self.audio_tab()
        self.body.addWidget(self.splitter)

        diagnostics = QWidget()
        details = QVBoxLayout(diagnostics)
        details.setContentsMargins(24, 12, 24, 20)
        details.addWidget(label('Диагностика сессии', 'brand'))
        self.notice = label('Выберите устройство и нажмите «Подключить».', 'notice', True)
        details.addWidget(self.notice)
        actions = QHBoxLayout()
        actions.addWidget(button('Экспорт отчёта', self.export_report))
        actions.addWidget(button('Папка сессии', self.open_run))
        actions.addStretch()
        details.addLayout(actions)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        details.addWidget(self.log, 1)
        details.addWidget(label('USB AOA · H.264 · MEDIA / TTS / VR · журнал и снимки сохраняются в папке сессии', 'muted', True))
        self.body.addWidget(diagnostics)
        self.tabs.addTab('Диагностика')
        layout.addWidget(self.body, 1)
        self.state = label('●  Отключено', 'footer')
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 18, 0)
        footer.addWidget(self.state, 1)
        footer.addWidget(label('Тема', 'muted'))
        self.theme = QComboBox()
        for title, value in [('Тёмная', 'dark'), ('Светлая', 'light'), ('Системная', 'system')]:
            self.theme.addItem(title, value)
        self.theme.setFixedWidth(132)
        self.theme.setAccessibleName('Тема приложения')
        footer.addWidget(self.theme)
        layout.addLayout(footer)
        self.setCentralWidget(root)
        self.frame_wake = FrameWake(self.refresh_frame, self)
        self.tabs.currentChanged.connect(self.tab_changed)
        self.transport.currentIndexChanged.connect(self.transport_changed)
        self.header_volume.valueChanged.connect(self.volume.setValue)
        self.volume.valueChanged.connect(self.header_volume.setValue)
        self.sound_enabled.toggled.connect(lambda enabled: self.muted.setChecked(not enabled))
        self.muted.toggled.connect(lambda muted: self.sound_enabled.setChecked(not muted))
        self.theme.currentIndexChanged.connect(self.apply_theme)
        QApplication.instance().styleHints().colorSchemeChanged.connect(self.apply_theme)
        self.setup_menu()
        self.load_settings()
        self.apply_theme()
        self.refresh_audio()
        self.transport_changed()
        self.tab_changed(0)
        self.refresh_buttons()
        self.metrics_time = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)
        self.log_event('Готово. Подключение USB автоматически включает AOA для выбранного устройства.')
        QTimer.singleShot(0, self.refresh_usb)

    def apply_theme(self, *_):
        choice = self.theme.currentData()
        if choice == 'system':
            choice = 'dark' if QApplication.instance().styleHints().colorScheme() == Qt.ColorScheme.Dark else 'light'
        self.theme_variant = choice
        palette = QPalette()
        colors = ({'Window':'#f4f6f9', 'WindowText':'#202b3b', 'Base':'#ffffff', 'AlternateBase':'#edf2f8',
                   'Text':'#202b3b', 'Button':'#ffffff', 'ButtonText':'#202b3b', 'Highlight':'#167d95',
                   'HighlightedText':'#ffffff', 'ToolTipBase':'#ffffff', 'ToolTipText':'#202b3b'} if choice == 'light' else
                  {'Window':'#10151e', 'WindowText':'#e8edf5', 'Base':'#101823', 'AlternateBase':'#192231',
                   'Text':'#e8edf5', 'Button':'#253145', 'ButtonText':'#e8edf5', 'Highlight':'#367d80',
                   'HighlightedText':'#ffffff', 'ToolTipBase':'#151d29', 'ToolTipText':'#e8edf5'})
        for role, color in colors.items():
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(stylesheet(choice))
        self.video.update()

    def tab_changed(self, index):
        self.cancel_typing()
        self.video.cancel('tab_changed')
        diagnostics = index == self.tabs.count() - 1
        self.body.setCurrentIndex(1 if diagnostics else 0)
        self.inspector.setVisible(index not in (0, self.tabs.count() - 1))
        if 1 <= index <= self.inspector.count():
            self.inspector.setCurrentIndex(index - 1)

    def tab(self, title):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        self.inspector.addWidget(area)
        self.tabs.addTab(title)
        return layout

    def connection_tab(self):
        layout = self.tab('Подключение')
        layout.addWidget(label('Параметры сессии', 'brand'))
        self.profile = QComboBox()
        self.profile.addItem('CarLife v1', 'v1')
        self.profile.addItem('CarLife v2 · Honda', 'honda-v2')
        self.profile.setCurrentIndex(1)
        layout.addWidget(label('Профиль протокола', 'muted'))
        layout.addWidget(self.profile)
        layout.addWidget(label('Устройство выбирается сверху. Режим USB AOA включается при подключении автоматически.', 'muted', True))
        self.demo_group = QGroupBox('Встроенный тест')
        box = QVBoxLayout(self.demo_group)
        box.addWidget(label('Локальный видеопоток и звук для проверки приложения без устройства.', 'muted', True))
        self.demo_tone = QCheckBox('Тестовый тон 440 Гц')
        self.demo_tone.toggled.connect(self.set_demo_tone)
        box.addWidget(self.demo_tone)
        layout.addWidget(self.demo_group)
        self.reconnect_button = button('Переподключить', self.reconnect_session)
        layout.addWidget(self.reconnect_button)
        layout.addStretch()
        layout.addWidget(label('Профиль применяется при следующем подключении. OEM-аутентификация и Bluetooth HFP не реализованы.', 'muted', True))

    def display_tab(self):
        layout = self.tab('Видео')
        layout.addWidget(label('Видеопоток', 'brand'))
        presets = QComboBox()
        for text, dimensions in [('1280 × 720 • 16:9',(1280,720)), ('1024 × 768 • 4:3',(1024,768)), ('800 × 480 • wide',(800,480)), ('1920 × 1080 • Full HD',(1920,1080)), ('Свой размер',None)]:
            presets.addItem(text, dimensions)
        self.presets = presets
        layout.addWidget(presets)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.width = QSpinBox(); self.width.setRange(160,4096); self.width.setSingleStep(2); self.width.setValue(1280)
        self.height = QSpinBox(); self.height.setRange(120,4096); self.height.setSingleStep(2); self.height.setValue(720)
        self.fps = QSpinBox(); self.fps.setRange(1,60); self.fps.setValue(30)
        form.addRow('Ширина, px', self.width); form.addRow('Высота, px', self.height); form.addRow('Частота, fps', self.fps)
        layout.addLayout(form)
        presets.currentIndexChanged.connect(self.preset_changed)
        self.fallback = QCheckBox('Принять режим от устройства')
        self.fallback.setToolTip('Разрешить размер и частоту кадров, предложенные MD при согласовании потока.')
        self.fallback.setChecked(True)
        layout.addWidget(self.fallback)
        self.apply_button = button('Применить и переподключить', self.reconnect_session, True)
        layout.addWidget(self.apply_button)
        layout.addWidget(label('Это разрешение CarLife-потока. Изменение требует нового согласования с устройством.', 'muted', True))
        box = QGroupBox('Отображение')
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.fit = QComboBox(); self.fit.addItem('Вписать целиком', 'fit'); self.fit.addItem('Заполнить с обрезкой', 'crop')
        self.rotation = QComboBox()
        for n in (0,90,180,270): self.rotation.addItem(f'{n}°',n)
        form.addRow('Масштаб', self.fit); form.addRow('Поворот', self.rotation)
        self.fit.currentIndexChanged.connect(self.viewport_changed)
        self.rotation.currentIndexChanged.connect(self.viewport_changed)
        layout.addWidget(box)
        self.touch_profile = QComboBox()
        self.touch_profile.addItem('Один указатель', 'legacy-single')
        self.touch_profile.addItem('Мультитач · 2 касания', 'legacy-dual')
        self.touch_profile.addItem('Мультитач · Action3', 'action3-multi')
        self.touch_profile.setCurrentIndex(1)
        layout.addWidget(label('Профиль касаний', 'muted')); layout.addWidget(self.touch_profile)
        gestures = QHBoxLayout()
        self.pinch_in = button('Масштаб −', lambda: self.gesture('pinch-in'))
        self.pinch_out = button('Масштаб +', lambda: self.gesture('pinch-out'))
        gestures.addWidget(self.pinch_in); gestures.addWidget(self.pinch_out)
        layout.addLayout(gestures)
        layout.addWidget(label('Мультитач: жест сведения на трекпаде, Alt + перетаскивание мышью или Ctrl + колесо. На сенсорном экране — два пальца.', 'muted', True))
        self.reset_button = button('Запросить перезапуск видео', lambda: self.invoke(lambda: self.client.reset()))
        layout.addWidget(self.reset_button)
        layout.addStretch()

    def input_tab(self):
        layout = self.tab('Ввод')
        layout.addWidget(label('Клавиатура и текст', 'brand'))
        self.hid_enabled = QCheckBox('Клавиатура по USB')
        self.hid_enabled.setChecked(True)
        self.hid_enabled.setToolTip('Применяется при подключении. Текст передаётся как нажатия физической USB-клавиатуры.')
        layout.addWidget(self.hid_enabled)
        layout.addWidget(label('Раскладка на Android', 'muted'))
        self.keyboard_layout = QComboBox()
        self.keyboard_layout.addItem('English (US)', 'us')
        self.keyboard_layout.addItem('Русская (ЙЦУКЕН)', 'ru')
        layout.addWidget(self.keyboard_layout)
        layout.addWidget(label('Выберите такую же раскладку физической клавиатуры на Android. Caps Lock должен быть выключен.', 'muted', True))
        self.keyboard_status = label('Клавиатура доступна при подключении по USB AOA 2.0.', 'notice', True)
        layout.addWidget(self.keyboard_status)
        self.text_input = QPlainTextEdit()
        self.text_input.setObjectName('inputText')
        self.text_input.setPlaceholderText('Текст для активного поля на Android')
        self.text_input.setFixedHeight(110)
        self.text_input.setTabChangesFocus(True)
        layout.addWidget(self.text_input)
        self.send_text_button = button('Отправить текст', self.send_editor_text, True)
        self.paste_button = button('Вставить текст из буфера', self.paste_clipboard)
        self.cancel_typing_button = button('Остановить ввод', self.cancel_typing)
        layout.addWidget(self.send_text_button)
        layout.addWidget(self.paste_button)
        layout.addWidget(self.cancel_typing_button)
        layout.addWidget(label('Щёлкните по полю на экране устройства и печатайте. Ctrl/Cmd + V вставляет текст из буфера компьютера. Текст и буфер Android обратно не считываются.', 'muted', True))
        layout.addWidget(label('Для TCP доступны стрелки, Enter, Esc, Home и мультимедийные клавиши. Текст требует USB AOA 2.0.', 'muted', True))
        layout.addStretch()

    def keyboard_ready(self):
        return bool(self.keyboard and self.keyboard.snapshot()['available'] and self.client and
                    self.client.session.state == 'STREAMING' and not self.busy and
                    not self.client.stopped and not self.client.halt.is_set())

    def send_text(self, text):
        if not self.keyboard_ready():
            self.show_error('Ввод текста доступен при активном подключении USB и готовой клавиатуре AOA 2.0.')
            return False
        if not text:
            return False
        self.video.cancel('keyboard_text')
        return self.invoke(lambda: self.keyboard.submit_text(text, self.keyboard_layout.currentData()), quiet=True)

    def send_editor_text(self):
        if self.send_text(self.text_input.toPlainText()):
            self.log_event('Текст поставлен в очередь ввода; содержимое не записывается в журнал.')

    def paste_clipboard(self):
        # This is the only clipboard read: an explicit user click or paste key.
        # Even unsupported sessions leave the host clipboard untouched/unread.
        if not self.keyboard_ready():
            self.show_error('Вставка текста требует активной USB-клавиатуры AOA 2.0.')
            return
        text = QApplication.clipboard().text()
        if text:
            self.send_text(text)
        else:
            self.show_error('В буфере обмена нет текста.')

    def cancel_typing(self):
        if self.keyboard: self.keyboard.cancel_pending()

    def keyboard_event(self, event):
        if not self.client or self.busy or self.client.session.state != 'STREAMING':
            return False
        try:
            command = qt_keyboard_command(event, self.keyboard_layout.currentData())
            if command.kind == 'paste':
                self.paste_clipboard()
            elif command.kind == 'ignore':
                return False
            elif self.keyboard_ready() and command.kind == 'key':
                self.video.cancel('keyboard_key')
                self.keyboard.submit_key(command.usage, command.modifiers)
            elif self.keyboard_ready() and command.kind == 'text':
                self.send_text(command.text)
            elif command.hard_key:
                self.control(command.hard_key)
            elif command.kind in ('key', 'text'):
                self.show_error('Для этой клавиши требуется готовая USB-клавиатура AOA 2.0. Откройте вкладку «Ввод».')
            else:
                return False
        except Exception as error:
            self.show_error(error)
        return True

    def update_keyboard_status(self):
        if not self.keyboard:
            self.keyboard_status.setText('Клавиатура доступна при подключении по USB AOA 2.0.')
        else:
            status = self.keyboard.snapshot()
            if status['available']:
                text = f'USB-клавиатура готова · отправлено {status["sent_keys"]} · в очереди {status["pending_keys"]}'
            elif status['status'] in ('idle', 'starting') and self.client and self.client.backend == 'DIRECT_USB_AOA':
                text = ('USB-клавиатура подключается…' if self.keyboard_start_requested else
                        'USB-клавиатура ожидает запуска CarLife.')
            elif status['status'] == 'failed':
                text = 'USB-клавиатура недоступна: ' + str(status['error'] or status['reason'])
            elif self.client and self.client.backend != 'DIRECT_USB_AOA':
                text = 'TCP / демо: клавиши навигации CarLife доступны. Для текста подключите устройство по USB AOA 2.0.'
            else:
                text = 'USB-клавиатура недоступна: ' + str(status['reason'] or status['status'])
            self.keyboard_status.setText(text)
            if status['status'] != self.last_keyboard_status:
                self.last_keyboard_status = status['status']
                self.log_event('Клавиатура: ' + text)
        ready = self.keyboard_ready()
        self.send_text_button.setEnabled(ready)
        self.paste_button.setEnabled(ready)
        self.cancel_typing_button.setEnabled(bool(self.keyboard and self.keyboard.snapshot()['pending_keys']))

    def audio_tab(self):
        layout = self.tab('Звук')
        layout.addWidget(label('Звук на компьютере', 'brand'))
        layout.addWidget(label('Устройство вывода', 'muted'))
        self.output_device = QComboBox()
        self.output_device.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.output_device.setMinimumContentsLength(12)
        self.output_device.currentIndexChanged.connect(self.output_changed)
        layout.addWidget(self.output_device)
        layout.addWidget(button('Обновить аудиоустройства', self.refresh_audio))
        tone_button = button('Проверить динамики', lambda: self.invoke(self.audio.test_tone))
        tone_button.setToolTip('Воспроизвести короткий тестовый тон на компьютере.')
        layout.addWidget(tone_button)
        self.volume_label = label('Громкость HU: 70%')
        layout.addWidget(self.volume_label)
        self.volume = QSlider(Qt.Horizontal); self.volume.setRange(0,100); self.volume.setValue(70)
        self.volume.valueChanged.connect(self.volume_changed)
        layout.addWidget(self.volume)
        self.muted = QCheckBox('Выключить звук HU')
        self.muted.toggled.connect(lambda value: self.audio.set_muted(value))
        layout.addWidget(self.muted)
        self.capture = QCheckBox('Сохранять PCM в WAV • до 8 MiB')
        layout.addWidget(self.capture)
        layout.addWidget(label('WAV включается со следующей сессии. MEDIA, TTS и входящий VR воспроизводятся раздельно. Микрофон не захватывается.', 'muted', True))
        self.audio_status = label('Ожидание audio INIT', 'notice', True)
        layout.addWidget(self.audio_status)
        layout.addWidget(label('Громкость меняет только звук этого приложения. Кнопки плеера отправляют команды CarLife устройству.', 'muted', True))
        layout.addStretch()

    def setup_menu(self):
        menu = self.menuBar().addMenu('Сессия')
        for text, shortcut, fn in [('Подключить','Ctrl+Return',self.connect_session),('Отключить','Ctrl+D',self.disconnect_session),('Сохранить снимок','Ctrl+S',self.screenshot),('Полный экран','Ctrl+Meta+F',self.fullscreen)]:
            action = QAction(text, self); action.setShortcut(QKeySequence(shortcut)); action.triggered.connect(fn); menu.addAction(action)
        quit_action = QAction('Завершить',self); quit_action.setShortcut(QKeySequence.Quit); quit_action.triggered.connect(self.close); menu.addAction(quit_action)

    def log_event(self, text):
        self.log.appendPlainText(f'{datetime.now().strftime("%H:%M:%S")}  {text}')

    def show_error(self, error):
        text = str(error)
        self.error_label.setText(text); self.error_label.show()
        self.log_event('Ошибка: ' + text)

    def invoke(self, fn, quiet=False):
        try:
            fn()
            if not quiet: self.log_event('Команда отправлена')
            return True
        except Exception as error:
            self.show_error(error)
            return False

    def submit(self, work, done):
        self.busy = True
        self.refresh_buttons()
        self.pending.append((self.executor.submit(work), done))

    def transport_changed(self):
        if not hasattr(self, 'tcp_group'): return
        kind = self.transport.currentData()
        self.usb_group.setVisible(kind == 'usb')
        self.tcp_group.setVisible(kind == 'tcp')
        self.demo_group.setVisible(kind == 'demo')
        self.demo_header.setVisible(kind == 'demo')

    def preset_changed(self):
        dimensions = self.presets.currentData()
        if dimensions:
            self.width.setValue(dimensions[0]); self.height.setValue(dimensions[1])

    def viewport_changed(self):
        if hasattr(self, 'video'):
            self.video.cancel('viewport_transform_changed')
            self.video.transform = None
            self.video.update()

    def refresh_usb(self):
        if self.busy or self.client: return
        def done(devices):
            previous = self.usb_devices.currentData()
            self.usb_devices.clear()
            for item in devices:
                suffix = ' • AOA' if item.get('desktop_supported') else ' • требуется AOA'
                self.usb_devices.addItem(item['label']+suffix,item['identity'])
            if not self.usb_devices.count(): self.usb_devices.addItem('USB-устройств не найдено',None)
            if previous:
                for n in range(self.usb_devices.count()):
                    if self.usb_devices.itemData(n) == previous: self.usb_devices.setCurrentIndex(n)
            self.log_event(f'USB: устройств {len(devices)}, готовых AOA: {sum(bool(d.get("desktop_supported")) for d in devices)}')
        self.submit(list_usb_devices, done)

    def refresh_audio(self):
        previous = self.output_device.currentData()
        self.output_device.blockSignals(True)
        self.output_device.clear()
        self.output_device.addItem('Системное устройство по умолчанию', None)
        for item in self.audio.available_outputs():
            self.output_device.addItem(item['name'],item['id'])
        if previous:
            for n in range(self.output_device.count()):
                if self.output_device.itemData(n) == previous: self.output_device.setCurrentIndex(n)
        self.output_device.blockSignals(False)
        self.audio.set_output_device(self.output_device.currentData())
        self.audio.set_volume(self.volume.value()/100)

    def set_demo_tone(self, enabled):
        if self.demo:
            self.demo.tone = enabled

    def output_changed(self):
        self.invoke(lambda: self.audio.set_output_device(self.output_device.currentData()))

    def volume_changed(self, value):
        self.volume_label.setText(f'Громкость HU: {value}%')
        self.header_volume_label.setText(f'{value}%')
        self.audio.set_volume(value/100)

    def configuration(self):
        from .usb import USBIdentity
        ident = self.usb_devices.currentData()
        kind = self.transport.currentData()
        if kind == 'usb' and not ident:
            raise ValueError('Подключите CarLife-устройство и обновите список USB.')
        if self.width.value() % 2 or self.height.value() % 2:
            raise ValueError('Для H.264 выберите чётные ширину и высоту.')
        return DesktopConfig(transport='tcp' if kind == 'demo' else kind,
            profile=self.profile.currentData(), host=self.host.text().strip(), port=self.port.value(),
            usb_identity=USBIdentity(**{**ident,'ports':tuple(ident['ports'])}) if kind == 'usb' else None,
            width=self.width.value(),height=self.height.value(),fps=self.fps.value(),
            touch_profile=self.touch_profile.currentData(),audio_capture=self.capture.isChecked(),
            allow_mode_fallback=self.fallback.isChecked(),aoa_start=kind == 'usb')

    def connect_session(self):
        if self.busy or self.client or self.closing: return
        try: config = self.configuration()
        except Exception as error: self.show_error(error); return
        self.error_label.hide()
        try:
            self.save_settings()
        except OSError as error:
            self.show_error(f'Настройки не сохранены: {error}')
        self.audio.reset()
        activity = self.activity.start()
        if activity['status'] == 'failed':
            self.log_event('Защита от App Nap недоступна: ' + str(activity['error']))
        ingress = AudioIngress(self.audio)
        self.ingress = ingress
        demo_mode = self.transport.currentData() == 'demo'
        tone = self.demo_tone.isChecked()
        hid_enabled = self.hid_enabled.isChecked()
        self.last_run = self.data_dir / 'sessions' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        output = self.last_run
        self.log_event('Подключение…')
        self.state.setText('●  Подключение…')
        def work():
            from dataclasses import replace
            peer = None
            client = None
            keyboard = None
            try:
                if demo_mode:
                    peer = DemoPeer(tone=tone, touch_profile=config.touch_profile)
                    local_config = replace(config, host='127.0.0.1',port=peer.port)
                else: local_config = config
                client = create_client(local_config, output, audio_sink=ingress,
                                       on_frame_available=self.frame_wake.notify)
                client.start()
                keyboard = HIDKeyboard.for_client(client) if hid_enabled else HIDKeyboard(unavailable_reason='USB-клавиатура отключена в настройках')
                # Optional HID must not issue control requests while Android
                # is opening the accessory and negotiating the CarLife stream.
                if not hid_enabled or client.backend != 'DIRECT_USB_AOA':
                    keyboard.start()
                return client, peer, keyboard
            except Exception:
                ingress.close()
                if keyboard: keyboard.close()
                self.activity.close()
                try:
                    if peer: peer.begin_close()
                    if client: client.stop()
                finally:
                    if peer: peer.close()
                raise
        def done(result):
            self.client, self.demo, self.keyboard = result
            self.keyboard_start_future = None
            self.keyboard_start_requested = False
            self.last_keyboard_status = None
            self.update_keyboard_status()
            if config.transport == 'usb':
                identity = getattr(self.client.sock, 'evidence', {}).get('identity')
                if identity:
                    self.usb_devices.setItemData(self.usb_devices.currentIndex(), identity)
                    self.usb_devices.setItemText(self.usb_devices.currentIndex(),
                        f'{identity["vid"]:04x}:{identity["pid"]:04x} • AOA')
            self.last_state = None
            self.video.clear()
            self.notice.setText(('ДЕМО • локальный H.264 + PCM, синтетический MD' if demo_mode else
                f'{config.transport.upper()} • {config.profile} • {config.width} × {config.height} / {config.fps} fps') + ' • мышь управляет экраном')
            self.log_event(f'Сессия: {output.name}')
            if self.closing: self.disconnect_session()
        self.submit(work,done)

    def start_keyboard_when_ready(self):
        client, keyboard = self.client, self.keyboard
        if (self.keyboard_start_requested or not client or not keyboard or
                client.backend != 'DIRECT_USB_AOA' or client.session.state != 'STREAMING' or
                client.halt.is_set() or client.stopped or self.closing or
                keyboard.snapshot()['status'] != 'idle'):
            return
        self.keyboard_start_requested = True
        def work():
            if not client.halt.is_set() and not client.stopped:
                return keyboard.start()
        # The same single worker serializes startup with disconnect/close, but
        # HID initialization does not mark the video/UI busy.
        self.keyboard_start_future = self.executor.submit(work)

    def disconnect_session(self, after=None):
        if isinstance(after,bool): after = None
        if self.busy: return
        if not self.client:
            if after: after()
            elif self.closing: self.close()
            return
        client, peer, keyboard = self.client, self.demo, self.keyboard
        self.video.cancel('disconnect')
        self.gesture_frames.clear()
        playback = self.audio.snapshot()
        if self.ingress:
            self.ingress.close()
        self.audio.reset()
        self.state.setText('●  Отключение…')
        def work():
            cleanup_errors = []
            if keyboard:
                try:
                    stopped_keyboard = keyboard.close()
                    if stopped_keyboard.get('worker_alive') or stopped_keyboard.get('cleanup_errors'):
                        cleanup_errors.append('Keyboard: ' + str(stopped_keyboard.get('cleanup_errors') or 'worker did not stop'))
                except Exception as error:
                    cleanup_errors.append(f'Keyboard stop: {error}')
            try:
                if peer: peer.begin_close()
                client.stop()
            except Exception as error:
                cleanup_errors.append(f'Stop: {error}')
            finally:
                try:
                    if peer: peer.close()
                finally:
                    self.activity.close()
            try:
                report = client.summary()
            except Exception as error:
                cleanup_errors.append(f'Summary: {error}')
                report = {'session_id':client.session_id,'error':client.error,
                          'decoded_frames':client.decoded_frames,'state':client.session.state}
            report.update(desktop_audio=playback, demo=peer is not None, demo_error=peer.error if peer else None,
                          frame_wake=self.frame_wake.snapshot(), macos_activity=self.activity.snapshot(),
                          keyboard=keyboard.snapshot() if keyboard else None)
            report['desktop_cleanup_errors'] = cleanup_errors
            try:
                (client.output_dir/'desktop-summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n', encoding='utf-8')
            except Exception as error:
                cleanup_errors.append(f'Report: {error}')
            return report
        def done(report):
            self.last_report = report
            self.history.append({'session_id':report['session_id'],'error':report['error'],'demo':report['demo']})
            self.client = self.demo = None
            self.keyboard = None
            self.keyboard_start_future = None
            self.keyboard_start_requested = False
            self.last_keyboard_status = None
            self.update_keyboard_status()
            self.activity.close()
            self.ingress = None
            self.video.clear()
            self.state.setText('●  Отключено')
            self.stats.setText('Подключите устройство в верхней панели')
            self.log_event(f'Отключено. Кадров: {report["decoded_frames"]}; отчёт сохранён.')
            if report['error']: self.show_error(report['error'])
            if report.get('desktop_cleanup_errors'):
                self.show_error('; '.join(report['desktop_cleanup_errors']))
            if after: after()
            elif self.closing: self.close()
        self.submit(work,done)

    def reconnect_session(self):
        if self.busy: return
        if self.client: self.disconnect_session(after=self.connect_session)
        else: self.connect_session()

    def control(self, name):
        if self.client:
            self.cancel_typing()
            self.video.cancel('hard_key')
            self.gesture_frames.clear()
            self.invoke(lambda: self.client.hard_key(name))

    def toggle_pause(self):
        if not self.client: return
        self.cancel_typing()
        self.video.cancel('pause')
        self.gesture_frames.clear()
        action = self.client.resume if self.client.session.state == 'PAUSED' else self.client.pause
        self.invoke(action)

    def gesture(self, kind):
        if not self.client or self.gesture_frames: return
        self.video.cancel('synthetic_gesture')
        self.gesture_frames = list(two_pointer_gesture(self.client.session.width,self.client.session.height,
            kind,0,12,self.client.decoder_generation))
        self.gesture_due = time.monotonic()

    def fullscreen(self):
        if self.isFullScreen(): self.showNormal()
        else: self.showFullScreen()

    def screenshot(self):
        if self.video.image is None: self.show_error('Нет кадра для сохранения.'); return
        destination = self.last_run / f'screenshot-{datetime.now().strftime("%H%M%S%f")}.png'
        if self.video.image.save(str(destination)):
            self.log_event(f'Снимок: {destination.name}')
        else: self.show_error('Не удалось сохранить снимок')

    def report(self):
        if self.client:
            result = self.client.summary()
            result.update(desktop_audio=self.audio.snapshot(),demo=self.demo is not None,
                          keyboard=self.keyboard.snapshot() if self.keyboard else None)
        else: result = dict(self.last_report or {})
        result.update(application='CarLife HeadUnit Emulator',version=VERSION,architecture=platform.machine(),sessions=self.history,
                      frame_wake=self.frame_wake.snapshot(),macos_activity=self.activity.snapshot())
        return result

    def export_report(self):
        path, _ = QFileDialog.getSaveFileName(self,'Экспорт отчёта',str(self.data_dir/'headunit-report.json'),'JSON (*.json)')
        if path:
            self.invoke(lambda: Path(path).write_text(json.dumps(self.report(),indent=2,ensure_ascii=False)+'\n', encoding='utf-8'))

    def open_run(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_run or self.data_dir)))

    def refresh_buttons(self):
        connected = self.client is not None
        streaming = connected and self.client.session.state == 'STREAMING' and not self.busy
        self.connect_button.setEnabled(not connected and not self.busy)
        self.disconnect_button.setEnabled(connected and not self.busy)
        self.reconnect_button.setEnabled(not self.busy)
        self.apply_button.setEnabled(not self.busy)
        self.scan_button.setEnabled(not connected and not self.busy)
        for widget in (self.transport, self.usb_devices, self.host, self.port, self.profile, self.touch_profile, self.hid_enabled):
            widget.setEnabled(not connected and not self.busy)
        self.reset_button.setEnabled(bool(streaming))
        self.pause_button.setEnabled(bool(connected and not self.busy and self.client.session.state in ('STREAMING','PAUSED')))
        self.pause_button.setText('Продолжить видео' if connected and self.client.session.state == 'PAUSED' else 'Пауза видео')
        for b in self.stream_buttons: b.setEnabled(bool(streaming))
        multi = streaming and self.client.touch_profile != 'LEGACY_SINGLE'
        self.pinch_in.setEnabled(bool(multi)); self.pinch_out.setEnabled(bool(multi))
        self.update_keyboard_status()

    def refresh_frame(self):
        client = self.client
        if client and not self.busy:
            with client.frame_lock: frame = client.latest_frame
            if frame and frame[-1] != self.video.index:
                pixels,w,h,stride,index = frame
                with client.metrics_lock: record = client.frame_records.get(index)
                self.video.index = index
                self.video.generation = record['generation'] if record else -1
                self.video.image = QImage(pixels,w,h,stride,QImage.Format_RGB888).copy()
                self.video.update()
            if self.video.generation != client.decoder_generation and self.video.image is not None:
                self.video.clear()

    def tick(self):
        if self.keyboard_start_future and self.keyboard_start_future.done():
            future, self.keyboard_start_future = self.keyboard_start_future, None
            try:
                future.result()
            except Exception as error:
                self.log_event(f'Клавиатура: {type(error).__name__}: {error}')
            self.update_keyboard_status()
        for future, done in list(self.pending):
            if future.done():
                self.pending.remove((future,done)); self.busy = False
                try: done(future.result())
                except Exception as error:
                    self.show_error(error)
                    if not self.client:
                        if self.ingress: self.ingress.close()
                        self.audio.reset()
                        self.activity.close()
                        self.state.setText('●  Ошибка подключения')
                    if self.closing and not self.client: self.close()
                self.refresh_buttons()
        client = self.client
        if client and not self.busy:
            state = client.session.state
            if state != self.last_state:
                if state != 'STREAMING': self.cancel_typing()
                self.last_state = state
                self.state.setText('●  ' + {'STREAMING': 'Подключено', 'PAUSED': 'Видео приостановлено',
                    'WAIT_VERSION': 'Согласование протокола…', 'WAIT_VIDEO': 'Ожидание видео…',
                    'CLOSED': 'Отключено'}.get(state, 'Подключение…'))
                self.log_event('Протокол: ' + state)
                if state == 'STREAMING':
                    requested = client.session.requested
                    accepted = client.session.accepted or {}
                    self.notice.setText(('ДЕМО • ' if self.demo else 'CARLIFE • ') +
                        f'запрос {requested["width"]} × {requested["height"]} / {requested["fps"]} fps → '
                        f'MD {client.session.width} × {client.session.height} / {client.session.fps} fps • мышь управляет экраном')
                    if (requested['width'],requested['height'],requested['fps']) != (client.session.width,client.session.height,client.session.fps):
                        self.log_event(f'MD предложил {client.session.width}×{client.session.height} / {client.session.fps} fps; принят fallback.')
                self.refresh_buttons()
            if client.halt.is_set() and not client.stopped:
                if client.error: self.show_error(client.error)
                self.disconnect_session()
                return
            self.start_keyboard_when_ready()
            self.refresh_frame()
            if self.gesture_frames and time.monotonic() >= self.gesture_due:
                f = self.gesture_frames.pop(0)
                if not self.invoke(lambda: client.send_touch_frame(f),quiet=True): self.gesture_frames.clear()
                self.gesture_due = time.monotonic()+.04
        if time.monotonic() - self.metrics_time > .5:
            self.metrics_time = time.monotonic()
            self.update_keyboard_status()
            audio = self.audio.snapshot()
            lanes = audio.get('lanes',{})
            descriptions = []
            for name,lane in lanes.items():
                error = lane.get('output_error') or lane.get('last_error')
                fmt = lane.get('format') or {}
                description = f'{name}: {lane.get("state", "—")}'
                if fmt:
                    description += f'\n{fmt.get("sample_rate", "?")} Hz · {fmt.get("channels", "?")} ch · {fmt.get("bits", "?")} bit'
                if error:
                    description += '\nОшибка выхода: ' + str(error)
                descriptions.append(description)
            self.audio_status.setText('\n'.join(descriptions) or 'Ожидание audio INIT')
            if client:
                times = list(client.frame_times)
                recent = [t for t in times if t >= time.monotonic()-2]
                fps = (len(recent)-1)/(recent[-1]-recent[0]) if len(recent)>1 else 0
                self.stats.setText(f'{client.session.width} × {client.session.height}     |     {fps:.1f} fps декодер     |     {client.decoded_frames} кадров     |     ввод: {client.input_packets}')
        if self.closing and not self.busy and not self.client: self.close()

    def load_settings(self):
        try: saved = json.loads((self.data_dir/'settings.json').read_text(encoding='utf-8'))
        except (OSError,ValueError): return
        for name in ('transport','profile','touch_profile','fit','rotation','theme','keyboard_layout'):
            widget = getattr(self,name)
            for n in range(widget.count()):
                if widget.itemData(n) == saved.get(name): widget.setCurrentIndex(n)
        for name in ('width','height','fps','port','volume'):
            if isinstance(saved.get(name),int): getattr(self,name).setValue(saved[name])
        if isinstance(saved.get('host'),str): self.host.setText(saved['host'])
        self.muted.setChecked(saved.get('muted',False))
        self.hid_enabled.setChecked(saved.get('hid_enabled', True))
        self.fallback.setChecked(saved.get('fallback',True))
        self.presets.setCurrentIndex(self.presets.count()-1)
        if saved.get('settings_version', 1) < 2 and saved.get('touch_profile') == 'legacy-single':
            self.touch_profile.setCurrentIndex(self.touch_profile.findData('legacy-dual'))

    def save_settings(self):
        saved = {name:getattr(self,name).currentData() for name in ('transport','profile','touch_profile','fit','rotation','theme','keyboard_layout')}
        saved.update({name:getattr(self,name).value() for name in ('width','height','fps','port','volume')})
        saved.update(host=self.host.text(),muted=self.muted.isChecked(),fallback=self.fallback.isChecked(), settings_version=2, hid_enabled=self.hid_enabled.isChecked())
        temporary = self.data_dir/'settings.json.tmp'
        temporary.write_text(json.dumps(saved,indent=2,ensure_ascii=False)+'\n', encoding='utf-8')
        temporary.replace(self.data_dir/'settings.json')

    def closeEvent(self, event):
        self.closing = True
        if self.busy or self.client:
            event.ignore()
            if not self.busy: self.disconnect_session()
            return
        self.timer.stop()
        self.frame_wake.close()
        self.activity.close()
        self.audio.close()
        try:
            self.save_settings()
        except OSError:
            pass  # A read-only preferences directory must not prevent shutdown.
        self.executor.shutdown(wait=False,cancel_futures=True)
        event.accept()


def default_data_dir():
    # Keep the original storage location so existing 0.2/0.3 settings survive the rename.
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'CarLife HeadUnit'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/CarLife HeadUnit'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'carlife-headunit'


def main(argv=None):
    parser = argparse.ArgumentParser(description='CarLife HeadUnit Emulator for macOS arm64 and Windows x64')
    parser.add_argument('--data-dir',type=Path,default=default_data_dir())
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--usb',metavar='PHYSICAL_KEY',help='connect only this explicitly selected USB port')
    parser.add_argument('--aoa-start',action='store_true',help='compatibility option; AOA is always enabled for the selected --usb device')
    parser.add_argument('--profile',choices=('v1','honda-v2'))
    parser.add_argument('--width',type=int)
    parser.add_argument('--height',type=int)
    parser.add_argument('--fps',type=int)
    parser.add_argument('--smoke-test',type=float,metavar='SECONDS')
    arguments = parser.parse_args(argv)
    if arguments.demo and arguments.usb:
        parser.error('--demo and --usb are mutually exclusive')
    if arguments.aoa_start and not arguments.usb:
        parser.error('--aoa-start requires an explicit --usb port')
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('CarLife HeadUnit Emulator')
    app.setOrganizationName('CarLife Lab')
    window = HeadUnitWindow(arguments.data_dir)
    for name in ('width','height','fps'):
        value = getattr(arguments,name)
        if value is not None:
            widget = getattr(window,name)
            if not widget.minimum() <= value <= widget.maximum():
                parser.error(f'--{name} outside {widget.minimum()}..{widget.maximum()}')
            widget.setValue(value)
    if arguments.profile:
        window.profile.setCurrentIndex(window.profile.findData(arguments.profile))
    window.show()
    if arguments.demo or arguments.usb:
        window.transport.setCurrentIndex(2 if arguments.demo else 0)
        def connect_when_ready():
            if window.busy: QTimer.singleShot(100,connect_when_ready)
            else:
                if arguments.usb:
                    selected = None
                    for n in range(window.usb_devices.count()):
                        ident = window.usb_devices.itemData(n)
                        if ident and f'{ident["bus"]}:'+'.'.join(map(str,ident['ports'])) == arguments.usb:
                            selected = n
                            break
                    if selected is None:
                        window.show_error(f'USB {arguments.usb} не найден. Обновите список.')
                        return
                    window.usb_devices.setCurrentIndex(selected)
                window.connect_session()
        QTimer.singleShot(250,connect_when_ready)
    if arguments.smoke_test is not None:
        def finish():
            window.grab().save(str(arguments.data_dir/'smoke-window.png'))
            window.close()
        QTimer.singleShot(max(1000,int(arguments.smoke_test*1000)),finish)
    result = app.exec()
    if arguments.smoke_test is not None:
        report = window.report()
        (arguments.data_dir/'smoke-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n', encoding='utf-8')
        if (arguments.demo or arguments.usb) and (report.get('decoded_frames',0)==0 or report.get('error') or report.get('demo_error') or report.get('desktop_cleanup_errors')):
            return 1
    return result


if __name__ == '__main__':
    raise SystemExit(main())
