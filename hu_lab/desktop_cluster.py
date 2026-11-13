"""CarLife instrument display. Only the active session supplies the values."""
from html import escape
import struct

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from .cluster import MAX_ARTWORK_BYTES


# CarLife nextTurn values used by Baidu and HeadSwap (AaMeta.T_*).
TURNS = {
    1: ('↑', 'Прямо'), 2: ('↗', 'Плавно направо'), 3: ('↱', 'Направо'),
    4: ('↘', 'Резко направо'), 5: ('↶', 'Разворот'), 6: ('↙', 'Резко налево'),
    7: ('↰', 'Налево'), 8: ('↖', 'Плавно налево'), 9: ('⟲', 'Въезд на круг'),
    10: ('↗', 'Выезд с круга'), 11: ('↖', 'Съезд слева'), 12: ('↗', 'Съезд справа'),
    13: ('↖', 'Слияние слева'), 14: ('↖', 'Держитесь левее'),
    15: ('↗', 'Слияние справа'), 16: ('↗', 'Держитесь правее'), 17: ('↑', 'Средняя ветвь'),
    18: ('↖', 'Въезд слева'), 19: ('↗', 'Въезд справа'), 20: ('↖', 'Левая ветвь'),
    21: ('↗', 'Правая ветвь'), 23: ('●', 'Старт'), 24: ('⚑', 'Пункт назначения'),
    29: ('≈', 'Паром'),
}


def distance_text(meters):
    if meters is None:
        return '—'
    if meters < 1000:
        return f'{meters} м'
    return f'{meters / 1000:.1f}'.replace('.', ',') + ' км'


def turn_pixmap(blob):
    # HeadSwap sends PNG. Inspect dimensions before asking Qt to allocate pixels.
    if (not isinstance(blob, bytes) or not 24 <= len(blob) <= 65535 or
            blob[:8] != b'\x89PNG\r\n\x1a\n' or blob[8:16] != b'\0\0\0\rIHDR'):
        return None
    width, height = struct.unpack('>II', blob[16:24])
    if not (1 <= width <= 512 and 1 <= height <= 512):
        return None
    image = QImage.fromData(blob, 'PNG')
    if image.isNull():
        return None
    return QPixmap.fromImage(image).scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def artwork_pixmap(blob):
    """Decode the raw albumArt field, never a path, URL or arbitrary Qt format."""
    if not isinstance(blob, bytes) or not 3 <= len(blob) <= MAX_ARTWORK_BYTES:
        return None
    if blob.startswith(b'\xff\xd8\xff'):
        image_format = b'JPEG'  # HeadSwap MetaImages.albumJpeg, 256/160 px.
    elif blob.startswith(b'\x89PNG\r\n\x1a\n'):
        image_format = b'PNG'
    else:
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(blob))
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer, image_format)
    reader.setAutoDetectImageFormat(False)
    # Check dimensions before decoding, including highly compressed images.
    size = reader.size()
    if not (1 <= size.width() <= 1024 and 1 <= size.height() <= 1024):
        return None
    image = reader.read()
    if image.isNull() or image.size() != size:
        return None
    return QPixmap.fromImage(image).scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class _ElidedLabel(QLabel):
    """Keep the original plain text for accessibility, and fit it to one line."""
    def setText(self, text):
        super().setText(text)
        # Qt tooltips can interpret markup. A road or track name is plain data.
        self.setToolTip(f'<qt>{escape(text)}</qt>' if text else '')

    def paintEvent(self, event):
        rect = self.contentsRect()
        text = self.text()
        short = self.fontMetrics().elidedText(text, Qt.ElideRight, max(0, rect.width()))
        if short == text:
            super().paintEvent(event)
            return
        with QPainter(self) as painter:
            self.style().drawItemText(painter, rect, self.alignment(), self.palette(), self.isEnabled(),
                                      short, self.foregroundRole())


def text_label(text, style, *, wrap=False, elide=False):
    widget = _ElidedLabel() if elide else QLabel()
    widget.setTextFormat(Qt.PlainText)
    widget.setText(text)
    widget.setObjectName(style)
    widget.setWordWrap(wrap)
    if wrap or elide:
        widget.setMinimumWidth(0)
        widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return widget


def card(title):
    frame = QFrame()
    frame.setObjectName('clusterCard')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(6)
    layout.addWidget(text_label(title, 'clusterKicker'))
    return frame, layout


def waiting_text(subscription, *, music):
    state = subscription.get('state')
    if state == 'unsupported':
        return 'Подписка не поддерживается'
    if state == 'active':
        return 'Ожидание трека' if music else 'Ожидание манёвра'
    if state == 'requested':
        return 'Ожидание ответа телефона'
    if state == 'stopped':
        return 'Подписка завершена'
    return 'Нет данных'


class ClusterPage(QWidget):
    def __init__(self, start_demo):
        super().__init__()
        # The inspector owns the outer margins and scroll area. These cards
        # should occupy only their natural height above the other HUD settings.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        heading = QHBoxLayout()
        heading.setSpacing(6)
        heading.addWidget(text_label('Подсказки приборки', 'clusterHeading', wrap=True), 1)
        self.source = text_label('Отключено', 'clusterSource')
        heading.addWidget(self.source, 0, Qt.AlignVCenter)
        layout.addLayout(heading)
        self.hint = text_label('', 'muted', wrap=True)
        layout.addWidget(self.hint)

        nav, box = card('СЛЕДУЮЩИЙ МАНЁВР')
        row = QHBoxLayout()
        row.setSpacing(10)
        self.arrow = text_label('—', 'clusterArrow')
        self.arrow.setAlignment(Qt.AlignCenter)
        self.arrow.setFixedSize(54, 54)
        self.arrow.setAccessibleName('Стрелка манёвра')
        row.addWidget(self.arrow, 0, Qt.AlignVCenter)
        values = QVBoxLayout()
        values.setSpacing(1)
        self.distance = text_label('—', 'clusterValue', elide=True)
        self.distance.setAccessibleName('Расстояние до манёвра')
        values.addWidget(self.distance)
        self.maneuver = text_label('', 'clusterTitle', wrap=True)
        values.addWidget(self.maneuver)
        row.addLayout(values, 1)
        box.addLayout(row)
        self.road = text_label('', 'clusterSecondary', elide=True)
        self.road.setAccessibleName('Дорога')
        box.addWidget(self.road)
        layout.addWidget(nav)

        music, box = card('СЕЙЧАС ИГРАЕТ')
        row = QHBoxLayout()
        row.setSpacing(10)
        self.artwork = QLabel()
        self.artwork.setObjectName('clusterArtwork')
        self.artwork.setFixedSize(64, 64)
        self.artwork.setAlignment(Qt.AlignCenter)
        self.artwork.setAccessibleName('Обложка альбома')
        row.addWidget(self.artwork, 0, Qt.AlignVCenter)
        values = QVBoxLayout()
        values.setSpacing(4)
        self.song = text_label('—', 'clusterTitle', elide=True)
        self.song.setAccessibleName('Композиция')
        values.addWidget(self.song)
        self.artist = text_label('', 'clusterSecondary', elide=True)
        self.artist.setAccessibleName('Исполнитель')
        values.addWidget(self.artist)
        self.album = text_label('', 'muted', elide=True)
        self.album.setAccessibleName('Альбом')
        values.addWidget(self.album)
        row.addLayout(values, 1)
        box.addLayout(row)
        layout.addWidget(music)

        self.demo_button = QPushButton('Запустить демо приборки')
        self.demo_button.clicked.connect(start_demo)
        layout.addWidget(self.demo_button)
        self._rendered = None
        self._icon = None
        self._artwork = None
        self.refresh(None, demo=False)

    def refresh(self, cluster, *, demo):
        snapshot = cluster.snapshot() if cluster is not None else {}
        key = (cluster, snapshot.get('revision'), demo)
        if key == self._rendered:
            return
        self._rendered = key
        self.source.setText('Демо' if demo else ('CarLife' if cluster is not None else 'Отключено'))
        hint = ('Демонстрационный маршрут и музыка.' if demo else
                ('Подключите телефон или запустите демо.' if cluster is None else ''))
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.demo_button.setVisible(cluster is None)
        subscriptions = snapshot.get('subscriptions', {})
        nav = snapshot.get('navigation') or {}
        music = snapshot.get('music') or {}
        turn = nav.get('turn_id')
        glyph, name = TURNS.get(turn, ('?', f'Манёвр {turn}'))
        self.maneuver.setText(name if nav else waiting_text(subscriptions.get('navigation', {}), music=False))
        self.distance.setText(distance_text(nav.get('distance_m')))
        self.arrow.setVisible(bool(nav))
        self.distance.setVisible(bool(nav) and nav.get('distance_m') is not None)
        self.road.setText(nav.get('road') or '')
        self.road.setVisible(bool(self.road.text()))
        icon_key = (turn, nav.get('icon'))
        if icon_key != self._icon:
            self._icon = icon_key
            picture = turn_pixmap(nav.get('icon'))
            if picture is not None:
                self.arrow.setPixmap(picture)
            else:
                self.arrow.setText(glyph if nav else '—')
        self.song.setText(music.get('title') or (waiting_text(subscriptions.get('music', {}), music=True)
                                                 if not music else 'Без названия'))
        self.artist.setText(music.get('artist') or '')
        self.album.setText(music.get('album') or '')
        self.artist.setVisible(bool(self.artist.text()))
        self.album.setVisible(bool(self.album.text()))
        artwork = music.get('artwork', b'')
        if artwork != self._artwork:
            self._artwork = artwork
            # A new track without art, an invalid image, STOP and disconnect
            # all clear the previous cover. Progress updates do not decode it.
            self.artwork.clear()
            picture = artwork_pixmap(artwork)
            if picture is not None:
                self.artwork.setPixmap(picture)
            self.artwork.setVisible(picture is not None)
